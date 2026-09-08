# X bookmark exporter

A standalone Python 3.11+ CLI for Linux/macOS. Downloads individual bookmarked posts
into an Obsidian vault, including photos, available MP4 video/GIF variants, long post
text, poll results, source links, and YAML frontmatter. No runtime dependencies.
X Articles include their full API-provided text and available media. Threads and
quoted posts are linked, not fetched. No LLM, scraping, or runtime dependencies.

**By default, successfully exported bookmarks are removed from X.** Start with
`--keep-bookmarks` to inspect the output. All exports are best-effort: missing text,
polls, images, or playable video produce a clearly marked partial note that is still
eligible for bookmark removal. The source link and post ID are always preserved.

Python 3.11+ is required. Linux and macOS are supported; Windows is not currently
supported because process locking uses `fcntl`. X developer access, OAuth consent,
and API credits are required. This project is not affiliated with X or Obsidian.

## Install

```sh
git clone https://github.com/ikaros-labs/x-bookmark-exporter.git
cd x-bookmark-exporter
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/x-bookmarks --help
```

The examples below run directly from the checkout with `python3 -m x_bookmarks`.
You can instead use the installed `.venv/bin/x-bookmarks` command. No PyPI release
is required.

## Setup

Create an app in the [X Developer Console](https://developer.x.com/), enable OAuth 2.0,
and register this exact callback URL:

```text
http://127.0.0.1:8765/callback
```

Choose Native App for a public client. For a confidential client, supply its secret
through `X_CLIENT_SECRET` during login and subsequent exports. Fund API credits as
needed. The scopes are `tweet.read users.read bookmark.read bookmark.write offline.access`.

Run directly from this project; installation is optional:

```sh
cd ~/x-bookmark-exporter
python3 -m x_bookmarks login --client-id YOUR_CLIENT_ID
```

Authorize in your browser, then paste the full redirected URL into the local CLI
prompt promptly (authorization codes are short lived). A localhost connection error
is expected: this CLI does not run a callback server. Do not paste the URL into chat.
Tokens are stored with owner-only file permissions in
`~/.config/x-bookmark-exporter/credentials.json`, outside the vault. Refresh tokens
are updated automatically. `--credentials PATH` before the subcommand selects a
different credentials file.

## Export

```sh
# Save all accessible bookmarks; remove each after its files are verified.
python3 -m x_bookmarks export --vault /absolute/path/to/vault

# Optional small first run that leaves bookmarks in place.
python3 -m x_bookmarks export --vault /absolute/path/to/vault --limit 5 --keep-bookmarks

# Choose another destination folder inside the vault.
python3 -m x_bookmarks export --vault /absolute/path/to/vault --folder Inbox/X
```

Output:

```text
vault/X/
  2026-09-08 - alice - A few thoughts on coding agents - 123456789.md
  assets/123456789/1.jpg
  assets/123456789/2.mp4
  .x-bookmarks/
    inventory-USER_ID.json
    state-USER_ID.json
    receipts/123456789.json
    raw/123456789.json
```

Notes use `YYYY-MM-DD - author - opening words - post ID.md`. Dates are post creation
dates in UTC, not bookmark dates (which the API does not supply). Missing dates use
`undated`. Titles use the full long-post text when available, strip URLs, normalize
whitespace, remove unsafe filename characters, and truncate at a word boundary within
70 UTF-8 bytes. URL-only posts use `Untitled post`. No LLM or extra API calls are needed.
Names remain stable after export, even if the author changes their handle.

Frontmatter includes author, creation/export dates, `first_seen_at`, post and
conversation IDs, source URL, and tags. `first_seen_at` records the first inventory
observation; the initial backlog shares a discovery date. Media uses local Obsidian embeds.
The hidden directory preserves API data and retry progress; keep it with the notes.

Existing verified ID-only notes migrate automatically before an export. To rename
them without connecting to X or removing any bookmarks:

```sh
python3 -m x_bookmarks migrate --vault /absolute/path/to/vault
```

Migration updates receipts and verification state and can resume after interruption.
For legacy notes, `first_seen_at` uses the original export timestamp as the earliest
recorded observation. Modified notes are skipped, and destination collisions stop
migration. Media paths remain unchanged. Links elsewhere that reference old note
filenames are not rewritten by this CLI.

The CLI collects the inventory before any deletion, downloads media, writes a receipt
and Markdown atomically, checks SHA-256 hashes, then removes the bookmark. Re-running
reuses verified exports and retries removal. A process lock prevents overlapping runs.
Rate limits wait automatically; interrupt with Ctrl-C and rerun the same command.
Transient API server errors retry up to three times. Unavailable content or failed
media downloads are skipped, with omissions recorded in a marked partial note.
Saved files must still pass verification before bookmark removal. Filesystem errors,
modified notes, and removal failures remain retryable errors. The manual `export` command exits with code 1 if any item fails.
An existing untracked note or an edited/missing tracked file blocks removal rather
than overwriting your content. Restore the original files to retry automatic removal.

`--limit` processes the first N returned bookmarks. With `--keep-bookmarks`, repeated
limited runs may revisit those same bookmarks; remove the limit to process the rest.

## Continuous monitoring

```sh
python3 -m x_bookmarks watch --vault /absolute/path/to/vault
```

Polls immediately, then every five minutes after the previous cycle completes.
Use `--interval 600` for ten minutes, `--once` for a single cycle, or
`--keep-bookmarks` to retain successful bookmarks. Ctrl-C stops foreground monitoring.
Account ID is cached with credentials. Each poll paginates the full bookmark list
without author/media expansions; only new or retryable posts get a content lookup.
This still incurs bookmark resource reads, including retained failed bookmarks;
separate post lookups and expanded objects may incur additional charges.

Failures retry after 1, 2, 4, 8, 16, then 24 hours (daily thereafter). Retry schedules and
first-seen timestamps persist in `.x-bookmarks/monitor-USER_ID.json`. To reset a
deferred entry, stop monitoring and remove its entry from this file.
The regular `export` command ignores monitor retry schedules for manual retries.

On Linux, the supplied user service/timer runs a cycle every five minutes without
overlap, with logs in the journal. The example expects the checkout at
`~/x-bookmark-exporter` and Python 3.11+ at `/usr/bin/python3`; adjust
`WorkingDirectory` and `ExecStart` if needed. Configure your destination using
`X_BOOKMARK_VAULT` in the local environment file. That directory receives an `X/`
subdirectory. Changing directories does not move existing exports or their state.

```sh
mkdir -p ~/.config/systemd/user ~/.config/x-bookmark-exporter
# For a new configuration only; preserve any existing local secrets/settings.
cp -n service.env.example ~/.config/x-bookmark-exporter/service.env
chmod 600 ~/.config/x-bookmark-exporter/service.env
# Edit service.env and set X_BOOKMARK_VAULT to your real absolute vault path.
cp x-bookmarks.service x-bookmarks.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now x-bookmarks.timer
systemctl --user start --no-block x-bookmarks.service
journalctl --user -u x-bookmarks.service -f
```

If your OAuth client needs `X_CLIENT_SECRET`, set it in the owner-readable file
`~/.config/x-bookmark-exporter/service.env` as `X_CLIENT_SECRET=...`.
Stop both timer and current cycle with:

```sh
systemctl --user disable --now x-bookmarks.timer
systemctl --user stop x-bookmarks.service
```

User timers run while the user service manager is active. To keep them running after
logout/start them at boot, enable user lingering with `loginctl enable-linger`.

## API limitations

X Articles export their API-provided `plain_text` body and title, with cover/inline
media downloaded locally and appended to the note. Article titles are used in
filenames. Separately supplied code and link entities are preserved where absent
from the body. Rich typography and original media placement are not reconstructed;
frontmatter records `content_type: x_article`, `article_format: plain_text`, and
`media_layout: appended`. The raw API response is saved alongside export records.
A preview-only Article is never treated as a complete export. If body or media
retrieval fails, all posts save available content plus the source link, post ID,
author and title, with `export_status: partial` and `export_warnings` explaining
omissions. Failed content lookups during monitoring fall back to cached or discovered
post metadata (except authentication/billing errors, which stop the cycle). These
best-effort exports are eligible for bookmark removal once saved files are verified.
Previously parked unsupported Articles automatically become eligible on the next poll.

- Only posts X makes accessible are exportable; historical completeness is not guaranteed.
- Partial API responses with usable posts export those posts with warnings. An
  error-only inventory response aborts collection before any deletion.
- Missing media or unavailable MP4 variants are skipped for both posts and Articles;
  verified partial exports are eligible for bookmark removal.
- Thread replies, quoted-post media, external articles, and live streams are not archived.
- The default request vocabulary is `tweet.fields` / `note_tweet`. X's current docs
  also show `post.fields` / `note_post`; if the API rejects the former, retry with
  `--api-vocabulary post`. Responses support both long-text naming variants.
- Removal requires `data.bookmarked: false`; an ambiguous response is retried next run.
- API charges apply to returned resources and bookmark-removal actions. Polling still
  reads retained bookmarks; local deduplication does not eliminate API read charges.
  Check current endpoint prices and spending limits in the X Developer Console.
- Integration has been exercised on a live account; automated tests use synthetic
  data and make no API calls. API availability and field names can vary.
- Inventory collection restarts if interrupted before pagination finishes. Saved
  exports and monitor retry schedules persist across restarts.
- Monitor item failures are deferred without failing the whole cycle. A failed
  discovery/authentication request makes `watch --once` exit nonzero; continuous
  `watch` logs it and retries after its configured interval.

References: [bookmarks](https://docs.x.com/x-api/users/get-bookmarks),
[OAuth scopes](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code),
[pricing](https://docs.x.com/x-api/getting-started/pricing),
[rate limits](https://docs.x.com/x-api/fundamentals/rate-limits).

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m x_bookmarks --help
```

Optional installation in a virtual environment exposes the `x-bookmarks` command:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/x-bookmarks --help
```

## Privacy and security

Credentials are stored outside the checkout and vault with owner-only file
permissions. The Git ignore rules exclude common credential files, environment
files, local vaults, archives, and export records. Keep your vault outside the
repository when possible; ignore rules do not sanitize arbitrary files or history.
Never attach raw inventories, OAuth callback URLs, or private exports to issues.
See [SECURITY.md](SECURITY.md) for vulnerability reporting.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).
Code is available under the [MIT license](LICENSE); downloaded post/article content
remains the property of its respective authors.
