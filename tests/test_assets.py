import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from x_bookmarks.export import archive, verified
from x_bookmarks.monitor import watch
from x_bookmarks.naming import filename
from x_bookmarks.paths import assets_directory


class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name)
        self.output = self.vault / 'Inbox' / 'X'
        self.item = {'post': {'id': '123', 'text': 'Photo', 'attachments': {'media_keys': ['m']}},
                     'includes': {'media': [{'media_key': 'm', 'type': 'photo',
                                             'url': 'https://example.com/photo.jpg'}]}}

    @staticmethod
    def download(url, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'photo fixture')

    def test_default_is_inside_notes_folder(self):
        self.assertEqual(assets_directory(self.vault, self.output), self.output / 'assets')

    def test_shared_folder_links_hashes_and_retry(self):
        target = assets_directory(self.vault, self.output, 'Attachments/X media')
        with patch('x_bookmarks.export.download', side_effect=self.download) as fetch:
            files = archive(self.item, self.output, assets_dir=target)
            self.assertEqual(archive(self.item, self.output, assets_dir=self.vault/'Other'), files)
        fetch.assert_called_once()
        self.assertTrue((target/'123/1.jpg').exists())
        self.assertTrue(verified(self.output, files))
        self.assertIn('../../Attachments/X media/123/1.jpg', files)
        text = (self.output / filename(self.item)).read_text()
        self.assertIn('![](../../Attachments/X%20media/123/1.jpg)', text)
        (target/'123/1.jpg').write_bytes(b'changed')
        self.assertFalse(verified(self.output, files))

    def test_path_validation(self):
        for folder in ('', '/tmp/media', '../outside', '.obsidian/assets', 'X/.x-bookmarks'):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                assets_directory(self.vault, self.output, folder)

    def test_symlink_cannot_escape_vault(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            (self.vault/'linked').symlink_to(elsewhere, target_is_directory=True)
            with self.assertRaises(ValueError):
                assets_directory(self.vault, self.output, 'linked')

    def test_monitor_forwards_assets_folder(self):
        self.output.mkdir(parents=True)
        target = assets_directory(self.vault, self.output, 'Attachments')
        client = Mock()
        client.user_id.return_value = '9'
        client.inventory.return_value = {'123': self.item}
        client.lookup.return_value = self.item
        with patch('x_bookmarks.export.download', side_effect=self.download):
            watch(client, self.output, once=True, assets_dir=target)
        self.assertTrue((target/'123/1.jpg').exists())
        state = json.loads((self.output/'.x-bookmarks/state-9.json').read_text())
        self.assertTrue(verified(self.output, state['123']['files']))
        client.remove.assert_called_once_with('9', '123')
