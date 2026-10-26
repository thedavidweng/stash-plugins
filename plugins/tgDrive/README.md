# TG Drive Backup for Stash

A configurable, metadata-preserving backup and disaster recovery plugin connecting **[Stash](https://github.com/stashapp/stash)** to Telegram channels via **[tg-drive](https://github.com/thedavidweng/tg-drive)**.

All Stash access goes through the **GraphQL API**: scene enumeration, settings, metadata export, database snapshots, library scans and metadata import. The plugin never reads or writes Stash's SQLite database directly.

---

## Requirements

- **Stash >= 0.28** (`exportObjects` / `importObjects` / `backupDatabase` are needed). On **Stash >= 0.31** database snapshots also include blobs (covers, performer images, labels); on older versions blobs are excluded (see [compatibility](#version-compatibility)).
- **`td` (tg-drive) >= v0.2.1** available to the process running Stash (or inside the Stash container), initialized with `td init`. The health check enforces the minimum version: it reads `td version` and fails below v0.2.1 (dev builds built from `main` pass with a note). The plugin accepts a binary, a local release archive, or a binary in its `bin/` directory. Recommended: a channel with a linked discussion group (ADR 0018) - since v0.2.1, captions are human-only and the full path, hash, MIME, and tag metadata lives in the discussion-thread `td-manifest:v1` / `td-album:v1` machine records.
- **Python 3** on the Stash host (the plugin is executed by Stash as `python3`).

## First-time setup

Read the platform-specific [English setup guide](SETUP.md) before the first
backup. It covers Docker and Synology NAS path mapping, local release
archives, persistent `td` data, Telegram authentication, and channel
initialization.

The plugin deliberately does **not** collect Telegram API hashes, login codes,
2FA passwords, or session files through Stash settings. Run `td auth setup` and
`td auth login` once from a terminal attached to the Stash runtime, then run
the **TD Drive Health Check** task.

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

## What Gets Backed Up

Each item is a setting you can toggle in **Settings** $\rightarrow$ **Plugins** $\rightarrow$ **TG Drive Backup**:

| Item | Default | Remote path | How it is produced |
| :--- | :--- | :--- | :--- |
| Scene media (`backup_scenes`) | on | mode-dependent | `findScenes` via GraphQL, then `td cp` in the selected archive or browse mode (below) |
| Gallery images (`backup_galleries`) | on | mode-dependent | `findGalleries` via GraphQL. Folder galleries publish their member images as byte-exact document media groups in archive mode; zip-based galleries upload the zip itself as a document. Browse mode publishes each gallery as a native Telegram photo album under `/stash-browse/gallery/` |
| Standalone images (`backup_images`) | on | mode-dependent | `findImages` via GraphQL (images not belonging to any gallery). Archive mode uploads byte-exact documents; browse mode sends native Telegram photos under `/stash-browse/image/` |
| Metadata export (`backup_metadata`) | on | `/stash-metadata/export.zip` | Stash native `exportObjects`: all scenes, images, and galleries (with markers, ratings, performer/tag/studio/group associations, fingerprints via files), performers, studios, tags, groups, with dependencies |
| Database snapshot (`backup_database`) | on | `/stash-backup/database/stash-backup.zip` | Stash native `backupDatabase` (consistent SQLite snapshot made by Stash itself; blobs included on Stash >= 0.31) |
| `config.yml` (`backup_config`) | **off** | `/stash-backup/config/config.yml` | the raw config file, located via GraphQL `configuration.general.configFilePath`. **Contains credentials (API keys, stash-box tokens) - only enable on a private channel you control.** |

Artifacts use fixed "latest snapshot" paths and are replaced on every backup run; scene, gallery, and image media are incremental (a local `ledger.sqlite` records what was already published, by content hash).

The export zip, database snapshot, and gallery zips are guarded by the same Telegram size ceiling as scene files (`max_file_size_gb`); oversized items are skipped and reported instead of silently failing.

## Settings

| Setting | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `backup_scenes` | Boolean | true | Upload scene video files. |
| `backup_images` | Boolean | true | Upload images that belong to no gallery. |
| `backup_galleries` | Boolean | true | Upload galleries (folder or zip based). |
| `backup_metadata` | Boolean | true | Export and upload the native metadata zip. |
| `backup_database` | Boolean | true | Snapshot and upload the database. |
| `backup_config` | Boolean | false | Upload `config.yml` (contains credentials). |
| `media_upload_mode` | String | `archive` | Applies to scenes, gallery images, and standalone images. `archive` uploads byte-exact files as Telegram documents (galleries as document media groups) and supports Disaster Recovery. `browse` uploads scenes as streamable Telegram videos, images as Telegram photos, and galleries as native photo albums under `/stash-browse`, with readable metadata in each caption. New captions are human-only: `td` renders the display name (the plugin's human title/code/date/duration/resolution summary, with bare studio/performer/tag hashtags embedded in the browse name); the remote path and generated `#td_*` chain never appear (ADR 0021). Previously named `scene_upload_mode`; existing installations keep their stored value. |
| `scene_selection` | String | `all` | Which scenes **Backup to Telegram** uploads. `all` uploads every scene. `curated` uploads only identified scenes: those with a human title, at least one tag, and a stash-box match (`stash_ids`) — the library items whose display names and hashtag terms add real value. Unmatched/untitled scenes are reported as skipped. Applies to scenes only; galleries and images are unaffected. |
| `max_file_size_gb` | Number | 2.0 | Telegram per-file ceiling (2.0 standard, 4.0 Premium). Larger files are skipped and reported. |
| `batch_size` | Number | 50 | Max scenes per backup run (0 = all). |
| `job_timeout_minutes` | Number | 120 | How long the restore task waits for Stash scan/import jobs. |
| `server_timeout_seconds` | Number | 120 | HTTP timeout for regular GraphQL calls. |
| `export_timeout_seconds` | Number | 1800 | HTTP timeout for the synchronous `exportObjects`/`backupDatabase` calls. |
| `td_binary_path` | String | - | Optional container-visible path to a `td` executable or `.tar.gz`/`.tgz`/`.zip` release archive. Empty uses the plugin directory and then PATH. |
| `td_data_dir` | String | plugin `td-data/` | Persistent, container-visible directory for `config.toml`, `session.json`, and `local_cache.db`. Set an explicit Docker/NAS volume path when the plugin directory is not persistent. |
| `td_version` | String | `latest` | Release tag used by the explicit Install / Update TD Core task. The health check requires td >= v0.2.1. Updates never happen silently during backup. |
| `target_channel` | String | - | Channel ID or username. **Required** for Backup to Telegram (and Verify/Adopt run in its scope); there is no default channel. Each channel keeps its own sync ledger (`ledger-<channel>.sqlite` in the TD Data Directory), so switching channels re-uploads that channel's files and never mis-reports another channel's state. When empty, the Setup/Health/Install tasks still work against the driver's bound channel. |
| `adopt_remote_dir` | String | `/adopted` | Remote directory used by the **Adopt Existing Channel Media** task (`td adopt --unmanaged --into`). Adopted messages are indexed in place under this path and recorded in the plugin ledger, without moving bytes. |
| `restore_dir` | String | - | Destination directory for the restore task. **Required for restore.** |

Settings are stored in Stash's own configuration and are read back through GraphQL `configuration.plugins` (Stash does not pass plugin settings to raw plugins on stdin).

## Plugin Tasks

| Task | Description |
| :--- | :--- |
| **TD Drive Setup Guide** | Shows the resolved binary, data directory, safe terminal commands, and the link to the English setup guide. |
| **TD Drive Health Check** | Checks the binary, data directory, Telegram session, `td doctor`, and initialized drive root without exposing credentials. |
| **Install / Update TD Core** | Downloads the matching GitHub Release asset, verifies `checksums.txt`, and atomically installs it into the plugin `bin/` directory. It runs only when explicitly selected. |
| **Backup to Telegram** | Uploads artifacts (metadata export, database, config - per settings), then incrementally uploads scene media, gallery albums/images, and standalone images. Returns coverage percentages and a full report. |
| **Dry Run Preview** | Lists what a backup run would do, without uploading anything or running GraphQL exports. |
| **Verify Remote & Ledger** | Audits the local ledger and compares every published item against the remote drive index (stored BLAKE3 hashes via `td ls`/`tree --json`) without downloading media. Reports verified items, missing remote files, hash/size mismatches, and unaccounted remote files. Also runs a **report-only Stash deletion check** that flags published media rows whose files no longer exist in the Stash library (deleted or moved, per kind); nothing on Telegram is ever modified by it. |
| **Adopt Existing Channel Media** | For hand-curated channels: runs `td adopt --unmanaged` into `adopt_remote_dir` so already-posted channel messages (which the backup never published) become first-class drive files with BLAKE3 hashes, then records them in the ledger so Verify accounts for them. No bytes move and nothing is deleted. |
| **Disaster Recovery Restore** | Rebuilds the media tree, scans it in Stash, and imports metadata via GraphQL (below). |

## Disaster Recovery Procedure

The **Disaster Recovery Restore** task automates the non-destructive parts:

1. **Rebuild index**: `td scan --full` walks the Telegram channel and discussion threads to reconstruct the catalog and hashes.
2. **Download media**: every archive-mode media root is downloaded into `restore_dir` (the plugin's `/stash-metadata`, `/stash-backup`, and browse-only `/stash-browse` directories are excluded).
3. **Recovery artifacts**: the database snapshot and `config.yml` are placed under `<restore_dir>/stash-recovery/` for **manual** application. Replacing the live database or config of a running Stash is destructive and is never done automatically.
4. **Scan**: the restored media roots are scanned via GraphQL `metadataScan` (the plugin waits for the job, up to `job_timeout_minutes`).
5. **Import metadata**: the metadata export zip is imported via GraphQL `importObjects` (`duplicateBehaviour: OVERWRITE`, `missingRefBehaviour: CREATE`), restoring scene metadata, markers, ratings and associations. Media bytes are untouched - file hashes (`oshash`, `pHash`, MD5) match automatically.

To apply the recovery artifacts on the new machine: stop Stash, replace the database/config files in the Stash config directory with the copies under `stash-recovery/`, and start Stash again. The database snapshot is the most complete recovery path (it restores everything, including play history); the metadata import is the incremental, safer path.

## Version Compatibility

| Stash version | Behavior |
| :--- | :--- |
| < 0.28 | Not supported (no `exportObjects` / `importObjects` / `backupDatabase`). |
| 0.28 - 0.30 | Supported. Database snapshots exclude blobs: scene covers, performer images and labels stored in the database are not in the snapshot (they can be regenerated from the restored media). |
| >= 0.31 | Database snapshots include blobs (complete backup). |

The plugin detects the server version via GraphQL and degrades gracefully.

## Design Notes

- **GraphQL-first**: everything that can go through the GraphQL API does. The only direct file access is (a) uploading the artifacts `td` needs on disk and (b) reading the raw `config.yml` for the opt-in config backup, because Stash exposes its parsed configuration but not the file bytes.
- **Rate limiting is absorbed, not fatal**: uploads run `td cp --wait`, so Telegram `FLOOD_WAIT` responses are slept through inside `td` (up to `rate_limit.max_wait_seconds`, 300 s by default) instead of failing the run. If a wait still exceeds that budget, the plugin retries each upload a bounded number of times (3), then records the item as `failed` in the ledger and moves on; the next run picks it up. Fresh Telegram sessions are rate-limited more aggressively - the first backup after `td auth login` may be slow, and that is expected.
- **Two media-upload modes** (one setting, applies to scenes, galleries, and standalone images): `archive` is the default and uploads the original bytes as Telegram documents, mirroring library paths for recovery. Browse-mode media names encode the title, code, date, duration, resolution, studio, performers, and tags; `td` renders that name as Telegram's readable caption, so the bare studio/performer/tag hashtags embedded in the browse name are directly clickable and searchable in Telegram. As of td v0.2.1 the caption carries only that human display name: the remote path and `td`'s generated `#td_*` path chain no longer leak into captions (ADR 0021). Browse uploads are deliberately stored under `/stash-browse` and excluded from Disaster Recovery because Telegram compression changes their bytes and their browse paths are not the Stash media tree.
- **Gallery albums are native Telegram media groups**: in archive mode a folder gallery publishes its member images as byte-exact documents grouped into media albums (one `td cp` multi-file invocation per library directory; `td` chunks at 10 members per group and a lone survivor publishes as a single document), while a zip-based gallery publishes its zip file as one document. In browse mode each gallery becomes one native Telegram photo album under `/stash-browse/gallery/<id>/`; zip-based gallery members are read out of the zip and uploaded as photos. Non-photo members split off as documents rather than mixing types in one album.
- **Content changes are detected and re-published**: the ledger records each file's Stash content fingerprint (md5/oshash/sha256 with a size fallback), so a file whose bytes changed at an unchanged path is re-published automatically instead of being skipped as "already done". Scenes, zip galleries, and standalone images re-publish in place via single-file `td cp --replace --confirm`. Folder-gallery album members are different: td albums cannot `--replace`, and probe testing showed an in-place single-file replace invalidates the whole media group at the next `td scan --full` (missing members). The plugin therefore removes the changed member (`td rm --confirm`) and uploads the new bytes at its freed slot as a fresh message; the album index survives a full scan and every member stays downloadable (validated end-to-end against td v0.2.1 in the offline harness's restore leg). Browse-mode changed members re-publish individually at their own display-name slot (never inside another album), which is scan-safe.
- **`scene_selection=curated` uploads only identified scenes**: a scene qualifies when it has a human title, at least one tag, and a stash-box match (`stash_ids`). Everything else is skipped and reported, so a curated channel never fills with untitled/unmatched files. Combined with browse mode, the uploaded display names (and therefore captions) carry real metadata instead of placeholder "Scene N" names.
- **Verify reports Stash-side deletions (report-only)**: `Verify Remote & Ledger` compares published media rows against the current Stash library and lists, per kind (scene/image/gallery), any published file that has been deleted or moved in Stash. It never modifies Telegram; use `td rm --confirm` yourself for remote copies you no longer want.
- **Adopt task for hand-curated channels**: `Adopt Existing Channel Media` runs `td adopt --unmanaged --into` (ADR 0019) so messages already posted on the channel that the backup never published are indexed in place with BLAKE3 hashes and recorded in the ledger as `adopted:archive` rows, making them visible to Verify and the hash audit.
- **Captions are human-only machine-free surfaces (td >= v0.2.1, ADR 0021)**: `td` renders new captions from the display name only - the caller's human text and the display name. The remote path, the parent line, and the generated `#td_*` path chain never appear in new captions. Machine reconstruction uses the discussion-thread records (`td-manifest:v1` per single file, `td-album:v1` per media group), which always keep the full canonical path, BLAKE3 hash, MIME, and tag chain - never captions or hashtags. Posts uploaded before v0.2.1 may still carry the old scaffold; clean them up idempotently with `td repair --captions /stash-browse --dry-run`, review the plan, then `td repair --captions /stash-browse --continue-on-error` (run from the Stash container terminal). This only edits the human caption; the machine records are untouched.
- **No chunking**: files above the Telegram ceiling are skipped and audited, never split (see the trade-offs below).
- **Archive media are byte-exact**: restored files match their original hashes with no reassembly. Browse media trade byte identity for Telegram-native previews.

### Why oversized files are skipped, not split

Telegram caps one file at 2 GB (4 GB with Premium). Splitting a 10 GB video into `.part1.rar...part5.rar` chunks would:

1. destroy inline playability - the channel would become a graveyard of binary blobs;
2. break hash provenance - Stash links metadata by file hashes, and reassembled files need forensic surgery to match;
3. multiply failure points - one lost chunk loses the whole file.

Instead, oversized files are a first-class workflow: the pre-flight gatekeeper records them in the ledger with the exact reason, and every report includes **Object Coverage %** and **Byte Coverage %** plus a per-file `skipped_details` list. For >4 GB media, enable Telegram Premium or transcode to an efficient H.265/AV1 encode under 4 GB.

## Limitations

- **td >= v0.2.1 is required.** The health check runs `td version` and fails below v0.2.1, because the plugin relies on `td`'s typed media uploads (`--as`), native media-group albums, hash-readback via `ls --json`, and the v0.2.1 human-only caption contract (no remote path or `#td_*` chain in new captions; full machine metadata in discussion-thread manifests). A "dev" build (built from `main`) passes with a note; pin the release via **Install / Update TD Core** (v0.2.1 is released).
- The plugin's ledgers live in the persistent TD Data Directory alongside td's local cache. With `target_channel` set, each channel keeps its own `ledger-<channel>.sqlite` (dedup, deletions, and audits are per-channel; switching channels never mis-reports "already backed up"); without it, the default `ledger.sqlite` covers the bound channel. Legacy plugin-local ledgers are migrated automatically. They are never uploaded.
- The plugin does not perform interactive Telegram login from a Stash task. The one-time `td auth setup`, `td auth login`, and `td init` steps must run in a terminal attached to the Stash runtime.
- Backup runs replace the remote artifact slots; only the latest metadata/database/config snapshot is kept on the remote.
- The remote audit (`Verify Remote & Ledger`) reflects what `td`'s drive index reports (`ls`/`tree --json`). Artifacts are recorded without a hash, so the audit verifies media against the drive index and reports artifacts separately as "no stored hash".
- **Stash library paths are resolved for the container**: Stash sometimes stores paths relative to the filesystem root (`data/Porn/...`) while the media lives at `/data/Porn/...` (root-prefixed). The uploader prefers the exact path and falls back to the root-prefixed form, so NAS/Docker deployments upload the real bytes and archive-mode remote paths mirror `/data/...`. Paths missing in both forms fail with a clear "file not found" error.

## Development

The plugin ships with an opt-in offline end-to-end harness that runs the real
plugin against the real `td` binary with **no Telegram account and no
network**: `td` runs against its fake Telegram client
(`TD_FAKE_TELEGRAM=1`, login code `12345`, state persisted per test) and
Stash is replaced by an in-process stub GraphQL server (`tests/e2e/stub_stash.py`).
Set `TD_E2E_BIN=/path/to/td` to use a prebuilt binary, or
`TD_E2E_SOURCE=/path/to/tg-drive` to build it with `go build ./cmd/td`:

```bash
cd plugins/tgDrive
TD_E2E_SOURCE=/path/to/tg-drive python3 -m unittest tests/e2e/test_offline_e2e.py -v
```

The journey covers one-time td auth/init, plugin health, a full-coverage
archive backup (scenes, folder and zip galleries, standalone images,
artifacts), the remote hash audit, an incremental rerun, content-change
re-publishing (mutating scene and gallery-member bytes on disk and in the
stub), report-only Stash deletion reporting, the adopt flow for
pre-existing channel messages, and a byte-exact restore that proves every
member of a changed gallery album is still recoverable. It also asserts the
td v0.2.1 caption contract end to end: every new caption is exactly the
display name (no remote path, no `#td_*` tags), and every published item
keeps a `td-manifest:v1` / `td-album:v1` discussion comment carrying the
canonical path, BLAKE3 hash, MIME, and tag chain. Rerunning the unit
suite does not need the harness; it skips when neither variable is set.
