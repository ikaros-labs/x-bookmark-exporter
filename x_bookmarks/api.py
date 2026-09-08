import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from .storage import write_json

BASE = "https://api.x.com/2"
SCOPES = "tweet.read users.read bookmark.read bookmark.write offline.access"


def content_fields(noun):
    return {
        f"{noun}.fields": f"id,text,author_id,created_at,conversation_id,entities,attachments,note_{noun},referenced_{noun}s,article",
        "expansions": "author_id,attachments.media_keys,attachments.poll_ids,article.cover_media,article.media_entities",
        "user.fields": "id,name,username",
        "media.fields": "media_key,type,url,variants,alt_text,preview_image_url",
        "poll.fields": "options,end_datetime,voting_status",
    }


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"X API returned HTTP {status}; check app permissions, credits, and API field compatibility.")


def token_request(client_id, form):
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    secret = os.environ.get("X_CLIENT_SECRET")
    if secret:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    form["client_id"] = client_id
    req = urllib.request.Request(BASE + "/oauth2/token", urllib.parse.urlencode(form).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise APIError(error.code) from None


def login(path: Path, client_id: str, redirect_uri: str):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    url = "https://x.com/i/oauth2/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Open this URL and authorize the app:\n" + url)
    webbrowser.open(url)
    callback = input("Paste the full redirected URL (a localhost connection error is expected): ").strip()
    received = urllib.parse.urlsplit(callback)
    expected = urllib.parse.urlsplit(redirect_uri)
    if (received.scheme, received.netloc, received.path) != (expected.scheme, expected.netloc, expected.path):
        raise ValueError("Callback URL does not match the configured redirect URI")
    query = urllib.parse.parse_qs(received.query)
    if not secrets.compare_digest(query.get("state", [""])[0], state) or not query.get("code"):
        raise ValueError("Invalid OAuth callback state or missing authorization code")
    token = token_request(client_id, {"grant_type": "authorization_code", "code": query["code"][0],
                                    "redirect_uri": redirect_uri, "code_verifier": verifier})
    token.update(client_id=client_id, expires_at=time.time() + token.get("expires_in", 7200))
    write_json(path, token)
    print(f"Credentials saved to {path}")


class Client:
    def __init__(self, token_path: Path):
        self.path = token_path
        self.token = json.loads(token_path.read_text())

    def user_id(self):
        if not self.token.get("user_id"):
            self.token["user_id"] = self.request("GET", "/users/me")["data"]["id"]
            write_json(self.path, self.token)
        return self.token["user_id"]

    def lookup(self, post_id, vocabulary="tweet"):
        result = self.request("GET", f"/tweets/{post_id}", content_fields(vocabulary))
        if not result.get("data"):
            raise RuntimeError("Post lookup incomplete; bookmark retained")
        item = {"post": result["data"], "includes": result.get("includes", {})}
        if result.get("errors"):
            item["export_warnings"] = ["API returned partial post data"]
        return item

    def refresh(self):
        if "refresh_token" not in self.token:
            raise RuntimeError("No refresh token; run login again")
        updated = token_request(self.token["client_id"], {
            "grant_type": "refresh_token", "refresh_token": self.token["refresh_token"]})
        self.token.update(updated)
        self.token["expires_at"] = time.time() + updated.get("expires_in", 7200)
        write_json(self.path, self.token)

    def request(self, method, path, params=None):
        if time.time() >= self.token.get("expires_at", 0) - 60:
            self.refresh()
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        refreshed = False
        failures = 0
        while True:
            request = urllib.request.Request(url, method=method, headers={
                "Authorization": "Bearer " + self.token["access_token"]})
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 401 and not refreshed:
                    self.refresh()
                    refreshed = True
                    continue
                if error.code == 429:
                    reset = float(error.headers.get("x-rate-limit-reset", time.time() + 900))
                    delay = max(1, reset - time.time() + 1)
                    print(f"Rate limited; retrying in {int(delay)} seconds.", flush=True)
                    while delay > 0:
                        interval = min(30, delay)
                        time.sleep(interval)
                        delay -= interval
                    continue
                if error.code >= 500 and failures < 3:
                    failures += 1
                    time.sleep(2 ** failures)
                    continue
                raise APIError(error.code) from None

    def inventory(self, user_id, vocabulary="tweet", limit=None, lightweight=False):
        params = {"max_results": min(100, limit) if limit else 100}
        if not lightweight:
            params.update(content_fields(vocabulary))
        items = {}
        seen = set()
        while True:
            page = self.request("GET", f"/users/{user_id}/bookmarks", params)
            if page.get("errors") and not page.get("data"):
                raise RuntimeError("Bookmark API returned partial errors; inventory not committed. No bookmarks removed.")
            includes = page.get("includes", {})
            for post in page.get("data", []):
                items[post["id"]] = {"post": post, "includes": includes}
                if page.get("errors"):
                    items[post["id"]]["export_warnings"] = ["Bookmark API returned partial data on this page"]
                if limit and len(items) >= limit:
                    return items
            token = page.get("meta", {}).get("next_token")
            if not token:
                return items
            if token in seen:
                raise RuntimeError("Repeated pagination token")
            seen.add(token)
            params["pagination_token"] = token

    def remove(self, user_id, post_id):
        result = self.request("DELETE", f"/users/{user_id}/bookmarks/{post_id}")
        if result.get("errors") or result.get("data", {}).get("bookmarked") is not False:
            raise RuntimeError("X did not confirm bookmark removal; will retry next run")
