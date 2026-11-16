"""Export your own X posts: backfill, incremental since_id polls, a daily read budget and stats refresh.

Reads only. Posts returned by the own timeline (/2/users/{me}/tweets) cost an
"Owned Read" ($0.001 each); lookups by ID (/2/tweets?ids=: reply parents, quoted
posts) $0.005 each, even when the post is our own. Every post returned counts
against the daily budget, so a poll with nothing new costs nothing.
"""
import hashlib
import html
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import APIError
from .export import download, media_source
from .naming import clean
from .storage import write_json
from .store import UpdateForbidden

OWN_COST, OTHER_COST = 0.001, 0.005
REFRESH_DAYS = 14
BASE_NAME = "My posts.base"
MEDIA_FIELDS = "media_key,type,url,variants,alt_text,preview_image_url"
TIMELINE = {
    "tweet.fields": "id,text,author_id,created_at,public_metrics,lang,conversation_id,referenced_tweets,"
                    "entities,attachments,note_tweet,in_reply_to_user_id",
    "expansions": "attachments.media_keys",
    "media.fields": MEDIA_FIELDS,
}
REFERENCED = {
    "tweet.fields": "id,text,author_id,created_at,note_tweet,entities",
    "expansions": "author_id",
    "user.fields": "id,name,username",
}
STATS = {"tweet.fields": "id,public_metrics"}
PRIVATE_STATS = {"tweet.fields": "id,public_metrics,non_public_metrics"}


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def text_of(post):
    """Full text as written: long posts from note_tweet, t.co links expanded, media links dropped."""
    full = post.get("note_tweet") or post
    text = html.unescape(full.get("text") or post.get("text") or "")
    for entity in (full.get("entities") or post.get("entities") or {}).get("urls", []):
        short = entity.get("url")
        if not short:
            continue
        if entity.get("media_key"):
            text = text.replace(short, "")
        else:
            text = text.replace(short, entity.get("unwound_url") or entity.get("expanded_url") or short)
    return text.strip()


def kind_of(post):
    types = {ref.get("type") for ref in post.get("referenced_tweets", [])}
    if "retweeted" in types:
        return "repost"
    if "replied_to" in types:
        return "reply"
    return "quote" if "quoted" in types else "post"


def stats(post):
    public, private = post.get("public_metrics") or {}, post.get("non_public_metrics") or {}
    return {
        "Likes": public.get("like_count"), "Reposts": public.get("retweet_count"),
        "Replies": public.get("reply_count"), "Quotes": public.get("quote_count"),
        "Bookmarks": public.get("bookmark_count"),
        "Impressions": private.get("impression_count", public.get("impression_count")),
    }


def note_name(post):
    created = post.get("created_at")
    date = utc(created).date().isoformat() if created else "undated"
    words = re.sub(r"^(@\w+\s+)+", "", text_of(post)).split()
    title = clean(" ".join(words[:8]), 70) or "Untitled post"
    return f"{date} {title} - {post['id']}.md"


def quote_block(label, ref):
    if ref.get("unavailable"):
        return f"\n{label} https://x.com/i/status/{ref['id']} (unavailable: deleted, protected or not returned)\n"
    link = f"https://x.com/{ref['username']}/status/{ref['id']}"
    lines = "\n".join(f"> {line}" if line else ">" for line in ref["text"].split("\n"))
    return f"\n{label} [@{ref['username']}]({link}):\n\n{lines}\n"


def render(item, handle, media_links, warnings, now):
    post = item["post"]
    kind = kind_of(post)
    metadata = {
        "Type": kind, "Date": utc(post["created_at"]).date().isoformat(), "Created": post["created_at"],
        "Lang": post.get("lang"), "Link": f"https://x.com/{handle}/status/{post['id']}",
        "Thread": post.get("conversation_id", post["id"]), **stats(post), "Stats updated": now,
    }
    frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items())
    body = f"---\n{frontmatter}\n---\n\n{text_of(post)}\n"
    for link, alt in media_links:
        body += f"\n![]({link})\n" + (f"\n{alt}\n" if alt else "")
    for warning in warnings:
        body += f"\n> {warning}\n"
    labels = {"replied_to": "Replying to", "quoted": "Quoting"}
    for ref in post.get("referenced_tweets", []):
        if ref.get("type") in labels:
            body += quote_block(labels[ref["type"]], item["refs"][ref["id"]])
    return body


