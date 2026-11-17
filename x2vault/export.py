import hashlib
import html
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import USER_AGENT
from .storage import atomic_write, write_json
from .naming import filename
from .articles import article_text, media_keys
from .store import INTERNAL, FileStore, digest


class MediaTooLarge(ValueError):
    pass


def as_store(output):
    """Accept a notes folder path (file mode, as before) or a store."""
    return output if hasattr(output, "create") else FileStore(output)


def media_source(media):
    if media.get("type") == "photo" and media.get("url"):
        url = media["url"]
        suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        return url, suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"
    videos = [v for v in media.get("variants", []) if v.get("content_type") == "video/mp4" and v.get("url")]
    if videos:
        best = max(videos, key=lambda v: v.get("bit_rate", v.get("bitrate", 0)))
        return best["url"], ".mp4"
    raise ValueError("Media has no downloadable photo or MP4")


def too_large(limit):
    return MediaTooLarge(f"larger than the {limit / 2**20:g} MB media limit")


def download(url, path, limit=None):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Media URL must use HTTPS")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".media-")
    try:
        with os.fdopen(fd, "wb") as stream:
            # Do not send the API bearer token to a media host; only a User-Agent.
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                if urllib.parse.urlsplit(response.url).scheme != "https":
                    raise ValueError("Insecure media redirect")
                content_type = response.headers.get_content_type()
                if not content_type.startswith(("image/", "video/", "application/octet-stream")):
                    raise ValueError("Media response is not an image or video")
                expected = response.headers.get("Content-Length")
                if limit is not None and expected is not None and int(expected) > limit:
                    raise too_large(limit)
                total = 0
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
                    total += len(chunk)
                    if limit is not None and total > limit:
                        raise too_large(limit)
                if not total or (expected is not None and total != int(expected)):
                    raise ValueError("Empty or incomplete media download")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def render(item, media_links):
    post = item["post"]
    user = next((u for u in item["includes"].get("users", []) if u["id"] == post.get("author_id")), {})
    url = f"https://x.com/i/status/{post['id']}"
    full = post.get("note_tweet") or post.get("note_post") or post
    text = full.get("text")
    article = post.get("article")
    if article:
        try:
            text = article_text(article)
        except ValueError:
            text = "Article body unavailable. Follow the original post link to read it on X."
    if not isinstance(text, str) or not text:
        text = "Post text unavailable. Follow the original post link to read it on X."
    for entity in full.get("entities", {}).get("urls", []):
        if entity.get("url"):
            text = text.replace(entity["url"], entity.get("unwound_url") or entity.get("expanded_url") or entity["url"])
    metadata = {
        "source": "x", "url": url, "post_id": post["id"],
        "author": user.get("username", post.get("author_id", "unknown")),
        "author_name": user.get("name", ""), "created_at": post.get("created_at"),
        "conversation_id": post.get("conversation_id"),
        "first_seen_at": item.get("first_seen_at") or datetime.now(timezone.utc).isoformat(),
        "exported_at": datetime.now(timezone.utc).isoformat(), "tags": ["x-bookmark"],
    }
    if article:
        metadata.update(content_type="x_article", article_title=article.get("title"),
                        article_format="plain_text", media_layout="appended", export_status="complete")
    if item.get("export_warnings"):
        metadata.update(export_status="partial", export_warnings=item["export_warnings"])
    # JSON values are valid YAML values and safely quote IDs and arbitrary metadata.
    frontmatter = "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in metadata.items())
    body = f"---\n{frontmatter}\n---\n\n[Original post]({url})\n\n{html.unescape(text)}\n"
    if item.get("export_warnings"):
        body += "\n> Partial export. Some content was unavailable; see export_warnings in the frontmatter.\n"
    for relative, alt in media_links:
        if relative.startswith("../") or any(c in relative for c in "[]#|^<>\n\r"):
            body += f"\n![]({urllib.parse.quote(relative, safe='/.-_~')})\n"
        else:
            body += f"\n![[{relative}]]\n"
        if alt:
            body += f"\n{alt}\n"
    polls = {p["id"]: p for p in item["includes"].get("polls", [])}
    for poll_id in post.get("attachments", {}).get("poll_ids", []):
        if poll_id not in polls:
            continue
        body += "\nPoll:\n\n" + "\n".join(f"- {o['label']}: {o.get('votes', 0)} votes" for o in polls[poll_id]["options"]) + "\n"
    refs = post.get("referenced_tweets", post.get("referenced_posts", []))
    for ref in refs:
        body += f"\n{ref['type']}: https://x.com/i/status/{ref['id']}\n"
    return body


