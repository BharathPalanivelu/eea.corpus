""" Limit the content stream to a fixed number of "rows"
"""

from colander import Schema, Int, SchemaNode
from eea.corpus.processing import register_pipeline_component
import logging

logger = logging.getLogger('eea.corpus')


class LimitResults(Schema):

    # description = "Limit the number of processed documents"

    max_count = SchemaNode(
        Int(),
        default=0,
        missing=0,
        title='Results limit',
        description='Set to 0 if you want unlimited results',
    )


def process(content, **settings):
    count = settings.get('max_count', 0)
    i = 0
    if not count:
        for doc in content:
            yield doc
    else:
        for doc in content:
            i += 1
            if i > count:
                break
            yield doc


def includeme(config):
    register_pipeline_component(
        schema=LimitResults,
        process=process,
        title="Limit number of results"
    )
