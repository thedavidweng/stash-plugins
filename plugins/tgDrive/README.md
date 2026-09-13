# TG Drive Backup for Stash

An incremental, metadata-preserving backup and disaster recovery plugin connecting **[Stash](https://github.com/stashapp/stash)** to Telegram channels via **[tg-drive-cli](https://github.com/thedavidweng/tg-drive-cli)**.

---

## Features

- **Inline Playable Videos**: Uploads scene videos with native video attributes (duration, width, height, streaming hint) and generated cover thumbnails.
- **Human-Readable Captions**: Generates clean, searchable Telegram captions with performer hashtags (`#Performer`), studio hashtags (`#Studio`), code, title, and release date, strictly budgeted under the 1024 UTF-16 code unit ceiling.
- **Discussion Comment Thread Machine Records**: Machine manifests (`td-manifest:v1`) live in linked discussion threads (ADR 0018), keeping the main channel feed human-friendly.
- **Zero-Loss Metadata Bundle**: Periodically exports all Stash performers, studios, tags, markers, and fingerprints into an atomic JSON bundle stored under `/stash-metadata/bundle.json`.
- **Incremental Ledger**: Maintains an offline SQLite ledger (`ledger.sqlite`) recording `canonical_path -> message_id, size, blake3, status`, avoiding redundant uploads and ensuring resume-after-interrupt.
- **Oversized Scene Guard**: Explicitly detects and reports files exceeding Telegram size ceilings (2 GB standard / 4 GB Premium), outputting an audit report with coverage percentages instead of failing silently.
- **Disaster Recovery**: Rebuilds the media library and Stash metadata from scratch using `td scan --full` and Stash's native incremental import.

---

## Installation

### Method 1: Community Source (Recommended)
1. In Stash, go to **Settings** $\rightarrow$ **Plugins** $\rightarrow$ **Available Sources**.
2. Add the source URL:
   ```
   https://thedavidweng.github.io/stash-plugins/main/index.yml
   ```
3. Go to **Available**, search for **TG Drive Backup**, and click **Install**.

### Method 2: Manual Installation
Copy the `tgDrive` directory into your Stash plugins folder:
```bash
# Example for Synology Docker
cp -r plugins/tgDrive /volume1/docker/stash/config/plugins/
```
In Stash, navigate to **Settings** $\rightarrow$ **Plugins** and click **Reload Plugins**.

---

## Prerequisites

- **`td` (tg-drive-cli)**: Install `td` on the machine running Stash (or inside the Stash container) and initialize your Telegram channel:
  ```bash
  td init
  ```
- **Discussion Group**: Recommended to have a linked discussion supergroup so machine records stay in comment threads (ADR 0018).

---

## Plugin Tasks

The plugin registers four tasks in Stash under **Settings** $\rightarrow$ **Tasks**:

| Task | Description |
| :--- | :--- |
| **Backup to Telegram** | Incrementally syncs scenes, thumbnails, and metadata bundle to Telegram. |
| **Dry Run Preview** | Simulates a backup run and logs what would be published without uploading. |
| **Verify Ledger & Hashes** | Audits the local ledger and displays statistics (published, skipped, failed). |
| **Disaster Recovery Restore** | Pulls the media tree and metadata bundle from Telegram into the target directory. |

---

## Disaster Recovery Procedure

When recovering onto a new machine or restoring after data loss:

1. **Rebuild Index**:
   Run `td scan --full` (or invoke the **Disaster Recovery Restore** task). `td` walks the Telegram channel and discussion thread to reconstruct the complete file catalog and BLAKE3 hashes.
2. **Download Media and Metadata**:
   `td cp --recursive / <restore_path>` downloads all media files to the library path.
   `td cp /stash-metadata/bundle.json <restore_path>/stash-metadata.json` downloads the metadata bundle.
3. **Stash Re-Index & Import**:
   - Start Stash and point your library path to the restored media folder.
   - Run a standard **Metadata Scan** to detect files and calculate fingerprints (`oshash`, `phash`).
   - Run Stash **Incremental Import** against `stash-metadata.json`. All performers, tags, studios, and markers will match automatically by file path and hash.

---

## Platform Limits & Trade-Offs

- **File Size Limits**: Telegram limits non-Premium uploads to 2.0 GB (4.0 GB for Premium). Scenes exceeding this ceiling are safely skipped, recorded in the ledger as `skipped`, and highlighted in the run report.
- **Streaming Codecs**: Inline streaming playback is broadest for H.264/AAC MP4. Other formats remain 100% byte-exact and verifiable, but may require external playback in Telegram clients.
