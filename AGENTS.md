# Contributor and agent instructions

## Project

Python 3.11+ CLI, Linux/macOS, standard library only at runtime. The `x_bookmarks`
package owns OAuth, API reads, Markdown/media export, migrations, and polling.
The Linux systemd examples are optional; foreground commands work on macOS too.

## Commands

```sh
python3 -m unittest discover -s tests -v
python3 -m x_bookmarks --help
```

Use synthetic fixtures and mock API/media calls. Tests must not use real credentials,
incur API charges, or mutate a live X account. Changes to file verification, retries,
OAuth, migrations, or removal behavior need focused regression tests.

## Data and credentials

- Never commit tokens, secrets, OAuth callback URLs containing codes, `.env` files,
  `service.env`, credentials JSON, personal vaults, inventories, or downloaded media.
- Credentials belong outside the repo/vault, normally in
  `~/.config/x-bookmark-exporter/credentials.json`, and must remain owner-readable only.
- Never log authorization headers, refresh/access tokens, client secrets, or raw
  OAuth token responses. Do not send API authorization headers to media hosts.
- Before publishing, inspect the explicit staged-file list. Keep private exports
  out of fixtures, examples, screenshots, build artifacts, and git history.

## Behavior to preserve

- Inventory pagination finishes before removals start; old post IDs may be newly bookmarked.
- Verify saved note/media/raw files before unbookmarking. Preserve edited user notes.
- All exports are intentionally best-effort: missing text, polls, or media produce
  a marked partial export with source URL and ID; verified partial notes may be unbookmarked.
- Filesystem failures, modified notes, and failed verification still block removal.
- Receipt/state migrations must be restartable and keep file hashes consistent.
- Polling persists first-seen timestamps and retry backoff and avoids unnecessary
  expanded reads. Keep manual export usable independently of the monitor.
- Keep filenames deterministic, filesystem-safe, byte-length bounded, and ID-suffixed.

Update README and CLI help when behavior changes. Keep changes focused; avoid adding
dependencies or abstractions when a small standard-library solution is sufficient.
Do not alter installed services or run paid/live exports merely to test code.
