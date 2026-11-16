import argparse
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .api import Client, login
from .export import process
from .storage import write_json
from .migration import migrate
from .monitor import watch
from .paths import assets_directory
from .store import SpallStore, FileStore, vault_path

FOLDER = os.environ.get("X2VAULT_BOOKMARKS_FOLDER", "X")


def positive_number(value):
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def add_target(command, api=True):
    target = command.add_mutually_exclusive_group()
    target.add_argument("--vault", type=Path, help="Local vault folder (file mode)")
    target.add_argument("--spall", metavar="URL", help="Spall base URL (Spall mode; token from X2VAULT_SPALL_TOKEN; "
                        "also X2VAULT_SPALL_URL)" if api else "Not supported: migrate is file-mode only")
    command.add_argument("--bookmarks-folder", default=FOLDER,
                         help="Notes folder relative to the vault (default: X); also X2VAULT_BOOKMARKS_FOLDER")
    command.add_argument("--data-dir", type=Path, default=os.environ.get("X2VAULT_DATA_DIR"),
                         help="Exporter state, receipts, raw JSON and lock (default: <folder>/.x2vault in file "
                              "mode; required in Spall mode); also X2VAULT_DATA_DIR")


def main():
    parser = argparse.ArgumentParser(description="Export individual X bookmarks and media to Markdown")
    parser.add_argument("--credentials", type=Path, default=Path.home() / ".config/x2vault/credentials.json",
                        help="OAuth credentials file (default: ~/.config/x2vault/credentials.json)")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("login", help="Authorize your own X developer app")
    auth.add_argument("--client-id", default=os.environ.get("X2VAULT_X_CLIENT_ID"))
    auth.add_argument("--redirect-uri", default="http://127.0.0.1:8765/callback")
    export = commands.add_parser("export", help="Save posts/media, then remove successfully exported bookmarks")
    add_target(export)
    export.add_argument("--keep-bookmarks", action="store_true")
    export.add_argument("--limit", type=int, help="Maximum bookmarks to process this run")
    export.add_argument("--api-vocabulary", choices=["tweet", "post"], default="tweet",
                        help="Field naming used by your API deployment; use post if tweet.fields is rejected")
    migration = commands.add_parser("migrate", help="Rename existing verified notes locally, without API calls")
    add_target(migration, api=False)
    monitor = commands.add_parser("watch", help="Continuously export new bookmarks with lightweight discovery")
    add_target(monitor)
    monitor.add_argument("--interval", type=int, default=300, help="Seconds between polls (default: 300)")
    monitor.add_argument("--once", action="store_true", help="Run one polling cycle, for service timers")
    monitor.add_argument("--keep-bookmarks", action="store_true")
    monitor.add_argument("--api-vocabulary", choices=["tweet", "post"], default="tweet")
    for command in (export, monitor):
        command.add_argument("--assets-folder", default=os.environ.get("X2VAULT_ASSETS_FOLDER"),
                             help="Media folder relative to vault root (default: assets inside the notes folder); also X2VAULT_ASSETS_FOLDER")
        command.add_argument("--max-media-mb", type=positive_number, default=os.environ.get("X2VAULT_MAX_MEDIA_MB"),
                             help="Skip larger media with a partial-note warning (default: 10 in Spall mode, "
                                  "unlimited in file mode); also X2VAULT_MAX_MEDIA_MB")
    args = parser.parse_args()
    args.credentials = args.credentials.expanduser().resolve()
    args.credentials.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.credentials.with_suffix(".lock")
    try:
        # A long-running watch (e.g. a fresh container) waits for `login` instead of exiting; the
        # credentials lock is not held yet, so login can run alongside it.
        while args.command == "watch" and not args.once and not args.credentials.exists():
            print(f"No credentials at {args.credentials}; run the login command. Checking again in 60 seconds.",
                  flush=True)
            time.sleep(60)
        with lock_path.open("a") as credential_lock:
            fcntl.flock(credential_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.command == "login":
                if not args.client_id:
                    parser.error("Provide --client-id or X2VAULT_X_CLIENT_ID")
                login(args.credentials, args.client_id, args.redirect_uri)
                return
            if args.command == "export" and args.limit is not None and args.limit < 1:
                parser.error("--limit must be positive")
            if args.command == "watch" and args.interval < 60:
                parser.error("--interval must be at least 60 seconds")
            if args.vault is None and args.spall is None and args.command != "migrate":
                args.spall = os.environ.get("X2VAULT_SPALL_URL")
            if args.spall is not None:
                if args.command == "migrate":
                    parser.error("migrate works only on a local vault: use --vault PATH")
                if args.data_dir is None:
                    parser.error("Spall mode requires --data-dir or X2VAULT_DATA_DIR (state must stay out of the vault)")
                internal = args.data_dir.expanduser().resolve()
                limit = args.max_media_mb if args.max_media_mb is not None else 10
                output = SpallStore(args.spall, os.environ.get("X2VAULT_SPALL_TOKEN"), args.bookmarks_folder, internal,
                                  max_media_bytes=int(float(limit) * 2**20))
                assets_dir = None
                if args.assets_folder is not None:
                    assets_dir = PurePosixPath("/") / vault_path(args.assets_folder, "--assets-folder")
            else:
                if args.vault is None:
                    parser.error("Provide exactly one of --vault PATH or --spall URL")
                vault = args.vault.expanduser().resolve()
                output = (vault / args.bookmarks_folder).resolve()
                if not output.is_relative_to(vault):
                    parser.error("--bookmarks-folder must remain inside the vault")
                assets_dir = assets_directory(vault, output, args.assets_folder) if args.command != "migrate" else None
                output.mkdir(parents=True, exist_ok=True)
                internal = args.data_dir.expanduser().resolve() if args.data_dir else output / ".x2vault"
                limit = getattr(args, "max_media_mb", None)
            internal.mkdir(parents=True, exist_ok=True)
            with (internal / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if isinstance(output, Path):
                    print(f"Migrated {migrate(output, internal)} existing notes.", flush=True)
                    if args.command == "migrate":
                        return
                    output = FileStore(output, internal, int(float(limit) * 2**20) if limit is not None else None)
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
                print(f"Finished: {len(items) - failed} successful, {failed} failed. Output: {output.root}")
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
