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

An offline `triage` command sorts the exported inbox into topic folders through a
reviewable plan, with an optional keyboard web UI. See [Triage the inbox](#triage-the-inbox-offline).

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

## Assets folder

By default, media is saved under `assets/POST_ID/` inside the notes folder, such as
`vault/X/assets/123/1.jpg`. To use a shared vault attachment directory:

```sh
python3 -m x_bookmarks export --vault /path/to/vault --assets-folder Attachments/X
python3 -m x_bookmarks watch --vault /path/to/vault --assets-folder "Attachments/X media"
```

An explicit `--assets-folder` is relative to the **vault root**, independently of
`--folder`. The examples place media under `vault/Attachments/X/POST_ID/` or
`vault/Attachments/X media/POST_ID/`. Paths outside the vault and application-state
directories are rejected. Local embeds and verification records point to the chosen
location. Shared folders outside the notes directory use relative Markdown embeds.

You can also set `X_BOOKMARK_ASSETS_FOLDER`; the command-line flag takes precedence.
For the systemd service, add that variable to `~/.config/x-bookmark-exporter/service.env`.
The service reads it on its next cycle. No value preserves the existing default.

This setting applies to **new exports**. Existing notes, media, and receipts are
reused at their original locations; changing the setting does not move them.
When backing up exports, include the configured media directory as well as the notes
and `.x-bookmarks` state.

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

On Linux, the templates in [`examples/systemd/`](examples/systemd/) run a cycle every five minutes without
overlap, with logs in the journal. The example expects the checkout at
`~/x-bookmark-exporter` and Python 3.11+ at `/usr/bin/python3`; adjust
`WorkingDirectory` and `ExecStart` if needed. Configure your destination using
`X_BOOKMARK_VAULT` in the local environment file. That directory receives an `X/`
subdirectory. Changing directories does not move existing exports or their state.

```sh
mkdir -p ~/.config/systemd/user ~/.config/x-bookmark-exporter
# For a new configuration only; preserve any existing local secrets/settings.
cp -n .env.example ~/.config/x-bookmark-exporter/service.env
chmod 600 ~/.config/x-bookmark-exporter/service.env
# Edit service.env and set X_BOOKMARK_VAULT to your real absolute vault path.
cp examples/systemd/x-bookmarks.service examples/systemd/x-bookmarks.timer ~/.config/systemd/user/
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

## Triage the inbox (offline)

Exported notes pile up in one folder. `triage` proposes where each one should go and
writes a plan for you to review. It needs no credentials and makes no network calls,
and it never writes into the vault: plans live outside it, and `--apply` only changes a
copy.

```sh
# 1. Propose a plan (default inbox: X/inbox; default plan: ./triage-plan.md).
x-bookmarks triage --vault ~/Obsidian --inbox "3. Resources/X/inbox" --plan out/triage-plan.md

# 2. Review it: edit out/triage-plan.md in any editor, or use the keyboard UI.
x-bookmarks triage-ui --vault ~/Obsidian --plan out/triage-plan.md   # http://localhost:8724

# 3. Execute it on a copy. The notes folder (the inbox's parent) is copied into
#    the target at the same vault-relative path, and notes are moved there.
x-bookmarks triage --vault ~/Obsidian --plan out/triage-plan.md --apply --target out/vault-copy
```

The steps above produce the following on a 179-note inbox:

```text
Triage plan for 179 notes in 3. Resources/X/inbox:
  ai                 60  new
  dev                23  new
  electronics        12  new
  3d-printing        11  new
  eink                7  existing
  startups            5  new
  learning            4  new
  health              3  new
  productivity        2  new
  keep               39
  delete             13
```

A plan entry looks like this. The action word is the part you edit.

```markdown
- [x] eink ← [[2026-09-08 - ryanlpeterman - Used Fable 5.1 to turn my Kindle into a Linux terminal so I could - 2097406401068896283]]
  - @ryanlpeterman · “Used Fable 5.1 to turn my Kindle into a Linux terminal…” · 1 media · why: kindle
```

**Actions.** Each entry's action is one of:
- a folder name, created as a sibling of the inbox. Put names that contain spaces in backticks.
- `delete`, which moves the note to `_delete/` in the copy. Nothing is ever unlinked.
- `keep`, which leaves the note in the inbox.

Untick `[x]` to skip an entry.

**Classification.** Each note is scored against keyword rules: a strong keyword scores 3 and
a weak one scores 1. Weak keywords alone never move a note. A note whose author is already
filed in an existing sibling folder (for example `eink/`) gets +4 for that folder. Niche
topics carry a boost, so a "Kindle" note goes to `eink`, not `dev`. Notes scoring below 3
are marked `keep`.

`--tfidf` also proposes a folder for unmatched notes by cosine similarity to each folder's
notes (`--tfidf-min`, default 0.2). These proposals are unticked, because TF-IDF on short
posts is noisy.

**Deletes.**
- Duplicates and posts whose text is unavailable are proposed as ticked deletes.
- Low-content posts are proposed as unticked deletes. These are media-only posts, posts
  with almost no text, and short replies. You confirm them.
- Dead external links are not checked, because triage makes no network calls.

**Custom rules.** Add topics or keywords with `--rules my-rules.json` (see
`examples/triage-rules.example.json`):
- A trailing `*` matches a prefix, which helps with inflected Russian words.
- `"replace": true` drops the built-in rules.

**UI keys.**

| Key | Action |
| --- | --- |
| `j` / `k` | Move down / up |
| `1`–`0` | File into a folder: existing folders first, then by plan count |
| `d` | Delete |
| `i` | Keep in inbox |
| `x` / space | Tick or untick |
| `n` | New folder |
| `o` | Open the post on X |

Press `?` in the UI for this list. The header shows a one-line summary and filter chips with
counts. On a phone, tapping a note opens it in a bottom sheet with tap targets for every folder,
so triage works without a keyboard. The detail pane shows the post's text, media and outbound
links.

Every keypress or tap rewrites the plan file atomically.

**UI network exposure.** The UI binds `100.66.44.47` (a tailscale address) by default. Set
`--host` or `X_BOOKMARK_TRIAGE_HOST` to change it, for example `--host 127.0.0.1`. Avoid
`0.0.0.0` on a machine with a public IP. The UI:
- answers only requests whose `Host` is the bound address or loopback;
- accepts writes only as `application/json` POSTs carrying a same-origin `Origin` header (a
  missing or foreign `Origin` gets 403, which blocks CSRF from any web page you open);
- returns generic errors without paths or tracebacks;
- reads notes and media from the vault read-only, and only from inside the plan's notes folder.

**After applying.** Moved notes keep their content unchanged:
- Wikilink embeds resolve vault-wide.
- `../assets` links still work, because topic folders sit next to the inbox.
- Export receipts stay in `inbox/.x-bookmarks/`.

Each apply writes a `triage-applied-*.json` log to the copy. Re-running an apply skips
notes that were already moved and never overwrites an existing file.

**Apply safety.** `--apply` only ever modifies a copy that it created itself:
- On the first run it copies the notes folder and writes a `.x-bookmarks-triage-copy` marker
  into the copy. Later runs reuse a copy only if that marker is present.
- It refuses a `--target` that is inside the vault or inside any directory with `.obsidian/`,
  and refuses copies that contain `.obsidian/`.
- It refuses a symlinked inbox and skips symlinked notes or topic folders.
- Every source and destination must resolve inside the copy and outside `--vault`. This
  holds even when `--vault` points at the wrong place.

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
