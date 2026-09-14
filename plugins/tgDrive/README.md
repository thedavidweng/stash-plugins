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

## Architectural Decisions: The Telegram File Size Ceiling & Trade-Offs

Telegram enforces hard physical limits on MTProto file transfers:
- **Standard Accounts**: 2.0 GB per file.
- **Telegram Premium Accounts**: 4.0 GB per file.

### Why not split oversized files into multi-part chunks (`.part1.rar` / binary chunking)?

Many generic backup tools work around this ceiling by slicing large files into multi-part archives or binary chunks. In this plugin and `tg-drive-cli`, **we explicitly reject multi-part chunking by architectural design**. Here is why:

1. **Inline Playability & Human-First Feed (Core Product Principle)**
   - The primary value proposition of backing up to a Telegram channel is **interactive media browsing**: videos appear as playable cards with duration, dimensions, instant cover thumbnails, and streaming support.
   - Slicing a 10 GB 4K video into five `.part1.rar`...`.part5.rar` chunks turns the Telegram channel into an unplayable graveyard of raw binary blobs. It destroys the ability to watch content on phones, tablets, or desktops directly inside Telegram.

2. **Preserving Fingerprints & Zero-Surgery Recovery (Hash Provenance)**
   - Stash relies on file-level hash algorithms (`oshash`, `MD5`, and perceptual `pHash`) to link video files to performers, studios, tags, and scenes.
   - By transferring files **verbatim and byte-exact**, restored files on a new machine match their StashDB and local database fingerprints 100% automatically without custom reassembly scripts, staging disk requirements, or forensic surgery.

3. **Blast Radius & Reliability**
   - Multi-part archives introduce multiplicative failure points: if a single chunk out of five is lost, corrupted, or rate-limited by Telegram, the entire 10 GB file becomes unrecoverable.
   - Independent atomic files ensure that every successfully published message is a completely self-contained, playable, and restorable asset.

---

### How Oversized Files Are Handled: First-Class Skip & Audit Gatekeeper

Instead of failing silently or crashing mid-transfer, oversized files are treated as a **first-class workflow**:

1. **Pre-flight Size Gatekeeper**:
   Before initiating any network transfer, the plugin compares the file size against `max_file_size_gb` (configured in plugin settings, default 2.0 GB, or 4.0 GB for Premium). Oversized files never touch the network, avoiding wasted bandwidth and Telegram flood waits.
2. **Ledger Auditing**:
   Oversized files are recorded in `ledger.sqlite` with status `skipped` along with the exact file size and reason (e.g., `Oversized file (6.20 GB exceeds platform limit 4.00 GB)`). This prevents redundant scanning on future runs.
3. **Transparent Coverage Reporting**:
   Every backup and dry-run report computes two explicit coverage metrics:
   - **Object Coverage %**: Percentage of total scenes successfully published.
   - **Byte Coverage %**: Percentage of total library byte volume safely offsite.
   The report returns a detailed `skipped_details` list containing every skipped file path and size, giving operators full visibility into what remains strictly local.

---

### Recommended Operator Strategies for >4 GB Media

For operators with libraries containing scenes > 4 GB:
- **Enable Telegram Premium**: Setting `max_file_size_gb: 4.0` immediately doubles the ceiling from 2 GB to 4 GB, covering the vast majority of 1080p and 4K scenes.
- **Transcoding (Best Practice)**: For massive raw camera rips or multi-hour compilations > 4 GB, transcode them using HandBrake or FFmpeg into high-efficiency H.265/AV1 10-bit MP4s targeting under 4 GB. This produces identical perceptual quality, unlocks native Telegram in-app streaming, and brings them into the automated Telegram backup system.
- **Streaming Codecs**: Inline streaming playback is broadest for H.264/AAC MP4. Other formats remain 100% byte-exact and verifiable, but may require external playback in Telegram clients.
