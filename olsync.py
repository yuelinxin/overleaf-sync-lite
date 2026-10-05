#!/usr/bin/env python3
"""Small, conservative two-way Overleaf synchronizer (Python 3.10+, free Overleaf web interface)."""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit

sys.modules.setdefault("olsync", sys.modules[__name__])

VERSION = "0.1.1"
STATE = ".overleaf-sync"
MAX_FILE = 50 * 1024 * 1024
MAX_TOTAL = 250 * 1024 * 1024
TEXT_SUFFIXES = {'.tex', '.bib', '.sty', '.cls', '.bst', '.txt', '.md', '.csv', '.tsv', '.svg'}
DEFAULT_EXCLUDES = [
    ".*", "backups", "output", "build", "dist", "tmp", "node_modules",
    "__pycache__", ".venv", "venv", "*.aux", "*.bbl", "*.blg", "*.log",
    "*.out", "*.toc", "*.lof", "*.lot", "*.fls", "*.fdb_latexmk",
    "*.synctex.gz", "*.nav", "*.snm", "*.vrb", "*.xdv", "*.dvi",
    "*.run.xml", "*.bcf", "*.bak", "*.swp", "*~", "main.pdf", "output.pdf",
    "proposal_research_notes_*.md",
]


class SyncError(Exception):
    pass


class ConflictError(SyncError):
    def __init__(self, report):
        self.report = report
        super().__init__("Conflicts found; no project files were changed.")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def normalize_text(name, data):
    if PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES:
        try:
            data.decode('utf-8')
        except UnicodeError as exc:
            raise SyncError(f"Convert this text file to UTF-8 before syncing: {name}") from exc
        return data.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
    return data


@dataclass(frozen=True)
class Entry:
    data: bytes
    mtime_ns: int
    mode: int = 0o644

    @property
    def sha(self):
        return digest(self.data)


def hashes(entries):
    return {p: e.sha for p, e in entries.items()}


def now():
    return datetime.now(timezone.utc).isoformat()


def stamp(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat() if ns else None


def atomic_write(path, data, mode=0o600, mtime_ns=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".olsync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if mtime_ns is not None:
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode())


def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise SyncError(f"Cannot read state file {path.name}: {exc}") from exc


def safe_name(name):
    p = PurePosixPath(name)
    if (not name or p.is_absolute() or p.as_posix() != name
            or any(x in {"", ".", "..", ".git", STATE} for x in p.parts)
            or "\\" in name or any(ord(c) < 32 for c in name)):
        raise SyncError(f"Unsafe project path: {name!r}")
    return name


def path_at(root, name):
    safe_name(name)
    path = root
    for part in PurePosixPath(name).parts:
        path = path / part
        if path.is_symlink():
            raise SyncError(f"Symbolic links are not synchronized: {name}")
    return path


def excluded(name, patterns):
    if any(x in {".git", STATE} for x in PurePosixPath(name).parts):
        return True
    return any(fnmatch.fnmatchcase(name, pat)
               or any(fnmatch.fnmatchcase(part, pat) for part in PurePosixPath(name).parts)
               for pat in patterns)


def local_snapshot(root, patterns):
    result = {}
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = Path(directory)
        kept = []
        for name in dirs:
            rel = (here / name).relative_to(root).as_posix()
            if excluded(rel, patterns):
                continue
            if (here / name).is_symlink():
                raise SyncError(f"Symbolic links are not synchronized: {rel}")
            kept.append(name)
        dirs[:] = kept
        for name in files:
            rel = (here / name).relative_to(root).as_posix()
            if excluded(rel, patterns):
                continue
            path = path_at(root, rel)
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise SyncError(f"Not a regular file: {rel}")
            if before.st_size > MAX_FILE:
                raise SyncError(f"File exceeds 50 MiB limit: {rel}")
            data = path.read_bytes()
            after = path.stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) != (
                    after.st_mtime_ns, after.st_size, after.st_ino):
                raise SyncError(f"File changed while reading: {rel}; retry.")
            total += len(data)
            if total > MAX_TOTAL:
                raise SyncError("Project exceeds 250 MiB limit; add exclusions.")
            result[rel] = Entry(normalize_text(rel, data), after.st_mtime_ns, stat.S_IMODE(after.st_mode))
    return result


