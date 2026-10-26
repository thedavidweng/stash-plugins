"""
Incremental backup sync engine.

Order of operations (artifacts first, then bulk media, so a run interrupted
mid-upload has already pushed the small high-value files):

1. metadata export: Stash native exportObjects -> upload to
   /stash-metadata/export.zip (full metadata: scene associations, markers,
   ratings, fingerprints, plus image/gallery objects - exactly what
   importObjects restores)
2. database backup: Stash native backupDatabase -> upload to
   /stash-backup/database/stash-backup.zip
3. config backup: raw config.yml (path located via GraphQL) ->
   /stash-backup/config/config.yml
4. scene media, galleries, standalone images: incremental uploads honoring
   the configured archive or browse mode.

Mode-consistent treatment for every media type:
- archive mode publishes byte-exact Telegram documents mirroring the Stash
  library paths (Disaster Recovery restores them hash-verified). Galleries
  publish as native document media groups (one td cp multi-file invocation
  per gallery directory, chunked at 10 members per Telegram group by td).
- browse mode publishes native Telegram presentation: scenes as streamable
  videos, images as photos, galleries as native photo albums under
  /stash-browse (Telegram-recompressed, excluded from Disaster Recovery).

All Stash access is GraphQL. Telegram access is td (tg-drive).
"""
import os
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ledger import Ledger
import log
from settings import (
    REMOTE_BROWSE_GALLERIES,
    REMOTE_BROWSE_IMAGES,
    REMOTE_BROWSE_ROOT,
    REMOTE_CONFIG_BACKUP,
    REMOTE_DATABASE_BACKUP,
    REMOTE_METADATA_EXPORT,
)
from stash_client import StashClient, file_fingerprint
from td_client import TDClient, TDError, TDRateLimitedError

# Artifacts are one file per kind; a transient Telegram flood wait must not
# lose them for the whole run, so uploads retry a bounded number of times.
ARTIFACT_UPLOAD_ATTEMPTS = 3

# Media uploads get the same bounded flood-wait retry. Telegram often
# flood-waits fresh sessions for a few seconds per send; retrying with the
# server-provided wait succeeds where moving on would just record a failure.
MEDIA_UPLOAD_ATTEMPTS = 3

# Telegram media groups hold at most 10 members. The plugin chunks gallery
# albums itself so one failed invocation never strands a partially published
# larger set: td is all-or-nothing per invocation.
ALBUM_CHUNK_SIZE = 10

IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
CAPTION_COMPONENT_LIMIT = 80
CAPTION_FILENAME_LIMIT = 700


def resolve_local_path(path: str) -> str:
    """
    Resolve a Stash-returned file path to a readable local path.

    Stash sometimes stores library paths relative to the filesystem root
    ("data/Porn/...") while the media actually lives at the root ("/data/
    Porn/..."). The exact path wins when it exists; otherwise the root-
    prefixed form is tried, so container/NAS deployments upload the real
    bytes (and archive remote paths mirror /data/...). A path that exists
    in neither form passes through unchanged and fails later with a clear
    "file not found" error.
    """
    if not path:
        return path
    if os.path.exists(path):
        return path
    rooted = path if path.startswith("/") else f"/{path}"
    if os.path.exists(rooted):
        return rooted
    return path


