import argparse
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .api import Client, login
from .export import process
from .storage import write_json
from .migration import migrate
from .monitor import watch
from .paths import assets_directory


def main():
    parser = argparse.ArgumentParser(description="Export individual X bookmarks and media to Markdown")
    parser.add_argument("--credentials", type=Path, default=Path.home() / ".config/x-bookmark-exporter/credentials.json")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("login", help="Authorize your own X developer app")
    auth.add_argument("--client-id", default=os.environ.get("X_CLIENT_ID"))
    auth.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    export = commands.add_parser("export", help="Save posts/media, then remove successfully exported bookmarks")
    export.add_argument("--vault", required=True, type=Path)
    export.add_argument("--folder", default="X", help="Relative folder within the vault (default: X)")
    export.add_argument("--keep-bookmarks", action="store_true")
    export.add_argument("--limit", type=int, help="Maximum bookmarks to process this run")
    export.add_argument("--api-vocabulary", choices=["tweet", "post"], default="tweet",
                        help="Field naming used by your API deployment; use post if tweet.fields is rejected")
    migration = commands.add_parser("migrate", help="Rename existing verified notes locally, without API calls")
    migration.add_argument("--vault", required=True, type=Path)
    migration.add_argument("--folder", default="X")
    monitor = commands.add_parser("watch", help="Continuously export new bookmarks with lightweight discovery")
    monitor.add_argument("--vault", required=True, type=Path)
    monitor.add_argument("--folder", default="X")
    monitor.add_argument("--interval", type=int, default=300, help="Seconds between polls (default: 300)")
    monitor.add_argument("--once", action="store_true", help="Run one polling cycle, for service timers")
    monitor.add_argument("--keep-bookmarks", action="store_true")
    monitor.add_argument("--api-vocabulary", choices=["tweet", "post"], default="tweet")
    for command in (export, monitor):
        command.add_argument("--assets-folder", default=os.environ.get("X_BOOKMARK_ASSETS_FOLDER"),
                             help="Media folder relative to vault root (default: assets inside the notes folder); also X_BOOKMARK_ASSETS_FOLDER")
    args = parser.parse_args()
    args.credentials = args.credentials.expanduser().resolve()
    args.credentials.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.credentials.with_suffix(".lock")
    try:
        with lock_path.open("a") as credential_lock:
            fcntl.flock(credential_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "login":
                if not args.client_id:
                    parser.error("Provide --client-id or X_CLIENT_ID")
                login(args.credentials, args.client_id, args.redirect_uri)
                return
            if args.command == "export" and args.limit is not None and args.limit < 1:
                parser.error("--limit must be positive")
            if args.command == "watch" and args.interval < 60:
                parser.error("--interval must be at least 60 seconds")
            vault = args.vault.expanduser().resolve()
            output = (vault / args.folder).resolve()
            if not output.is_relative_to(vault):
                parser.error("--folder must remain inside the vault")
            assets_dir = assets_directory(vault, output, args.assets_folder) if args.command != "migrate" else None
            output.mkdir(parents=True, exist_ok=True)
            internal = output / ".x-bookmarks"
            internal.mkdir(exist_ok=True)
            with (internal / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                print(f"Migrated {migrate(output)} existing notes.", flush=True)
                if args.command == "migrate":
                    return
                client = Client(args.credentials)
                if args.command == "watch":
                    watch(client, output, args.api_vocabulary, args.keep_bookmarks, args.interval, args.once,
                          assets_dir=assets_dir)
                    return
                user_id = client.user_id()
                state_path = internal / f"state-{user_id}.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                print("Collecting bookmarks before making any removals…", flush=True)
                items = client.inventory(user_id, args.api_vocabulary, args.limit)
                inventory_path = internal / f"inventory-{user_id}.json"
                previous = json.loads(inventory_path.read_text()) if inventory_path.exists() else {}
                now = datetime.now(timezone.utc).isoformat()
                for post_id, item in items.items():
                    item["first_seen_at"] = previous.get(post_id, {}).get("first_seen_at", now)
                previous.update(items)
                write_json(inventory_path, previous)
                print(f"Collected {len(items)} bookmarks.", flush=True)
                failed = process(client, user_id, items, output, state, state_path, args.keep_bookmarks,
                                 assets_dir=assets_dir)
                print(f"Finished: {len(items) - failed} successful, {failed} failed. Output: {output}")
                if failed:
                    raise SystemExit(1)
    except BlockingIOError:
        parser.exit(1, "Another exporter is using these credentials or this output folder.\n")
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        parser.exit(1, f"Error: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted. Saved progress will be reused on the next run.\n")


if __name__ == "__main__":
    main()
