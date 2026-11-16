"""Opt-in contract tests against a real Spall server. No X API calls are made.

    XBM_API_CONTRACT=1 XBM_API_URL=http://127.0.0.1:18787 XBM_API_TOKEN=vlt_... \\
    XBM_API_VAULT=/path/to/the/served/vault [XBM_FIXTURE_STATE=/path/to/inbox/.x-bookmarks] \\
    python3 -m unittest discover -s tests -p test_contract.py -v

The token's account needs read and create on "3. Resources/X". XBM_API_VAULT is the
local folder the server serves, used to check what arrived on disk.
"""
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from x2vault import cli
from x2vault.export import verified
from x2vault.store import FileStore

ENABLED = os.environ.get("XBM_API_CONTRACT") == "1"
FOLDER, ASSETS = "3. Resources/X/inbox", "3. Resources/X/assets"
IMAGE = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 200
VIDEO_SIZE = 11 * 2**20


class FakeX:
    """Stands in for x2vault.api.Client."""

    def __init__(self, user_id, posts):
        self.id, self.posts, self.removed, self.lookups = user_id, posts, [], []

    def user_id(self):
        return self.id

    def inventory(self, user_id, vocabulary="tweet", limit=None, lightweight=False):
        return {post_id: {} for post_id in self.posts}

    def lookup(self, post_id, vocabulary="tweet"):
        self.lookups.append(post_id)
        return json.loads(json.dumps(self.posts[post_id]))

    def remove(self, user_id, post_id):
        self.removed.append(post_id)


class FakeMedia(io.RawIOBase):
    """HTTPS media response; the video body is generated lazily and never fully held."""

    def __init__(self, url):
        self.url = url
        self.size = VIDEO_SIZE if url.endswith(".mp4") else len(IMAGE)
        self.sent = 0
        self.headers = Message()
        self.headers["Content-Type"] = "video/mp4" if url.endswith(".mp4") else "image/png"
        self.headers["Content-Length"] = str(self.size)

    def read(self, n=-1):
        n = self.size - self.sent if n < 0 else min(n, self.size - self.sent)
        data = IMAGE[self.sent:self.sent + n] if self.size == len(IMAGE) else b"\0" * n
        self.sent += n
        return data


def run(argv, client):
    log = io.StringIO()
    real = urllib.request.urlopen
    def urlopen(url, timeout):
        # Media hosts are faked; Spall requests (Request objects) go to the real server.
        return FakeMedia(url) if isinstance(url, str) and url.startswith("https://media.example/") else real(url, timeout=timeout)
    with patch.object(sys, "argv", ["x2vault", *argv]), patch("x2vault.cli.Client", return_value=client), \
            patch("urllib.request.urlopen", side_effect=urlopen), \
            contextlib.redirect_stdout(log):
        try:
            cli.main()
        except SystemExit as exit:
            if exit.code:
                raise AssertionError(f"exit {exit.code}: {log.getvalue()}")
    print(log.getvalue(), file=sys.stderr, flush=True)
    return log.getvalue()


def snapshot(vault):
    return {p.relative_to(vault).as_posix(): p.stat().st_mtime_ns for p in vault.rglob("*") if p.is_file()}


@unittest.skipUnless(ENABLED, "set XBM_API_CONTRACT=1 and XBM_API_URL/TOKEN/VAULT to run")
class ContractTests(unittest.TestCase):
    def setUp(self):
        self.url, self.vault = os.environ["XBM_API_URL"], Path(os.environ["XBM_API_VAULT"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = patch.dict(os.environ, {"X_VAULT_API_TOKEN": os.environ["XBM_API_TOKEN"]})
        self.env.start()
        self.addCleanup(self.env.stop)

    def argv(self, data_dir):
        return ["--credentials", str(self.tmp / "credentials.json"), "watch", "--once", "--vault-api", self.url,
                "--data-dir", str(data_dir), "--folder", FOLDER, "--assets-folder", ASSETS]

    def test_export_three_posts(self):
        base = str(int(time.time() * 1000))
        image, video, text = base + "1", base + "2", base + "3"
        user = {"users": [{"id": "9", "username": "contract", "name": "Contract test"}]}
        def post(post_id, body, media=None):
            item = {"post": {"id": post_id, "text": body, "author_id": "9", "created_at": "2026-11-16T10:00:00.000Z"},
                    "includes": dict(user)}
            if media:
                item["post"]["attachments"] = {"media_keys": ["k"]}
                item["includes"]["media"] = [dict(media, media_key="k")]
            return item
        client = FakeX("9", {
            image: post(image, "Post with an image", {"type": "photo", "url": f"https://media.example/{image}.png"}),
            video: post(video, "Post with a large video", {"type": "video", "variants": [
                {"content_type": "video/mp4", "bit_rate": 1, "url": f"https://media.example/{video}.mp4"}]}),
            text: post(text, "Text only post"),
        })
        log = run(self.argv(self.tmp / "state"), client)
        self.assertIn(f"{video}: warning: media k skipped: larger than the 10 MB media limit", log)
        self.assertEqual(sorted(client.removed), sorted(client.posts))

        state = json.loads((self.tmp / "state/state-9.json").read_text())
        for post_id in client.posts:
            for name, expected in state[post_id]["files"].items():
                if name.startswith(".x-bookmarks/"):
                    local = self.tmp / "state" / name.removeprefix(".x-bookmarks/")
                else:
                    local = self.vault / FOLDER / name
                self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), expected, name)
        self.assertEqual((self.vault / ASSETS / image / "1.png").read_bytes(), IMAGE)
        self.assertFalse((self.vault / ASSETS / video).exists())
        note = next((self.vault / FOLDER).glob(f"* - {video}.md")).read_text()
        self.assertIn('export_status: "partial"', note)
        self.assertIn(f"https://x.com/i/status/{video}", note)
        self.assertEqual([p for p in self.vault.rglob("*") if ".x-bookmarks" in p.parts], [])
        self.assertTrue((self.tmp / "state/receipts").is_dir())

    @unittest.skipUnless(os.environ.get("XBM_FIXTURE_STATE"), "set XBM_FIXTURE_STATE to an existing .x-bookmarks")
    def test_cutover_from_copied_state(self):
        data = self.tmp / "state"
        shutil.copytree(os.environ["XBM_FIXTURE_STATE"], data, ignore=shutil.ignore_patterns("lock"))
        user_id = re.fullmatch(r"state-(\d+)\.json", next(p.name for p in data.glob("state-*.json"))).group(1)
        posts = sorted(re.search(r"(\d+)\.md$", p.name).group(1) for p in (self.vault / FOLDER).glob("*.md"))
        state = json.loads((data / f"state-{user_id}.json").read_text())
        posts = [post_id for post_id in posts if post_id in state]
        client = FakeX(user_id, {post_id: None for post_id in posts})
        before = snapshot(self.vault)
        run(self.argv(data), client)
        # Nothing re-exported: no content lookups and not a single file created or changed in the vault.
        self.assertEqual(client.lookups, [])
        self.assertEqual(snapshot(self.vault), before)
        # API verification agrees exactly with a file-mode check of the same vault.
        local = FileStore(self.vault / FOLDER, data)
        expected = [post_id for post_id in posts if verified(local, state[post_id]["files"])]
        self.assertEqual(sorted(client.removed), expected)
        print(f"cutover: {len(posts)} posts, {len(expected)} verified via API, "
              f"{len(posts) - len(expected)} retained", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
