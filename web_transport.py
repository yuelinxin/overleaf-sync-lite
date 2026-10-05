"""Free Overleaf web transport, using a dedicated, user-authenticated browser.

No Git bridge, token extraction, browser-profile harvesting, or paid feature.
The public web UI's HTTP endpoints are not a stable third-party API.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time
from urllib.parse import urlsplit, quote
import zipfile

from olsync import Entry, SyncError, MAX_FILE, MAX_TOTAL, excluded, hashes, safe_name, normalize_text


def profile_path(site, override=None):
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "overleaf-sync" / (urlsplit(site).netloc.replace(":", "_") + "-browser")


def launch(site, profile=None, visible=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SyncError("Install dependencies: python3 -m pip install -e .") from exc
    path = profile_path(site, profile)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.chmod(0o700)
    runtime = sync_playwright().start()
    try:
        context = runtime.chromium.launch_persistent_context(
            str(path), channel=os.environ.get("OLSYNC_BROWSER_CHANNEL", "chrome"),
            headless=not visible, accept_downloads=True)
    except Exception as exc:
        runtime.stop()
        raise SyncError("Cannot start the dedicated browser. Close another ols login/sync window. "
                        "Install Chrome, or run playwright install chromium and set "
                        "OLSYNC_BROWSER_CHANNEL=chromium.") from exc
    return runtime, context


def login(site, profile=None):
    url = urlsplit(site)
    if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
        raise SyncError("Login site must be an HTTPS origin without credentials.")
    site = f"https://{url.netloc}"
    runtime, context = launch(site, profile, visible=True)
    try:
        page = context.new_page()
        page.goto(site + "/project", wait_until="domcontentloaded")
        print("Sign in to Overleaf in this separate browser window. Complete any verification yourself.")
        input("When the project list is visible, press Enter here: ")
        response = context.request.get(site + "/user/projects", max_redirects=0)
        if response.status != 200 or "projects" not in response.json():
            raise SyncError("Login was not confirmed; run login again.")
    finally:
        context.close()
        runtime.stop()


class MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name"):
            self.values[attrs["name"]] = attrs.get("content", "")


def project_from_frame(raw, project_id):
    """Parse a joinProject acknowledgement from the page's own websocket.

    Socket.IO v1 uses 6:::ID+[...]; newer framing prefixes JSON with numeric codes.
    Do not log frames (they can contain collaborator information).
    """
    if not isinstance(raw, str):
        return None
    candidates = [i for c in ("[", "{") if (i := raw.find(c)) >= 0]
    if not candidates:
        return None
    try:
        value = json.loads(raw[min(candidates):])
    except ValueError:
        return None

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("_id") == project_id and isinstance(obj.get("rootFolder"), list):
                return obj
            for v in obj.values():
                result = walk(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for v in obj:
                result = walk(v)
                if result:
                    return result
        return None
    return walk(value)


def entity_map(project):
    folders, files = {}, {}

    def walk(folder, prefix):
        folder_id = folder.get("_id")
        if not re.fullmatch(r"[a-fA-F0-9]{24}", str(folder_id)):
            raise SyncError("Unsupported Overleaf folder metadata.")
        folders[prefix] = folder_id
        for collection, kind in (("docs", "doc"), ("fileRefs", "file")):
            for item in folder.get(collection, []):
                name = safe_name(prefix + item["name"])
                if name in files:
                    raise SyncError("Duplicate remote path: " + name)
                if not re.fullmatch(r"[a-fA-F0-9]{24}", str(item.get("_id"))):
                    raise SyncError("Unsupported Overleaf entity metadata.")
                files[name] = {"id": item["_id"], "type": kind, "folder": folder_id}
        for child in folder.get("folders", []):
            name = safe_name(prefix + child["name"])
            walk(child, name + "/")
    roots = project.get("rootFolder", [])
    if len(roots) != 1:
        raise SyncError("Unsupported Overleaf root-folder metadata.")
    walk(roots[0], "")
    return folders, files


def unpack_zip(data, patterns):
    result, total = {}, 0
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SyncError("Overleaf did not return a ZIP archive; login may have expired.") from exc
    with archive:
        for info in archive.infolist():
            name = info.filename.rstrip("/")
            safe_name(name)
            if excluded(name, patterns):
                continue
            if info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16):
                raise SyncError("Remote archive contains a symlink: " + name)
            if name in result:
                raise SyncError("Duplicate ZIP entry: " + name)
            total += info.file_size
            if info.file_size > MAX_FILE or total > MAX_TOTAL:
                raise SyncError("Remote archive exceeds size limit.")
            # ZIP has no timezone and may contain export time, not edit time.
            # Record it as an approximate UTC export timestamp; never choose a winner by it.
            ns = int(datetime(*info.date_time, tzinfo=timezone.utc).timestamp() * 1e9)
            result[name] = Entry(normalize_text(name, archive.read(info)), ns)
    return result


class WebTransport:
    def __init__(self, project_url, patterns, profile=None):
        url = urlsplit(project_url)
        self.site = f"https://{url.netloc}"
        self.project_id = url.path.rsplit("/", 1)[-1]
        self.url = project_url
        self.patterns, self.profile = patterns, profile
        self.project = None
        self.last_zip_time = 0

    def __enter__(self):
        self.runtime, self.context = launch(self.site, self.profile,
                                           visible=os.environ.get("OLSYNC_VISIBLE") == "1")
        try:
            self.page = self.context.new_page()
            self.page.on("websocket", lambda ws: ws.on("framereceived", self.frame))
            self.page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
            self.refresh_metadata()
            return self
        except Exception as exc:
            self.__exit__()
            if isinstance(exc, SyncError):
                raise
            raise SyncError("Cannot open Overleaf. Run ols login, then retry. "
                            "If a browser verification appears, complete it yourself with OLSYNC_VISIBLE=1.") from exc

    def __exit__(self, *args):
        self.context.close()
        self.runtime.stop()

    def frame(self, raw):
        project = project_from_frame(raw, self.project_id)
        if project:
            self.project = project

    def refresh_metadata(self):
        deadline = time.monotonic() + 25
        while not self.project and time.monotonic() < deadline:
            if "/login" in self.page.url:
                raise SyncError("Overleaf login required: run ols login.")
            self.page.wait_for_timeout(100)
        if not self.project:
            raise SyncError("No project metadata received. Login/session expired or web API changed; nothing written.")
        parser = MetaParser()
        parser.feed(self.page.content())
        csrf = parser.values.get("ol-csrfToken") or parser.values.get("csrf-token")
        if not csrf:
            raise SyncError("No CSRF token in Overleaf page; nothing written.")
        self.csrf = csrf
        self.folders, self.entities = entity_map(self.project)

    def request(self, method, path, **kwargs):
        headers = {"X-CSRF-Token": self.csrf, "Referer": self.url, "Origin": self.site}
        try:
            response = self.context.request.fetch(self.site + path, method=method,
                                                 headers=headers, max_redirects=0, timeout=60000, **kwargs)
        except Exception as exc:
            raise SyncError("Overleaf network request interrupted; recovery state retained if writes had begun.") from exc
        if not response.ok:
            raise SyncError(f"Overleaf HTTP {response.status}; stop and retry later/login again. "
                            "No automatic retries of writes.")
        return response

    def snapshot(self):
        # Official server default permits ten ZIP exports per minute. Stay below it.
        remaining = 6.5 - (time.monotonic() - self.last_zip_time)
        if remaining > 0:
            self.page.wait_for_timeout(remaining * 1000)
        response = self.request("GET", f"/project/{self.project_id}/download/zip")
        self.last_zip_time = time.monotonic()
        return unpack_zip(response.body(), self.patterns)

    def validate(self, desired):
        for name in desired:
            if name + "/" in self.folders:
                raise SyncError("Remote file/directory collision: " + name)
            for parent in PurePosixPath(name).parents:
                if str(parent) in self.entities:
                    raise SyncError("Remote file/directory collision: " + name)
            for part in PurePosixPath(name).parts:
                if len(part) >= 150:
                    raise SyncError("Overleaf filename component too long: " + name)

    def folder(self, prefix):
        if prefix in self.folders:
            return self.folders[prefix]
        parent, _, name = prefix.rstrip("/").rpartition("/")
        parent_id = self.folder(parent + "/" if parent else "")
        response = self.request("POST", f"/project/{self.project_id}/folder",
                                data={"name": name, "parent_folder_id": parent_id})
        value = response.json()
        folder_id = value.get("_id")
        if not re.fullmatch(r"[a-fA-F0-9]{24}", str(folder_id)):
            raise SyncError("Unexpected folder response; journal retained.")
        self.folders[prefix] = folder_id
        return folder_id

    def put(self, name, entry):
        parent, _, basename = name.rpartition("/")
        folder_id = self.folder(parent + "/" if parent else "")
        response = self.request("POST", f"/project/{self.project_id}/upload?folder_id={quote(folder_id)}",
                                multipart={"name": basename,
                                           "qqfile": {"name": basename,
                                                      "mimeType": "application/octet-stream",
                                                      "buffer": entry.data}})
        value = response.json()
        if value.get("success") is not True:
            raise SyncError("Overleaf rejected upload for " + name)
        entity_id, kind = value.get("entity_id"), value.get("entity_type")
        # IDs are only needed later for deletion; refresh on a subsequent invocation.
        if entity_id and kind in {"doc", "file"}:
            self.entities[name] = {"id": entity_id, "type": kind, "folder": folder_id}

    def delete(self, name):
        entity = self.entities.get(name)
        if not entity:
            raise SyncError("Missing remote entity metadata for " + name + "; journal retained.")
        self.request("DELETE", f"/project/{self.project_id}/{entity['type']}/{entity['id']}")
        del self.entities[name]

    def apply(self, expected, desired):
        self.validate(desired)
        working = dict(expected)
        # Full preflight and per-file checks catch visible concurrent edits.
        # The web API has no compare-and-swap: edits within a request's race window
        # cannot be excluded. Users must pause editing for the duration of a sync.
        for name in sorted(set(expected) | set(desired)):
            old, new = expected.get(name), desired.get(name)
            if (old.sha if old else None) == (new.sha if new else None):
                continue
            if hashes(self.snapshot()) != hashes(working):
                raise SyncError("Overleaf changed during sync; stopped. Already-written files may remain; run recover.")
            if new is None:
                self.delete(name)
                working.pop(name, None)
            else:
                self.put(name, new)
                working[name] = new
        if hashes(self.snapshot()) != hashes(desired):
            raise SyncError("Overleaf content verification failed; recovery journal retained. "
                            "A collaborator edit or server text conversion may have occurred.")
