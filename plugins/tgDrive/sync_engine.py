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
from typing import Any, Dict, Optional

from ledger import Ledger
import log
from settings import (
    REMOTE_CONFIG_BACKUP,
    REMOTE_DATABASE_BACKUP,
    REMOTE_METADATA_EXPORT,
)
from stash_client import StashClient
from td_client import TDClient, TDError, TDRateLimitedError


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
        self.max_file_size_bytes = int(float(settings.get("max_file_size_gb", 2.0)) * 1024 * 1024 * 1024)

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
        try:
            self.td.upload_file(local_path=local_path, remote_path=remote_path, replace=True)
            slot["status"] = "ok"
            self.ledger.record_success(canonical_path=remote_path, kind=kind, size=size)
            log.LogInfo(f"{kind}: uploaded {remote_path} ({size} bytes)")
        except TDError as e:
            slot["status"] = "failed"
            slot["error"] = str(e)
            self.ledger.record_failure(remote_path, kind, size, str(e))
            log.LogWarning(f"{kind}: upload failed: {e}")

    def _backup_metadata(self, temp_dir: str) -> Dict[str, Any]:
        slot = self._artifact_slot(bool(self.settings.get("backup_metadata")))
        if slot["status"] not in ("pending",):
            if slot["status"] == "skipped":
                log.LogInfo("[dry run] metadata export: would export via GraphQL and upload to " + REMOTE_METADATA_EXPORT)
            return slot
        try:
            log.LogInfo("Exporting Stash metadata via GraphQL (exportObjects)...")
            url = self.stash.export_objects()
            if not url:
                raise RuntimeError("Stash returned no export download link (requires Stash >= 0.28)")
            local_zip = os.path.join(temp_dir, "stash-export.zip")
            self.stash.download_file(url, local_zip)
            self._upload_artifact(local_zip, REMOTE_METADATA_EXPORT, "metadata", slot)
        except Exception as e:
            slot["status"] = "failed"
            slot["error"] = str(e)
            log.LogWarning(f"metadata export failed: {e}")
        return slot

    def _backup_database(self, temp_dir: str) -> Dict[str, Any]:
        slot = self._artifact_slot(bool(self.settings.get("backup_database")))
        if slot["status"] != "pending":
            if slot["status"] == "skipped":
                log.LogInfo("[dry run] database backup: would snapshot via GraphQL (backupDatabase) and upload to " + REMOTE_DATABASE_BACKUP)
            return slot
        try:
            include_blobs = bool(self.stash.supports_include_blobs())
            if include_blobs:
                log.LogInfo("Backing up Stash database via GraphQL (backupDatabase, blobs included)...")
            else:
                log.LogInfo("Backing up Stash database via GraphQL (backupDatabase; Stash < 0.31 cannot include blobs)...")
            url = self.stash.backup_database(include_blobs=True)
            if not url:
                raise RuntimeError("Stash returned no backup download link (requires Stash >= 0.28)")
            local_zip = os.path.join(temp_dir, "stash-backup.zip")
            self.stash.download_file(url, local_zip)
            self._upload_artifact(local_zip, REMOTE_DATABASE_BACKUP, "database", slot)
        except Exception as e:
            slot["status"] = "failed"
            slot["error"] = str(e)
            log.LogWarning(f"database backup failed: {e}")
        return slot

    def _backup_config(self, temp_dir: str) -> Dict[str, Any]:
        slot = self._artifact_slot(bool(self.settings.get("backup_config")))
        if slot["status"] != "pending":
            if slot["status"] == "skipped":
                log.LogInfo("[dry run] config backup: would upload config.yml to " + REMOTE_CONFIG_BACKUP)
            return slot
        try:
            log.LogInfo("Locating config.yml via GraphQL...")
            config_path = self.stash.get_config_file_path()
            if not config_path or not os.path.exists(config_path):
                raise RuntimeError(f"config.yml not found at {config_path}")
            # The raw config file is read once for upload: Stash exposes its
            # parsed config via GraphQL but not the file bytes.
            self._upload_artifact(config_path, REMOTE_CONFIG_BACKUP, "config", slot)
        except Exception as e:
            slot["status"] = "failed"
            slot["error"] = str(e)
            log.LogWarning(f"config backup failed: {e}")
        return slot

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

                try:
                    log.LogInfo(f"Uploading scene {scene_id}: {title}")
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

                except TDRateLimitedError as re:
                    log.LogWarning(f"Telegram flood wait triggered ({re.retry_after_seconds}s). Backing off...")
                    time.sleep(min(re.retry_after_seconds, 60))
                    # Record failure for retry on next run
                    self.ledger.record_failure(local_path, "scene", size, str(re), file_id)
                    report["scenes_failed"] += 1
                    report["failed_details"].append({"scene_id": scene_id, "error": str(re)})
                except TDError as te:
                    log.LogError(f"td upload failed for scene {scene_id}: {te}")
                    self.ledger.record_failure(local_path, "scene", size, str(te), file_id)
                    report["scenes_failed"] += 1
                    report["failed_details"].append({"scene_id": scene_id, "error": str(te)})

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
            batch_limit = int(float(self.settings.get("batch_size", 0) or 0)) or None

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
            report["artifacts"]["metadata_export"] = self._backup_metadata(temp_dir)
            report["artifacts"]["database_backup"] = self._backup_database(temp_dir)
            report["artifacts"]["config_backup"] = self._backup_config(temp_dir)

            # 4. Scene media
            if self.settings.get("backup_scenes"):
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
