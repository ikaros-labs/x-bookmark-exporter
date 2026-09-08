import html
import re
import unicodedata
from datetime import datetime, timezone


def clean(value, budget):
    text = html.unescape(str(value))
    text = re.sub(r"https?://\S+", "", text)
    text = "".join(" " if unicodedata.category(c).startswith("C") or c in '<>:"/\\|?*[]#^' else c for c in text)
    text = " ".join(text.split()).strip(" .-")
    if len(text.encode()) > budget:
        shortened = text.encode()[:budget].decode("utf-8", errors="ignore")
        text = shortened.rsplit(" ", 1)[0] if " " in shortened else shortened
    return text.strip(" .-")


def filename(item):
    post = item["post"]
    post_id = post["id"]
    if not re.fullmatch(r"[0-9]{1,20}", post_id):
        raise ValueError("Invalid post ID")
    created = post.get("created_at")
    try:
        date = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except (AttributeError, ValueError):
        date = "undated"
    user = next((u for u in item["includes"].get("users", []) if u["id"] == post.get("author_id")), {})
    author = clean(user.get("username") or post.get("author_id") or "unknown", 40)
    full = post.get("note_tweet") or post.get("note_post") or post
    article = post.get("article") or {}
    title = clean(article.get("title") or full.get("text", ""), 70) or "Untitled post"
    return f"{date} - {author} - {title} - {post_id}.md"
