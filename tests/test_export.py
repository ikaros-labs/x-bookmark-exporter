import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from x_bookmarks.api import Client
from x_bookmarks.naming import filename
from x_bookmarks.export import archive, media_source, process, render

ITEM = {"post": {"id": "123", "text": "Short", "author_id": "9",
                 "note_tweet": {"text": "Full long text https://t.co/abc", "entities": {
                     "urls": [{"url": "https://t.co/abc", "expanded_url": "https://example.com"}]}}},
        "includes": {"users": [{"id": "9", "username": "alice", "name": 'Alice: "A"'}]}}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.state_path = self.output / "state.json"
        self.client = Mock()

    def test_full_text_and_quoted_metadata(self):
        result = render(ITEM, [])
        self.assertIn("Full long text https://example.com", result)
        self.assertIn('post_id: "123"', result)
        self.assertNotIn("\nShort\n", result)

    def test_save_before_remove_and_reuse_after_delete_failure(self):
        state = {}
        def failure(*args):
            self.assertTrue((self.output / filename(ITEM)).exists())
            self.assertIn("files", json.loads(self.state_path.read_text())["123"])
            raise RuntimeError("temporary delete failure")
        self.client.remove.side_effect = failure
        self.assertEqual(process(self.client, "9", {"123": ITEM}, self.output, state, self.state_path), 1)
        original = (self.output / filename(ITEM)).read_bytes()
        self.client.remove.side_effect = None
        self.assertEqual(process(self.client, "9", {"123": ITEM}, self.output, state, self.state_path), 0)
        self.assertEqual(original, (self.output / filename(ITEM)).read_bytes())

    def test_missing_media_never_removes(self):
        item = copy.deepcopy(ITEM)
        item["post"]["attachments"] = {"media_keys": ["missing"]}
        self.assertEqual(process(self.client, "9", {"123": item}, self.output, {}, self.state_path), 1)
        self.client.remove.assert_not_called()
        self.assertFalse((self.output / filename(ITEM)).exists())

    def test_download_failure_never_removes(self):
        item = copy.deepcopy(ITEM)
        item["post"]["attachments"] = {"media_keys": ["m"]}
        item["includes"]["media"] = [{"media_key": "m", "type": "photo", "url": "https://example.com/m.jpg"}]
        with patch("x_bookmarks.export.download", side_effect=OSError("network failure")):
            self.assertEqual(process(self.client, "9", {"123": item}, self.output, {}, self.state_path), 1)
        self.client.remove.assert_not_called()

    def test_media_saved_and_embedded_before_removal(self):
        item = copy.deepcopy(ITEM)
        item["post"]["attachments"] = {"media_keys": ["m"]}
        item["includes"]["media"] = [{"media_key": "m", "type": "photo", "url": "https://example.com/m.jpg"}]
        def save_media(url, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"image fixture")
        def check_saved(*args):
            self.assertEqual((self.output / "assets/123/1.jpg").read_bytes(), b"image fixture")
            self.assertIn("![[assets/123/1.jpg]]", (self.output / filename(ITEM)).read_text())
        self.client.remove.side_effect = check_saved
        with patch("x_bookmarks.export.download", side_effect=save_media):
            self.assertEqual(process(self.client, "9", {"123": item}, self.output, {}, self.state_path), 0)
        self.client.remove.assert_called_once_with("9", "123")

    def test_keep_and_modified_note(self):
        state = {}
        process(self.client, "9", {"123": ITEM}, self.output, state, self.state_path, keep=True)
        self.client.remove.assert_not_called()
        (self.output / filename(ITEM)).write_text("User edit")
        self.assertEqual(process(self.client, "9", {"123": ITEM}, self.output, state, self.state_path), 1)
        self.client.remove.assert_not_called()

    def test_receipt_recovers_missing_state(self):
        files = archive(ITEM, self.output)
        self.assertEqual(archive(ITEM, self.output), files)

    def test_best_mp4(self):
        self.assertEqual(media_source({"type": "video", "variants": [
            {"content_type": "application/x-mpegURL", "url": "hls"},
            {"content_type": "video/mp4", "bit_rate": 10, "url": "low"},
            {"content_type": "video/mp4", "bit_rate": 20, "url": "high"}]}), ("high", ".mp4"))

    def test_pagination_and_partial_errors(self):
        client = Client.__new__(Client)
        client.request = Mock(side_effect=[{"data": [ITEM["post"]], "meta": {"next_token": "next"}},
                                           {"data": [{"id": "124", "text": "second"}]}])
        self.assertEqual(set(client.inventory("9")), {"123", "124"})
        self.assertEqual(client.request.call_count, 2)
        client.request = Mock(return_value={"data": [ITEM["post"]], "errors": [{"title": "Unavailable"}]})
        with self.assertRaises(RuntimeError):
            client.inventory("9")

    def test_delete_requires_false_confirmation(self):
        client = Client.__new__(Client)
        client.request = Mock(return_value={"data": {"bookmarked": True}})
        with self.assertRaises(RuntimeError):
            client.remove("9", "123")
        client.request.return_value = {"data": {"bookmarked": False}}
        client.remove("9", "123")


if __name__ == "__main__":
    unittest.main()