def normalize_remote(value):
    if re.fullmatch(r"[0-9a-fA-F]{24}", value):
        return "https://www.overleaf.com/project/" + value
    url = urlsplit(value)
    if url.scheme == "https":
        if url.username or url.password or url.query or url.fragment:
            raise SyncError("Use a project URL without credentials, query, or fragment.")
        if not url.hostname or not re.fullmatch(r"/project/[0-9a-fA-F]{24}/?", url.path):
            raise SyncError("Expected https://HOST/project/PROJECT_ID.")
        return value.rstrip("/")
    path = Path(value).expanduser()
    if path.is_absolute() and path.is_dir():
        return str(path.resolve())
    raise SyncError("Use an Overleaf project URL/id or absolute local directory for offline testing.")


class DirectoryTransport:
    """Offline mirror for integration tests; does not contact Overleaf."""
    def __init__(self, directory, patterns):
        self.root, self.patterns = Path(directory), patterns

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def snapshot(self):
        return local_snapshot(self.root, self.patterns)

    def validate(self, desired):
        for name in desired:
            path = path_at(self.root, name)
            if path.exists() and path.is_dir():
                raise SyncError("Remote file/directory collision: " + name)
            for parent in path.parents:
                if parent == self.root:
                    break
                if parent.exists() and not parent.is_dir():
                    raise SyncError("Remote file/directory collision: " + name)

    def apply(self, expected, desired):
        if hashes(self.snapshot()) != hashes(expected):
            raise SyncError("Remote changed before apply; journal retained.")
        self.validate(desired)
        for name in sorted(set(expected) | set(desired)):
            old, new = expected.get(name), desired.get(name)
            if (old.sha if old else None) == (new.sha if new else None):
                continue
            path = path_at(self.root, name)
            if new is None:
                path.unlink()
            else:
                atomic_write(path, new.data, new.mode, new.mtime_ns)
        if hashes(self.snapshot()) != hashes(desired):
            raise SyncError("Remote verification failed; journal retained.")



def conflict_detail(name, local, remote):
    a, b = local.get(name), remote.get(name)
    info = {"path": name, "local_sha256": a.sha if a else None,
            "remote_sha256": b.sha if b else None,
            "local_modified_utc": stamp(a.mtime_ns) if a else None,
            "remote_export_utc": stamp(b.mtime_ns) if b else None}
    try:
        atext = a.data.decode() if a else ""
        btext = b.data.decode() if b else ""
        if "\0" in atext + btext:
            raise UnicodeError()
        diff = list(difflib.unified_diff(atext.splitlines(), btext.splitlines(),
                    fromfile="local/" + name if a else "local/(deleted)",
                    tofile="overleaf/" + name if b else "overleaf/(deleted)", n=2, lineterm=""))
        info["diff"] = [line[:240] + ("…" if len(line) > 240 else "") for line in diff[:28]]
        info["truncated"] = len(diff) > 28 or any(len(line) > 240 for line in diff[:28])
    except UnicodeError:
        info["binary"] = True
        info["local_bytes"] = len(a.data) if a else 0
        info["remote_bytes"] = len(b.data) if b else 0
    return info


