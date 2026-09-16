"""
Incremental backup sync engine.

Order of operations (artifacts first, then bulk media, so a run interrupted
mid-upload has already pushed the small high-value files):

1. metadata export: Stash native exportObjects -> upload to
   /stash-metadata/export.zip (full metadata: scene associations, markers,
   ratings, fingerprints - exactly what importObjects restores)
2. database backup: Stash native backupDatabase -> upload to
   /stash-backup/database/stash-backup.zip
3. config backup: raw config.yml (path located via GraphQL) ->
   /stash-backup/config/config.yml
4. scene videos: byte-exact uploads mirroring local library paths

All Stash access is GraphQL. Telegram access is td (tg-drive-cli).
"""
import os
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, Optional

from ledger import Ledger
import log
from settings import (
    REMOTE_CONFIG_BACKUP,
    REMOTE_DATABASE_BACKUP,
    REMOTE_METADATA_EXPORT,
)
from stash_client import StashClient
from td_client import TDClient, TDError, TDRateLimitedError

# Artifacts are one file per kind; a transient Telegram flood wait must not
# lose them for the whole run, so uploads retry a bounded number of times.
ARTIFACT_UPLOAD_ATTEMPTS = 3

# Scene uploads get the same bounded flood-wait retry. Telegram often
# flood-waits fresh sessions for a few seconds per send; retrying with the
# server-provided wait succeeds where moving on would just record a failure.
SCENE_UPLOAD_ATTEMPTS = 3


