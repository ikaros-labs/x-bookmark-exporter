import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from fake_spall import FakeSpall
from x2vault.api import APIError
from x2vault.posts import PostsExport
from x2vault.store import FileStore, SpallStore

ME = "9"
NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def post(post_id, text="Hello world", days_ago=100, refs=(), reply_to=None, likes=1, **extra):
    created = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    data = {"id": str(post_id), "text": text, "author_id": ME, "created_at": created, "lang": "en",
            "conversation_id": extra.pop("conversation_id", str(post_id)),
            "public_metrics": {"like_count": likes, "retweet_count": 2, "reply_count": 3, "quote_count": 4,
                               "bookmark_count": 5, "impression_count": 600}}
    if refs:
        data["referenced_tweets"] = [{"type": kind, "id": str(ref)} for kind, ref in refs]
    if reply_to:
        data["in_reply_to_user_id"] = reply_to
    data.update(extra)
    return data


class FakeX:
    """The own-posts timeline (newest first, since_id/until_id/max_results) and batch lookups."""

    def __init__(self, own, others=()):
        self.own = {p["id"]: p for p in own}
        self.others = {p["id"]: p for p in others}
        self.timeline_calls, self.lookup_calls = [], []
        self.reject_private = False

    def user_id(self):
        return ME

    def username(self):
        return "me"

    def timeline(self, user_id, params):
        self.timeline_calls.append(dict(params))
        ids = sorted((int(i) for i in self.own), reverse=True)
        ids = [i for i in ids if i > int(params.get("since_id", 0)) and i < int(params.get("until_id", 10**20))]
        page = [self.own[str(i)] for i in ids[:params["max_results"]]]
        meta = {"result_count": len(page)}
        if len(ids) > len(page):
            meta["next_token"] = "more"
        return {"data": page, "meta": meta} if page else {"meta": meta}

    def posts(self, ids, params):
        self.lookup_calls.append((list(ids), dict(params)))
        if self.reject_private and "non_public_metrics" in params["tweet.fields"]:
            raise APIError(400)
        found = [p for i in ids for p in [self.own.get(i) or self.others.get(i)] if p]
        users = [{"id": "77", "username": "alice", "name": "Alice"}]
        return {"data": found, "includes": {"users": users},
                "errors": [{"resource_id": i} for i in ids if i not in self.own and i not in self.others]}