def base_file(folder):
    columns = "\n".join(f"      - {c}" for c in ("file.name", "Type", "Date", "Likes", "Reposts", "Impressions", "Lang"))
    def view(name, sort, extra=""):
        return (f"  - type: table\n    name: {name}\n{extra}    order:\n{columns}\n"
                f"    sort:\n      - property: {sort}\n        direction: DESC\n")
    return (f"filters:\n  and:\n    - file.inFolder({json.dumps(folder, ensure_ascii=False)})\n"
            f"    - file.ext == \"md\"\nviews:\n" + view("All", "Date") + view("Top", "Likes")
            + view("Posts only", "Date", "    filters:\n      and:\n        - Type != \"reply\"\n"))


class Budget:
    """Billable post reads per UTC day, persisted in the posts state."""

    def __init__(self, state, limit, today):
        if state.get("budget", {}).get("day") != today:
            state["budget"] = {"day": today, "own": 0, "others": 0}
        self.day, self.limit = state["budget"], limit
        self.own = self.others = 0

    def remaining(self):
        return max(0, self.limit - self.day["own"] - self.day["others"])

    def charge(self, own=0, others=0):
        self.own += own
        self.others += others
        self.day["own"] += own
        self.day["others"] += others


class PostsExport:
    def __init__(self, client, store, budget=200, assets_dir=None, folder=None, now=None):
        self.client, self.store, self.assets_dir, self.folder = client, store, assets_dir, folder
        self.now = now or datetime.now(timezone.utc)
        self.today = self.now.date().isoformat()
        self.user_id = client.user_id()
        self.handle = client.username()
        self.path = store.data_dir / f"posts-{self.user_id}.json"
        self.state = read_json(self.path)
        for key in ("posts", "skipped", "pending", "sweep"):
            self.state.setdefault(key, {})
        self.budget = Budget(self.state, budget, self.today)
        self.out_of_budget = False

    def save(self):
        write_json(self.path, self.state)

    def run(self):
        self.base()
        self.refresh()
        self.fetch()
        written = self.process()
        self.save()
        cost = self.budget.own * OWN_COST + self.budget.others * OTHER_COST
        day = self.state["budget"]
        print(f"Posts: {written} saved, {len(self.state['pending'])} pending, {len(self.state['posts'])} total. "
              f"Reads: {self.budget.own} own + {self.budget.others} others ≈ ${cost:.3f} "
              f"(today {day['own'] + day['others']}/{self.budget.limit}).", flush=True)
        if self.out_of_budget:
            print(f"Posts: daily read budget of {self.budget.limit} reached; continuing on the next UTC day "
                  "(raise it with --budget or X2VAULT_POSTS_DAILY_READ_BUDGET).", flush=True)
        return written

    def base(self):
        if self.state.get("base_created") or self.folder is None:
            return
        if not self.store.exists(BASE_NAME):
            self.store.create(BASE_NAME, base_file(self.folder).encode())
        self.state["base_created"] = True
        self.save()

    def fetch(self):
        """Newest first. Resumable: until_id marks the oldest post read so far in this sweep."""
        sweep = self.state["sweep"]
        if not sweep:
            sweep.update(since_id=self.state.get("newest_id"))
        while True:
            remaining = self.budget.remaining()
            if remaining < 5:  # the timeline's smallest page
                self.out_of_budget = True
                return
            params = dict(TIMELINE, max_results=min(100, remaining))
            for key in ("since_id", "until_id"):
                if sweep.get(key):
                    params[key] = sweep[key]
            page = self.client.timeline(self.user_id, params)
            posts = page.get("data", [])
            self.budget.charge(own=len(posts))
            media = {m["media_key"]: m for m in page.get("includes", {}).get("media", [])}
            for post in posts:
                if post["id"] in self.state["posts"] or post["id"] in self.state["skipped"]:
                    continue
                keys = post.get("attachments", {}).get("media_keys", [])
                self.state["pending"][post["id"]] = {
                    "post": post, "includes": {"media": [media[k] for k in keys if k in media]}, "refs": {},
                    "fetched_at": self.now.isoformat()}
            if posts:
                ids = [int(p["id"]) for p in posts]
                sweep["until_id"] = str(min(ids))
                sweep["top_id"] = str(max(ids + [int(sweep.get("top_id") or 0)]))
            done = not posts or not page.get("meta", {}).get("next_token")
            if done and sweep.get("top_id"):
                self.state["newest_id"] = str(max(int(sweep["top_id"]), int(self.state.get("newest_id") or 0)))
            if done:
                self.state["sweep"] = {}
            self.save()
            if done:
                return

    def local_ref(self, post_id):
        """A post of our own we already have: no paid lookup needed."""
        item = self.state["pending"].get(post_id)
        if item is None and post_id in self.state["posts"]:
            raw = self.store.data_dir / f"posts/raw/{post_id}.json"
            item = read_json(raw) or None
        if item is None:
            return None
        return {"id": post_id, "username": self.handle, "text": text_of(item["post"])}

    def lookup(self, ids):
        """Batch lookups of referenced posts, up to 100 per request and within the budget."""
        found = {}
        ids = list(ids)
        while ids:
            count = min(100, len(ids), self.budget.remaining())
            if count < 1:
                self.out_of_budget = True
                break
            batch, ids = ids[:count], ids[count:]
            page = self.client.posts(batch, REFERENCED)
            users = {u["id"]: u for u in page.get("includes", {}).get("users", [])}
            data = page.get("data", [])
            # A lookup by ID is never an Owned Read, even for posts of our own.
            self.budget.charge(others=len(data))
            for post in data:
                user = users.get(post.get("author_id"), {})
                found[post["id"]] = {"id": post["id"], "username": user.get("username", "i"), "text": text_of(post)}
            for post_id in batch:
                found.setdefault(post_id, {"id": post_id, "unavailable": True})
        return found

    def process(self):
        pending = self.state["pending"]
        for post_id, item in list(pending.items()):
            if kind_of(item["post"]) == "repost":
                self.state["skipped"][post_id] = "repost"
                del pending[post_id]
        wanted = {}
        oldest = self.state["sweep"].get("until_id")
        for item in pending.values():
            post = item["post"]
            for ref in post.get("referenced_tweets", []):
                if ref.get("type") in {"replied_to", "quoted"} and ref["id"] not in item["refs"]:
                    local = self.local_ref(ref["id"])
                    if local:
                        item["refs"][ref["id"]] = local
                    elif (oldest and ref["type"] == "replied_to" and post.get("in_reply_to_user_id") == self.user_id
                          and int(ref["id"]) < int(oldest)):
                        pass  # own earlier post: the unfinished backfill will read it, no separate lookup
                    else:
                        wanted.setdefault(ref["id"], []).append(item)
        if wanted:
            for ref_id, ref in self.lookup(wanted).items():
                for item in wanted[ref_id]:
                    item["refs"][ref_id] = ref
            self.save()
        written = 0
        for post_id in sorted(pending, key=int):
            item = pending[post_id]
            refs = [r["id"] for r in item["post"].get("referenced_tweets", []) if r.get("type") in {"replied_to", "quoted"}]
            if any(ref not in item["refs"] for ref in refs):
                continue  # waits for budget to fetch its parent or quoted post
            try:
                self.write(item)
                written += 1
            except (OSError, ValueError, RuntimeError) as error:
                item["last_error"] = str(error)
                print(f"{post_id}: post not saved: {error}", flush=True)
            self.save()
        return written

    def write(self, item):
        post = item["post"]
        post_id = post["id"]
        if not re.fullmatch(r"[0-9]{1,20}", post_id):
            raise ValueError("Invalid post ID")
        relative = item.get("path") or (("replies/" if kind_of(post) == "reply" else "") + note_name(post))
        if "body" not in item:
            # Media first, then the note; the rendered body is kept so a retry publishes identical bytes.
            media = {m["media_key"]: m for m in item["includes"].get("media", [])}
            note_dir = Path(os.path.dirname(relative) or ".")
            links, warnings = [], []
            for index, key in enumerate(post.get("attachments", {}).get("media_keys", []), 1):
                source = None
                try:
                    if key not in media:
                        raise ValueError("missing from the API response")
                    source, extension = media_source(media[key])
                    base = self.assets_dir if self.assets_dir is not None else self.store.root / "assets"
                    target = Path(os.path.relpath(base / post_id / f"{index}{extension}", self.store.root)).as_posix()
                    limit = {"limit": self.store.max_media_bytes} if self.store.max_media_bytes is not None else {}
                    with self.store.staging(target) as temp:
                        download(source, temp, **limit)
                        self.store.create(target, temp)
                    links.append((urllib.parse.quote(Path(os.path.relpath(target, note_dir)).as_posix(), safe="/.-_~"),
                                  media[key].get("alt_text", "")))
                except (OSError, ValueError) as error:  # StoreError (vault write) fails the post instead
                    warnings.append(f"Media {index} not saved ({error}); see the original post.")
                    print(f"{post_id}: warning: media {key} not saved: {error} ({source})", flush=True)
            item.update(path=relative, body=render(item, self.handle, links, warnings, item["fetched_at"]))
            write_json(self.store.data_dir / f"posts/raw/{post_id}.json", item)
            self.save()
        self.store.create(relative, item["body"].encode())
        self.state["posts"][post_id] = {"path": relative, "created": post["created_at"], "type": kind_of(post),
                                        "stats_at": item["fetched_at"],
                                        "sha256": hashlib.sha256(item["body"].encode()).hexdigest()}
        del self.state["pending"][post_id]
        print(f"{post_id}: saved {relative}", flush=True)

    def refresh(self):
        """Once per UTC day: re-read stats of posts younger than 14 days, then they are frozen.

        Reads the own timeline (an Owned Read) from 14 days ago, not a lookup by ID.
        """
        if self.state.get("stats_day") == self.today:
            return
        cutoff = self.now - timedelta(days=REFRESH_DAYS)
        due = {post_id for post_id, record in self.state["posts"].items()
               if utc(record["created"]) >= cutoff and record.get("stats_at", "")[:10] != self.today}
        fields = STATS if self.state.get("no_private_metrics") else PRIVATE_STATS
        token = None
        while due:
            remaining = self.budget.remaining()
            if remaining < 5:  # the timeline's smallest page
                self.out_of_budget = True
                return  # try again on a later poll
            params = dict(fields, start_time=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"), max_results=min(100, remaining))
            if token:
                params["pagination_token"] = token
            try:
                page = self.client.timeline(self.user_id, params)
            except APIError as error:
                if fields is STATS or error.status not in {400, 403}:
                    raise
                print("Posts: non_public_metrics rejected; using public metrics only.", flush=True)
                self.state["no_private_metrics"] = True
                fields, token = STATS, None
                continue
            data = page.get("data", [])
            self.budget.charge(own=len(data))
            stamp = self.now.isoformat()
            for post in data:
                if post["id"] not in due:
                    continue  # reposts, posts not saved yet, already refreshed today
                record = self.state["posts"][post["id"]]
                try:
                    self.store.update_frontmatter(record["path"], dict(stats(post), **{"Stats updated": stamp}))
                except UpdateForbidden as error:
                    print(f"Posts: warning: stats not refreshed: {error}. Grant update on the posts folder.",
                          flush=True)
                    self.state["stats_day"] = self.today
                    self.save()
                    return
                except (OSError, ValueError, RuntimeError) as error:
                    print(f"{post['id']}: warning: stats not refreshed: {error}", flush=True)
                record["stats_at"] = stamp
                due.discard(post["id"])
            self.save()
            token = page.get("meta", {}).get("next_token")
            if not data or not token:
                break
        self.state["stats_day"] = self.today
        self.save()

def export_posts(client, store, budget=200, assets_dir=None, folder=None):
    return PostsExport(client, store, budget, assets_dir, folder).run()
