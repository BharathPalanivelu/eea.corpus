from eea.corpus.utils import set_text
from textacy.doc import Doc
from unittest.mock import Mock, patch
import pytest


class TestMiscUtils:
    """ Tests for misc utils
    """

    def test_rand(self):
        from eea.corpus.utils import rand
        x = rand(10)
        assert len(x) == 10
        assert x.isalnum()

    def test_hashed_id(self):
        from eea.corpus.utils import hashed_id
        assert hashed_id({}) == \
            "d14a028c2a3a2bc9476102bb288234c415a2b01f828ea62ac5b3e42f"
        assert hashed_id({'a': 'b'}) == \
            "abd37534c7d9a2efb9465de931cd7055ffdb8879563ae98078d6d6d5"

    def test_invalid_document_name(self):
        from eea.corpus.utils import document_name
        req = Mock()

        req.matchdict = {}
        with pytest.raises(ValueError):
            # this is not a valid document name
            document_name(req)

        req.matchdict = {'doc': 'first'}
        with pytest.raises(ValueError):
            # this is not a valid document name
            document_name(req)

    @patch('eea.corpus.utils.is_valid_document')
    def test_valid_document_name(self, is_valid_document):
        from eea.corpus.utils import document_name
        req = Mock()
        req.matchdict = {'doc': 'first'}
        is_valid_document.return_value = True
        assert document_name(req) == 'first'

    def test_is_safe_to_save(self):
        from eea.corpus.utils import is_safe_to_save
        from pandas import read_csv
        from pkg_resources import resource_filename
        from textacy.doc import Doc

        fpath = resource_filename('eea.corpus', 'tests/fixtures/broken.csv')
        text_col = read_csv(fpath)['text']

        assert is_safe_to_save(Doc(text_col[1], lang='en')) is True
        assert is_safe_to_save(Doc(text_col[0], lang='en')) is False


class TestConvertorDecorators:
    """ Tests for stream conversion decorators found in eea.corpus.utils
    """

    def test_set_text(self):
        doc = Doc('hello world', metadata={'1': 2})
        res = set_text(doc, 'second time with more words')

        assert isinstance(res, Doc)
        assert res is not doc
        assert res.text == 'second time with more words'
        assert res.metadata == {'1': 2}
