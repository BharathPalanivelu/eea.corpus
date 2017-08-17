from eea.corpus.async import queue
from eea.corpus.processing import build_pipeline
from eea.corpus.utils import corpus_base_path
from eea.corpus.utils import metadata
from eea.corpus.utils import to_text
from rq.decorators import job
import json
import logging
import os.path
import textacy


logger = logging.getLogger('eea.corpus')


def save_corpus_metadata(corpus, file_name, corpus_id, text_column, **kw):
    cpath = corpus_base_path(file_name)      # corpus_id
    meta_name = "{0}_eea.json".format(corpus_id)
    meta_path = os.path.join(cpath, meta_name)

    title = kw.pop('title')
    description = kw.pop('description', '')

    info = {
        'title': title,
        'description': description,
        'metadata': metadata(corpus),
        'text_column': text_column,
        'kw': kw,
    }
    with open(meta_path, 'w') as f:
        json.dump(info, f)


@job(queue=queue)
def build_corpus(pipeline, corpus_id, file_name, text_column, **kw):
    """ Async job to build a corpus using the provided pipeline
    """

    cpath = corpus_base_path(file_name)      # corpus_id
    logger.info('Creating corpus for %s at %s', file_name, cpath)

    content_stream = build_pipeline(file_name, text_column, pipeline,
                                    preview_mode=False)

    content = (to_text(doc) for doc in content_stream)
    # TODO: save metadata stream
    corpus = textacy.Corpus('en', texts=content)
    corpus.save(cpath, name=corpus_id)
    save_corpus_metadata(corpus, file_name, corpus_id, text_column, **kw)
