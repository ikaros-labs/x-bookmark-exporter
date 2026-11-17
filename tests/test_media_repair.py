"""User-Agent on outbound requests, and repair of "Media N not saved" lines in saved post notes."""
import contextlib
import io
import json
import tempfile
import time
import unittest
from email.message import Message
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from fake_spall import FakeSpall
from test_posts import NOW, FakeX, post
from x2vault import USER_AGENT
from x2vault.api import Client
from x2vault.export import download
from x2vault.posts import PostsExport, repair_posts_media
from x2vault.store import FileStore, SpallStore

MEDIA = [{"media_key": "a", "type": "photo", "url": "https://pbs.example/a.jpg", "alt_text": "A cat"},
         {"media_key": "b", "type": "video", "variants": [
             {"content_type": "video/mp4", "bit_rate": 1, "url": "https://video.example/b.mp4"}]}]


class Response(io.BytesIO):
    def __init__(self, url):
        super().__init__(b"{}")
        self.url, self.status = url, 200
        self.headers = Message()
        self.headers["Content-Type"] = "image/jpeg"


class UserAgentTests(unittest.TestCase):
    def test_media_download_sends_user_agent_and_no_token(self):
        seen = []
        def urlopen(request, timeout):
            seen.append(request)
            return Response(request.full_url)
        with tempfile.TemporaryDirectory() as tmp, patch("x2vault.export.urllib.request.urlopen", side_effect=urlopen):
            download("https://pbs.example/a.jpg", Path(tmp) / "a.jpg")
        self.assertEqual(seen[0].get_header("User-agent"), USER_AGENT)
        self.assertIsNone(seen[0].get_header("Authorization"))
        self.assertRegex(USER_AGENT, r"^x2vault/\d+\.\d+\.\d+ \(\+https://github\.com/ikaros-labs/x2vault\)$")

    def test_api_request_sends_user_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials.json"
            path.write_text(json.dumps({"access_token": "t", "expires_at": time.time() + 3600}))
            seen = []
            def urlopen(request, timeout):
                seen.append(request)
                return Response(request.full_url)
            with patch("x2vault.api.urllib.request.urlopen", side_effect=urlopen):
                Client(path).request("GET", "/users/me")
        self.assertEqual(seen[0].get_header("User-agent"), USER_AGENT)

    def test_spall_requests_send_user_agent(self):
        api = FakeSpall()
        self.addCleanup(api.close)
        with tempfile.TemporaryDirectory() as tmp:
            store = SpallStore(api.url, "secret", "X", Path(tmp), sleep=lambda s: None)
            store.create("a.md", b"hi")
            store.exists("a.md")
        self.assertEqual({r[2].get("User-Agent") for r in api.requests}, {USER_AGENT})


