"""
Remote hash audit: verify the published ledger against the td drive.

The Verify task historically reported the local ledger only. td ls --json
file entries always carry the stored BLAKE3 hash, and td tree --json
returns the whole nested remote tree in one index-only call, so the plugin
can run a real remote comparison without downloading a single byte:

1. td tree --json / walks the whole remote (one call, no network: it reads
   td's local index).
2. Each published ledger row is compared against its remote entry:
   presence, stored hash, and size.
3. Remote files no ledger row accounts for are reported as extras.

td stores the ORIGINAL upload's size and blake3 hash even for native
photos (Telegram-recompressed bytes), so browse rows verify the same way
archive rows do. Rows adopted without --hash carry "" remotely and are
reported as remote_without_hash instead of failing the audit. Legacy rows
written before remote paths were recorded cannot be located remotely and
are counted as unverifiable instead of guessed at.

Two more report-only helpers live here:

- run_deletion_check compares published media rows against the current
  Stash library (children of backup: items deleted or moved in Stash are
  reported, never modified on Telegram).
- backfill_adopted_library records td-adopted rows into the ledger so the
  audit and coverage math see them (adopt itself moves no bytes).
"""
import os
from typing import Any, Dict, List

import log
from ledger import Ledger
from stash_client import StashClient
from td_client import TDClient, TDError, TDNotFoundError

# Sample lists stay bounded so a huge drift cannot flood the task log.
SAMPLE_LIMIT = 25

# Ledger kinds holding media rows (scenes/images/galleries). Artifacts
# (metadata/database/config) and adopted rows are snapshots, not library
# children, so deletion checks skip them.
MEDIA_KIND_PREFIXES = ("scene:", "image:", "gallery:")

# Archive and browse rows are both keyed by the media file path; browse rows
# carry a "browse:" canonical prefix and their underlying path rows are the
# archive ones, so browse rows are skipped in the deletion check.
SKIP_DELETION_PREFIXES = ("browse:", "missing:")


def _paged_paths(fetch, items_key: str) -> List[str]:
    """Enumerate primary file paths of one Stash object type, paged."""
    paths: List[str] = []
    page = 1
    while True:
        data = fetch(page=page, per_page=200)
        objects = data.get(items_key) or []
        if not objects:
            break
        count = data.get("count") or 0
        for obj in objects:
            files = obj.get("visual_files") or obj.get("files") or []
            if files and files[0].get("path"):
                paths.append(files[0]["path"])
        if page * 200 >= count or count == 0:
            break
        page += 1
    return paths


def run_deletion_check(stash: StashClient, ledger: Ledger) -> Dict[str, Any]:
    """
    Report-only Stash-side deletion check for the Verify task.

    Every published media ledger row is matched against the current Stash
    library. A row whose file no longer exists in Stash (scene/file deleted,
    gallery member removed, standalone image deleted, gallery zip removed)
    is reported per kind with bounded samples. Nothing is modified: the
    operator decides whether to purge the remote copy (td rm) or re-run.
    """
    rows = ledger.all_items(status="published")

    def kind_of(row: Dict[str, Any]) -> Optional[str]:
        kind = row.get("kind") or ""
        for prefix in MEDIA_KIND_PREFIXES:
            if kind.startswith(prefix):
                return prefix.rstrip(":")
        return None

    by_kind: Dict[str, List[str]] = {"scene": [], "image": [], "gallery": []}
    for row in rows:
        canonical = row.get("canonical_path") or ""
        if canonical.startswith(SKIP_DELETION_PREFIXES):
            continue
        kind = kind_of(row)
        if kind:
            by_kind[kind].append(canonical)

    report: Dict[str, Any] = {
        "status": "ok",
        "deleted": {},
        "checked": {"scene": 0, "image": 0, "gallery": 0},
        "errors": [],
    }
    try:
        scene_paths = set(_paged_paths(stash.find_scenes, "scenes"))
        report["checked"]["scene"] = len(scene_paths)
    except Exception as e:
        report["errors"].append(f"findScenes: {e}")
        scene_paths = set()
    try:
        image_paths = set(_paged_paths(stash.find_images, "images"))
        report["checked"]["image"] = len(image_paths)
    except Exception as e:
        report["errors"].append(f"findImages: {e}")
        image_paths = set()
    try:
        gallery_paths = {os.path.normpath(p) for p in _paged_paths(stash.find_galleries, "galleries")}
        report["checked"]["gallery"] = len(gallery_paths)
    except Exception as e:
        report["errors"].append(f"findGalleries: {e}")
        gallery_paths = set()

    current = scene_paths | image_paths | gallery_paths
    for kind, paths in by_kind.items():
        missing = [p for p in paths if p not in current]
        if missing:
            report["deleted"][kind] = _sample(missing)
    if report["deleted"]:
        report["status"] = "deleted"
    return report


