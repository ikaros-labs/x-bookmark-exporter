"""Offline, restartable migration of verified legacy notes."""
import hashlib
import json

from .export import digest, verified
from .naming import filename
from .storage import atomic_write, write_json


def finish(output, journal):
    change = json.loads(journal.read_text())
    old, new = output / change["old"], output / change["new"]
    if old.exists() and digest(old) != change["old_hash"]:
        raise ValueError(f"Edited note; migration paused: {old}")
    if new.exists() and digest(new) != change["files"][change["new"]]:
        raise ValueError(f"Destination conflict: {new}")
    if not new.exists():
        atomic_write(new, change["body"].encode())
    if not verified(output, change["files"]):
        raise ValueError("Migration files failed verification")
    internal = output / ".x-bookmarks"
    write_json(internal / f"receipts/{change['id']}.json", change["files"])
    for path in internal.glob("state-*.json"):
        state = json.loads(path.read_text())
        record = state.get(change["id"], {})
        if change["old"] in record.get("files", {}):
            record["files"] = change["files"]
            write_json(path, state)
    old.unlink(missing_ok=True)
    journal.unlink()


def migrate(output):
    internal = output / ".x-bookmarks"
    journal = internal / "rename-pending.json"
    if journal.exists():
        finish(output, journal)
    count = 0
    for receipt in sorted((internal / "receipts").glob("*.json")):
        post_id = receipt.stem
        old = f"{post_id}.md"
        files = json.loads(receipt.read_text())
        if old not in files:
            continue
        if not verified(output, files):
            print(f"{post_id}: migration skipped; saved files changed or are missing", flush=True)
            continue
        item = json.loads((internal / f"raw/{post_id}.json").read_text())
        new = filename(item)
        if (output / new).exists():
            raise ValueError(f"Destination already exists: {new}")
        body = (output / old).read_text()
        if "\nfirst_seen_at:" not in body.split("\n---\n", 1)[0]:
            exported = next((line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("exported_at: ")), None)
            if exported:
                body = body.replace("---\n", f"---\nfirst_seen_at: {exported}\n", 1)
        updated = {name: value for name, value in files.items() if name != old}
        updated[new] = hashlib.sha256(body.encode()).hexdigest()
        write_json(journal, {"id": post_id, "old": old, "new": new, "old_hash": files[old],
                             "files": updated, "body": body})
        finish(output, journal)
        count += 1
    return count