class RepairTests(unittest.TestCase):
    """Two attachments failed with 403 at export time; the repair fills them in later."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "my posts"
        self.store = FileStore(self.root, Path(tmp.name) / "state")
        self.client = FakeX([post(i, f"Post {i}", attachments={"media_keys": ["a", "b"]}) for i in range(1, 4)])
        timeline = self.client.timeline
        def with_media(user_id, params):
            page = timeline(user_id, params)
            page["includes"] = {"media": MEDIA}
            return page
        self.client.timeline = with_media
        with patch("x2vault.posts.download", side_effect=OSError("HTTP Error 403: Forbidden")):
            self.export_log = self.export(lambda e: e.run())
        self.client.timeline_calls.clear()

    def export(self, action, store=None, **kwargs):
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            export = PostsExport(self.client, store or self.store, 1000, now=NOW, **kwargs)
            action(export)
            export.save()
        return log.getvalue()

    def repair(self, fetch=None, limit=30):
        fetch = fetch or (lambda url, path, limit=None: path.write_bytes(url.encode()))
        with patch("x2vault.posts.download", side_effect=fetch) as mock:
            log = self.export(lambda e: e.repair_media(limit))
        return mock, log

    def note(self, post_id):
        return self.root / f"2026-08-08 Post {post_id} - {post_id}.md"

    def test_lines_become_embeds_keeping_frontmatter_and_edits(self):
        note = self.note(1)
        before = note.read_text()
        self.assertEqual(before.count("not saved (HTTP Error 403: Forbidden); see the original post."), 2)
        self.assertIn("Media failed: 6.", self.export_log)
        self.assertNotIn("Media repair", self.export_log)
        self.assertIn("1: warning: media b not saved from video.example: HTTP Error 403", self.export_log)
        edited = before.replace('Lang: "en"\n', 'Lang: "en"\nRating: 5\n') + "\nMy own comment.\n"
        note.write_text(edited)
        self.repair()
        after = note.read_text()
        self.assertNotIn("not saved", after)
        self.assertIn("\n![](assets/1/1.jpg)\n\nA cat\n", after)
        self.assertIn("\n![](assets/1/2.mp4)\n", after)
        self.assertIn("Rating: 5\n", after)
        self.assertTrue(after.endswith("\nMy own comment.\n"))
        self.assertEqual(after.split("---")[1], edited.split("---")[1])
        self.assertEqual((self.root / "assets/1/2.mp4").read_bytes(), b"https://video.example/b.mp4")
        # Exactly what a fresh export renders for these media.
        fresh = "\n![](assets/1/1.jpg)\n\nA cat\n\n![](assets/1/2.mp4)\n"
        self.assertIn(fresh, after)
        self.assertEqual((self.client.timeline_calls, self.client.lookup_calls), ([], []))

    def test_idempotent(self):
        self.repair()
        texts = [self.note(i).read_text() for i in range(1, 4)]
        mock, _ = self.repair()
        self.assertEqual(mock.call_count, 0)
        self.assertEqual([self.note(i).read_text() for i in range(1, 4)], texts)

    def test_per_poll_cap(self):
        mock, _ = self.repair(limit=2)
        self.assertEqual(mock.call_count, 4)
        self.assertIn("not saved", self.note(3).read_text())
        self.repair(limit=2)
        self.assertNotIn("not saved", self.note(3).read_text())

    def test_gives_up_after_three_tries_and_keeps_line(self):
        def flaky(url, path, limit=None):
            if url.endswith(".mp4"):
                raise OSError("HTTP Error 403: Forbidden")
            path.write_bytes(b"jpg")
        calls = [self.repair(flaky, limit=1)[0].call_count for _ in range(5)]
        # Post 1: photo + video, then the video twice more; then posts 2 and 3 get their turn.
        self.assertEqual(calls[:3], [2, 1, 1])
        text = self.note(1).read_text()
        self.assertIn("![](assets/1/1.jpg)", text)
        self.assertIn("> Media 2 not saved (HTTP Error 403: Forbidden); see the original post.", text)
        state = json.loads((self.store.data_dir / "posts-9.json").read_text())["media_repair"]
        self.assertIn("1", state["done"])
        self.assertEqual(state["tries"]["1/2"], 3)

    def test_subcommand_entry_point_makes_no_api_calls(self):
        with patch("x2vault.posts.download", side_effect=lambda url, path, limit=None: path.write_bytes(b"x")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(repair_posts_media(self.client, self.store), 3)
        self.assertEqual((self.client.timeline_calls, self.client.lookup_calls), ([], []))

    def test_spall_rewrite_uses_if_match(self):
        api = FakeSpall()
        self.addCleanup(api.close)
        store = SpallStore(api.url, "secret", "X/my posts", self.store.data_dir, sleep=lambda s: None)
        path = "X/my posts/2026-08-08 Post 1 - 1.md"
        api.files[path] = self.note(1).read_bytes()
        state = json.loads((self.store.data_dir / "posts-9.json").read_text())
        state["media_repair"] = {"done": ["2", "3"]}
        (self.store.data_dir / "posts-9.json").write_text(json.dumps(state))
        api.can_update = False
        with patch("x2vault.posts.download", side_effect=lambda url, path, limit=None: path.write_bytes(b"x")):
            log = self.export(lambda e: e.repair_media(), store, assets_dir=PurePosixPath("/X/assets"))
            self.assertIn("Grant update on the posts folder", log)
            self.assertIn(b"not saved", api.files[path])
            api.can_update = True
        # The forbidden marker holds for the rest of the UTC day; next day it runs.
        with patch("x2vault.posts.download", side_effect=lambda url, path, limit=None: path.write_bytes(b"x")), \
                contextlib.redirect_stdout(io.StringIO()):
            export = PostsExport(self.client, store, 1000, assets_dir=PurePosixPath("/X/assets"),
                                 now=NOW.replace(day=17))
            export.repair_media()
        self.assertIn(b"![](../assets/1/1.jpg)", api.files[path])
        self.assertNotIn(b"not saved", api.files[path])
        put = [r for r in api.requests if r[0] == "PUT" and r[1] == path][-1]
        self.assertTrue(put[2]["If-Match"].startswith('"sha256:'))


if __name__ == "__main__":
    unittest.main()
