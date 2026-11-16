"""Where exported notes and media are saved: a local folder or Spall.

Paths given to a store are relative to the notes folder, exactly as they appear in
receipts (e.g. ``note.md`` or ``../assets/123/1.jpg``). Receipt entries under
``.x2vault/`` are exporter-internal and always live in the local data dir.
"""
import contextlib
import hashlib
import http.client
import json
import os
import posixpath
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

INTERNAL = ".x2vault/"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fsync_directory(path):
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class StoreError(RuntimeError):
    """A hard failure for one post; the bookmark is kept and the error recorded."""


class UpdateForbidden(StoreError):
    """The Spall account may create but not update notes in this folder."""


def set_frontmatter(text, values):
    """Set top-level frontmatter keys (JSON-encoded values), leaving every other line as it is."""
    rendered = {key: f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()}
    lines = text.split("\n")
    end = None
    if lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), None)
    if end is None:
        return "---\n" + "\n".join(rendered.values()) + "\n---\n" + text
    for i in range(1, end):
        line = lines[i]
        if line[:1] in {" ", "\t", "-", "#"} or ":" not in line:
            continue
        key = line.split(":", 1)[0].strip().strip("'\"")
        if key in rendered:
            lines[i] = rendered.pop(key)
    lines[end:end] = rendered.values()
    return "\n".join(lines)


