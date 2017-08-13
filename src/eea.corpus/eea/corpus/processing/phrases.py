""" Build a list of collocations (phrases) and show them in the stream.

Normally it would be impossible to use the phrase detection in "real-time", as
the phrases need to be detected in the entire corpus. To overcome this, we do:

    * Try to load a phrase model suitable for the current pipeline.
    * If one doesn't exist, schedule an async job that will generate the phrase
      model
    * Inform the user, in the UI, about the availability of the preview
"""

from colander import Schema
from deform.widget import MappingWidget
from deform.widget import default_resource_registry
from eea.corpus.async import queue
from eea.corpus.processing import build_pipeline
from eea.corpus.processing import get_pipeline_for_component
from eea.corpus.processing import pipeline_component  # , needs_tokenized_input
from eea.corpus.utils import CORPUS_STORAGE
from eea.corpus.utils import corpus_base_path
from eea.corpus.utils import hashed_id
from gensim.models.phrases import Phrases, Phraser
from glob import iglob
from itertools import tee, chain
from pyramid.threadlocal import get_current_request
from redis.exceptions import ConnectionError
from rq.decorators import job
from rq.registry import StartedJobRegistry
from textacy.doc import Doc
import colander
import deform.widget
import logging
import os.path


logger = logging.getLogger('eea.corpus')

default_resource_registry.set_js_resources(
    'phrase-widget', None, 'eea.corpus:static/phrase-widget.js'
)


class PhraseFinderWidget(MappingWidget):
    """ Mapping widget with custom template

    Template customizations:

        * frame color based on phrase model status
        * the reload button is disabled/enabled based on live phrase model
          status
        * there is an AJAX js script that queries job status and updates the
          widget status indicators (frame color, reload preview button)
    """

    template = 'phrase_form'
    requirements = (('phrase-widget', None),)

    def get_template_values(self, field, cstruct, kw):
        """ Inserts the job status and preview status into template values
        """
        values = super(PhraseFinderWidget, self).\
            get_template_values(field, cstruct, kw)

        values['job_status'] = 'preview_not_available'

        req = get_current_request()

        pstruct = req.create_corpus_pipeline_struct.copy()
        pstruct['step_id'] = field.schema.name
        phash_id = phrase_model_id(
            file_name=pstruct['file_name'],
            text_column=pstruct['text_column'],
            pipeline=get_pipeline_for_component(pstruct)
        )
        values['phash_id'] = phash_id

        logger.info("Phrase widget: need phrase model id %s", phash_id)

        # Calculate the initial "panel status" to assign a status color to this
        # widget
        base_path = corpus_base_path(pstruct['file_name'])
        cpath = os.path.join(base_path, '%s.phras' % phash_id)

        for f in os.listdir(base_path):
            if f.startswith(cpath):    # it's an ngram model
                logger.info("Phrase widget: found a phrase model at %s", cpath)
                values['job_status'] = 'preview_available'
                return values

        # look for a job created for this model
        job = get_assigned_job(phash_id)
        if job is not None:
            values['job_status'] = 'preview_' + job.get_status()

        return values