def archive(item, output, assets_dir=None):
    store = as_store(output)
    post = item["post"]
    post_id = post["id"]
    if not re.fullmatch(r"[0-9]+", post_id):
        raise ValueError("Invalid post ID")
    receipt = store.data_dir / f"receipts/{post_id}.json"
    if receipt.exists():
        files = json.loads(receipt.read_text())
        if verified(store, files):
            return files
    article = post.get("article")
    warnings = list(item.get("export_warnings", []))
    if article:
        try:
            article_text(article)
        except ValueError:
            warnings.append("Article body unavailable; saved metadata and source link")
    else:
        full = post.get("note_tweet") or post.get("note_post") or post
        if not isinstance(full.get("text"), str) or not full["text"]:
            warnings.append("Post text unavailable; saved metadata and source link")
    polls = {p["id"] for p in item["includes"].get("polls", [])}
    for poll_id in post.get("attachments", {}).get("poll_ids", []):
        if poll_id not in polls:
            warnings.append(f"Poll {poll_id} unavailable")
    media = {m["media_key"]: m for m in item["includes"].get("media", [])}
    links, files, hashes = [], [], {}
    try:
        keys = media_keys(post)
    except ValueError:
        keys = []
        warnings.append("Media references unavailable")
    for index, key in enumerate(keys, 1):
        source = None
        try:
            if key not in media:
                raise ValueError("Attached media missing from API response")
            source, extension = media_source(media[key])
            destination = (assets_dir if assets_dir is not None else store.root / "assets") / post_id / f"{index}{extension}"
            relative = Path(os.path.relpath(destination, store.root)).as_posix()
            limit = {"limit": store.max_media_bytes} if store.max_media_bytes is not None else {}
            with store.staging(relative) as temp:
                download(source, temp, **limit)
                hashes[relative] = store.create(relative, temp)
            files.append(relative)
            links.append((relative, media[key].get("alt_text", "")))
        except MediaTooLarge as error:
            warnings.append(f"Media {key} skipped: {error}; view it on the original post ({source})")
            print(f"{post_id}: warning: media {key} skipped: {error} ({source})", flush=True)
        except (OSError, ValueError) as error:
            # StoreError (vault write failed) is not caught here: the post fails and the bookmark is kept.
            warnings.append(f"Media {key} unavailable: {error}")
    item = dict(item, export_warnings=warnings)
    relative = filename(item)
    if store.exists(relative):
        raise ValueError(f"Untracked note already exists: {relative}; refusing to overwrite")
    # Publish the note only after all available content and omissions are accounted for.
    body = render(item, links)
    raw = f"{INTERNAL}raw/{post_id}.json"
    write_json(store.data_dir / raw.removeprefix(INTERNAL), item)
    files.extend([raw, relative])
    hashes[raw] = digest(store.data_dir / raw.removeprefix(INTERNAL))
    hashes[relative] = hashlib.sha256(body.encode()).hexdigest()
    hashes = {name: hashes[name] for name in files}
    # Receipt precedes note publication: recover a crash between note and state writes.
    write_json(receipt, hashes)
    with store.staging(relative) as temp:
        atomic_write(temp, body.encode())
        store.create(relative, temp)
    return hashes


def verified(output, files):
    """Every receipt entry must match: internal files locally, the rest through the store."""
    store = as_store(output)

    def check(name, expected):
        if name.startswith(INTERNAL):
            local = store.data_dir / name.removeprefix(INTERNAL)
            return local.is_file() and digest(local) == expected
        return store.verify(name, expected)
    return bool(files) and all(check(name, expected) for name, expected in files.items())


def process(client, user_id, items, output, state, state_path, keep=False, assets_dir=None):
    output = as_store(output)
    failed = 0
    for post_id, item in items.items():
        record = state.setdefault(post_id, {})
        try:
            if not record.get("files"):
                record["files"] = archive(item, output, assets_dir=assets_dir)
                write_json(state_path, state)
            if not verified(output, record["files"]):
                raise ValueError("Saved files changed or are missing; bookmark retained")
            if not keep:
                client.remove(user_id, post_id)
                record["removed"] = True
                write_json(state_path, state)
            record.pop("last_error", None)
            write_json(state_path, state)
            print(f"{post_id}: saved" + ("; bookmark kept" if keep else "; bookmark removed"), flush=True)
        except (OSError, ValueError, RuntimeError) as error:
            failed += 1
            record["last_error"] = str(error)
            write_json(state_path, state)
            print(f"{post_id}: {error}", flush=True)
    return failed