def backfill_adopted_library(
    td: TDClient,
    ledger: Ledger,
    remote_root: str = "/",
) -> Dict[str, Any]:
    """
    Record td-adopted files into the ledger so the audit and coverage math
    account for them. adopt itself moves no bytes; the ledger views the
    adopted channel content as archive rows (kind "adopted:archive").
    Existing published rows are never overwritten. Returns the number of
    rows added and a bounded list of the paths.
    """
    remote_root = remote_root.rstrip("/") or "/"
    nodes = td.tree(remote_root, depth=256)
    remote_files: Dict[str, str] = {}
    _flatten_tree(nodes, remote_files)

    by_dir: Dict[str, List[Dict[str, Any]]] = {}
    for path in remote_files:
        directory = path.rsplit("/", 1)[0] or "/"
        by_dir.setdefault(directory, []).append(path)

    added: List[str] = []
    for directory, paths in sorted(by_dir.items()):
        try:
            entries = td.list_dir(directory)
        except TDError as e:
            log.LogWarning(f"adopt backfill: could not list {directory}: {e}")
            continue
        remote_by_path = {e.get("path"): e for e in entries if e.get("type") == "file"}
        for path in sorted(paths):
            entry = remote_by_path.get(path) or {}
            if ledger.get(path) is not None:
                continue
            ledger.record_success(
                canonical_path=path,
                kind="adopted:archive",
                size=entry.get("size") or 0,
                blake3=entry.get("hash") or None,
                remote_path=path,
            )
            added.append(path)

    log.LogInfo(f"adopt backfill: recorded {len(added)} adopted file(s) in the ledger")
    return {"rows_added": len(added), "paths": added[:SAMPLE_LIMIT]}


def _flatten_tree(nodes: List[Dict[str, Any]], files: Dict[str, str]) -> None:
    for node in nodes or []:
        if node.get("type") == "file":
            path = node.get("path")
            if path:
                files[path] = path
        _flatten_tree(node.get("children") or [], files)


def _sample(items: List[str]) -> Dict[str, Any]:
    return {"count": len(items), "sample": items[:SAMPLE_LIMIT]}


def run_remote_audit(td: TDClient, ledger: Ledger) -> Dict[str, Any]:
    """
    Compare published ledger rows against the remote drive index.

    Returns an audit report. When td is unusable (offline, not initialized)
    the audit degrades to a local-only status instead of failing, so the
    Verify task still reports the ledger summary.
    """
    # 1. Whole remote tree in one index-only call.
    try:
        nodes = td.tree("/", depth=64)
    except TDError as e:
        log.LogWarning(f"Remote audit could not read the drive tree: {e}")
        return {"status": "local_only", "error": str(e), "errors": [str(e)]}

    remote_files: Dict[str, str] = {}
    _flatten_tree(nodes, remote_files)

    missing_remote: List[str] = []
    hash_mismatch: List[str] = []
    size_mismatch: List[str] = []
    remote_without_hash: List[str] = []
    ledger_without_hash: List[str] = []
    unverifiable: List[str] = []
    errors: List[str] = []
    verified = 0

    # 2. Group published rows by remote directory and read hashes with ls
    # (tree entries carry paths only; ls file entries always carry hash).
    rows = ledger.all_items(status="published")
    by_dir: Dict[str, List[Dict[str, Any]]] = {}
    ledger_paths = set()
    for row in rows:
        remote_path = row.get("remote_path")
        canonical = row.get("canonical_path") or ""
        if remote_path:
            ledger_paths.add(remote_path)
            directory = remote_path.rsplit("/", 1)[0] or "/"
            by_dir.setdefault(directory, []).append({**row, "_remote": remote_path})
        elif canonical.startswith("browse:"):
            # Legacy browse rows predate recorded remote paths; their
            # display-name remote slot cannot be reconstructed from the
            # ledger, so they are reported instead of guessed at.
            unverifiable.append(canonical)
        else:
            # Archive and artifact rows mirror their remote path exactly.
            ledger_paths.add(canonical)
            directory = canonical.rsplit("/", 1)[0] or "/"
            by_dir.setdefault(directory, []).append({**row, "_remote": canonical})

    for directory, dir_rows in sorted(by_dir.items()):
        try:
            entries = td.list_dir(directory)
        except TDNotFoundError:
            missing_remote.extend(row["_remote"] for row in dir_rows)
            continue
        except TDError as e:
            errors.append(f"ls {directory}: {e}")
            continue
        remote_by_path = {e.get("path"): e for e in entries if e.get("type") == "file"}
        for row in dir_rows:
            entry = remote_by_path.get(row["_remote"])
            if entry is None:
                missing_remote.append(row["_remote"])
                continue
            ledger_hash = row.get("blake3") or ""
            remote_hash = entry.get("hash") or ""
            ledger_size = row.get("size") or 0
            remote_size = entry.get("size") or 0
            if remote_hash and ledger_hash and remote_hash != ledger_hash:
                hash_mismatch.append(row["_remote"])
            elif not remote_hash:
                remote_without_hash.append(row["_remote"])
            elif not ledger_hash:
                ledger_without_hash.append(row["_remote"])
            elif ledger_size and remote_size and ledger_size != remote_size:
                size_mismatch.append(row["_remote"])
            else:
                verified += 1

    # 3. Remote files no ledger row accounts for.
    remote_extras = [path for path in sorted(remote_files) if path not in ledger_paths]

    problems = missing_remote + hash_mismatch + size_mismatch
    status = "ok"
    if problems:
        status = "drift"
    elif errors:
        status = "partial"

    return {
        "status": status,
        "remote_files": len(remote_files),
        "verified": verified,
        "missing_remote": _sample(missing_remote),
        "hash_mismatch": _sample(hash_mismatch),
        "size_mismatch": _sample(size_mismatch),
        "remote_without_hash": _sample(remote_without_hash),
        "ledger_without_hash": _sample(ledger_without_hash),
        "unverifiable_legacy_paths": _sample(unverifiable),
        "remote_extras": _sample(remote_extras),
        "errors": errors[:SAMPLE_LIMIT],
    }
