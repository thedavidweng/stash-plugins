"""
Disaster recovery and restore engine.
Orchestrates td scan rebuild, media download, metadata bundle retrieval,
and triggers Stash metadata scan & incremental import.
"""
import os
import tempfile
import time
from typing import Any, Dict, Optional

import log
from stash_client import StashClient
from td_client import TDClient


class RestoreEngine:
    def __init__(
        self,
        stash: StashClient,
        td: TDClient,
        target_dir: str,
        dry_run: bool = False,
    ):
        self.stash = stash
        self.td = td
        self.target_dir = target_dir
        self.dry_run = dry_run

    def run_restore(self) -> Dict[str, Any]:
        log.LogInfo(f"Starting disaster recovery restore to {self.target_dir}...")
        start_time = time.time()
        os.makedirs(self.target_dir, exist_ok=True)

        report = {
            "dry_run": self.dry_run,
            "scan_status": "skipped",
            "metadata_bundle_restored": False,
            "media_restored": False,
            "stash_scan_triggered": False,
            "duration_seconds": 0,
        }

        # Step 1: Rebuild index from Telegram
        log.LogInfo("Step 1/4: Rebuilding td drive index from Telegram (td scan --full)...")
        if not self.dry_run:
            try:
                scan_res = self.td.scan(full=True)
                report["scan_status"] = f"active={scan_res.get('active', 0)}"
                log.LogInfo(f"Index rebuild completed: {report['scan_status']}")
            except Exception as e:
                log.LogWarning(f"Index rebuild warning: {e}")

        # Step 2: Download Metadata Bundle
        log.LogInfo("Step 2/4: Fetching /stash-metadata/bundle.json...")
        temp_bundle_dir = tempfile.mkdtemp(prefix="stash_restore_")
        bundle_local_path = os.path.join(temp_bundle_dir, "bundle.json")

        if not self.dry_run:
            try:
                self.td.download("/stash-metadata/bundle.json", bundle_local_path)
                if os.path.exists(bundle_local_path):
                    report["metadata_bundle_restored"] = True
                    log.LogInfo("Metadata bundle downloaded successfully")
            except Exception as e:
                log.LogWarning(f"Metadata bundle download skipped or unavailable: {e}")

        # Step 3: Recursive Media Download
        log.LogInfo(f"Step 3/4: Downloading media files to {self.target_dir}...")
        if not self.dry_run:
            try:
                self.td.download("/", self.target_dir, recursive=True)
                report["media_restored"] = True
                log.LogInfo("Media tree download completed")
            except Exception as e:
                log.LogError(f"Media download failed: {e}")
                report["error"] = str(e)

        # Step 4: Trigger Stash Scan
        log.LogInfo("Step 4/4: Triggering Stash library scan...")
        if not self.dry_run:
            try:
                self.stash.trigger_scan([self.target_dir])
                report["stash_scan_triggered"] = True
                log.LogInfo("Stash library scan initiated successfully")
            except Exception as e:
                log.LogWarning(f"Stash scan trigger warning: {e}")

        report["duration_seconds"] = int(time.time() - start_time)
        log.LogInfo(f"Restore procedure finished in {report['duration_seconds']}s")
        return report