def _caption_text(value: Any, limit: int = CAPTION_COMPONENT_LIMIT) -> str:
    """Make Stash text safe and compact for td's path-derived caption."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace("/", "∕").replace("\\", "∖")
    return text[:limit].rstrip()


def _hashtag(value: Any) -> str:
    """Build a bare, Telegram-searchable hashtag from a name.

    Bare hashtags make studio, performer, and tag names directly clickable
    and searchable: a prefixed "#studio_X" is a distinct token from "#X" and
    can never be found by searching the name itself, so categories only add
    noise to captions. Names are underscore-joined (Unicode letters
    retained) so multi-word names stay one token. td's own path-derived
    tags are always #td_* and never appear in modern captions, so they
    cannot collide with bare tag names.
    """
    text = re.sub(r"\W+", "_", _caption_text(value, 48), flags=re.UNICODE)
    text = text.strip("_")
    if not text:
        return ""
    return f"#{text}"


def _format_duration(seconds: Any) -> str:
    if seconds is None:
        return ""
    try:
        total = max(int(float(seconds)), 0)
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:d}:{seconds:02d}"


def _browse_hashtags(obj: Dict[str, Any]) -> List[str]:
    hashtags = []
    studio = (obj.get("studio") or {}).get("name")
    if studio:
        hashtags.append(_hashtag(studio))
    hashtags.extend(
        _hashtag(performer.get("name"))
        for performer in obj.get("performers") or []
        if performer.get("name")
    )
    hashtags.extend(
        _hashtag(tag.get("name"))
        for tag in obj.get("tags") or []
        if tag.get("name")
    )
    return [tag for tag in hashtags if tag]


def build_browse_display_name(
    title: str,
    details: List[str],
    hashtags: List[str],
    extension: str,
) -> str:
    """
    Build a td display name that doubles as the human Telegram caption.

    td renders captions from a file's display name and parent path, but its
    cp command deliberately has no arbitrary-caption option. Encoding this
    bounded summary in the display name makes it readable and searchable in
    native Telegram clients without changing original media.
    """
    parts = [title]
    if details:
        parts.append(" · ".join(details))
    if hashtags:
        parts.append(" ".join(hashtags))
    display_name = " | ".join(parts)
    return display_name[:CAPTION_FILENAME_LIMIT - len(extension)].rstrip(" .|") + extension


def build_browse_remote_path(scene: Dict[str, Any], local_path: str, duration: Any, width: Any, height: Any) -> str:
    """Build the browse-only remote path for a scene."""
    scene_id = str(scene.get("id") or "unknown")
    title = _caption_text(scene.get("title") or f"Scene {scene_id}", 240)
    extension = os.path.splitext(os.path.basename(local_path))[1].lower() or ".media"

    details = []
    for value in (scene.get("code"), scene.get("date"), _format_duration(duration)):
        text = _caption_text(value)
        if text:
            details.append(text)
    if width and height:
        details.append(f"{width}x{height}")

    display_name = build_browse_display_name(title, details, _browse_hashtags(scene), extension)
    return f"{REMOTE_BROWSE_ROOT}/{scene_id}/{display_name}"


def build_image_browse_remote_path(image: Dict[str, Any], width: Any, height: Any) -> str:
    """Build the browse-only remote path for a standalone image."""
    image_id = str(image.get("id") or "unknown")
    title = _caption_text(image.get("title") or f"Image {image_id}", 240)
    file_path = _primary_file(image, {}).get("path") or ""
    extension = _browse_extension(file_path, image_id)
    details = []
    for value in (image.get("code"), image.get("date")):
        text = _caption_text(value)
        if text:
            details.append(text)
    if width and height:
        details.append(f"{width}x{height}")
    display_name = build_browse_display_name(title, details, _browse_hashtags(image), extension)
    return f"{REMOTE_BROWSE_IMAGES}/{image_id}/{display_name}"


def build_gallery_browse_display_name(gallery: Dict[str, Any], extension: str) -> str:
    """
    Build the display name carrying the gallery caption.

    In an album the single human caption is rendered from the FIRST member's
    display name, so the first member of a gallery album is staged under
    this name (the browse display-name trick applied to galleries).
    """
    gallery_id = str(gallery.get("id") or "unknown")
    title = _caption_text(gallery.get("title") or f"Gallery {gallery_id}", 240)
    details = []
    for value in (gallery.get("code"), gallery.get("date")):
        text = _caption_text(value)
        if text:
            details.append(text)
    image_count = gallery.get("image_count")
    if image_count:
        details.append(f"{image_count} photos")
    return build_browse_display_name(title, details, _browse_hashtags(gallery), extension)


def scene_presentation(local_path: str, upload_mode: str) -> str:
    """Choose the td presentation type for the configured media upload mode."""
    if upload_mode == "archive":
        return "document"
    return "photo" if os.path.splitext(local_path)[1].lower() in IMAGE_EXTENSIONS else "video"


def is_image_file(path: str) -> bool:
    """Whether a file path is an image by extension (Stash images can also be video clips)."""
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def _mirror_remote_path(local_path: str) -> str:
    """Archive-mode remote path mirroring the Stash library path."""
    return local_path if local_path.startswith("/") else f"/{local_path}"


def _primary_file(obj: Dict[str, Any], default: Dict[str, Any]) -> Dict[str, Any]:
    files = obj.get("visual_files") or obj.get("files") or []
    return files[0] if files else default


def _browse_extension(file_path: str, fallback_id: Any) -> str:
    extension = os.path.splitext(os.path.basename(file_path or ""))[1].lower()
    return extension if extension and extension != ".zip" else ".jpg"


def _album_chunks(items: List[Any]) -> List[List[Any]]:
    return [items[i:i + ALBUM_CHUNK_SIZE] for i in range(0, len(items), ALBUM_CHUNK_SIZE)]


def _content_changed(file_dict: Dict[str, Any], row: Optional[Dict[str, Any]]) -> bool:
    """
    Whether a Stash file's content differs from the published ledger row.

    Fingerprints are the reliable signal (md5/oshash/sha256); when either
    side lacks them the size comparison is the fallback. Content-change
    detection must never trigger on metadata-only edits: Stash fingerprints
    only change when the bytes do.
    """
    if not row:
        return False
    fingerprint = file_fingerprint(file_dict)
    stored_fingerprint = row.get("fingerprint") or None
    if fingerprint and stored_fingerprint:
        return fingerprint != stored_fingerprint
    return bool(file_dict.get("size")) and file_dict.get("size") != row.get("size")


def _scene_is_curated(scene: Dict[str, Any]) -> bool:
    """
    Whether a scene qualifies for scene_selection=curated: a human title,
    at least one tag, and a stash-box match (scene_stash_ids).

    These are the library's identified scenes — the ones whose Telegram
    caption and hashtag search add real value. Scenes missing any of the
    three stay un-uploaded under the curated selection.
    """
    if not (scene.get("title") or "").strip():
        return False
    if not any((tag or {}).get("name") for tag in (scene.get("tags") or [])):
        return False
    return bool([s for s in (scene.get("stash_ids") or []) if s.get("stash_id")])


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
        # The download inherits the long export budget: on large libraries
        # the export itself takes minutes and the resulting zip is big.
        self.stash.download_file(url, local_zip, timeout=self.settings["export_timeout_seconds"])
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
        self.stash.download_file(url, local_zip, timeout=self.settings["export_timeout_seconds"])
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
            batch_exhausted = False

            for s in scenes:
                if batch_limit is not None and processed_count >= batch_limit:
                    log.LogInfo(f"Reached configured batch limit of {batch_limit} scenes")
                    batch_exhausted = True
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
                local_path = resolve_local_path(main_file.get("path") or "")
                size = main_file.get("size", 0)
                duration = main_file.get("duration")
                width = main_file.get("width")
                height = main_file.get("height")
                upload_mode = self.settings["media_upload_mode"]
                # Modes deliberately use distinct records. Switching modes
                # must publish the other representation rather than being
                # skipped due to the archive representation's ledger entry.
                ledger_path = local_path if upload_mode == "archive" else f"browse:{local_path}"

                report["bytes_total"] += size

                # Check 0: curated-only scene selection excludes scenes
                # without a title, tags, or stash-box match.
                if (
                    self.settings.get("scene_selection", "all") == "curated"
                    and not _scene_is_curated(s)
                ):
                    reason = (
                        "Scene skipped by scene_selection=curated: "
                        "missing title, tags, or stash-box match"
                    )
                    self.ledger.record_skip(ledger_path, f"scene:{upload_mode}", size, reason, file_id)
                    report["scenes_skipped"] += 1
                    report["bytes_skipped"] += size
                    report["skipped_details"].append({
                        "scene_id": scene_id,
                        "title": title,
                        "path": local_path,
                        "size_bytes": size,
                        "reason": reason,
                    })
                    log.LogWarning(f"Skipping scene {scene_id}: {reason}")
                    continue

                # Check 1: Oversized scenes (> Telegram limit)
                if size > self.max_file_size_bytes:
                    limit_gb = self.max_file_size_bytes / (1024 ** 3)
                    size_gb = size / (1024 ** 3)
                    reason = f"Oversized file ({size_gb:.2f} GB exceeds platform limit {limit_gb:.2f} GB)"
                    self.ledger.record_skip(ledger_path, f"scene:{upload_mode}", size, reason, file_id)
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

                if upload_mode == "archive":
                    # Archive paths mirror the library for Disaster Recovery.
                    remote_path = local_path if local_path.startswith("/") else f"/{local_path}"
                else:
                    remote_path = build_browse_remote_path(s, local_path, duration, width, height)
                as_kind = scene_presentation(local_path, upload_mode)

                # Check 2: Already published in this mode
                existing = self.ledger.get(ledger_path)
                scene_replace = False
                if existing and existing.get("status") == "published":
                    if _content_changed(main_file, existing):
                        # Changed bytes at the same path: re-publish with
                        # single-path cp --replace --confirm (td supersedes
                        # the old message and updates the manifest).
                        report["scenes_updated"] += 1
                        scene_replace = True
                        log.LogInfo(f"Scene {scene_id} content changed; re-publishing at {remote_path}")
                    else:
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

                log.LogInfo(f"Uploading scene {scene_id} as {upload_mode}: {title}")
                published = False
                error: Optional[str] = None
                for attempt in range(1, MEDIA_UPLOAD_ATTEMPTS + 1):
                    try:
                        res = self.td.upload_file(
                            local_path=local_path,
                            remote_path=remote_path,
                            as_kind=as_kind,
                            duration=duration if as_kind == "video" else None,
                            width=width if as_kind == "video" else None,
                            height=height if as_kind == "video" else None,
                            streaming=as_kind == "video",
                            thumb_path=thumb_path if as_kind != "photo" else None,
                            replace=scene_replace,
                        )

                        msg_id = res.get("message_id")
                        blake3 = res.get("hash")
                        self.ledger.record_success(
                            canonical_path=ledger_path,
                            kind=f"scene:{upload_mode}",
                            size=size,
                            file_id=file_id,
                            blake3=blake3,
                            message_id=msg_id,
                            remote_path=remote_path,
                            fingerprint=file_fingerprint(main_file),
                        )
                        report["scenes_published"] += 1
                        report["bytes_published"] += size
                        published = True
                        break
                    except TDRateLimitedError as re:
                        error = str(re)
                        if attempt >= MEDIA_UPLOAD_ATTEMPTS:
                            log.LogWarning(
                                f"Scene {scene_id}: Telegram flood wait persisted after "
                                f"{MEDIA_UPLOAD_ATTEMPTS} attempts"
                            )
                            break
                        wait = min(re.retry_after_seconds, 60)
                        log.LogWarning(
                            f"Scene {scene_id}: Telegram flood wait {re.retry_after_seconds}s, "
                            f"retrying in {wait}s (attempt {attempt}/{MEDIA_UPLOAD_ATTEMPTS})"
                        )
                        time.sleep(wait)
                    except TDError as te:
                        error = str(te)
                        log.LogError(f"td upload failed for scene {scene_id}: {te}")
                        break

                if not published:
                    self.ledger.record_failure(ledger_path, f"scene:{upload_mode}", size, error, file_id)
                    report["scenes_failed"] += 1
                    report["failed_details"].append({"scene_id": scene_id, "error": error})

                if total_scenes > 0:
                    log.LogProgress(processed_count / total_scenes)

            if processed_count >= total_scenes:
                break

            if batch_exhausted:
                break

            page += 1

    # ------------------------------------------------------ galleries & images

    def _sweep_images(self) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], int]:
        """
        One paged findImages pass classifying images by gallery membership.

        Returns (images_by_gallery, standalone_images, total). Gallery
        membership comes from the images side: every image lists its
        galleries, so no per-gallery image query is needed. The complete
        library is required: truncating a repeatable sorted query would
        permanently omit the tail and publish incomplete gallery albums.
        """
        by_gallery: Dict[str, List[Dict[str, Any]]] = {}
        standalone: List[Dict[str, Any]] = []
        total: Optional[int] = None
        processed = 0
        page = 1
        per_page = 100
        while True:
            data = self.stash.find_images(page=page, per_page=per_page)
            if total is None:
                total = data.get("count", 0)
                log.LogInfo(f"Found {total} total images in Stash library")
                if total == 0:
                    return by_gallery, standalone, 0
            images = data.get("images", [])
            if not images:
                break
            for image in images:
                processed += 1
                gallery_ids = [
                    str(g.get("id"))
                    for g in image.get("galleries") or []
                    if g.get("id") is not None
                ]
                if gallery_ids:
                    for gid in gallery_ids:
                        by_gallery.setdefault(gid, []).append(image)
                else:
                    standalone.append(image)
            if processed >= total:
                break
            page += 1
        log.LogInfo(
            f"Image sweep complete: {sum(len(v) for v in by_gallery.values())} gallery images "
            f"across {len(by_gallery)} galleries, {len(standalone)} standalone"
        )
        return by_gallery, standalone, total or 0

    def _record_skip(
        self,
        report: Dict[str, Any],
        ledger_path: str,
        kind: str,
        size: int,
        reason: str,
        file_id: Optional[int] = None,
    ) -> None:
        if not self.dry_run:
            self.ledger.record_skip(ledger_path, kind, size, reason, file_id)
        report["skipped_details"].append({"path": ledger_path, "size_bytes": size, "reason": reason})
        log.LogWarning(f"Skipping {ledger_path}: {reason}")

    def _publish_single_media(
        self,
        report: Dict[str, Any],
        local_path: str,
        remote_path: str,
        ledger_path: str,
        kind: str,
        size: int,
        as_kind: Optional[str],
        file_id: Optional[int] = None,
        duration: Optional[float] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        thumb_path: Optional[str] = None,
        label: str = "",
        replace: bool = False,
        fingerprint: Optional[str] = None,
    ) -> bool:
        """
        Publish one file with bounded flood-wait retries; True when published.

        Records the outcome in the ledger either way. Used for zip-gallery
        documents, standalone images, and lone gallery members; scenes keep
        their own loop (scene covers and per-scene logging). replace=True
        re-publishes changed content at the same remote path (td cp
        --replace --confirm) and is only used for non-album slots (scenes,
        zip documents, standalone images, browse display-name slots), where
        an in-place replace survives td scan --full. Album members cannot
        --replace; they go through _republish_album_member (rm + fresh cp),
        which keeps the album index valid across a full scan.
        """
        for attempt in range(1, MEDIA_UPLOAD_ATTEMPTS + 1):
            try:
                streaming = as_kind == "video"
                res = self.td.upload_file(
                    local_path=local_path,
                    remote_path=remote_path,
                    as_kind=as_kind,
                    duration=duration if streaming else None,
                    width=width if streaming else None,
                    height=height if streaming else None,
                    streaming=streaming,
                    thumb_path=thumb_path if as_kind != "photo" else None,
                    replace=replace,
                )
                self.ledger.record_success(
                    canonical_path=ledger_path,
                    kind=kind,
                    size=size,
                    file_id=file_id,
                    blake3=res.get("hash"),
                    message_id=res.get("message_id"),
                    remote_path=remote_path,
                    fingerprint=fingerprint,
                )
                return True
            except TDRateLimitedError as re:
                if attempt >= MEDIA_UPLOAD_ATTEMPTS:
                    log.LogWarning(
                        f"{label or local_path}: Telegram flood wait persisted after "
                        f"{MEDIA_UPLOAD_ATTEMPTS} attempts"
                    )
                    self.ledger.record_failure(ledger_path, kind, size, str(re), file_id)
                    return False
                wait = min(re.retry_after_seconds, 60)
                log.LogWarning(
                    f"{label or local_path}: Telegram flood wait {re.retry_after_seconds}s, "
                    f"retrying in {wait}s (attempt {attempt}/{MEDIA_UPLOAD_ATTEMPTS})"
                )
                time.sleep(wait)
            except TDError as te:
                log.LogError(f"td upload failed for {label or local_path}: {te}")
                self.ledger.record_failure(ledger_path, kind, size, str(te), file_id)
                return False
        return False

    def _publish_album_chunk(
        self,
        local_paths: List[str],
        remote_dir: str,
        items: List[Dict[str, Any]],
        kind: str,
        as_kind: Optional[str],
    ) -> bool:
        """
        Publish <=10 files as one native media group; True when published.

        All-or-nothing per invocation: td deletes unsent media and rolls its
        index rows back on failure, so a failed chunk can simply be retried
        next run. Member hashes are read back from the td index with ls
        (album responses carry message ids, not hashes).
        """
        for attempt in range(1, MEDIA_UPLOAD_ATTEMPTS + 1):
            try:
                res = self.td.upload_album(local_paths, remote_dir, as_kind=as_kind)
                id_by_base: Dict[str, Any] = {}
                for album in res.get("albums") or []:
                    paths = album.get("paths") or []
                    message_ids = album.get("message_ids") or []
                    for path, msg in zip(paths, message_ids):
                        id_by_base[path.rsplit("/", 1)[-1]] = msg
                hash_by_base: Dict[str, Any] = {}
                try:
                    entries = self.td.list_dir(remote_dir)
                    hash_by_base = {
                        e.get("name", ""): e.get("hash")
                        for e in entries
                        if e.get("type") == "file"
                    }
                except TDError as te:
                    log.LogWarning(f"Could not read back member hashes for {remote_dir}: {te}")
                for item in items:
                    base = os.path.basename(item["local_path"])
                    self.ledger.record_success(
                        canonical_path=item["ledger_path"],
                        kind=kind,
                        size=item["size"],
                        file_id=item.get("file_id"),
                        blake3=hash_by_base.get(base) or None,
                        message_id=id_by_base.get(base),
                        remote_path=f"{remote_dir}{base}",
                        fingerprint=item.get("fingerprint"),
                    )
                return True
            except TDRateLimitedError as re:
                if attempt >= MEDIA_UPLOAD_ATTEMPTS:
                    log.LogWarning(
                        f"Album {remote_dir}: Telegram flood wait persisted after "
                        f"{MEDIA_UPLOAD_ATTEMPTS} attempts"
                    )
                    for item in items:
                        self.ledger.record_failure(
                            item["ledger_path"], kind, item["size"], str(re), item.get("file_id")
                        )
                    return False
                wait = min(re.retry_after_seconds, 60)
                log.LogWarning(
                    f"Album {remote_dir}: Telegram flood wait {re.retry_after_seconds}s, "
                    f"retrying in {wait}s (attempt {attempt}/{MEDIA_UPLOAD_ATTEMPTS})"
                )
                time.sleep(wait)
            except TDError as te:
                log.LogError(f"td album upload failed for {remote_dir}: {te}")
                for item in items:
                    self.ledger.record_failure(
                        item["ledger_path"], kind, item["size"], str(te), item.get("file_id")
                    )
                return False
        return False

    # gallery/image item classification shared by both modes

    def _image_item(
        self,
        report: Dict[str, Any],
        image: Dict[str, Any],
        mode: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Build the publishable unit for one image, or None with the reason
        recorded when it cannot be published in the current mode.
        """
        image_id = str(image.get("id") or "unknown")
        file = _primary_file(image, {})
        file_id = file.get("id")
        path = resolve_local_path(file.get("path") or "")
        size = file.get("size", 0) or 0
        width = file.get("width")
        height = file.get("height")
        title = image.get("title") or f"Image {image_id}"
        fingerprint = file_fingerprint(file)

        if mode == "archive":
            ledger_path = path or f"image:{image_id}"
            remote_path = _mirror_remote_path(path) if path else ""
            as_kind = "document"
        else:
            ledger_path = f"browse:image:{image_id}"
            remote_path = build_image_browse_remote_path(image, width, height)
            as_kind = "photo" if (path and is_image_file(path)) else "video"

        report["bytes_total"] += size

        if not path:
            reason = "image has no files in Stash"
        elif size > self.max_file_size_bytes:
            reason = (
                f"Oversized file ({size / (1024 ** 3):.2f} GB exceeds platform "
                f"limit {self.max_file_size_bytes / (1024 ** 3):.2f} GB)"
            )
        elif mode == "archive" and not os.path.exists(path):
            reason = f"image file not found on disk: {path}"
        else:
            reason = None

        if reason:
            if not self.dry_run:
                self.ledger.record_skip(ledger_path, f"image:{mode}", size, reason, file_id)
            report["skipped_details"].append({
                "image_id": image_id,
                "title": title,
                "path": path,
                "size_bytes": size,
                "reason": reason,
            })
            log.LogWarning(f"Skipping image {image_id}: {reason}")
            return None

        existing = self.ledger.get(ledger_path)
        if existing and existing.get("status") == "published":
            # The publishable shape of an already-published item: callers
            # use "changed" to re-publish via single-path cp --replace.
            return {
                "already": True,
                "changed": _content_changed(file, existing),
                "size": size,
                "previously": True,
                "image_id": image_id,
                "ledger_path": ledger_path,
                "remote_path": remote_path,
                "local_path": path,
                "file_id": file_id,
                "as_kind": as_kind,
                "fingerprint": fingerprint,
            }

        if self.dry_run:
            log.LogInfo(
                f"[Dry Run] Would publish image {image_id}: {title} "
                f"({size / (1024 ** 2):.1f} MB)"
            )
            return {"already": True, "size": size}

        return {
            "image": image,
            "image_id": image_id,
            "ledger_path": ledger_path,
            "remote_path": remote_path,
            "local_path": path,
            "size": size,
            "file_id": file_id,
            "as_kind": as_kind,
            "fingerprint": fingerprint,
            "duration": file.get("duration") if as_kind == "video" else None,
            "width": width,
            "height": height,
        }

    # ------------------------------------------------------------- galleries

    def _backup_galleries(self, report: Dict[str, Any], sweep, temp_dir: str) -> None:
        by_gallery = sweep[0]
        mode = self.settings["media_upload_mode"]
        total: Optional[int] = None
        processed = 0
        page = 1
        per_page = 40
        while True:
            data = self.stash.find_galleries(page=page, per_page=per_page)
            if total is None:
                total = data.get("count", 0)
                report["galleries_total"] = total
                if total == 0:
                    log.LogInfo("Found 0 galleries in Stash library")
                    return
                log.LogInfo(f"Found {total} total galleries in Stash library")
            galleries = data.get("galleries", [])
            if not galleries:
                break
            for g in galleries:
                processed += 1
                try:
                    self._backup_one_gallery(report, g, by_gallery, mode, temp_dir)
                except Exception as e:  # one broken gallery must not abort the run
                    log.LogWarning(f"Gallery backup failed: {e}")
                    report["galleries_failed"] += 1
                    report["failed_details"].append({"gallery_id": g.get("id"), "error": str(e)})
                if total:
                    log.LogProgress(processed / total)
            if processed >= total:
                break
            page += 1

    def _backup_one_gallery(
        self,
        report: Dict[str, Any],
        gallery: Dict[str, Any],
        by_gallery: Dict[str, List[Dict[str, Any]]],
        mode: str,
        temp_dir: str,
    ) -> None:
        gid = str(gallery.get("id") or "unknown")
        title = gallery.get("title") or f"Gallery {gid}"
        members = by_gallery.get(gid, [])
        zip_files = gallery.get("files") or []
        published = skipped = failed = 0

        def account(outcomes: List[str]) -> None:
            nonlocal published, skipped, failed
            for status in outcomes:
                if status == "published":
                    published += 1
                elif status == "skipped":
                    skipped += 1
                else:
                    failed += 1

        if zip_files:
            # Zip-based gallery: the zip is the media unit. Archive mode
            # uploads it byte-exact at its library path (its member images
            # live inside); browse mode publishes the member images as a
            # native photo album instead (Telegram-recompressed anyway).
            if mode == "archive":
                for z in zip_files:
                    report["bytes_total"] += z.get("size", 0) or 0
                for z in zip_files:
                    published_status = self._publish_zip_gallery_item(report, gallery, z)
                    if published_status == "published":
                        report["bytes_published"] += z.get("size", 0) or 0
                    elif published_status == "skipped":
                        report["bytes_skipped"] += z.get("size", 0) or 0
                    account([published_status])
            else:
                account(self._publish_browse_gallery_images(report, gallery, members, temp_dir))
        else:
            if mode == "archive":
                account(self._publish_archive_gallery_images(report, gallery, members))
            else:
                account(self._publish_browse_gallery_images(report, gallery, members, temp_dir))

        if failed:
            report["galleries_failed"] += 1
        elif skipped:
            report["galleries_skipped"] += 1
        else:
            report["galleries_published"] += 1

    def _publish_zip_gallery_item(
        self,
        report: Dict[str, Any],
        gallery: Dict[str, Any],
        zip_file: Dict[str, Any],
    ) -> str:
        gid = str(gallery.get("id") or "unknown")
        path = resolve_local_path(zip_file.get("path") or "")
        size = zip_file.get("size", 0) or 0
        file_id = zip_file.get("id")
        ledger_path = path or f"missing:gallery:{gid}"
        remote_path = _mirror_remote_path(path) if path else ""

        if not path:
            reason = "gallery zip file has no path in Stash"
        elif size > self.max_file_size_bytes:
            reason = (
                f"Oversized gallery zip ({size / (1024 ** 3):.2f} GB exceeds platform "
                f"limit {self.max_file_size_bytes / (1024 ** 3):.2f} GB)"
            )
        elif not os.path.exists(path):
            reason = f"gallery zip not found on disk: {path}"
        else:
            reason = None

        if reason:
            if not self.dry_run:
                self.ledger.record_skip(ledger_path, "gallery:archive", size, reason, file_id)
            report["skipped_details"].append({
                "gallery_id": gid,
                "title": gallery.get("title") or f"Gallery {gid}",
                "path": path,
                "size_bytes": size,
                "reason": reason,
            })
            log.LogWarning(f"Skipping gallery {gid} zip: {reason}")
            return "skipped"

        existing = self.ledger.get(ledger_path)
        if existing and existing.get("status") == "published":
            if _content_changed(zip_file, existing):
                report["galleries_updated"] += 1
                log.LogInfo(f"Gallery {gid} zip content changed; re-publishing at {remote_path}")
                ok = self._publish_single_media(
                    report,
                    local_path=path,
                    remote_path=remote_path,
                    ledger_path=ledger_path,
                    kind="gallery:archive",
                    size=size,
                    as_kind="document",
                    file_id=file_id,
                    label=f"gallery {gid} zip",
                    replace=True,
                    fingerprint=file_fingerprint(zip_file),
                )
                return "published" if ok else "failed"
            return "published"
        if self.dry_run:
            log.LogInfo(
                f"[Dry Run] Would publish gallery {gid} zip: "
                f"{gallery.get('title') or f'Gallery {gid}'} ({size / (1024 ** 2):.1f} MB)"
            )
            return "published"

        log.LogInfo(f"Uploading gallery {gid} zip as document: {gallery.get('title') or f'Gallery {gid}'}")
        ok = self._publish_single_media(
            report,
            local_path=path,
            remote_path=remote_path,
            ledger_path=ledger_path,
            kind="gallery:archive",
            size=size,
            as_kind="document",
            file_id=file_id,
            label=f"gallery {gid} zip",
            fingerprint=file_fingerprint(zip_file),
        )
        return "published" if ok else "failed"

    def _republish_album_member(
        self,
        report: Dict[str, Any],
        item: Dict[str, Any],
        kind: str,
        label: str,
    ) -> str:
        """
        Re-publish a changed album member scan-safely: remove the remote
        slot (td rm), then upload the new bytes at the freed path as a
        fresh single message.

        td albums cannot --replace, and an in-place single-file replace of
        an album member invalidates the whole media group at the next
        scan --full (probe: active:1 invalid:1 missing:2). Removing the
        member and re-uploading keeps the album index valid (probe: scan
        clean, every member downloadable), including when the changed
        member is the album's first (inventory-carrying) one.
        """
        try:
            self.td.remove(item["remote_path"])
        except TDError as e:
            log.LogError(f"{label}: could not remove stale member {item['remote_path']}: {e}")
            self.ledger.record_failure(
                item["ledger_path"], kind, item["size"], str(e), item.get("file_id")
            )
            return "failed"
        ok = self._publish_single_media(
            report,
            local_path=item["local_path"],
            remote_path=item["remote_path"],
            ledger_path=item["ledger_path"],
            kind=kind,
            size=item["size"],
            as_kind=item.get("as_kind"),
            file_id=item.get("file_id"),
            fingerprint=item.get("fingerprint"),
        )
        return "published" if ok else "failed"

    def _publish_archive_gallery_images(
        self,
        report: Dict[str, Any],
        gallery: Dict[str, Any],
        members: List[Dict[str, Any]],
    ) -> List[str]:
        """
        Archive mode for a folder-based gallery: byte-exact documents
        mirroring library paths, grouped into native document media groups
        per parent directory (td names members by their local basenames, so
        one directory per album keeps the mirror intact).
        """
        gid = str(gallery.get("id") or "unknown")
        if not members:
            report["skipped_details"].append({
                "gallery_id": gid,
                "title": gallery.get("title") or f"Gallery {gid}",
                "reason": "gallery has no images in Stash",
            })
            log.LogWarning(f"Skipping gallery {gid}: no images in Stash")
            return ["skipped"]

        # Directory groups keep archive member paths mirroring the library.
        dir_groups: Dict[str, List[Dict[str, Any]]] = {}
        outcomes: List[str] = []
        for image in sorted(members, key=lambda img: str(_primary_file(img, {}).get("path") or "")):
            item = self._image_item(report, image, "archive")
            if item is None:
                outcomes.append("skipped")
            elif item.get("already"):
                report["bytes_published"] += item["size"]
                if item.get("changed"):
                    # Changed member: td albums cannot --replace (in-place
                    # replace breaks the group at the next scan --full), so
                    # the member is removed and re-uploaded fresh at its
                    # freed slot.
                    report["images_updated"] += 1
                    log.LogInfo(
                        f"Gallery {gid} member {item['image_id']} content changed; "
                        f"re-publishing at {item['remote_path']}"
                    )
                    outcomes.append(
                        self._republish_album_member(
                            report, item, "image:archive",
                            f"gallery {gid} member {item['image_id']}",
                        )
                    )
                else:
                    outcomes.append("published")
            else:
                dir_groups.setdefault(os.path.dirname(item["local_path"]), []).append(item)

        for dir_path, items in dir_groups.items():
            for chunk in _album_chunks(items):
                if len(chunk) == 1:
                    item = chunk[0]
                    log.LogInfo(
                        f"Uploading gallery {gid} member as document: "
                        f"{os.path.basename(item['local_path'])}"
                    )
                    ok = self._publish_single_media(
                        report,
                        local_path=item["local_path"],
                        remote_path=item["remote_path"],
                        ledger_path=item["ledger_path"],
                        kind="image:archive",
                        size=item["size"],
                        as_kind="document",
                        file_id=item.get("file_id"),
                    )
                    outcomes.append("published" if ok else "failed")
                    if ok:
                        report["bytes_published"] += item["size"]
                else:
                    remote_dir = _mirror_remote_path(dir_path).rstrip("/") + "/"
                    log.LogInfo(
                        f"Uploading gallery {gid} album of {len(chunk)} documents to {remote_dir}"
                    )
                    ok = self._publish_album_chunk(
                        [i["local_path"] for i in chunk],
                        remote_dir,
                        chunk,
                        kind="image:archive",
                        as_kind=None,
                    )
                    outcomes.extend("published" if ok else "failed" for _ in chunk)
                    if ok:
                        report["bytes_published"] += sum(i["size"] for i in chunk)
        return outcomes

    def _publish_browse_gallery_images(
        self,
        report: Dict[str, Any],
        gallery: Dict[str, Any],
        members: List[Dict[str, Any]],
        temp_dir: str,
    ) -> List[str]:
        """
        Browse mode for any gallery: native photo albums under
        /stash-browse/gallery/<id>/, one human gallery caption rendered from
        the first member's display name. Non-image members cannot share a
        Telegram media group with photos, so they publish separately as
        documents (td mixing rule). Member bytes come from the library file
        when it exists on disk (folder galleries) and from Stash's image
        endpoint otherwise (images inside zip galleries).
        """
        gid = str(gallery.get("id") or "unknown")
        if not members:
            report["skipped_details"].append({
                "gallery_id": gid,
                "title": gallery.get("title") or f"Gallery {gid}",
                "reason": "gallery has no images in Stash",
            })
            log.LogWarning(f"Skipping gallery {gid}: no images in Stash")
            return ["skipped"]

        remote_dir = f"{REMOTE_BROWSE_GALLERIES}/{gid}/"
        gallery_stage_dir = os.path.join(temp_dir, f"gallery_{gid}")
        os.makedirs(gallery_stage_dir, exist_ok=True)
        outcomes: List[str] = []
        photo_items: List[Dict[str, Any]] = []

        for image in sorted(members, key=lambda img: str(_primary_file(img, {}).get("path") or "")):
            item = self._image_item(report, image, "browse")
            if item is None:
                outcomes.append("skipped")
                continue
            replace = False
            if item.get("already"):
                report["bytes_published"] += item["size"]
                if not item.get("changed"):
                    outcomes.append("published")
                    continue
                replace = True
                report["images_updated"] += 1
                log.LogInfo(
                    f"Gallery {gid} member {item['image_id']} content changed; "
                    f"re-publishing at {item['remote_path']}"
                )

            # Resolve a local source file: the library path when it exists on
            # disk (folder galleries), Stash's image endpoint otherwise
            # (members that only exist inside a zip gallery).
            source = item["local_path"] if os.path.exists(item["local_path"]) else None
            if not source:
                image_url = (image.get("paths") or {}).get("image")
                member_name = os.path.basename(item["local_path"] or "")
                stem, member_ext = os.path.splitext(member_name)
                if not stem or not member_ext:
                    member_name = f"image_{item['image_id']}{_browse_extension(item['local_path'], item['image_id'])}"
                staged_path = os.path.join(gallery_stage_dir, member_name)
                if not image_url or not self.stash.download_image(image_url, staged_path):
                    reason = "could not fetch image bytes from Stash"
                    self.ledger.record_skip(
                        item["ledger_path"], "image:browse", item["size"], reason, item.get("file_id")
                    )
                    report["skipped_details"].append({
                        "image_id": item["image_id"],
                        "title": gallery.get("title") or f"Gallery {gid}",
                        "path": item["local_path"],
                        "size_bytes": item["size"],
                        "reason": reason,
                    })
                    log.LogWarning(f"Skipping gallery image {item['image_id']}: {reason}")
                    outcomes.append("skipped")
                    continue
                source = staged_path

            if item["as_kind"] != "photo":
                # Mixing rule: documents never join a photo media group.
                log.LogInfo(
                    f"{'Re-publishing' if replace else 'Uploading'} gallery {gid} "
                    f"non-image member as document: {item['image_id']}"
                )
                ok = self._publish_single_media(
                    report,
                    local_path=source,
                    remote_path=item["remote_path"] if replace
                    else f"{REMOTE_BROWSE_GALLERIES}/{gid}/{os.path.basename(source)}",
                    ledger_path=item["ledger_path"],
                    kind="image:browse",
                    size=item["size"],
                    as_kind=None,
                    file_id=item.get("file_id"),
                    replace=replace,
                    fingerprint=item.get("fingerprint"),
                )
                outcomes.append("published" if ok else "failed")
                if ok:
                    report["bytes_published"] += item["size"]
                continue

            if replace:
                # A changed photo cannot enter a media group (no --replace on
                # albums), so it re-publishes at its display-name slot.
                ok = self._publish_single_media(
                    report,
                    local_path=source,
                    remote_path=item["remote_path"],
                    ledger_path=item["ledger_path"],
                    kind="image:browse",
                    size=item["size"],
                    as_kind="photo",
                    file_id=item.get("file_id"),
                    replace=True,
                    fingerprint=item.get("fingerprint"),
                )
                outcomes.append("published" if ok else "failed")
                continue

            photo_items.append({**item, "local_path": source})

        for chunk_index, chunk in enumerate(_album_chunks(photo_items)):
            # The single human caption renders from the first member's
            # display name: stage a copy NAMED as the gallery display name
            # for the first member of the first chunk (td derives the
            # caption and the remote slot from the member's basename).
            if chunk_index == 0:
                first = chunk[0]
                ext = _browse_extension(first["local_path"], first["image_id"])
                display_name = build_gallery_browse_display_name(gallery, ext)
                caption_copy = os.path.join(gallery_stage_dir, display_name)
                try:
                    shutil.copyfile(first["local_path"], caption_copy)
                    first["local_path"] = caption_copy
                except OSError as e:
                    log.LogWarning(f"Could not stage gallery caption member: {e}")

            log.LogInfo(f"Uploading gallery {gid} photo album ({len(chunk)} members) to {remote_dir}")
            ok = self._publish_album_chunk(
                [i["local_path"] for i in chunk],
                remote_dir,
                chunk,
                kind="image:browse",
                as_kind="photo",
            )
            outcomes.extend("published" if ok else "failed" for _ in chunk)
            if ok:
                report["bytes_published"] += sum(i["size"] for i in chunk)
        return outcomes

    # --------------------------------------------------- standalone images

    def _backup_standalone_images(self, report: Dict[str, Any], sweep) -> None:
        standalone = sweep[1]
        mode = self.settings["media_upload_mode"]
        report["images_total"] = len(standalone)
        if not standalone:
            log.LogInfo("Found 0 standalone images in Stash library")
            return
        log.LogInfo(f"Found {len(standalone)} standalone images to back up")
        processed = 0
        for image in standalone:
            processed += 1
            item = self._image_item(report, image, mode)
            if item is None:
                report["images_skipped"] += 1
                continue
            if item.get("already") and not item.get("changed"):
                report["images_published"] += 1
                report["bytes_published"] += item["size"]
                continue
            if mode == "browse" and not os.path.exists(item["local_path"]):
                reason = f"image file not found on disk: {item['local_path']}"
                self.ledger.record_skip(
                    item["ledger_path"], f"image:{mode}", item["size"], reason, item.get("file_id")
                )
                report["skipped_details"].append({
                    "image_id": item["image_id"],
                    "path": item["local_path"],
                    "size_bytes": item["size"],
                    "reason": reason,
                })
                log.LogWarning(f"Skipping image {item['image_id']}: {reason}")
                report["images_skipped"] += 1
                continue
            replace = bool(item.get("changed"))
            if replace:
                report["images_updated"] += 1
            log.LogInfo(
                f"{'Re-publishing' if replace else 'Uploading'} image {item['image_id']} "
                f"as {mode}: {item['local_path']}"
            )
            ok = self._publish_single_media(
                report,
                local_path=item["local_path"],
                remote_path=item["remote_path"],
                ledger_path=item["ledger_path"],
                kind=f"image:{mode}",
                size=item["size"],
                as_kind=item["as_kind"],
                file_id=item.get("file_id"),
                duration=item.get("duration"),
                width=item.get("width"),
                height=item.get("height"),
                replace=replace,
                fingerprint=item.get("fingerprint"),
            )
            if ok:
                report["images_published"] += 1
                report["bytes_published"] += item["size"]
            else:
                report["images_failed"] += 1
                report["failed_details"].append({"image_id": item["image_id"], "error": "upload failed"})
            if len(standalone):
                log.LogProgress(processed / len(standalone))

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
            "scenes_updated": 0,
            "galleries_total": 0,
            "galleries_published": 0,
            "galleries_skipped": 0,
            "galleries_failed": 0,
            "galleries_updated": 0,
            "images_total": 0,
            "images_published": 0,
            "images_skipped": 0,
            "images_failed": 0,
            "images_updated": 0,
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
                log.LogInfo("Scene backup disabled in settings; skipping scene upload")

            # 5. Galleries and standalone images. The image sweep is shared:
            # one paged findImages pass classifies every image by gallery
            # membership before either consumer runs. batch_size limits
            # scenes; gallery membership must be complete on every run.
            if self.settings["backup_galleries"] or self.settings["backup_images"]:
                sweep = self._sweep_images()
            else:
                sweep = None
                log.LogInfo("Gallery and image backup disabled in settings")
            if self.settings["backup_galleries"]:
                self._backup_galleries(report, sweep, temp_dir)
            if self.settings["backup_images"]:
                self._backup_standalone_images(report, sweep)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        report["duration_seconds"] = int(time.time() - start_time)

        # Media coverage across scenes, galleries and standalone images
        objects_total = (
            report["scenes_total"] + report["galleries_total"] + report["images_total"]
        )
        objects_published = (
            report["scenes_published"]
            + report["galleries_published"]
            + report["images_published"]
        )
        if objects_total > 0:
            report["coverage_objects_percent"] = round(
                (objects_published / objects_total) * 100, 2
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
            f"scenes {report['scenes_published']}/{report['scenes_total']} published (+"
            f"{report['scenes_updated']} updated), "
            f"galleries {report['galleries_published']}/{report['galleries_total']} (+"
            f"{report['galleries_updated']} updated), "
            f"images {report['images_published']}/{report['images_total']} (+"
            f"{report['images_updated']} updated). "
            f"Object coverage: {report['coverage_objects_percent']}%, Byte coverage: {report['coverage_bytes_percent']}%. "
            f"Artifacts: metadata={report['artifacts']['metadata_export']['status']}, "
            f"database={report['artifacts']['database_backup']['status']}, "
            f"config={report['artifacts']['config_backup']['status']}"
        )

        return report