class SyncEngine:
    def __init__(
        self,
        stash: StashClient,
        td: TDClient,
        ledger: Ledger,
        settings: Dict[str, Any],
        dry_run: bool = False,
    ):
        self.stash = stash
        self.td = td
        self.ledger = ledger
        self.settings = settings
        self.dry_run = dry_run
        self.max_file_size_bytes = int(settings["max_file_size_gb"] * 1024 * 1024 * 1024)

    # ------------------------------------------------------------- artifacts

    def _artifact_slot(self, enabled: bool) -> Dict[str, Any]:
        status = "disabled" if not enabled else ("skipped" if self.dry_run else "pending")
        return {"status": status}

    def _upload_artifact(
        self,
        local_path: str,
        remote_path: str,
        kind: str,
        slot: Dict[str, Any],
    ) -> None:
        size = os.path.getsize(local_path)
        slot["size_bytes"] = size
        slot["remote_path"] = remote_path
        if size > self.max_file_size_bytes:
            reason = (
                f"Oversized artifact ({size / (1024 ** 3):.2f} GB exceeds platform "
                f"limit {self.max_file_size_bytes / (1024 ** 3):.2f} GB); not uploaded"
            )
            slot["status"] = "skipped"
            slot["reason"] = reason
            self.ledger.record_skip(remote_path, kind, size, reason)
            log.LogWarning(f"{kind}: {reason}")
            return
        for attempt in range(1, ARTIFACT_UPLOAD_ATTEMPTS + 1):
            try:
                self.td.upload_file(local_path=local_path, remote_path=remote_path, replace=True)
                slot["status"] = "ok"
                self.ledger.record_success(canonical_path=remote_path, kind=kind, size=size)
                log.LogInfo(f"{kind}: uploaded {remote_path} ({size} bytes)")
                return
            except TDRateLimitedError as e:
                if attempt >= ARTIFACT_UPLOAD_ATTEMPTS:
                    slot["status"] = "failed"
                    slot["error"] = str(e)
                    self.ledger.record_failure(remote_path, kind, size, str(e))
                    log.LogWarning(f"{kind}: upload failed: Telegram rate limit persisted after {ARTIFACT_UPLOAD_ATTEMPTS} attempts")
                    return
                wait = min(e.retry_after_seconds, 60)
                log.LogWarning(
                    f"{kind}: Telegram rate limited, retrying in {wait}s "
                    f"(attempt {attempt}/{ARTIFACT_UPLOAD_ATTEMPTS})"
                )
                time.sleep(wait)
            except TDError as e:
                slot["status"] = "failed"
                slot["error"] = str(e)
                self.ledger.record_failure(remote_path, kind, size, str(e))
                log.LogWarning(f"{kind}: upload failed: {e}")
                return

    def _run_artifact(
        self,
        temp_dir: str,
        kind: str,
        settings_key: str,
        remote_path: str,
        produce: Callable[[str], str],
    ) -> Dict[str, Any]:
        """Run one artifact backup: honor the settings gate, produce the local
        file, upload it, and record the outcome in the returned slot."""
        slot = self._artifact_slot(bool(self.settings[settings_key]))
        if slot["status"] != "pending":
            if slot["status"] == "skipped":
                log.LogInfo(f"[dry run] {kind}: would produce and upload to {remote_path}")
            return slot
        try:
            self._upload_artifact(produce(temp_dir), remote_path, kind, slot)
        except Exception as e:
            slot["status"] = "failed"
            slot["error"] = str(e)
            log.LogWarning(f"{kind} backup failed: {e}")
        return slot

    # artifact producers: turn a temp dir into a local file to upload

    def _produce_metadata_export(self, temp_dir: str) -> str:
        log.LogInfo("Exporting Stash metadata via GraphQL (exportObjects)...")
        url = self.stash.export_objects()
        if not url:
            raise RuntimeError("Stash returned no export download link (requires Stash >= 0.28)")
        local_zip = os.path.join(temp_dir, "stash-export.zip")
        self.stash.download_file(url, local_zip)
        return local_zip

    def _produce_database_backup(self, temp_dir: str) -> str:
        if self.stash.supports_include_blobs():
            log.LogInfo("Backing up Stash database via GraphQL (backupDatabase, blobs included)...")
        else:
            log.LogInfo("Backing up Stash database via GraphQL (backupDatabase; Stash < 0.31 cannot include blobs)...")
        url = self.stash.backup_database(include_blobs=True)
        if not url:
            raise RuntimeError("Stash returned no backup download link (requires Stash >= 0.28)")
        local_zip = os.path.join(temp_dir, "stash-backup.zip")
        self.stash.download_file(url, local_zip)
        return local_zip

    def _produce_config_backup(self, temp_dir: str) -> str:
        log.LogInfo("Locating config.yml via GraphQL...")
        config_path = self.stash.get_config_file_path()
        if not config_path or not os.path.exists(config_path):
            raise RuntimeError(f"config.yml not found at {config_path}")
        # The raw config file is read once for upload: Stash exposes its
        # parsed config via GraphQL but not the file bytes.
        return config_path

    # ---------------------------------------------------------------- scenes

    def _backup_scenes(self, report: Dict[str, Any], batch_limit: Optional[int], temp_dir: str) -> None:
        page = 1
        per_page = 40
        processed_count = 0

        first_page = self.stash.find_scenes(page=1, per_page=per_page)
        total_scenes = first_page.get("count", 0)
        report["scenes_total"] = total_scenes
        if total_scenes == 0:
            log.LogInfo("Found 0 scenes in Stash library")
            return
        log.LogInfo(f"Found {total_scenes} total scenes in Stash library")

        while True:
            data = first_page if page == 1 else self.stash.find_scenes(page=page, per_page=per_page)
            scenes = data.get("scenes", [])
            if not scenes:
                break

            for s in scenes:
                if batch_limit is not None and processed_count >= batch_limit:
                    log.LogInfo(f"Reached configured batch limit of {batch_limit} scenes")
                    break

                processed_count += 1
                scene_id = s.get("id")
                title = s.get("title") or f"Scene {scene_id}"
                files = s.get("files", [])

                if not files:
                    report["scenes_skipped"] += 1
                    report["skipped_details"].append({
                        "scene_id": scene_id,
                        "title": title,
                        "path": None,
                        "size_bytes": 0,
                        "reason": "scene has no files in Stash",
                    })
                    log.LogWarning(f"Skipping scene {scene_id}: no files in Stash")
                    continue

                main_file = files[0]
                file_id = main_file.get("id")
                local_path = main_file.get("path")
                size = main_file.get("size", 0)
                duration = main_file.get("duration")
                width = main_file.get("width")
                height = main_file.get("height")

                report["bytes_total"] += size

                # Check 1: Oversized scenes (> Telegram limit)
                if size > self.max_file_size_bytes:
                    limit_gb = self.max_file_size_bytes / (1024 ** 3)
                    size_gb = size / (1024 ** 3)
                    reason = f"Oversized file ({size_gb:.2f} GB exceeds platform limit {limit_gb:.2f} GB)"
                    self.ledger.record_skip(local_path, "scene", size, reason, file_id)
                    report["scenes_skipped"] += 1
                    report["bytes_skipped"] += size
                    report["skipped_details"].append({
                        "scene_id": scene_id,
                        "title": title,
                        "path": local_path,
                        "size_bytes": size,
                        "reason": reason,
                    })
                    log.LogWarning(f"Skipping oversized scene {scene_id}: {reason}")
                    continue

                # Check 2: Already published in ledger and content unchanged
                existing = self.ledger.get(local_path)
                if existing and existing.get("status") == "published":
                    report["scenes_published"] += 1
                    report["bytes_published"] += size
                    continue

                # Dry run check
                if self.dry_run:
                    report["scenes_published"] += 1
                    report["bytes_published"] += size
                    log.LogInfo(f"[Dry Run] Would publish scene {scene_id}: {title} ({size / (1024**2):.1f} MB)")
                    continue

                # Prepare cover image
                thumb_path = None
                cover_rel = (s.get("paths") or {}).get("screenshot")
                if cover_rel:
                    t_dest = os.path.join(temp_dir, f"thumb_{scene_id}.jpg")
                    if self.stash.download_image(cover_rel, t_dest):
                        thumb_path = t_dest

                # Remote path mirrors local library path for verbatim restoration
                remote_path = local_path if local_path.startswith("/") else f"/{local_path}"

                log.LogInfo(f"Uploading scene {scene_id}: {title}")
                published = False
                error: Optional[str] = None
                for attempt in range(1, SCENE_UPLOAD_ATTEMPTS + 1):
                    try:
                        res = self.td.upload_file(
                            local_path=local_path,
                            remote_path=remote_path,
                            as_kind="video",
                            duration=duration,
                            width=width,
                            height=height,
                            streaming=True,
                            thumb_path=thumb_path,
                        )

                        msg_id = res.get("message_id")
                        blake3 = res.get("hash")
                        self.ledger.record_success(
                            canonical_path=local_path,
                            kind="scene",
                            size=size,
                            file_id=file_id,
                            blake3=blake3,
                            message_id=msg_id,
                        )
                        report["scenes_published"] += 1
                        report["bytes_published"] += size
                        published = True
                        break
                    except TDRateLimitedError as re:
                        error = str(re)
                        if attempt >= SCENE_UPLOAD_ATTEMPTS:
                            log.LogWarning(
                                f"Scene {scene_id}: Telegram flood wait persisted after "
                                f"{SCENE_UPLOAD_ATTEMPTS} attempts"
                            )
                            break
                        wait = min(re.retry_after_seconds, 60)
                        log.LogWarning(
                            f"Scene {scene_id}: Telegram flood wait {re.retry_after_seconds}s, "
                            f"retrying in {wait}s (attempt {attempt}/{SCENE_UPLOAD_ATTEMPTS})"
                        )
                        time.sleep(wait)
                    except TDError as te:
                        error = str(te)
                        log.LogError(f"td upload failed for scene {scene_id}: {te}")
                        break

                if not published:
                    self.ledger.record_failure(local_path, "scene", size, error, file_id)
                    report["scenes_failed"] += 1
                    report["failed_details"].append({"scene_id": scene_id, "error": error})

                if total_scenes > 0:
                    log.LogProgress(processed_count / total_scenes)

            if processed_count >= total_scenes:
                break

            if batch_limit is not None and processed_count >= batch_limit:
                break

            page += 1

    # ------------------------------------------------------------------- run

    def run_backup(self, batch_limit: Optional[int] = None) -> Dict[str, Any]:
        log.LogInfo("Starting Stash -> Telegram incremental backup run...")
        start_time = time.time()
        if batch_limit is None:
            batch_limit = self.settings["batch_size"] or None

        report: Dict[str, Any] = {
            "dry_run": self.dry_run,
            "artifacts": {},
            "scenes_total": 0,
            "scenes_published": 0,
            "scenes_skipped": 0,
            "scenes_failed": 0,
            "bytes_total": 0,
            "bytes_published": 0,
            "bytes_skipped": 0,
            "skipped_details": [],
            "failed_details": [],
            "duration_seconds": 0,
        }

        temp_dir = tempfile.mkdtemp(prefix="stash_tg_drive_")
        try:
            # 1-3. Small high-value artifacts first
            report["artifacts"]["metadata_export"] = self._run_artifact(
                temp_dir, "metadata", "backup_metadata", REMOTE_METADATA_EXPORT,
                self._produce_metadata_export,
            )
            report["artifacts"]["database_backup"] = self._run_artifact(
                temp_dir, "database", "backup_database", REMOTE_DATABASE_BACKUP,
                self._produce_database_backup,
            )
            report["artifacts"]["config_backup"] = self._run_artifact(
                temp_dir, "config", "backup_config", REMOTE_CONFIG_BACKUP,
                self._produce_config_backup,
            )

            # 4. Scene media
            if self.settings["backup_scenes"]:
                self._backup_scenes(report, batch_limit, temp_dir)
            else:
                log.LogInfo("Scene backup disabled in settings; skipping media upload")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        report["duration_seconds"] = int(time.time() - start_time)

        # Scene coverage percentages
        if report["scenes_total"] > 0:
            report["coverage_objects_percent"] = round(
                (report["scenes_published"] / report["scenes_total"]) * 100, 2
            )
        else:
            report["coverage_objects_percent"] = 100.0

        if report["bytes_total"] > 0:
            report["coverage_bytes_percent"] = round(
                (report["bytes_published"] / report["bytes_total"]) * 100, 2
            )
        else:
            report["coverage_bytes_percent"] = 100.0

        log.LogInfo(
            f"Backup complete in {report['duration_seconds']}s: "
            f"{report['scenes_published']} published, {report['scenes_skipped']} skipped, {report['scenes_failed']} failed. "
            f"Object coverage: {report['coverage_objects_percent']}%, Byte coverage: {report['coverage_bytes_percent']}%. "
            f"Artifacts: metadata={report['artifacts']['metadata_export']['status']}, "
            f"database={report['artifacts']['database_backup']['status']}, "
            f"config={report['artifacts']['config_backup']['status']}"
        )

        return report