class PhraseFinder(Schema):
    """ Schema for the phrases finder
    """

    widget = PhraseFinderWidget()       # overrides the default template
    description = "Find and process phrases in text."

    MODES = (
        ('tokenize', 'Tokenize phrases in text'),
        ('append', 'Append phrases to text'),
        ('replace', 'Replace all text with found phrases')
    )

    SCORING = (
        ('default', 'Default'),
        ('npmi', 'NPMI: Slower, better with common words'),
    )

    LEVELS = (
        (2, 'Bigrams'),
        (3, 'Trigrams'),
        (4, 'Quadgrams'),
    )

    mode = colander.SchemaNode(
        colander.String(),
        validator=colander.OneOf([x[0] for x in MODES]),
        default=MODES[0][0],
        missing=MODES[0][0],
        title="Operating mode",
        widget=deform.widget.RadioChoiceWidget(values=MODES)
    )

    level = colander.SchemaNode(
        colander.Int(),
        default=LEVELS[0][0],
        missing=LEVELS[0][0],
        title='N-gram level',
        widget=deform.widget.RadioChoiceWidget(values=LEVELS),
        description='How many words to include in phrase detection',
    )

    min_count = colander.SchemaNode(
        colander.Int(),
        default=5,
        missing=5,
        title='Minimum number',
        description='Ignore all words with total count lower then this',
    )

    threshold = colander.SchemaNode(
        colander.Float(),
        default=10.0,
        missing=10.0,
        title='Threshold',
        description='Score threshold for forming phrases. Higher means '
                    'fewer phrases.',
    )

    scoring = colander.SchemaNode(
        colander.String(),
        validator=colander.OneOf([x[0] for x in SCORING]),
        default=SCORING[0][0],
        missing=SCORING[0][0],
        title="Scoring algorithm",
        widget=deform.widget.RadioChoiceWidget(values=SCORING)
    )


@pipeline_component(schema=PhraseFinder,
                    title="Find and process phrases")
def process(content, env, **settings):       # pipeline, preview_mode,
    """ We can run in several modes:

    * remove all text but leave the phrases tokens
    * append the collocations to the document
    * replace the collocations where they appear

    TODO: implement the above
    """

    # convert content stream to ``textacy.doc.Doc``
    # TODO: treat case when doc is a list of words.
    content = (isinstance(doc, str) and Doc(doc) or doc for doc in content)
    # TODO: this is just to avoid errors on not installed language models
    # In the future, this should be treated properly or as a pipeline component
    content = (doc for doc in content if doc.lang == 'en')

    # tokenized text is list of statements, chain them to make list of tokens
    content = chain.from_iterable(doc.tokenized_text for doc in content)

    file_name = env['file_name']
    text_column = env['text_column']
    phrase_model_pipeline = get_pipeline_for_component(env)

    phash_id = phrase_model_id(file_name, text_column, phrase_model_pipeline)
    logger.info("Phrase processing: need phrase model id %s", phash_id)

    base_path = corpus_base_path(file_name)
    cache_path = os.path.join(base_path, '%s.phras' % phash_id)

    # TODO: test that the phrase model file is "finished"
    # maybe use transactional file system?
    for f in os.listdir(base_path):
        if f.startswith(cache_path):    # it's an ngram model
            yield from cached_phrases(cache_path, content)
            raise StopIteration

    if not env.get('preview_mode'):  # production mode, generate phrase model
        logger.info("Phrase processor: producing phrase model %s", cache_path)

        # According to tee() docs, this may be inefficient in terms of memory.
        # We need to do this because we need multiple passes through the
        # content stream.
        cs1, cs2 = tee(content, 2)
        for i in range(settings['level'] - 1):
            phrases = Phrases(cs1)
            path = "%s.%s" % (cache_path, i + 1)
            logger.info("Phrase processor: Saving %s", path)
            phrases.save(path)
            content = phrases[cs2]  # tokenize phrases in content stream
            cs1, cs2 = tee(content, 2)

        yield from content
        raise StopIteration

    # if running in preview mode, look for an async job already doing
    # phrase model building
    job = get_assigned_job(phash_id)
    job = None      # TODO: end debug

    if job is None:
        try:
            job = queue.enqueue(build_phrases,
                                timeout='1h',
                                args=(
                                    phrase_model_pipeline,
                                    file_name,
                                    text_column,
                                ),
                                meta={'phrase_model_id': phash_id},
                                kwargs={})
            logger.warning("Phrase processing: enqueued a new job %s", job.id)
        except ConnectionError:
            # swallow the error
            logger.warning("Phrase processing: could not enqueue a job")
    else:
        logger.info("Phrase processor: found a job (%s), passing through",
                    job.id)

    yield from content  # TODO: this is now tokenized text, should fix


