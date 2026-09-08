"""Persistent polling state, with no expanded reads for known/parked bookmarks."""
import json
import time
from datetime import datetime, timezone

from .api import APIError
from .export import process
from .storage import write_json


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def poll(client, output, vocabulary="tweet", keep=False):
    user_id = client.user_id()
    internal = output / ".x-bookmarks"
    state_path = internal / f"state-{user_id}.json"
    queue_path = internal / f"monitor-{user_id}.json"
    inventory_path = internal / f"inventory-{user_id}.json"
    state, queue, previous = map(read_json, (state_path, queue_path, inventory_path))
    # Always paginate fully: a newly bookmarked post can have an old post ID.
    current = client.inventory(user_id, vocabulary, lightweight=True)
    now = time.time()
    stamp = datetime.now(timezone.utc).isoformat()
    for post_id in current:
        entry = queue.setdefault(post_id, {})
        if entry.get("parked") and entry.get("last_error") == "X Article export is unsupported":
            entry.pop("parked", None)
            entry.pop("last_error", None)
            entry["retry_at"] = 0
        entry.setdefault("first_seen_at", previous.get(post_id, {}).get("first_seen_at", stamp))
    write_json(queue_path, queue)
    successful = failed = skipped = 0
    for post_id in current:
        entry = queue[post_id]
        record = state.get(post_id, {})
        if entry.get("parked") or entry.get("retry_at", 0) > now or (keep and record.get("files")):
            skipped += 1
            continue
        try:
            if record.get("files"):
                # Only verification/removal remains; do not pay for another content lookup.
                item = {"post": {"id": post_id}, "includes": {}}
            else:
                try:
                    item = client.lookup(post_id, vocabulary)
                except (OSError, ValueError, RuntimeError) as error:
                    cached = previous.get(post_id, {})
                    if not cached.get("post", {}).get("article") or (
                        isinstance(error, APIError) and error.status in {401, 402}
                    ):
                        raise
                    item = dict(cached, export_warnings=["Article refresh failed; saved available cached data"])
                item["first_seen_at"] = entry["first_seen_at"]
                previous[post_id] = item
                write_json(inventory_path, previous)
            failures = process(client, user_id, {post_id: item}, output, state, state_path, keep)
            if failures:
                raise RuntimeError(state[post_id].get("last_error", "Export failed"))
            entry.pop("last_error", None)
            entry.update(attempts=0, retry_at=0)
            successful += 1
        except (OSError, ValueError, RuntimeError) as error:
            if isinstance(error, APIError) and error.status in {401, 402}:
                # Account/permission/billing failure: do not repeat it for every post.
                raise
            attempts = entry.get("attempts", 0) + 1
            delay = min(86400, 3600 * 2 ** min(attempts - 1, 5))
            entry.update(attempts=attempts, retry_at=time.time() + delay, last_error=str(error))
            failed += 1
            print(f"{post_id}: retry deferred for {delay // 3600} hours: {error}", flush=True)
        write_json(queue_path, queue)
    print(f"Poll complete: {len(current)} bookmarks, {successful} saved/processed, {skipped} skipped, {failed} deferred.", flush=True)
    return failed


def watch(client, output, vocabulary="tweet", keep=False, interval=300, once=False):
    while True:
        try:
            poll(client, output, vocabulary, keep)
        except (OSError, ValueError, RuntimeError) as error:
            if once:
                raise
            print(f"Poll failed: {error}. Retrying after the polling interval.", flush=True)
        if once:
            return
        print(f"Next poll in {interval} seconds.", flush=True)
        time.sleep(interval)
