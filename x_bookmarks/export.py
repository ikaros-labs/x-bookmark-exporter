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

from .storage import atomic_write, write_json
from .naming import filename
from .articles import article_text, media_keys


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def media_source(media):
    if media["type"] == "photo" and media.get("url"):
        url = media["url"]
        suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        return url, suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"
    videos = [v for v in media.get("variants", []) if v.get("content_type") == "video/mp4" and v.get("url")]
    if videos:
        best = max(videos, key=lambda v: v.get("bit_rate", v.get("bitrate", 0)))
        return best["url"], ".mp4"
    raise ValueError("Media has no downloadable photo or MP4; bookmark retained")


def download(url, path):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("Media URL must use HTTPS")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".media-")
    try:
        with os.fdopen(fd, "wb") as stream:
            # Do not send the API bearer token to a media host.
            with urllib.request.urlopen(url, timeout=60) as response:
                if urllib.parse.urlsplit(response.url).scheme != "https":
                    raise ValueError("Insecure media redirect")
                content_type = response.headers.get_content_type()
                if not content_type.startswith(("image/", "video/", "application/octet-stream")):
                    raise ValueError("Media response is not an image or video")
                total = 0
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
                    total += len(chunk)
                expected = response.headers.get("Content-Length")
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
        raise ValueError("Post text missing; bookmark retained")
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
        body += "\n> Partial article export. Some content was unavailable; see export_warnings in the frontmatter.\n"
    for relative, alt in media_links:
        body += f"\n![[{relative}]]\n"
        if alt:
            body += f"\n{alt}\n"
    polls = {p["id"]: p for p in item["includes"].get("polls", [])}
    for poll_id in post.get("attachments", {}).get("poll_ids", []):
        if poll_id not in polls:
            raise ValueError("Missing poll data; bookmark retained")
        body += "\nPoll:\n\n" + "\n".join(f"- {o['label']}: {o.get('votes', 0)} votes" for o in polls[poll_id]["options"]) + "\n"
    refs = post.get("referenced_tweets", post.get("referenced_posts", []))
    for ref in refs:
        body += f"\n{ref['type']}: https://x.com/i/status/{ref['id']}\n"
    return body


def archive(item, output):
    post = item["post"]
    post_id = post["id"]
    if not re.fullmatch(r"[0-9]+", post_id):
        raise ValueError("Invalid post ID")
    receipt = output / f".x-bookmarks/receipts/{post_id}.json"
    if receipt.exists():
        files = json.loads(receipt.read_text())
        if verified(output, files):
            return files
    article = post.get("article")
    warnings = list(item.get("export_warnings", []))
    if article:
        try:
            article_text(article)
        except ValueError:
            warnings.append("Article body unavailable; saved metadata and source link")
    media = {m["media_key"]: m for m in item["includes"].get("media", [])}
    links, files = [], []
    try:
        keys = media_keys(post)
    except ValueError:
        if not article:
            raise
        keys = []
        warnings.append("Article media references unavailable")
    for index, key in enumerate(keys, 1):
        try:
            if key not in media:
                raise ValueError("Attached media missing from API response")
            source, extension = media_source(media[key])
            relative = f"assets/{post_id}/{index}{extension}"
            download(source, output / relative)
            files.append(relative)
            links.append((relative, media[key].get("alt_text", "")))
        except (OSError, ValueError) as error:
            if not article:
                raise
            warnings.append(f"Media {key} unavailable: {error}")
    if article:
        item = dict(item, export_warnings=warnings)
    relative = filename(item)
    if (output / relative).exists():
        raise ValueError(f"Untracked note already exists: {relative}; refusing to overwrite")
    # Render before publishing the note, so incomplete posts never become completed exports.
    body = render(item, links)
    raw = f".x-bookmarks/raw/{post_id}.json"
    write_json(output / raw, item)
    files.extend([raw, relative])
    hashes = {name: digest(output / name) for name in files if name != relative}
    hashes[relative] = hashlib.sha256(body.encode()).hexdigest()
    # Receipt precedes note publication: recover a crash between note and state writes.
    write_json(receipt, hashes)
    atomic_write(output / relative, body.encode())
    return hashes


def verified(output, files):
    return bool(files) and all((output / name).is_file() and digest(output / name) == expected
                               for name, expected in files.items())


def process(client, user_id, items, output, state, state_path, keep=False):
    failed = 0
    for post_id, item in items.items():
        record = state.setdefault(post_id, {})
        try:
            if not record.get("files"):
                record["files"] = archive(item, output)
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