@job(queue=queue)
def build_phrases(pipeline, file_name, text_column, **kw):
    """ Async job to build a corpus using the provided pipeline
    """

    content_stream = build_pipeline(file_name, text_column, pipeline,
                                    preview_mode=False)

    pid = phrase_model_id(file_name, text_column, pipeline)

    base_path = corpus_base_path(file_name)
    cache_path = os.path.join(base_path, '%s.phras' % pid)

    logger.info('Creating phrases at %s', cache_path)

    phrases = Phrases(content_stream)     # TODO: pass settings here
    phrases.save(cache_path)


def phrase_model_id(file_name, text_column, pipeline):
    """ Calculate a hash as consistent uid based on the pipeline
    """
    salt = [(file_name, text_column)]
    for name, step_id, settings in pipeline:
        if isinstance(settings, dict):
            settings = settings.copy()
            settings.pop('schema_position', None)
            settings.pop('schema_type', None)
            settings = sorted(settings.items())
        salt.append((name, settings))
    return hashed_id(salt)


def phrase_model_status(request):
    """ A view for information about the async status of a phrase model

    It looks up any existing running or queued async job that would process
    the phrases and returns JSON info about that.
    """

    phash_id = request.matchdict['phash_id']

    # look for a filename in corpus var folder
    fname = phash_id + '.phras'
    glob_path = os.path.join(CORPUS_STORAGE, '**', fname)
    files = list(iglob(glob_path, recursive=True))
    if files:
        return {
            'status': 'OK'
        }

    # TODO: this is the place to flatten all these available statuses
    # statuses: queued,

    try:
        jobs = queue.get_jobs()
    except ConnectionError:
        logger.warning("Phrase model status: could not get job status")
        jobs = []

    for jb in jobs:  # look for a job created for this model
        if jb.meta['phrase_model_id'] == phash_id:
            return {
                'status': 'preview_' + jb.get_status()
            }

    return {
        'status': 'unavailable'
    }


def get_assigned_job(phash_id):
    """ Get the queued or processing job for this phrase model

    TODO: look into more registries
    """

    # First, look for an already started job
    registry = StartedJobRegistry(queue.name, queue.connection)
    try:
        jids = registry.get_job_ids()
    except ConnectionError:
        logger.warning("Phrase processing: ConnectionError, could not get "
                       "a list of job ids")
        jids = []

    for jid in jids:
        job = queue.fetch_job(jid)
        if phash_id == job.meta.get('phrase_model_id'):
            logger.info("Phrase widget: async job found %s", job.id)
            return job

    # Look for a queued job
    try:
        jobs = queue.get_jobs()
    except ConnectionError:
        logger.warning("Phrase widget: ConnectionError, could not get a list "
                       "of jobs")
    jobs = []
    for job in jobs:  # look for a job created for this model
        if job.meta['phrase_model_id'] == phash_id:
            logger.info("Phrase widget: async job found %s", job.id)
            return job


def cached_phrases(cache_path, content):
    """ Returns tokenized phrases using saved phrase models.

    The phrase models are saved using a commong name, like:

        <phash_id>.phras.X

    where X is the n-gram level (one of 2,3,4).

    The phrase models are loaded and process the content stream.
    """

    content = chain.from_iterable(doc.tokenized_text for doc in content)

    logger.info("Phrase processor: using phrase models from %s", cache_path)

    phrase_model_files = []
    base_path, base_name = os.path.split(cache_path)
    for name in os.listdir(base_path):
        if name.startswith(base_name):
            phrase_model_files.append(os.path.join(base_path, name))

    cache_files.sort()
    for fpath in phrase_model_files:
        phrases = Phrases.load(fpath)
        content = phrases[content]

    # convert list of words back to full text document
    content = (
        ". ".join(
            (" ".join(w for w in sent) for sent in content)
        )
    )
    yield from content


def includeme(config):
    config.add_route('phrase-model-status', '/phrase-model-status/{phash_id}')
    config.add_view(phrase_model_status, route_name='phrase-model-status',
                    renderer='json')
