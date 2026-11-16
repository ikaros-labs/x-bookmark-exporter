import copy
import hashlib
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

from fake_vault_api import FakeVaultAPI
from x2vault.export import MediaTooLarge, archive, download, process, verified
from x2vault.monitor import poll
from x2vault.naming import filename
from x2vault.store import ApiStore, StoreError

FOLDER = "3. Resources/X/inbox"
ITEM = {"post": {"id": "123", "text": "Photo post", "attachments": {"media_keys": ["m"]}},
        "includes": {"media": [{"media_key": "m", "type": "photo", "url": "https://example.com/m.jpg"}]}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ApiStoreTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeVaultAPI()
        self.addCleanup(self.api.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name) / "state"
        self.sleeps = []
        self.store = ApiStore(self.api.url, "secret", FOLDER, self.data, max_media_bytes=10 * 2**20,
                              sleep=self.sleeps.append)
        self.client = Mock()
        self.client.user_id.return_value = "9"

    def test_create_streams_file_and_encodes_paths(self):
        source = self.data / "upload.bin"
        self.data.mkdir(parents=True)
        source.write_bytes(b"media bytes")
        self.assertEqual(self.store.create("../assets/1/a b#.jpg", source), sha(b"media bytes"))
        self.assertEqual(self.api.files, {"3. Resources/X/assets/1/a b#.jpg": b"media bytes"})
        method, _, headers = self.api.requests[-1]
        self.assertEqual((method, headers["If-None-Match"], headers["Content-Length"]), ("PUT", "*", "11"))

    def test_create_only_conflict(self):
        self.api.files[f"{FOLDER}/note.md"] = b"existing"
        # Identical content (e.g. a retried PUT that already landed) is accepted.
        self.store.create("note.md", b"existing")
        with self.assertRaisesRegex(StoreError, "refusing to overwrite"):
            self.store.create("note.md", b"different")
        self.assertEqual(self.api.files[f"{FOLDER}/note.md"], b"existing")

    def test_verify_match_mismatch_and_missing(self):
        self.api.files[f"{FOLDER}/note.md"] = b"body"
        self.assertTrue(self.store.verify("note.md", sha(b"body")))
        self.assertEqual(self.api.requests[-1][2]["If-None-Match"], f'"sha256:{sha(b"body")[:16]}"')
        self.assertFalse(self.store.verify("note.md", sha(b"edited")))
        self.assertFalse(self.store.verify("missing.md", sha(b"body")))

    def test_retries_5xx_then_succeeds(self):
        self.api.failures.extend([503, 500])
        self.store.create("note.md", b"body")
        self.assertEqual(self.api.files[f"{FOLDER}/note.md"], b"body")
        self.assertEqual(self.sleeps, [2, 4])

    def test_rate_limit_waits_for_retry_after(self):
        self.api.failures.extend([429, 429])
        self.assertTrue(self.store.create("note.md", b"body"))
        self.assertEqual(self.sleeps, [7, 7])
        self.api.failures.extend([429] * 11)
        with self.assertRaisesRegex(StoreError, "HTTP 429"):
            self.store.exists("note.md")

    def test_gives_up_after_retries(self):
        self.api.failures.extend([502] * 4)
        with self.assertRaisesRegex(StoreError, "HTTP 502"):
            self.store.create("note.md", b"body")
        self.assertEqual(len(self.sleeps), 3)

    def test_network_error_retried(self):
        store = ApiStore("http://127.0.0.1:9", "secret", FOLDER, self.data, sleep=self.sleeps.append)
        with self.assertRaisesRegex(StoreError, "unreachable"):
            store.exists("note.md")
        self.assertEqual(len(self.sleeps), 3)

    def test_4xx_keeps_bookmark_and_records_error(self):
        self.client.inventory.return_value = {"123": {}}
        self.client.lookup.return_value = {"post": {"id": "123", "text": "Hello"}, "includes": {}}
        self.api.failures.extend([403] * 2)  # HEAD (exists check), then PUT of the note
        self.assertEqual(poll(self.client, self.store), 1)
        self.client.remove.assert_not_called()
        self.assertEqual(self.sleeps, [])
        queue = json.loads((self.data / "monitor-9.json").read_text())
        self.assertIn("HTTP 403", queue["123"]["last_error"])
        self.assertEqual(self.api.files, {})

    def test_export_through_api_then_remove(self):
        def fetch(url, path, limit=None):
            self.assertEqual(limit, 10 * 2**20)
            path.write_bytes(b"photo")
        def check_saved(*args):
            self.assertEqual(self.api.files[f"3. Resources/X/assets/123/1.jpg"], b"photo")
            self.assertIn(f"{FOLDER}/{filename(ITEM)}", self.api.files)
        self.client.remove.side_effect = check_saved
        assets = PurePosixPath("/3. Resources/X/assets")
        state = {}
        with patch("x2vault.export.download", side_effect=fetch):
            self.assertEqual(process(self.client, "9", {"123": ITEM}, self.store, state, self.data / "s.json",
                                     assets_dir=assets), 0)
        files = state["123"]["files"]
        self.assertEqual(set(files), {"../assets/123/1.jpg", ".x-bookmarks/raw/123.json", filename(ITEM)})
        self.assertTrue((self.data / "raw/123.json").is_file())
        self.assertTrue((self.data / "receipts/123.json").is_file())
        self.assertFalse(any(".x-bookmarks" in path for path in self.api.files))
        self.assertTrue(verified(self.store, files))
        self.assertIn("![](../assets/123/1.jpg)", self.api.files[f"{FOLDER}/{filename(ITEM)}"].decode())
        # The receipt is reused: no second upload or download.
        with patch("x2vault.export.download") as again:
            self.assertEqual(archive(ITEM, self.store, assets_dir=assets), files)
        again.assert_not_called()
        (self.data / "raw/123.json").write_text("{}")
        self.assertFalse(verified(self.store, files))

    def test_oversized_media_saved_as_partial_note(self):
        with patch("x2vault.export.download", side_effect=MediaTooLarge("larger than the 10 MB media limit")):
            self.assertEqual(process(self.client, "9", {"123": copy.deepcopy(ITEM)}, self.store, {},
                                     self.data / "s.json"), 0)
        note = self.api.files[f"{FOLDER}/{filename(ITEM)}"].decode()
        self.assertIn('export_status: "partial"', note)
        self.assertIn("Media m skipped: larger than the 10 MB media limit", note)
        self.assertIn("https://example.com/m.jpg", note)
        self.assertNotIn("assets/123", "".join(self.api.files))
        self.client.remove.assert_called_once()

    def test_paths_must_stay_in_vault(self):
        for folder in ("", "/abs", "../up", "X/.x-bookmarks"):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                ApiStore(self.api.url, "secret", folder, self.data)
        with self.assertRaises(ValueError):
            self.store.url("../../../../outside.md")


class FakeResponse(io.BytesIO):
    def __init__(self, body, length):
        super().__init__(body)
        self.url = "https://media.example/x.mp4"
        self.headers = Message()
        self.headers["Content-Type"] = "video/mp4"
        if length is not None:
            self.headers["Content-Length"] = str(length)


class SizeCapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "media.mp4"

    def fetch(self, body, length, limit):
        response = FakeResponse(body, length)
        with patch("x2vault.export.urllib.request.urlopen", return_value=response):
            download("https://media.example/x.mp4", self.path, limit=limit)
        return response

    def test_content_length_checked_before_download(self):
        response = FakeResponse(b"x" * 11, 11)
        response.read = Mock(side_effect=AssertionError("body read despite Content-Length over the limit"))
        with self.assertRaises(MediaTooLarge), \
                patch("x2vault.export.urllib.request.urlopen", return_value=response):
            download("https://media.example/x.mp4", self.path, limit=10)
        self.assertFalse(self.path.exists())

    def test_aborts_mid_stream_without_content_length(self):
        with self.assertRaises(MediaTooLarge):
            self.fetch(b"x" * (3 * 2**20), None, 2**20)
        self.assertFalse(self.path.exists())

    def test_within_limit_and_unlimited(self):
        self.fetch(b"x" * 10, 10, 10)
        self.assertEqual(self.path.read_bytes(), b"x" * 10)
        self.fetch(b"y" * 20, 20, None)
        self.assertEqual(self.path.read_bytes(), b"y" * 20)


if __name__ == "__main__":
    unittest.main()
