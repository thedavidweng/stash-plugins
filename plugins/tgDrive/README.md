# TG Drive Backup for Stash

A configurable, metadata-preserving backup and disaster recovery plugin connecting **[Stash](https://github.com/stashapp/stash)** to Telegram channels via **[tg-drive-cli](https://github.com/thedavidweng/tg-drive-cli)**.

All Stash access goes through the **GraphQL API**: scene enumeration, settings, metadata export, database snapshots, library scans and metadata import. The plugin never reads or writes Stash's SQLite database directly.

---

## Requirements

- **Stash >= 0.28** (`exportObjects` / `importObjects` / `backupDatabase` are needed). On **Stash >= 0.31** database snapshots also include blobs (covers, performer images, labels); on older versions blobs are excluded (see [compatibility](#version-compatibility)).
- **`td` (tg-drive-cli)** available to the process running Stash (or inside the Stash container), initialized with `td init`. The plugin accepts a binary, a local release archive, or a binary in its `bin/` directory. Recommended: a channel with a linked discussion group so machine records live in comment threads (ADR 0018).
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
| Scene media (`backup_scenes`) | on | mirrors local library paths | `findScenes` via GraphQL, then byte-exact `td cp --as video` uploads (native video presentation: duration, dimensions, streaming hint, generated cover thumbnail) |
| Metadata export (`backup_metadata`) | on | `/stash-metadata/export.zip` | Stash native `exportObjects`: all scenes (with markers, ratings, performer/tag/studio/group associations, fingerprints via files), performers, studios, tags, groups, with dependencies |
| Database snapshot (`backup_database`) | on | `/stash-backup/database/stash-backup.zip` | Stash native `backupDatabase` (consistent SQLite snapshot made by Stash itself; blobs included on Stash >= 0.31) |
| `config.yml` (`backup_config`) | **off** | `/stash-backup/config/config.yml` | the raw config file, located via GraphQL `configuration.general.configFilePath`. **Contains credentials (API keys, stash-box tokens) - only enable on a private channel you control.** |

Artifacts use fixed "latest snapshot" paths and are replaced on every backup run; scene files are incremental (a local `ledger.sqlite` records what was already published, by content hash).

The export zip and database snapshot are guarded by the same Telegram size ceiling as scene files (`max_file_size_gb`); oversized artifacts are skipped and reported instead of silently failing.

## Settings

| Setting | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `backup_scenes` | Boolean | true | Upload scene video files. |
| `backup_metadata` | Boolean | true | Export and upload the native metadata zip. |
| `backup_database` | Boolean | true | Snapshot and upload the database. |
| `backup_config` | Boolean | false | Upload `config.yml` (contains credentials). |
| `max_file_size_gb` | Number | 2.0 | Telegram per-file ceiling (2.0 standard, 4.0 Premium). Larger files are skipped and reported. |
| `batch_size` | Number | 50 | Max scenes per backup run (0 = all). |
| `job_timeout_minutes` | Number | 120 | How long the restore task waits for Stash scan/import jobs. |
| `server_timeout_seconds` | Number | 120 | HTTP timeout for regular GraphQL calls. |
| `export_timeout_seconds` | Number | 1800 | HTTP timeout for the synchronous `exportObjects`/`backupDatabase` calls. |
| `td_binary_path` | String | - | Optional container-visible path to a `td` executable or `.tar.gz`/`.tgz`/`.zip` release archive. Empty uses the plugin directory and then PATH. |
| `td_data_dir` | String | plugin `td-data/` | Persistent, container-visible directory for `config.toml`, `session.json`, and `local_cache.db`. Set an explicit Docker/NAS volume path when the plugin directory is not persistent. |
| `td_version` | String | `latest` | Release tag used by the explicit Install / Update TD Core task. Updates never happen silently during backup. |
| `target_channel` | String | - | Channel ID/username (empty = the channel bound by `td init`). |
| `restore_dir` | String | - | Destination directory for the restore task. **Required for restore.** |

Settings are stored in Stash's own configuration and are read back through GraphQL `configuration.plugins` (Stash does not pass plugin settings to raw plugins on stdin).

## Plugin Tasks

| Task | Description |
| :--- | :--- |
| **TD Drive Setup Guide** | Shows the resolved binary, data directory, safe terminal commands, and the link to the English setup guide. |
| **TD Drive Health Check** | Checks the binary, data directory, Telegram session, `td doctor`, and initialized drive root without exposing credentials. |
| **Install / Update TD Core** | Downloads the matching GitHub Release asset, verifies `checksums.txt`, and atomically installs it into the plugin `bin/` directory. It runs only when explicitly selected. |
| **Backup to Telegram** | Uploads artifacts (metadata export, database, config - per settings), then incrementally uploads scene media. Returns coverage percentages and a full report. |
| **Dry Run Preview** | Lists what a backup run would do, without uploading anything or running GraphQL exports. |
| **Verify Ledger & Hashes** | Audits the local ledger: published/skipped/failed counts and byte totals. |
| **Disaster Recovery Restore** | Rebuilds the media tree, scans it in Stash, and imports metadata via GraphQL (below). |

## Disaster Recovery Procedure

The **Disaster Recovery Restore** task automates the non-destructive parts:

1. **Rebuild index**: `td scan --full` walks the Telegram channel and discussion threads to reconstruct the catalog and hashes.
2. **Download media**: every remote media root is downloaded into `restore_dir` (the plugin's own `/stash-metadata` and `/stash-backup` directories are excluded, so archives never leak into the Stash library).
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
- **Captions are generated by `td`**, from the file's display name, parent directory and directory-derived hashtags (`td` caption model). They are presentation only; machine reconstruction uses `td-manifest:v1` / `td-album:v1` records, never captions or hashtags.
- **No chunking**: files above the Telegram ceiling are skipped and audited, never split (see the trade-offs below).
- **Media are byte-exact**: restored files match their original hashes with no reassembly.

### Why oversized files are skipped, not split

Telegram caps one file at 2 GB (4 GB with Premium). Splitting a 10 GB video into `.part1.rar...part5.rar` chunks would:

1. destroy inline playability - the channel would become a graveyard of binary blobs;
2. break hash provenance - Stash links metadata by file hashes, and reassembled files need forensic surgery to match;
3. multiply failure points - one lost chunk loses the whole file.

Instead, oversized files are a first-class workflow: the pre-flight gatekeeper records them in the ledger with the exact reason, and every report includes **Object Coverage %** and **Byte Coverage %** plus a per-file `skipped_details` list. For >4 GB media, enable Telegram Premium or transcode to an efficient H.265/AV1 encode under 4 GB.

## Limitations

- **Images and galleries are not backed up.** Only scene video files are uploaded; the metadata export therefore excludes image/gallery objects (their media would be missing on restore anyway).
- The plugin's own `ledger.sqlite` is a local cache only; it is rebuilt from the remote index by `td scan` and is never uploaded.
- The plugin does not perform interactive Telegram login from a Stash task. The one-time `td auth setup`, `td auth login`, and `td init` steps must run in a terminal attached to the Stash runtime.
- Backup runs replace the remote artifact slots; only the latest metadata/database/config snapshot is kept on the remote.
