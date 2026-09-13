"""
Incremental backup sync engine.
Orchestrates metadata extraction, size gatekeeper, caption composition,
td upload invocation, ledger tracking, and coverage reporting.
"""
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

import caption
from ledger import Ledger
import log
from stash_client import StashClient
from td_client import TDClient, TDRateLimitedError, TDError

DEFAULT_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB default platform limit


class SyncEngine:
    def __init__(
        self,
        stash: StashClient,
        td: TDClient,
        ledger: Ledger,
        max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
        dry_run: bool = False,
    ):
        self.stash = stash
        self.td = td
        self.ledger = ledger
        self.max_file_size_bytes = max_file_size_bytes
        self.dry_run = dry_run

    def run_backup(
        self,
        batch_limit: Optional[int] = None,
        include_galleries: bool = False,
    ) -> Dict[str, Any]:
        log.LogInfo("Starting Stash -> Telegram incremental backup run...")
        start_time = time.time()

        # Temporary directory for thumbnails and export bundles
        temp_dir = tempfile.mkdtemp(prefix="stash_tg_drive_")

        report = {
            "dry_run": self.dry_run,
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

        try:
            # 1. Process Scenes
            page = 1
            per_page = 40
            processed_count = 0

            # Get first page to learn total count
            first_page = self.stash.find_scenes(page=1, per_page=per_page)
            total_scenes = first_page.get("count", 0)
            report["scenes_total"] = total_scenes
            log.LogInfo(f"Found {total_scenes} total scenes in Stash library")

            while True:
                if total_scenes == 0:
                    break

                data = first_page if page == 1 else self.stash.find_scenes(page=page, per_page=per_page)
                scenes = data.get("scenes", [])
                if not scenes:
                    break

                for s in scenes:
                    if batch_limit and processed_count >= batch_limit:
                        log.LogInfo(f"Reached configured batch limit of {batch_limit} scenes")
                        break

                    processed_count += 1
                    scene_id = s.get("id")
                    title = s.get("title") or f"Scene {scene_id}"
                    date = s.get("date")
                    code = s.get("code")
                    studio = (s.get("studio") or {}).get("name")
                    performers = [p.get("name") for p in s.get("performers", []) if p.get("name")]
                    tags = [t.get("name") for t in s.get("tags", []) if t.get("name")]
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

                    # Build Telegram caption within budget
                    cap = caption.build_scene_caption(
                        title=title,
                        date=date,
                        studio=studio,
                        performers=performers,
                        code=code,
                        tags=tags,
                    )

                    # Remote path mirrors local library path for verbatim restoration
                    remote_path = local_path if local_path.startswith("/") else f"/{local_path}"

                    # Invoke td upload with presentation attributes
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

                    # Report progress
                    if total_scenes > 0:
                        log.LogProgress(processed_count / total_scenes)

                if processed_count >= total_scenes:
                    break

                if batch_limit and processed_count >= batch_limit:
                    break

                page += 1

            # 2. Export & Upload Metadata Bundle
            if not self.dry_run and report["scenes_published"] > 0:
                bundle_path = os.path.join(temp_dir, "stash-metadata-bundle.json")
                log.LogInfo("Exporting full Stash metadata bundle...")
                try:
                    self.stash.export_metadata_bundle(bundle_path)
                    log.LogInfo("Uploading metadata bundle to /stash-metadata/bundle.json...")
                    self.td.upload_file(
                        local_path=bundle_path,
                        remote_path="/stash-metadata/bundle.json",
                        replace=True,
                    )
                except Exception as e:
                    log.LogWarning(f"Metadata bundle export/upload failed: {e}")

        finally:
            # Clean up temporary files
            try:
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

        report["duration_seconds"] = int(time.time() - start_time)

        # Calculate coverage percentages
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
            f"Object coverage: {report['coverage_objects_percent']}%, Byte coverage: {report['coverage_bytes_percent']}%"
        )

        return report
