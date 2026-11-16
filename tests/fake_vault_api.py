"""A minimal in-memory stand-in for obsidian-api's /v1/files endpoints."""
import hashlib
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def rev(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def path_of(self):
        return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path.removeprefix("/v1/files/"))

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
            return self.reply(server.failures.pop(0))
        path = self.path_of()
        if self.command == "PUT":
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if path in server.files:
                return self.reply(409, b'{"error":{"code":"already_exists"}}')
            server.files[path] = body
            return self.reply(201, headers={"ETag": f'"{rev(body)}"'})
        if path not in server.files:
            return self.reply(404)
        body = server.files[path]
        tags = [t.strip().removeprefix("W/").strip('"') for t in self.headers.get("If-None-Match", "").split(",")]
        if rev(body) in tags:
            return self.reply(304, headers={"ETag": f'"{rev(body)}"'})
        return self.reply(200, body, {"ETag": f'"{rev(body)}"'})

    do_GET = do_HEAD = do_PUT = handle_one


class FakeVaultAPI:
    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.files, self.server.failures, self.server.requests = {}, [], []
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    files = property(lambda self: self.server.files)
    failures = property(lambda self: self.server.failures)
    requests = property(lambda self: self.server.requests)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