def notes(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md"))


class PostsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "my posts"
        self.store = FileStore(self.root, Path(tmp.name) / "state")

    def run_export(self, client, budget=1000, now=NOW):
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            PostsExport(client, self.store, budget, folder="X/my posts", now=now).run()
        return log.getvalue()

    def state(self):
        return json.loads((self.store.data_dir / f"posts-{ME}.json").read_text())

    def test_backfill_paginates_then_since_id_polls_return_nothing(self):
        client = FakeX([post(1000 + i, f"Post number {i}") for i in range(250)])
        log = self.run_export(client)
        self.assertEqual([c["max_results"] for c in client.timeline_calls], [100, 100, 100])
        self.assertEqual(len(notes(self.root)), 250)
        self.assertIn("250 own + 0 others ≈ $0.250", log)
        state = self.state()
        self.assertEqual((state["newest_id"], state["sweep"]), ("1249", {}))

        client.timeline_calls.clear()
        log = self.run_export(client)
        self.assertEqual(len(client.timeline_calls), 1)
        self.assertEqual(client.timeline_calls[0]["since_id"], "1249")
        self.assertIn("Reads: 0 own + 0 others", log)

        client.own["2000"] = post(2000, "Something new", days_ago=0)
        self.run_export(client)
        self.assertIn("2026-11-16 Something new - 2000.md", notes(self.root))
        self.assertEqual(self.state()["newest_id"], "2000")

    def test_repost_skipped_and_not_reprocessed(self):
        client = FakeX([post(10, "RT @alice: theirs", refs=[("retweeted", 5)]), post(11, "Mine")])
        self.run_export(client)
        self.assertEqual(notes(self.root), ["2026-08-08 Mine - 11.md"])
        self.assertEqual(self.state()["skipped"], {"10": "repost"})
        self.assertEqual(client.lookup_calls, [])

    def test_replies_and_quotes_fetch_referenced_posts_in_batches(self):
        others = [dict(post(500 + i, f"Parent {i}"), author_id="77") for i in range(150)]
        own = [post(1000 + i, f"@alice reply {i}", refs=[("replied_to", 500 + i)], reply_to="77") for i in range(149)]
        own.append(post(2000, "Look at this", refs=[("quoted", 649)]))
        client = FakeX(own, others)
        log = self.run_export(client)
        self.assertEqual([len(ids) for ids, _ in client.lookup_calls], [100, 50])
        self.assertIn("150 own + 150 others ≈ $0.900", log)
        self.assertEqual(len([n for n in notes(self.root) if n.startswith("replies/")]), 149)
        reply = (self.root / "replies/2026-08-08 reply 7 - 1007.md").read_text()
        self.assertIn('Type: "reply"', reply)
        self.assertIn("Replying to [@alice](https://x.com/alice/status/507):\n\n> Parent 7\n", reply)
        quote = (self.root / "2026-08-08 Look at this - 2000.md").read_text()
        self.assertIn('Type: "quote"', quote)
        self.assertIn("Quoting [@alice](https://x.com/alice/status/649):\n\n> Parent 149\n", quote)

    def test_self_thread_uses_own_posts_without_lookups(self):
        client = FakeX([post(1, "Thread start"),
                        post(2, "@me second part", refs=[("replied_to", 1)], reply_to=ME, conversation_id="1")])
        self.run_export(client)
        self.assertEqual(client.lookup_calls, [])
        note = (self.root / "replies/2026-08-08 second part - 2.md").read_text()
        self.assertIn('Thread: "1"', note)
        self.assertIn('Link: "https://x.com/me/status/2"', note)
        self.assertIn("Replying to [@me](https://x.com/me/status/1):\n\n> Thread start\n", note)

    def test_unavailable_parent_still_saves_reply(self):
        client = FakeX([post(1, "@ghost hi", refs=[("replied_to", 404)], reply_to="66")])
        self.run_export(client)
        note = (self.root / "replies/2026-08-08 hi - 1.md").read_text()
        self.assertIn("Replying to https://x.com/i/status/404 (unavailable", note)

    def test_budget_stops_paging_and_resumes_next_day(self):
        client = FakeX([post(100 + i, f"Post {i}") for i in range(30)])
        log = self.run_export(client, budget=12)
        self.assertEqual(len(notes(self.root)), 12)
        self.assertIn("daily read budget of 12 reached", log)
        self.assertEqual(self.state()["sweep"]["until_id"], "118")
        # Same day: nothing left to spend, no request at all.
        client.timeline_calls.clear()
        self.run_export(client, budget=12)
        self.assertEqual(client.timeline_calls, [])
        # Next days: continues below until_id, never re-reads.
        self.run_export(client, budget=12, now=NOW + timedelta(days=1))
        self.assertEqual(client.timeline_calls[0]["until_id"], "118")
        self.run_export(client, budget=12, now=NOW + timedelta(days=2))
        self.assertEqual(len(notes(self.root)), 30)
        self.assertEqual((self.state()["newest_id"], self.state()["sweep"]), ("129", {}))

    def test_reply_waits_for_budget_to_fetch_parent(self):
        own = [post(1, "@alice yes", refs=[("replied_to", 500)], reply_to="77")] + [post(i, "Plain") for i in range(2, 6)]
        client = FakeX(own, [dict(post(500, "Question?"), author_id="77")])
        self.run_export(client, budget=5)
        self.assertEqual(len(notes(self.root)), 4)
        self.assertEqual(list(self.state()["pending"]), ["1"])
        self.run_export(client, budget=5, now=NOW + timedelta(days=1))
        self.assertIn("replies/2026-08-08 yes - 1.md", notes(self.root))
        self.assertEqual(self.state()["pending"], {})

    def test_refresh_only_young_posts_once_a_day_and_keeps_user_edits(self):
        client = FakeX([post(1, "Old post", days_ago=20), post(2, "Fresh post", days_ago=3, likes=1)])
        self.run_export(client)
        fresh = self.root / "2026-11-13 Fresh post - 2.md"
        text = fresh.read_text().replace('Lang: "en"\n', 'Lang: "en"\nRating: 5\n') + "\nMy own comment.\n"
        fresh.write_text(text)
        old_before = (self.root / "2026-10-27 Old post - 1.md").read_text()
        client.own["2"]["public_metrics"]["like_count"] = 42
        client.own["2"]["non_public_metrics"] = {"impression_count": 9000}
        tomorrow = NOW + timedelta(days=1)
        self.run_export(client, now=tomorrow)
        self.assertEqual([ids for ids, _ in client.lookup_calls], [["2"]])
        self.assertIn("non_public_metrics", client.lookup_calls[0][1]["tweet.fields"])
        updated = fresh.read_text()
        self.assertIn("Likes: 42\n", updated)
        self.assertIn("Impressions: 9000\n", updated)
        self.assertIn(f'Stats updated: "{tomorrow.isoformat()}"', updated)
        self.assertIn("Rating: 5\n", updated)
        self.assertTrue(updated.endswith("\nMy own comment.\n"))
        self.assertEqual((self.root / "2026-10-27 Old post - 1.md").read_text(), old_before)
        # Once per UTC day; frozen after 14 days.
        self.run_export(client, now=tomorrow + timedelta(hours=1))
        self.run_export(client, now=NOW + timedelta(days=10))  # 13 days old: still refreshed
        self.assertEqual(len(client.lookup_calls), 2)
        self.run_export(client, now=NOW + timedelta(days=12))  # 15 days old: frozen
        self.assertEqual(len(client.lookup_calls), 2)

    def test_refresh_falls_back_when_private_metrics_rejected(self):
        client = FakeX([post(2, "Fresh", days_ago=1)])
        self.run_export(client)
        client.reject_private = True
        log = self.run_export(client, now=NOW + timedelta(days=1))
        self.assertIn("non_public_metrics rejected", log)
        self.assertEqual(client.lookup_calls[-1][1]["tweet.fields"], "id,public_metrics")
        self.assertTrue(self.state()["no_private_metrics"])

    def test_base_created_once_and_never_overwritten(self):
        client = FakeX([post(1)])
        self.run_export(client)
        base = self.root / "My posts.base"
        self.assertIn('file.inFolder("X/my posts")', base.read_text())
        self.assertIn("name: Posts only", base.read_text())
        base.write_text("mine")
        self.run_export(client)
        self.assertEqual(base.read_text(), "mine")

    def test_media_and_long_text(self):
        long_post = post(3, "short version https://t.co/m", attachments={"media_keys": ["k"]},
                         note_tweet={"text": "The full long text\n\nsecond paragraph https://t.co/l https://t.co/m",
                                     "entities": {"urls": [
                                         {"url": "https://t.co/l", "expanded_url": "https://example.com/a"},
                                         {"url": "https://t.co/m", "media_key": "k"}]}},
                         refs=[("replied_to", 1)], reply_to=ME)
        client = FakeX([post(1, "Root"), long_post])
        client.timeline = self.with_media(client.timeline)
        def fetch(url, path, limit=None):
            path.write_bytes(b"jpeg")
        with patch("x2vault.posts.download", side_effect=fetch):
            self.run_export(client)
        note = (self.root / "replies/2026-08-08 The full long text second paragraph - 3.md").read_text()
        self.assertIn("The full long text\n\nsecond paragraph https://example.com/a\n", note)
        self.assertIn("![](../assets/3/1.jpg)", note)
        self.assertEqual((self.root / "assets/3/1.jpg").read_bytes(), b"jpeg")

    @staticmethod
    def with_media(timeline):
        def wrapped(user_id, params):
            page = timeline(user_id, params)
            page["includes"] = {"media": [{"media_key": "k", "type": "photo", "url": "https://media.example/k.jpg"}]}
            return page
        return wrapped


class SpallPostsTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeSpall()
        self.addCleanup(self.api.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SpallStore(self.api.url, "secret", "X/my posts", Path(tmp.name), sleep=lambda s: None)

    def run_export(self, client, now):
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            PostsExport(client, self.store, 100, assets_dir=PurePosixPath("/X/assets"),
                        folder="X/my posts", now=now).run()
        return log.getvalue()

    def test_stats_refresh_patches_frontmatter(self):
        client = FakeX([post(2, "Fresh", days_ago=1)])
        self.run_export(client, NOW)
        path = "X/my posts/2026-11-15 Fresh - 2.md"
        self.assertIn(path, self.api.files)
        self.assertIn("X/my posts/My posts.base", self.api.files)
        client.own["2"]["public_metrics"]["like_count"] = 7
        self.run_export(client, NOW + timedelta(days=1))
        self.assertIn(b"Likes: 7\n", self.api.files[path])
        patch_request = [r for r in self.api.requests if r[0] == "PATCH"][0]
        self.assertTrue(patch_request[2]["If-Match"].startswith('"sha256:'))

    def test_refresh_without_update_permission_warns_and_continues(self):
        client = FakeX([post(2, "Fresh", days_ago=1)])
        self.run_export(client, NOW)
        self.api.can_update = False
        client.own["3"] = post(3, "Newer", days_ago=0)
        log = self.run_export(client, NOW + timedelta(days=1))
        self.assertIn("stats not refreshed", log)
        self.assertIn("X/my posts/2026-11-16 Newer - 3.md", self.api.files)


if __name__ == "__main__":
    unittest.main()