def plan(baseline, local, remote, take_local=(), take_remote=()):
    lh, rh = hashes(local), hashes(remote)
    desired, actions, conflicts = {}, [], []
    choices_l, choices_r = set(take_local), set(take_remote)
    if choices_l & choices_r:
        raise SyncError("Cannot choose both sides of the same file.")
    names = set(baseline) | set(local) | set(remote)
    if (choices_l | choices_r) - names:
        raise SyncError("Resolution path not found: " + ", ".join(sorted((choices_l | choices_r) - names)))
    for name in sorted(names):
        base, left, right = baseline.get(name), lh.get(name), rh.get(name)
        if left == right:
            chosen = local.get(name)
        elif name in choices_l:
            chosen = local.get(name)
        elif name in choices_r:
            chosen = remote.get(name)
        elif left == base:
            chosen = remote.get(name)
        elif right == base:
            chosen = local.get(name)
        else:
            conflicts.append(conflict_detail(name, local, remote))
            continue
        if chosen is not None:
            desired[name] = chosen
        target = chosen.sha if chosen else None
        for side, previous in (("local", left), ("overleaf", right)):
            if previous != target:
                actions.append({"path": name, "side": side,
                                "action": "delete" if target is None else ("add" if previous is None else "update"),
                                "modified_utc": stamp(chosen.mtime_ns) if chosen else None})
    # File-vs-directory cases are intentionally stopped, even if a rename could be inferred.
    for name in sorted(desired):
        for parent in PurePosixPath(name).parents:
            if str(parent) in desired:
                conflicts.append({"path": name, "reason": f"File/directory collision with {parent}"})
    return desired, actions, conflicts