class FileStore:
    """Writes into a vault folder on the local filesystem (the original mode)."""

    def __init__(self, output, data_dir=None, max_media_bytes=None):
        self.root = Path(output)
        self.data_dir = Path(data_dir) if data_dir is not None else self.root / ".x2vault"
        self.max_media_bytes = max_media_bytes

    def local(self, relative):
        return self.root / relative

    def exists(self, relative):
        return self.local(relative).exists()

    @contextlib.contextmanager
    def staging(self, relative):
        """Temporary file on the destination's filesystem, so create() is an atomic rename."""
        destination = self.local(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=destination.parent, prefix=".export-")
        os.close(fd)
        try:
            yield Path(name)
        finally:
            Path(name).unlink(missing_ok=True)

    def create(self, relative, source):
        """Create-only: an existing file is accepted only if its content is identical."""
        destination = self.local(relative)
        if isinstance(source, (bytes, bytearray)):
            expected = hashlib.sha256(source).hexdigest()
        else:
            expected = digest(source)
        if destination.exists():
            if destination.is_file() and digest(destination) == expected:
                return expected
            raise StoreError(f"File already exists with different content: {relative}; refusing to overwrite")
        if isinstance(source, (bytes, bytearray)):
            with self.staging(relative) as temp:
                temp.write_bytes(source)
                return self.create(relative, temp)
        with open(source, "rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(source, destination)
        fsync_directory(destination.parent)
        return expected

    def verify(self, relative, expected):
        path = self.local(relative)
        return path.is_file() and digest(path) == expected

    def update_frontmatter(self, relative, values):
        """Atomic rewrite of only the given frontmatter keys; the user's other edits are kept."""
        path = self.local(relative)
        if not path.is_file():
            raise StoreError(f"Note missing: {relative}")
        updated = set_frontmatter(path.read_text(), values)
        with self.staging(relative) as temp:
            temp.write_text(updated)
            os.replace(temp, path)
        fsync_directory(path.parent)


class SpallStore:
    """Writes through Spall: create-only for files; frontmatter updates need `update`."""

    retries = 3
    rate_limit_waits = 10  # Spall limits requests per account (default 120/min)

    def __init__(self, base_url, token, folder, data_dir, max_media_bytes=None, sleep=time.sleep, timeout=120,
                 option="--bookmarks-folder"):
        if urllib.parse.urlsplit(base_url).scheme not in {"http", "https"}:
            raise ValueError("--spall must be an http(s) URL")
        if not token:
            raise ValueError("Set X2VAULT_SPALL_TOKEN for --spall")
        self.api = base_url.rstrip("/") + "/v1/"
        self.token = token
        self.root = PurePosixPath("/") / vault_path(folder, option)
        self.data_dir = Path(data_dir)
        self.max_media_bytes = max_media_bytes
        self.sleep = sleep
        self.timeout = timeout

    def vault_path(self, relative):
        parts = []
        for part in (self.root / relative).parts[1:]:
            if part == "..":
                if not parts:
                    raise ValueError(f"Path outside the vault: {relative}")
                parts.pop()
            elif part != ".":
                parts.append(part)
        if not parts or any(part.startswith(".") for part in parts):
            raise ValueError(f"Path outside the vault or hidden: {relative}")
        return "/".join(parts)

    def url(self, relative, kind="files"):
        return self.api + kind + "/" + "/".join(urllib.parse.quote(part, safe="") for part in self.vault_path(relative).split("/"))

    def request(self, method, relative, headers=None, body=None, ok=(), kind="files"):
        """Retries network errors and 5xx; returns (status, headers) for 2xx/3xx or listed codes."""
        url = self.url(relative, kind)
        headers = dict(headers or {}, Authorization="Bearer " + self.token)
        attempt = limited = 0
        while True:
            stream = None
            try:
                data = None
                if isinstance(body, (bytes, bytearray)):
                    data = bytes(body)
                    headers["Content-Length"] = str(len(data))
                elif body is not None:
                    stream = open(body, "rb")
                    data = stream
                    headers["Content-Length"] = str(os.fstat(stream.fileno()).st_size)
                request = urllib.request.Request(url, data=data, method=method, headers=headers)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.status, response.headers
            except urllib.error.HTTPError as error:
                error.close()
                if error.code < 400 or error.code in ok:
                    return error.code, error.headers
                if error.code == 429 and limited < self.rate_limit_waits:
                    limited += 1
                    try:
                        wait = float(error.headers.get("Retry-After", 1))
                    except ValueError:
                        wait = 1
                    self.sleep(min(max(wait, 1), 120))
                    continue
                if error.code < 500 or attempt >= self.retries:
                    raise StoreError(f"Spall returned HTTP {error.code} for {method} "
                                     f"{self.vault_path(relative)}") from None
            except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
                if attempt >= self.retries:
                    raise StoreError(f"Spall unreachable for {method} {self.vault_path(relative)}: "
                                     f"{getattr(error, 'reason', error)}") from None
            finally:
                if stream:
                    stream.close()
            attempt += 1
            self.sleep(2 ** attempt)

    def exists(self, relative):
        status, _ = self.request("HEAD", relative, ok=(404,))
        return status != 404

    @contextlib.contextmanager
    def staging(self, relative):
        """Temporary local file in the data dir; media is streamed from it on upload."""
        directory = self.data_dir / "tmp"
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=directory, prefix=".upload-")
        os.close(fd)
        try:
            yield Path(name)
        finally:
            Path(name).unlink(missing_ok=True)

    def create(self, relative, source):
        """PUT with If-None-Match: * so an existing file is never overwritten."""
        if isinstance(source, (bytes, bytearray)):
            expected = hashlib.sha256(source).hexdigest()
        else:
            expected = digest(source)
        status, _ = self.request("PUT", relative, {"If-None-Match": "*", "Content-Type": "application/octet-stream"},
                                 source, ok=(409, 412))
        if status in {409, 412}:
            # Also covers a retried PUT whose first attempt succeeded.
            if not self.verify(relative, expected):
                raise StoreError(f"File already exists in the vault with different content: "
                                 f"{self.vault_path(relative)}; refusing to overwrite")
        return expected

    def verify(self, relative, expected):
        """Spall's ETag is "sha256:<first 16 hex chars>"; 304 means it matches."""
        tag = "sha256:" + expected[:16]
        status, headers = self.request("GET", relative, {"If-None-Match": f'"{tag}"'}, ok=(404,))
        if status == 304:
            return True
        etag = (headers.get("ETag") or "").removeprefix("W/").strip('"')
        return status == 200 and etag == tag


    def update_frontmatter(self, relative, values):
        """PATCH /v1/notes/{path} with frontmatter.set ops, conditional on the current rev."""
        body = json.dumps({"ops": [{"op": "frontmatter.set", "key": key, "value": value}
                                   for key, value in values.items()]}).encode()
        for _ in range(3):
            status, headers = self.request("HEAD", relative, ok=(404,))
            if status == 404:
                raise StoreError(f"Note missing: {self.vault_path(relative)}")
            status, _ = self.request("PATCH", relative, {"Content-Type": "application/json",
                                                          "If-Match": headers.get("ETag", "")},
                                     body, ok=(403, 412), kind="notes")
            if status == 403:
                raise UpdateForbidden(f"The Spall account lacks update on {self.vault_path(relative)}")
            if status != 412:
                return
        raise StoreError(f"Note kept changing during the stats update: {self.vault_path(relative)}")


def vault_path(folder, option):
    """Validates a vault-relative folder for Spall mode."""
    if not folder or not folder.strip() or folder.startswith("/"):
        raise ValueError(f"{option} must be a nonempty path relative to the vault")
    parts = PurePosixPath(posixpath.normpath(folder)).parts
    if any(part == ".." or part.startswith(".") for part in parts):
        raise ValueError(f"{option} must stay inside the vault and not use hidden folders")
    return "/".join(parts)
