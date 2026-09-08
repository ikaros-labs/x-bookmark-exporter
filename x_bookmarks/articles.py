"""Preserve API article text without inventing missing rich-text layout."""


def article_text(article):
    text = article.get("plain_text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Article body unavailable (preview is not a full export); bookmark retained")
    title = article.get("title")
    if isinstance(title, str) and title.strip():
        text = "# " + " ".join(title.split()) + "\n\n" + text
    # Some API responses keep code separate from plain_text. Preserve it verbatim.
    for entity in article.get("entities", {}).get("code", []):
        code = entity.get("code")
        if isinstance(code, str) and code.strip() and code not in text:
            fence = "```"
            while fence in code:
                fence += "`"
            text += f"\n\n{fence}\n{code}\n{fence}\n"
    for entity in article.get("entities", {}).get("urls", []):
        url = entity.get("expanded_url") or entity.get("url") or entity.get("text")
        if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in text:
            text += f"\n\n<{url}>\n"
    return text


def media_keys(post):
    keys = list(post.get("attachments", {}).get("media_keys", []))
    article = post.get("article") or {}
    if article.get("cover_media"):
        keys.append(article["cover_media"])
    keys.extend(article.get("media_entities", []))
    if not all(isinstance(key, str) for key in keys):
        raise ValueError("Unknown article media reference format; bookmark retained")
    return list(dict.fromkeys(keys))
