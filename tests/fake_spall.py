"""A minimal in-memory stand-in for Spall's /v1/files endpoints (create, If-Match replace) and frontmatter PATCH."""
import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from x2vault.store import set_frontmatter


def rev(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def path_of(self):
        path = urllib.parse.urlsplit(self.path).path
        return urllib.parse.unquote(path.removeprefix("/v1/files/").removeprefix("/v1/notes/"))

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_one(self):
        server = self.server
        server.requests.append((self.command, self.path_of(), dict(self.headers)))
        if self.headers.get("Authorization") != "Bearer secret":
            return self.reply(401)
        if server.failures:
            status = server.failures.pop(0)
            return self.reply(status, headers={"Retry-After": "7"} if status == 429 else None)
        path = self.path_of()
        if self.command == "PATCH":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if path not in server.files:
                return self.reply(404)
            if not server.can_update:
                return self.reply(403, b'{"error":{"code":"forbidden"}}')
            current = server.files[path]
            if self.headers.get("If-Match", "").strip('"') != rev(current):
                return self.reply(412)
            values = {op["key"]: op["value"] for op in body["ops"] if op["op"] == "frontmatter.set"}
            server.files[path] = set_frontmatter(current.decode(), values).encode()
            return self.reply(200, b"{}", {"ETag": f'"{rev(server.files[path])}"'})
        if self.command == "PUT":
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if path in server.files:
                if "If-Match" not in self.headers or "*" in self.headers.get("If-None-Match", ""):
                    return self.reply(409, b'{"error":{"code":"already_exists"}}')
                if not server.can_update:
                    return self.reply(403, b'{"error":{"code":"forbidden"}}')
                if self.headers["If-Match"].strip('"') != rev(server.files[path]):
                    return self.reply(412)
                server.files[path] = body
                return self.reply(200, headers={"ETag": f'"{rev(body)}"'})
            server.files[path] = body
            return self.reply(201, headers={"ETag": f'"{rev(body)}"'})
        if path not in server.files:
            return self.reply(404)
        body = server.files[path]
        tags = [t.strip().removeprefix("W/").strip('"') for t in self.headers.get("If-None-Match", "").split(",")]
        if rev(body) in tags:
            return self.reply(304, headers={"ETag": f'"{rev(body)}"'})
        return self.reply(200, body, {"ETag": f'"{rev(body)}"'})

    do_GET = do_HEAD = do_PUT = do_PATCH = handle_one


class FakeSpall:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.files, self.server.failures, self.server.requests = {}, [], []
        self.server.can_update = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    files = property(lambda self: self.server.files)
    failures = property(lambda self: self.server.failures)
    requests = property(lambda self: self.server.requests)

    @property
    def can_update(self):
        return self.server.can_update

    @can_update.setter
    def can_update(self, value):
        self.server.can_update = value

    def close(self):
        self.server.shutdown()
        self.server.server_close()
