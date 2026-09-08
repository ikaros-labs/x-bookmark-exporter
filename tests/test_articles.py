import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from x_bookmarks.articles import article_text, media_keys
from x_bookmarks.export import process, render, verified
from x_bookmarks.naming import filename


class ArticleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.item = {'post': {'id': '123', 'article': {
            'title': 'Useful article', 'plain_text': 'Full body\nSecond paragraph',
            'preview_text': 'Short preview'}}, 'includes': {}}

    def test_body_and_title_without_post_text(self):
        result = render(self.item, [])
        self.assertIn('# Useful article\n\nFull body\nSecond paragraph', result)
        self.assertNotIn('Short preview', result)
        self.assertIn('content_type: "x_article"', result)
        self.assertIn('Useful article', filename(self.item))

    def test_preview_only_saves_metadata_then_removes(self):
        del self.item['post']['article']['plain_text']
        client = Mock()
        self.assertEqual(process(client, '9', {'123': self.item}, self.output, {}, self.output/'state.json'), 0)
        client.remove.assert_called_once()
        text = (self.output / filename(self.item)).read_text()
        self.assertIn('https://x.com/i/status/123', text)
        self.assertIn('export_status: "partial"', text)
        self.assertNotIn('Short preview', text)

    def test_article_media_and_verification(self):
        article = self.item['post']['article']
        article.update(cover_media='m', media_entities=['m', 'n'])
        self.item['includes']['media'] = [
            {'media_key': key, 'type': 'photo', 'url': f'https://example.com/{key}.jpg'} for key in ['m', 'n']]
        def download(url, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'image')
        state = {}
        client = Mock()
        with patch('x_bookmarks.export.download', side_effect=download) as fetch:
            self.assertEqual(process(client, '9', {'123': self.item}, self.output, state, self.output/'state.json'), 0)
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(verified(self.output, state['123']['files']))
        client.remove.assert_called_once()

    def test_missing_article_media_saves_body_then_removes(self):
        self.item['post']['article']['cover_media'] = 'missing'
        client = Mock()
        self.assertEqual(process(client, '9', {'123': self.item}, self.output, {}, self.output/'state.json'), 0)
        client.remove.assert_called_once()
        text = (self.output / filename(self.item)).read_text()
        self.assertIn('Full body', text)
        self.assertIn('export_status: "partial"', text)
        self.assertNotIn('![[', text)

    def test_forbidden_cover_still_verifies_before_removal(self):
        self.item['post']['article']['cover_media'] = 'm'
        self.item['includes']['media'] = [{'media_key': 'm', 'type': 'photo', 'url': 'https://example.com/m.jpg'}]
        state = {}
        client = Mock()
        def check(*args):
            self.assertTrue(verified(self.output, state['123']['files']))
        client.remove.side_effect = check
        with patch('x_bookmarks.export.download', side_effect=OSError('403 Forbidden')):
            self.assertEqual(process(client, '9', {'123': self.item}, self.output, state, self.output/'state.json'), 0)
        client.remove.assert_called_once()

    def test_separate_code_and_links_preserved(self):
        article = self.item['post']['article']
        article['entities'] = {'code': [{'code': 'print("```")'}], 'urls': [{'text': 'https://example.com'}]}
        text = article_text(article)
        self.assertIn('````\nprint("```")\n````', text)
        self.assertIn('<https://example.com>', text)
