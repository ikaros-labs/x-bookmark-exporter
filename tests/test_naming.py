import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from x_bookmarks.export import digest, render, verified
from x_bookmarks.migration import migrate
from x_bookmarks.naming import filename
from x_bookmarks.storage import atomic_write, write_json


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.item = {"post": {"id": "123", "author_id": "9", "created_at": "2026-09-08T09:00:00Z",
                              "text": "A few thoughts\non coding agents https://t.co/link"},
                     "includes": {"users": [{"id": "9", "username": "alice"}]}}

    def legacy(self):
        root = self.output / ".x-bookmarks"
        raw = ".x-bookmarks/raw/123.json"
        write_json(self.output / raw, self.item)
        body = "\n".join(line for line in render(self.item, []).split("\n") if not line.startswith("first_seen_at:"))
        atomic_write(self.output / "123.md", body.encode())
        files = {name: digest(self.output / name) for name in [raw, "123.md"]}
        write_json(root / "receipts/123.json", files)
        write_json(root / "state-9.json", {"123": {"files": files}})
        return root

    def test_readable_name(self):
        self.assertEqual(filename(self.item), "2026-09-08 - alice - A few thoughts on coding agents - 123.md")

    def test_unicode_and_unsafe_characters(self):
        self.item["post"]["text"] = '../[A]: bad / name? ' + '🌍' * 100
        name = filename(self.item)
        self.assertLess(len(name.encode()), 255)
        for char in '/\\[]:?':
            self.assertNotIn(char, name)

    def test_long_post_and_empty_title(self):
        self.item["post"]["note_post"] = {"text": "The full post"}
        self.assertIn("The full post", filename(self.item))
        self.item["post"]["note_post"]["text"] = "https://example.com"
        self.assertIn("Untitled post", filename(self.item))

    def test_migration_updates_state_receipt_and_first_seen(self):
        root = self.legacy()
        self.assertEqual(migrate(self.output), 1)
        self.assertFalse((self.output / "123.md").exists())
        body = (self.output / filename(self.item)).read_text()
        metadata = dict(line.split(": ", 1) for line in body.split("\n---\n")[0].splitlines() if ": " in line)
        self.assertEqual(metadata["first_seen_at"], metadata["exported_at"])
        files = json.loads((root / "state-9.json").read_text())["123"]["files"]
        self.assertTrue(verified(self.output, files))
        self.assertEqual(files, json.loads((root / "receipts/123.json").read_text()))
        self.assertEqual(migrate(self.output), 0)

    def test_migration_preserves_user_edits(self):
        self.legacy()
        (self.output / "123.md").write_text("My changes")
        self.assertEqual(migrate(self.output), 0)
        self.assertEqual((self.output / "123.md").read_text(), "My changes")

    def test_migration_resumes_after_receipt_written(self):
        root = self.legacy()
        real_write = write_json
        def fail_state(path, value):
            if path.name == "state-9.json":
                raise OSError("interruption")
            real_write(path, value)
        with patch("x_bookmarks.migration.write_json", side_effect=fail_state):
            with self.assertRaises(OSError):
                migrate(self.output)
        migrate(self.output)
        self.assertFalse((root / "rename-pending.json").exists())
        files = json.loads((root / "state-9.json").read_text())["123"]["files"]
        self.assertTrue(verified(self.output, files))
        self.assertNotIn("123.md", files)