class Synchronizer:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.state = self.root / STATE
        if self.state.is_symlink():
            raise SyncError("State directory must not be a symlink.")
        self.config = read_json(self.state / "config.json")
        if not self.config or self.config.get("schema") != 1:
            raise SyncError("Project not initialized; run ols init first.")
        self.patterns = self.config["exclude"]

    @contextlib.contextmanager
    def lock(self):
        try:
            import fcntl
        except ImportError as exc:
            raise SyncError("Use Linux/macOS or Windows WSL (file locking requires POSIX).") from exc
        with open(self.state / "lock", "a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SyncError("Another sync is running for this project.") from exc
            yield

    def transport(self):
        remote = self.config["remote"]
        if remote.startswith("https://"):
            from web_transport import WebTransport
            return WebTransport(remote, self.patterns, self.config.get("profile"))
        return DirectoryTransport(remote, self.patterns)

    def baseline(self):
        state = read_json(self.state / "baseline.json", {"files": {}})
        return {p: e["sha256"] for p, e in state["files"].items()}

    def preflight(self, desired, local):
        for name in desired:
            path = path_at(self.root, name)
            if path.exists() and path.is_dir():
                raise SyncError(f"File/directory collision: {name}; resolve it before syncing.")
            for parent in path.parents:
                if parent == self.root:
                    break
                if parent.exists() and not parent.is_dir():
                    raise SyncError(f"File/directory collision: {name}; resolve it before syncing.")
        current = local_snapshot(self.root, self.patterns)
        if hashes(current) != hashes(local):
            raise SyncError("Local files changed during planning; nothing applied. Retry.")

    def journal(self, local, remote, desired):
        objects = self.state / "objects"
        records = {}
        for entry in list(local.values()) + list(remote.values()) + list(desired.values()):
            obj = objects / entry.sha
            if not obj.exists():
                atomic_write(obj, entry.data)
        for name, entry in desired.items():
            records[name] = {"sha256": entry.sha, "mtime_ns": entry.mtime_ns, "mode": entry.mode}
        journal = {"schema": 1, "started_at": now(), "before": hashes(local), "desired": records,
                   "remote_before": hashes(remote), "remote_target": hashes(desired)}
        save_json(self.state / "pending.json", journal)
        return journal

    def journal_entries(self, journal):
        result = {}
        for name, record in journal["desired"].items():
            safe_name(name)
            sha = record["sha256"]
            if not re.fullmatch("[0-9a-f]{64}", sha):
                raise SyncError("Invalid journal object hash.")
            data = (self.state / "objects" / sha).read_bytes()
            if digest(data) != sha:
                raise SyncError("Damaged recovery data; refusing to write project files.")
            result[name] = Entry(data, record["mtime_ns"], record["mode"])
        return result

    def apply_local(self, journal, desired):
        before, target = journal["before"], hashes(desired)
        current = local_snapshot(self.root, self.patterns)
        current_hash = hashes(current)
        # All paths are checked before any write. Post-push local edits are never overwritten.
        changed = [name for name in sorted(set(before) | set(current_hash) | set(target))
                   if current_hash.get(name) not in {before.get(name), target.get(name)}]
        if changed:
            raise SyncError("Local edits appeared during an interrupted sync: " + ", ".join(changed)
                            + ". Preserve them elsewhere, restore those paths, then run recover.")
        self.preflight(desired, current)
        for name in sorted(set(before) | set(target)):
            expected, wanted = current_hash.get(name), target.get(name)
            if expected == wanted:
                continue
            path = path_at(self.root, name)
            actual = digest(normalize_text(name, path.read_bytes())) if path.is_file() else None
            if actual != expected:
                raise SyncError(f"Local file changed during apply: {name}; run recover after preserving edits.")
            if wanted is None:
                path.unlink()
            else:
                entry = desired[name]
                mode = current[name].mode if name in current else entry.mode
                atomic_write(path, entry.data, mode, entry.mtime_ns)
        # Verify the target before advancing the baseline (including changes in other files).
        final = local_snapshot(self.root, self.patterns)
        if hashes(final) != target:
            raise SyncError("Local files changed before sync finished; recovery journal retained.")
        save_json(self.state / "baseline.json", {
            "schema": 1, "synced_at": now(), "remote_sha256": journal["remote_target"],
            "files": {p: {"sha256": e.sha, "local_mtime_ns": final[p].mtime_ns,
                           "source_mtime_ns": e.mtime_ns} for p, e in desired.items()}})
        (self.state / "pending.json").unlink()
        # Only one current snapshot is retained; this is not a backup/version archive.
        for obj in (self.state / "objects").glob("*"):
            if obj.name not in set(target.values()):
                obj.unlink()

    def sync(self, dry_run=False, take_local=(), take_remote=()):
        with self.lock():
            if (self.state / "pending.json").exists():
                raise SyncError("Interrupted sync found. Run recover before another sync.")
            with self.transport() as transport:
                remote = transport.snapshot()
                local = local_snapshot(self.root, self.patterns)
                desired, actions, conflicts = plan(self.baseline(), local, remote, take_local, take_remote)
                report = {"project": str(self.root), "dry_run": dry_run,
                          "actions": actions, "conflicts": conflicts}
                if conflicts:
                    save_json(self.state / "conflicts.json", report)
                    raise ConflictError(report)
                self.preflight(desired, local)
                transport.validate(desired)
                if dry_run:
                    return report
                self.preflight(desired, local)
                journal = self.journal(local, remote, desired)
                if hashes(desired) != hashes(remote):
                    transport.apply(remote, desired)
                elif hashes(transport.snapshot()) != hashes(remote):
                    raise SyncError("Overleaf changed during planning. Journal retained; run recover.")
                self.apply_local(journal, desired)
                (self.state / "conflicts.json").unlink(missing_ok=True)
                report["synced_at"] = now()
                return report

    def recover(self):
        with self.lock():
            journal = read_json(self.state / "pending.json")
            if journal is None:
                return {"message": "No interrupted sync to recover."}
            desired = self.journal_entries(journal)
            current = hashes(local_snapshot(self.root, self.patterns))
            target = hashes(desired)
            changed = [p for p in sorted(set(current) | set(target) | set(journal["before"]))
                       if current.get(p) not in {journal["before"].get(p), target.get(p)}]
            if changed:
                raise SyncError("Local files changed since interruption: " + ", ".join(changed)
                                + ". Preserve these edits before recovery.")
            with self.transport() as transport:
                remote = transport.snapshot()
                rh = hashes(remote)
                changed = [p for p in sorted(set(rh) | set(target) | set(journal["remote_before"]))
                           if rh.get(p) not in {journal["remote_before"].get(p), target.get(p)}]
                if changed:
                    raise SyncError("Overleaf changed since interruption: " + ", ".join(changed)
                                    + ". Journal retained; preserve these edits before recovery.")
                self.preflight(desired, local_snapshot(self.root, self.patterns))
                transport.validate(desired)
                if rh != target:
                    transport.apply(remote, desired)
                self.apply_local(journal, desired)
                return {"message": "Recovery completed."}


def initialize(root, remote, excludes=(), profile=None):
    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state = root / STATE
    if state.is_symlink():
        raise SyncError("State directory must not be a symlink.")
    if (state / "config.json").exists():
        raise SyncError("Project is already initialized.")
    remote = normalize_remote(remote)
    if not remote.startswith("https://"):
        mirror = Path(remote)
        if root == mirror or root in mirror.parents or mirror in root.parents:
            raise SyncError("Local project and offline mirror must not overlap.")
    state.mkdir(mode=0o700, exist_ok=True)
    save_json(state / "config.json", {"schema": 1, "remote": remote, "profile": profile,
                                      "exclude": DEFAULT_EXCLUDES + list(excludes)})
    return {"message": f"Initialized {root}. Run status to preview the first sync.", "remote": remote}


def print_report(report, json_output=False):
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if "message" in report:
        print(report["message"])
        return
    conflicts = report.get("conflicts", [])
    if conflicts:
        print(f"STOP: {len(conflicts)} conflict(s). No project files changed locally or on Overleaf.")
        for item in conflicts:
            print("\nCONFLICT: " + item["path"])
            if "reason" in item:
                print(item["reason"])
            elif item.get("binary"):
                print(f"Binary/deletion: local {item['local_bytes']} bytes; Overleaf {item['remote_bytes']} bytes.")
                print(f"SHA256: local={item['local_sha256']} remote={item['remote_sha256']}")
            else:
                print("Local time:", item.get("local_modified_utc"),
                      " Overleaf export time:", item.get("remote_export_utc"))
                print("\n".join(item.get("diff", [])))
                if item.get("truncated"):
                    print("… diff abbreviated")
        print("\nCompare both files, then run sync --take-local FILE or --take-remote FILE.")
    else:
        print("Preview (no project file changes):" if report.get("dry_run") else "Sync complete:")
        for item in report.get("actions", []):
            print(f"  {item['side']:8s} {item['action']:6s} {item['path']}")
        if not report.get("actions"):
            print("  Already in sync.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in using an isolated browser profile (no paid token)")
    login.add_argument("--site", default="https://www.overleaf.com")
    login.add_argument("--profile", help="Optional independent browser profile directory")
    login.add_argument("--json", action="store_true")
    for name in ("init", "sync", "status", "recover"):
        sub = commands.add_parser(name)
        sub.add_argument("--path", default=".", help="Local project directory (default: current directory)")
        sub.add_argument("--json", action="store_true", help="Machine-readable output")
        if name == "init":
            sub.add_argument("--project", required=True, help="Overleaf project URL/id (free web transport)")
            sub.add_argument("--profile", help="Browser profile directory used by login")
            sub.add_argument("--exclude", action="append", default=[], help="Additional glob to ignore")
        if name == "sync":
            sub.add_argument("--dry-run", action="store_true")
            sub.add_argument("--take-local", action="append", default=[], metavar="FILE")
            sub.add_argument("--take-remote", action="append", default=[], metavar="FILE")
    args = parser.parse_args(argv)
    try:
        if args.command == "login":
            from web_transport import login
            login(args.site, args.profile)
            report = {"message": "Browser login saved. You can now run status or sync."}
        elif args.command == "init":
            report = initialize(args.path, args.project, args.exclude, args.profile)
        elif args.command == "recover":
            report = Synchronizer(args.path).recover()
        else:
            report = Synchronizer(args.path).sync(
                dry_run=args.command == "status" or getattr(args, "dry_run", False),
                take_local=getattr(args, "take_local", []), take_remote=getattr(args, "take_remote", []))
        print_report(report, args.json)
        return 0
    except ConflictError as exc:
        print_report(exc.report, args.json)
        return 2
    except (SyncError, OSError, UnicodeError, ValueError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print("Error:", exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. If pending.json exists, run recover.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
