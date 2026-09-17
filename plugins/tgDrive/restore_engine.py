"""
Disaster recovery and restore engine.

Steps:
1. Rebuild the td index from Telegram (td scan --full).
2. List the remote tree root; download every archival media root into the
   restore directory (service directories and the browse-only projection are
   excluded so they never enter the Stash library).
3. Pull the database backup and config backup into
   <restore_dir>/stash-recovery/ for manual application (replacing the live
   database or config of a running Stash is destructive and is NOT done
   automatically).
4. Pull the metadata export zip and, once a Stash library scan over the
   restored media has finished, import it through GraphQL importObjects
   (incremental import; restores scene associations, markers, ratings and
   fingerprints without touching the media bytes).
"""
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional

import log
from settings import (
    REMOTE_CONFIG_BACKUP,
    REMOTE_DATABASE_BACKUP,
    REMOTE_METADATA_EXPORT,
    SERVICE_ROOTS,
)
from stash_client import StashClient
from td_client import TDClient, TDError, TDNotFoundError


class RestoreEngine:
    def __init__(
        self,
        stash: StashClient,
        td: TDClient,
        restore_dir: str,
        job_timeout_seconds: int = 7200,
    ):
        self.stash = stash
        self.td = td
        self.restore_dir = restore_dir
        self.job_timeout_seconds = max(int(job_timeout_seconds), 1)

    # ------------------------------------------------------------ artifacts

    def _download_recovery_artifact(self, remote_path: str, dest_path: str) -> Dict[str, Any]:
        slot: Dict[str, Any] = {"remote_path": remote_path, "local_path": dest_path}
        try:
            self.td.download(remote_path, dest_path, skip_existing=True)
            slot["status"] = "ok" if os.path.exists(dest_path) else "failed"
        except TDNotFoundError:
            slot["status"] = "absent"
        except TDError as e:
            slot["status"] = "failed"
            slot["error"] = str(e)
        return slot

    def _wait_for_job(self, label: str, job_id: str) -> Dict[str, Any]:
        log.LogInfo(f"Waiting for Stash {label} job {job_id} (timeout {self.job_timeout_seconds}s)...")
        status = self.stash.wait_for_job(job_id, self.job_timeout_seconds, progress=log.LogProgress)
        if status == "FINISHED":
            log.LogInfo(f"Stash {label} job finished")
        else:
            log.LogWarning(f"Stash {label} job ended with status: {status}")
        return {"job_id": job_id, "status": status}

    # ------------------------------------------------------------------ run

    def run_restore(self) -> Dict[str, Any]:
        log.LogInfo(f"Starting disaster recovery restore to {self.restore_dir}...")
        start_time = time.time()
        os.makedirs(self.restore_dir, exist_ok=True)

        report: Dict[str, Any] = {
            "scan_status": "skipped",
            "media": {"status": "skipped", "roots": []},
            "recovery_artifacts": {},
            "stash_scan": {"status": "skipped"},
            "metadata_import": {"status": "skipped"},
            "duration_seconds": 0,
        }

        temp_dir = tempfile.mkdtemp(prefix="stash_tg_restore_")
        try:
            # Step 1: Rebuild index from Telegram
            log.LogInfo("Step 1/5: Rebuilding td drive index from Telegram (td scan --full)...")
            try:
                scan_res = self.td.scan(full=True)
                report["scan_status"] = f"active={scan_res.get('active', 0)}"
                log.LogInfo(f"Index rebuild completed: {report['scan_status']}")
            except TDError as e:
                report["scan_status"] = f"failed: {e}"
                log.LogWarning(f"Index rebuild warning: {e}")

            # Step 2: Download media roots (skip the plugin's service directories)
            log.LogInfo("Step 2/5: Listing remote root and downloading media tree...")
            media_roots: List[Dict[str, Any]] = []
            media_errors: List[str] = []
            try:
                entries = self.td.list_dir("/")
            except TDError as e:
                entries = []
                media_errors.append(str(e))
                log.LogError(f"Could not list remote root: {e}")
            for e in entries:
                name = e.get("name", "")
                if name in SERVICE_ROOTS:
                    continue
                remote_path = e.get("path", "/" + name)
                local_path = os.path.join(self.restore_dir, name)
                try:
                    if e.get("type") == "dir":
                        self.td.download(
                            remote_path,
                            local_path,
                            recursive=True,
                            skip_existing=True,
                            continue_on_error=True,
                        )
                    else:
                        self.td.download(remote_path, local_path, skip_existing=True)
                    media_roots.append({"remote": remote_path, "local": local_path})
                except TDError as err:
                    # one failing root must not abort the remaining roots
                    media_errors.append(f"{remote_path}: {err}")
                    log.LogWarning(f"Media root download failed: {err}")
            report["media"]["roots"] = media_roots
            if media_errors:
                report["media"]["status"] = "partial" if media_roots else "failed"
                report["media"]["errors"] = media_errors
            else:
                report["media"]["status"] = "ok"
            log.LogInfo(f"Downloaded {len(media_roots)} media root(s) into {self.restore_dir}")

            # Step 3: Recovery artifacts (database + config) for manual application
            log.LogInfo("Step 3/5: Fetching database and config backups...")
            recovery_dir = os.path.join(self.restore_dir, "stash-recovery")
            os.makedirs(os.path.join(recovery_dir, "database"), exist_ok=True)
            os.makedirs(os.path.join(recovery_dir, "config"), exist_ok=True)
            report["recovery_artifacts"]["database"] = self._download_recovery_artifact(
                REMOTE_DATABASE_BACKUP,
                os.path.join(recovery_dir, "database", "stash-backup.zip"),
            )
            report["recovery_artifacts"]["config"] = self._download_recovery_artifact(
                REMOTE_CONFIG_BACKUP,
                os.path.join(recovery_dir, "config", "config.yml"),
            )

            # Step 4: Metadata export -> scan restored media
            log.LogInfo("Step 4/5: Fetching metadata export and scanning restored media in Stash...")
            export_zip = os.path.join(temp_dir, "stash-export.zip")
            report["recovery_artifacts"]["export"] = self._download_recovery_artifact(
                REMOTE_METADATA_EXPORT, export_zip
            )

            media_local_roots = [r["local"] for r in media_roots if os.path.exists(r["local"])]
            if media_local_roots:
                try:
                    scan_job = self.stash.trigger_scan(media_local_roots)
                    report["stash_scan"] = self._wait_for_job("scan", scan_job)
                except Exception as e:
                    report["stash_scan"] = {"status": f"failed: {e}"}
                    log.LogWarning(f"Could not trigger Stash scan: {e}")
            else:
                log.LogWarning("No media roots restored; skipping Stash scan and metadata import")

            # Step 5: Import metadata via GraphQL (only meaningful after the
            # scan has indexed the restored files)
            log.LogInfo("Step 5/5: Importing Stash metadata (importObjects, GraphQL upload)...")
            if report["recovery_artifacts"]["export"].get("status") == "ok" and report["stash_scan"].get("status") == "FINISHED":
                try:
                    import_job = self.stash.import_objects(
                        export_zip,
                        duplicate_behaviour="OVERWRITE",
                        missing_ref_behaviour="CREATE",
                    )
                    report["metadata_import"] = self._wait_for_job("import", import_job)
                except Exception as e:
                    report["metadata_import"] = {"status": f"failed: {e}"}
                    log.LogWarning(f"Metadata import failed: {e}")
            elif report["recovery_artifacts"]["export"].get("status") == "ok":
                report["metadata_import"] = {"status": "skipped (library scan did not finish)"}
                log.LogWarning("Skipping metadata import because the library scan did not finish cleanly")
            else:
                report["metadata_import"] = {"status": "absent (metadata backup disabled at backup time)"}
                log.LogWarning("No metadata export available on the remote; skipping import")

            # Manual instructions for the destructive artifacts
            if report["recovery_artifacts"].get("database", {}).get("status") == "ok" or \
               report["recovery_artifacts"].get("config", {}).get("status") == "ok":
                log.LogInfo(
                    "Database/config backups were placed under "
                    f"{recovery_dir}. To apply them: stop Stash, replace the database/config files in the "
                    "Stash config directory, and start Stash again. This step is manual by design."
                )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        report["duration_seconds"] = int(time.time() - start_time)
        log.LogInfo(f"Restore procedure finished in {report['duration_seconds']}s")
        return report
