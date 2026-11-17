# x2vault

A standalone Python 3.11+ CLI for Linux/macOS. Exports your X bookmarks and your own
posts (see [Your own posts](#your-own-posts)). Downloads individual bookmarked posts
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
git clone https://github.com/ikaros-labs/x2vault.git
cd x2vault
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/x2vault --help
```

The examples below run directly from the checkout with `python3 -m x2vault`.
You can instead use the installed `.venv/bin/x2vault` command. No PyPI release
is required.

## Setup

Create an app in the [X Developer Console](https://developer.x.com/), enable OAuth 2.0,
and register this exact callback URL:

```text
http://127.0.0.1:8765/callback
```

Choose Native App for a public client. For a confidential client, supply its secret
through `X2VAULT_X_CLIENT_SECRET` during login and subsequent exports. Fund API credits as
needed. The scopes are `tweet.read users.read bookmark.read bookmark.write offline.access`.

Run directly from this project; installation is optional:

```sh
cd ~/x2vault
python3 -m x2vault login --client-id YOUR_CLIENT_ID
```

Authorize in your browser, then paste the full redirected URL into the local CLI
prompt promptly (authorization codes are short lived). A localhost connection error
is expected: this CLI does not run a callback server. Do not paste the URL into chat.
Tokens are stored with owner-only file permissions in
`~/.config/x2vault/credentials.json`, outside the vault. Refresh tokens
are updated automatically. `--credentials PATH` before the subcommand selects a
different credentials file.

## Export

```sh
# Save all accessible bookmarks; remove each after its files are verified.
python3 -m x2vault export --vault /absolute/path/to/vault

# Optional small first run that leaves bookmarks in place.
python3 -m x2vault export --vault /absolute/path/to/vault --limit 5 --keep-bookmarks

# Choose another destination folder inside the vault.
python3 -m x2vault export --vault /absolute/path/to/vault --bookmarks-folder Inbox/X
```

Output:

```text
vault/X/
  2026-09-08 - alice - A few thoughts on coding agents - 123456789.md
  assets/123456789/1.jpg
  assets/123456789/2.mp4
  .x2vault/
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
python3 -m x2vault migrate --vault /absolute/path/to/vault
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

## Assets folder

By default, media is saved under `assets/POST_ID/` inside the notes folder, such as
`vault/X/assets/123/1.jpg`. To use a shared vault attachment directory:

```sh
python3 -m x2vault export --vault /path/to/vault --assets-folder Attachments/X
python3 -m x2vault watch --vault /path/to/vault --assets-folder "Attachments/X media"
```

An explicit `--assets-folder` is relative to the **vault root**, independently of
`--bookmarks-folder`. The examples place media under `vault/Attachments/X/POST_ID/` or
`vault/Attachments/X media/POST_ID/`. Paths outside the vault and application-state
directories are rejected. Local embeds and verification records point to the chosen
location. Shared folders outside the notes directory use relative Markdown embeds.

You can also set `X2VAULT_ASSETS_FOLDER`; the command-line flag takes precedence.
For the systemd service, add that variable to `~/.config/x2vault/service.env`.
The service reads it on its next cycle. No value preserves the existing default.

This setting applies to **new exports**. Existing notes, media, and receipts are
reused at their original locations; changing the setting does not move them.
When backing up exports, include the configured media directory as well as the notes
and `.x2vault` state.

## Continuous monitoring

```sh
python3 -m x2vault watch --vault /absolute/path/to/vault
```

Polls immediately, then every five minutes after the previous cycle completes.
Use `--interval 600` for ten minutes, `--once` for a single cycle, or
`--keep-bookmarks` to retain successful bookmarks. Ctrl-C stops foreground monitoring.
Account ID is cached with credentials. Each poll paginates the full bookmark list
without author/media expansions; only new or retryable posts get a content lookup.
This still incurs bookmark resource reads, including retained failed bookmarks;
separate post lookups and expanded objects may incur additional charges.

Failures retry after 1, 2, 4, 8, 16, then 24 hours (daily thereafter). Retry schedules and
first-seen timestamps persist in `.x2vault/monitor-USER_ID.json`. To reset a
deferred entry, stop monitoring and remove its entry from this file.
The regular `export` command ignores monitor retry schedules for manual retries.

On Linux, the templates in [`examples/systemd/`](examples/systemd/) run a cycle every five minutes without
overlap, with logs in the journal. The example expects the checkout at
`~/x2vault` and Python 3.11+ at `/usr/bin/python3`; adjust
`WorkingDirectory` and `ExecStart` if needed. Configure your destination using
`X2VAULT_VAULT` in the local environment file. That directory receives an `X/`
subdirectory. Changing directories does not move existing exports or their state.

```sh
mkdir -p ~/.config/systemd/user ~/.config/x2vault
# For a new configuration only; preserve any existing local secrets/settings.
cp -n .env.example ~/.config/x2vault/service.env
chmod 600 ~/.config/x2vault/service.env
# Edit service.env and set X2VAULT_VAULT to your real absolute vault path.
cp examples/systemd/x2vault.service examples/systemd/x2vault.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now x2vault.timer
systemctl --user start --no-block x2vault.service
journalctl --user -u x2vault.service -f
```

If your OAuth client needs `X2VAULT_X_CLIENT_SECRET`, set it in the owner-readable file
`~/.config/x2vault/service.env` as `X2VAULT_X_CLIENT_SECRET=...`.
Stop both timer and current cycle with:

```sh
systemctl --user disable --now x2vault.timer
systemctl --user stop x2vault.service
```

User timers run while the user service manager is active. To keep them running after
logout/start them at boot, enable user lingering with `loginctl enable-linger`.

## Your own posts

`watch` also exports your own posts after each bookmarks poll (`--no-posts` turns that
off), and `posts` runs the same pass once. This only reads from X.

```sh
# First run: backfill everything X returns (up to 3,200 posts), with a raised budget.
python3 -m x2vault posts --vault /absolute/path/to/vault --budget 1000
```

- **Where:** `--posts-folder` (`X2VAULT_POSTS_FOLDER`, default `3. Resources/X/my posts`).
  Replies go to its `replies/` subfolder. File names are
  `YYYY-MM-DD <first words> - <id>.md`. Media is saved like bookmark media, in the same
  assets folder and with the same size cap.
- **What:** posts (`Type: post`), quote posts (`Type: quote`, with the quoted post as a
  `>` block) and replies (`Type: reply`, with the post they answer as a `>` block). A
  reply to yourself is part of a thread: `Thread` holds the conversation ID, so a thread's
  posts can be grouped. Plain reposts are skipped. Each note has the frontmatter `Type`,
  `Date`, `Created`, `Lang`, `Link`, `Thread`, `Likes`, `Reposts`, `Replies`, `Quotes`,
  `Bookmarks`, `Impressions` and `Stats updated`, then the text exactly as written
  (long posts in full, `t.co` links expanded), media, and the parent or quoted post.
- **Incremental:** after the backfill, each poll asks only for posts newer than the
  newest one saved, so a poll with nothing new costs nothing.
- **Stats:** once per UTC day, the stats of posts younger than 14 days are read again from
  your own timeline (`start_time` 14 days back, an Owned Read) and only those frontmatter
  keys (and `Stats updated`) are rewritten. Your other edits to the
  note stay. After 14 days the stats are frozen. In Spall mode this uses
  `PATCH /v1/notes/{path}`, so the Spall account needs `update` on the posts folder:

  ```yaml
  accounts:
    x2vault:
      access:
        "3. Resources/X": [read, create]
        "3. Resources/X/my posts": [read, create, update]
  ```

  Without `update`, the exporter logs a warning and skips the refresh.
- **Missing media repair:** a note whose media failed to download keeps a line like
  `> Media 1 not saved (HTTP Error 403: Forbidden); see the original post.` Each poll, up
  to 30 such posts are retried from the saved raw JSON (no X API calls): the media is
  downloaded to the usual assets path and only that line is replaced by the embed, so the
  frontmatter and your other edits stay. A media item that fails 3 times keeps its line.
  `x2vault repair-media` (same target options as `posts`) does one pass over all of them.
  In Spall mode this replaces the note with `PUT` and `If-Match`, so it also needs
  `update` on the posts folder. Bookmark notes are not rewritten.
- **Cost and budget:** X charges per post returned: $0.001 for posts read from your own
  timeline (an "Owned Read" when the app belongs to you) and $0.005 for posts looked up
  by ID (reply parents and quoted posts, fetched in batches of up to 100), even when they
  are your own. Every post returned counts
  against `X2VAULT_POSTS_DAILY_READ_BUDGET` (default 200 per UTC day, `--budget N` on
  `posts`). When the budget runs out, paging stops and the next day continues where it
  left off. Each pass logs its reads and the estimated cost. Set a spending limit in the
  X developer console as well.
- **Base:** `My posts.base` is created in the posts folder once, if missing, with the
  views "All" (newest first), "Top" (most likes) and "Posts only" (no replies). It is
  never overwritten.

## Storage modes

Every `export` and `watch` run needs exactly one destination:

- `--vault PATH` (**file mode**, the default behavior above): notes and media are written
  directly into a local vault folder. State lives in `<folder>/.x2vault/` unless
  `--data-dir` says otherwise. Media size is unlimited unless `--max-media-mb` is set.
- `--spall URL` (**Spall mode**): notes and media are created through
  [Spall](https://spall.nikmel.xyz) ([source](https://github.com/ikaros-labs/spall)),
  a scoped REST API for Obsidian vaults, using `PUT /v1/files/...` with
  `If-None-Match: *`. The token comes from `X2VAULT_SPALL_TOKEN`; the URL can also come
  from `X2VAULT_SPALL_URL`. The API never overwrites: an existing file with different
  content is an error and the bookmark is kept. Files are verified through the API's
  SHA-256 ETag before a bookmark is removed.

```sh
export X2VAULT_SPALL_TOKEN=spl_...   # account with [read, create] on "3. Resources/X"
python3 -m x2vault watch --spall http://127.0.0.1:8787 \
  --data-dir ~/.local/state/x2vault \
  --bookmarks-folder "3. Resources/X/inbox" --assets-folder "3. Resources/X/assets"
```

In Spall mode:

- `--data-dir` (or `X2VAULT_DATA_DIR`) is **required**. It holds the state, monitor
  queue, inventory, receipts, raw JSON and lock. Spall hides dotfiles, so none
  of this goes into the vault.
- `--bookmarks-folder` and `--assets-folder` are vault paths, exactly as in file mode (the
  token's account should have no `root`). Both also read `X2VAULT_BOOKMARKS_FOLDER` and
  `X2VAULT_ASSETS_FOLDER`.
- Media larger than `--max-media-mb` (`X2VAULT_MAX_MEDIA_MB`, **default 10**, matching
  Spall's default 10 MiB body limit) is skipped. The note is saved as a partial
  export with a warning naming the media URL; the original post link is always in the
  note. The size is checked from `Content-Length` before downloading, and a download
  stops as soon as it passes the limit.
- Network errors and HTTP 5xx from the API are retried 3 times with backoff. Any 4xx
  (bad token, missing grant, body too large) fails that post: the bookmark is kept
  and the error is recorded in the monitor queue for a later retry.
- `migrate` works only in file mode.

### Moving existing state into a data dir

To switch an existing file-mode setup to Spall mode (or to an explicit `--data-dir`),
stop the old exporter first (never run two: X rotates refresh tokens), then copy its
state folder without the lock:

```sh
mkdir -p /path/to/data-dir
rsync -a --exclude lock "/path/to/vault/X/.x2vault/" /path/to/data-dir/
```

Receipts keep listing raw files as `.x2vault/raw/ID.json`; those are checked in
the data dir and everything else through the store, so every already-exported
bookmark is recognised and nothing is exported again. Keep the old folder until the
new setup has run cleanly; it is the rollback.

## Docker

A multi-arch image (`linux/amd64`, `linux/arm64`) is published to
`ghcr.io/ikaros-labs/x2vault` on every push to `main` (`latest` plus the
full commit SHA) and on `v*` tags (semver tags). It runs `watch --interval 300` in Spall
mode as a non-root user (UID 10001). There is no HTTP port.

`/data` is the volume: credentials in `/data/credentials.json`, state in `/data/state`.

| Variable | |
|---|---|
| `X2VAULT_SPALL_URL` | Spall base URL (required) |
| `X2VAULT_SPALL_TOKEN` | Spall token with `[read, create]` on the folders below, plus `update` on the posts folder (required) |
| `X2VAULT_X_CLIENT_ID` | your X developer app's client ID (required for login) |
| `X2VAULT_X_CLIENT_SECRET` | only for confidential OAuth clients |
| `X2VAULT_BOOKMARKS_FOLDER` | notes folder, default `3. Resources/X/inbox` |
| `X2VAULT_ASSETS_FOLDER` | media folder, default `3. Resources/X/assets` |
| `X2VAULT_MAX_MEDIA_MB` | media size cap, default `10` |
| `X2VAULT_DATA_DIR` | state folder, default `/data/state` |
| `X2VAULT_POSTS_FOLDER` | your own posts, default `3. Resources/X/my posts` |
| `X2VAULT_POSTS_DAILY_READ_BUDGET` | billable post reads per UTC day, default `200` |

```sh
docker run -d --name x2vault --restart unless-stopped \
  -e X2VAULT_SPALL_URL=http://spall-host:8787 -e X2VAULT_SPALL_TOKEN=spl_... \
  -e X2VAULT_X_CLIENT_ID=... -v x2vault-data:/data \
  ghcr.io/ikaros-labs/x2vault:latest
# One-time login: open the printed URL, then paste the redirected URL.
docker exec -it x2vault python -m x2vault --credentials /data/credentials.json login
```

With compose, use [`examples/docker/docker-compose.yml`](examples/docker/docker-compose.yml)
(`docker compose up -d`, then the same login command via `docker compose exec`). To
build locally instead: `docker build -t x2vault .`.

Until credentials exist, the container waits and checks again every minute, and it
reports healthy while it waits for login. After that, the `HEALTHCHECK` reports
unhealthy when no poll has succeeded for 15 minutes. To carry over existing
credentials and state instead of logging in again, copy `credentials.json` to `/data/`
and the old `.x2vault` contents (without `lock`) to `/data/state/`, owned by UID 10001.

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
python3 -m x2vault --help
```

Optional installation in a virtual environment exposes the `x2vault` command:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/x2vault --help
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
