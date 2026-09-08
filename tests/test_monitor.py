import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from x_bookmarks.api import APIError, Client
from x_bookmarks.monitor import poll
from x_bookmarks.storage import write_json


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.internal = self.output / '.x-bookmarks'
        self.client = Mock()
        self.client.user_id.return_value = '9'
        self.client.inventory.return_value = {'123': {}}
        self.client.lookup.return_value = {'post': {'id': '123', 'text': 'Hello'}, 'includes': {}}

    def test_keep_only_fetches_content_once(self):
        poll(self.client, self.output, keep=True)
        poll(self.client, self.output, keep=True)
        self.client.lookup.assert_called_once()
        self.client.remove.assert_not_called()
        self.assertTrue(self.client.inventory.call_args.kwargs['lightweight'])

    def test_remove_retry_does_not_refetch_content(self):
        self.client.remove.side_effect = RuntimeError('temporary failure')
        self.assertEqual(poll(self.client, self.output), 1)
        poll(self.client, self.output)
        self.client.remove.assert_called_once()
        queue_path = self.internal / 'monitor-9.json'
        queue = json.loads(queue_path.read_text())
        queue['123']['retry_at'] = 0
        write_json(queue_path, queue)
        self.client.remove.side_effect = None
        poll(self.client, self.output)
        self.client.lookup.assert_called_once()
        self.assertEqual(self.client.remove.call_count, 2)

    def test_legacy_parked_article_retried(self):
        write_json(self.internal / 'monitor-9.json', {'123': {
            'parked': True, 'last_error': 'X Article export is unsupported'}})
        self.client.lookup.return_value = {'post': {'id': '123', 'article': {
            'title': 'Article title', 'plain_text': 'Full article body'}}, 'includes': {}}
        poll(self.client, self.output)
        self.client.lookup.assert_called_once()
        self.client.remove.assert_called_once_with('9', '123')
        self.assertNotIn('parked', json.loads((self.internal / 'monitor-9.json').read_text())['123'])

    def test_failed_discovery_does_not_remove(self):
        self.client.inventory.side_effect = RuntimeError('partial error')
        with self.assertRaises(RuntimeError):
            poll(self.client, self.output)
        self.client.lookup.assert_not_called()
        self.client.remove.assert_not_called()

    def test_forbidden_post_does_not_block_other_posts(self):
        self.client.inventory.return_value = {'122': {}, '123': {}}
        self.client.lookup.side_effect = [APIError(403), self.client.lookup.return_value]
        self.assertEqual(poll(self.client, self.output), 1)
        self.client.remove.assert_called_once_with('9', '123')

    def test_account_id_cached(self):
        client = Client.__new__(Client)
        client.path = self.output / 'credentials.json'
        client.token = {}
        client.request = Mock(return_value={'data': {'id': '9'}})
        self.assertEqual(client.user_id(), '9')
        self.assertEqual(client.user_id(), '9')
        client.request.assert_called_once()

    def test_discovery_has_no_expansions(self):
        client = Client.__new__(Client)
        client.request = Mock(return_value={'data': [{'id': '123'}]})
        client.inventory('9', lightweight=True)
        self.assertEqual(client.request.call_args.args[2], {'max_results': 100})

    def test_first_seen_survives_failed_lookup(self):
        self.client.lookup.side_effect = OSError('network failure')
        poll(self.client, self.output)
        queue_path = self.internal / 'monitor-9.json'
        queue = json.loads(queue_path.read_text())
        first = queue['123']['first_seen_at']
        queue['123']['retry_at'] = 0
        write_json(queue_path, queue)
        self.client.lookup.side_effect = None
        poll(self.client, self.output, keep=True)
        raw = json.loads((self.internal / 'raw/123.json').read_text())
        self.assertEqual(raw['first_seen_at'], first)
