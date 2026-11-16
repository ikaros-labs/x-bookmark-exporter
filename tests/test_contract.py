"""Opt-in contract tests against a real Spall server. No X API calls are made.

    X2VAULT_TEST_SPALL=1 X2VAULT_TEST_SPALL_URL=http://127.0.0.1:18787 X2VAULT_TEST_SPALL_TOKEN=spl_... \\
    X2VAULT_TEST_VAULT=/path/to/the/served/vault [X2VAULT_TEST_FIXTURE_STATE=/path/to/inbox/.x2vault] \\
    python3 -m unittest discover -s tests -p test_contract.py -v

The token's account needs read and create on "3. Resources/X", plus read, create and update on
"3. Resources/X/my posts". X2VAULT_TEST_VAULT is the local folder the server serves, used to
check what arrived on disk.
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
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from x2vault import cli
from x2vault.export import verified
from x2vault.store import FileStore
from test_posts import FakeX as FakeTimeline, post as own_post

ENABLED = os.environ.get("X2VAULT_TEST_SPALL") == "1"
FOLDER, ASSETS = "3. Resources/X/inbox", "3. Resources/X/assets"
POSTS = "3. Resources/X/my posts"
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


@unittest.skipUnless(ENABLED, "set X2VAULT_TEST_SPALL=1 and X2VAULT_TEST_SPALL_URL/TOKEN/VAULT to run")
class ContractTests(unittest.TestCase):
    def setUp(self):
        self.url, self.vault = os.environ["X2VAULT_TEST_SPALL_URL"], Path(os.environ["X2VAULT_TEST_VAULT"])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = patch.dict(os.environ, {"X2VAULT_SPALL_TOKEN": os.environ["X2VAULT_TEST_SPALL_TOKEN"]})
        self.env.start()
        self.addCleanup(self.env.stop)

    def argv(self, data_dir):
        return ["--credentials", str(self.tmp / "credentials.json"), "watch", "--once", "--spall", self.url,
                "--data-dir", str(data_dir), "--bookmarks-folder", FOLDER, "--assets-folder", ASSETS, "--no-posts"]

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
                if name.startswith(".x2vault/"):
                    local = self.tmp / "state" / name.removeprefix(".x2vault/")
                else:
                    local = self.vault / FOLDER / name
                self.assertEqual(hashlib.sha256(local.read_bytes()).hexdigest(), expected, name)
        self.assertEqual((self.vault / ASSETS / image / "1.png").read_bytes(), IMAGE)
        self.assertFalse((self.vault / ASSETS / video).exists())
        note = next((self.vault / FOLDER).glob(f"* - {video}.md")).read_text()
        self.assertIn('export_status: "partial"', note)
        self.assertIn(f"https://x.com/i/status/{video}", note)
        self.assertEqual([p for p in self.vault.rglob("*") if ".x2vault" in p.parts], [])
        self.assertTrue((self.tmp / "state/receipts").is_dir())

    def test_own_posts_and_stats_refresh(self):
        base = int(time.time() * 1000)
        now = datetime.now(timezone.utc)
        created = lambda minutes: (now - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
        ids = {kind: str(base + i) for i, kind in enumerate(("post", "reply", "thread", "quote", "repost", "parent"))}
        client = FakeTimeline([
            own_post(ids["post"], f"Contract plain post {base}", created_at=created(50)),
            own_post(ids["reply"], f"@alice contract reply {base}", refs=[("replied_to", ids["parent"])],
                     reply_to="77", created_at=created(40)),
            own_post(ids["thread"], f"@me contract thread part {base}", refs=[("replied_to", ids["post"])],
                     reply_to="9", conversation_id=ids["post"], created_at=created(30)),
            own_post(ids["quote"], f"Contract quote {base}", refs=[("quoted", ids["parent"])], created_at=created(20)),
            own_post(ids["repost"], "RT @alice: something", refs=[("retweeted", ids["parent"])], created_at=created(10)),
        ], [dict(own_post(ids["parent"], "Alice asks a question", created_at=created(60)), author_id="77")])
        argv = ["--credentials", str(self.tmp / "credentials.json"), "posts", "--spall", self.url,
                "--data-dir", str(self.tmp / "state"), "--posts-folder", POSTS, "--assets-folder", ASSETS]
        log = run(argv, client)
        self.assertIn("Reads: 5 own + 1 others ≈ $0.010", log)
        folder = self.vault / POSTS
        def note(post_id):
            return next(folder.rglob(f"* - {post_id}.md"))
        self.assertEqual(note(ids["post"]).parent, folder)
        self.assertEqual(note(ids["quote"]).parent, folder)
        self.assertEqual(note(ids["reply"]).parent, folder / "replies")
        self.assertEqual(note(ids["thread"]).parent, folder / "replies")
        self.assertEqual(list(folder.rglob(f"* - {ids['repost']}.md")), [])
        self.assertEqual([ids for ids, _ in client.lookup_calls], [[ids["parent"]]])
        self.assertIn("Replying to [@alice]", note(ids["reply"]).read_text())
        self.assertIn('Type: "quote"', note(ids["quote"]).read_text())
        self.assertIn(f'Thread: "{ids["post"]}"', note(ids["thread"]).read_text())
        self.assertTrue((folder / "My posts.base").is_file())
        # The base evaluates in Spall: the "Posts only" view leaves out replies.
        request = urllib.request.Request(f"{self.url}/v1/bases/{urllib.parse.quote(POSTS)}/My%20posts.base/query",
                                         json.dumps({"view": "Posts only"}).encode(), method="POST", headers={
                                             "Authorization": "Bearer " + os.environ["X2VAULT_SPALL_TOKEN"],
                                             "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            rows = json.dumps(json.load(response), ensure_ascii=False)
        self.assertIn(ids["quote"], rows)
        self.assertNotIn(ids["reply"], rows)

        # A day later: stats change, the user has edited a note, and the refresh goes through PATCH.
        state_path = self.tmp / "state/posts-9.json"
        state = json.loads(state_path.read_text())
        state["stats_day"] = "2000-01-01"
        for record in state["posts"].values():
            record["stats_at"] = "2000-01-01T00:00:00+00:00"
        state_path.write_text(json.dumps(state))
        edited = note(ids["post"])
        edited.write_text(edited.read_text() + "\nNikita's comment.\n")
        client.own[ids["post"]]["public_metrics"]["like_count"] = 99
        log = run(argv, client)
        self.assertIn("Reads: 4 own + 0 others", log)
        text = edited.read_text()
        self.assertIn("Likes: 99", text)
        self.assertIn("Nikita's comment.", text)
        audit = (Path(os.environ["X2VAULT_TEST_SPALL_AUDIT"]).read_text().splitlines()
                 if os.environ.get("X2VAULT_TEST_SPALL_AUDIT") else [])
        patches = [line for line in audit if '"patch"' in line]
        print(f"posts: {len(list(folder.rglob('*.md')))} notes in {POSTS}, {len(patches)} PATCH audit entries",
              file=sys.stderr)

    @unittest.skipUnless(os.environ.get("X2VAULT_TEST_FIXTURE_STATE"), "set X2VAULT_TEST_FIXTURE_STATE to an existing .x2vault")
    def test_cutover_from_copied_state(self):
        data = self.tmp / "state"
        shutil.copytree(os.environ["X2VAULT_TEST_FIXTURE_STATE"], data, ignore=shutil.ignore_patterns("lock"))
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
