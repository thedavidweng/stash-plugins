# Stash Plugins Repository

Personal repository of community plugins for **[Stash](https://github.com/stashapp/stash)**, managed and published according to Stash official best practices.

---

## Available Plugins

| Plugin | ID | Description | Docs |
| :--- | :--- | :--- | :--- |
| **TG Drive Backup** | `tgDrive` | Incremental, metadata-preserving backup and disaster recovery connecting Stash to Telegram channels via `tg-drive-cli`. | [README](plugins/tgDrive/README.md) |

---

## How to Install in Stash

1. Open your Stash instance.
2. Go to **Settings** $\rightarrow$ **Plugins** $\rightarrow$ **Available Sources**.
3. Add this source index URL:
   ```
   https://thedavidweng.github.io/stash-plugins/main/index.yml
   ```
4. Navigate to **Available**, find **TG Drive Backup**, and click **Install**.

---

## Key Design Principles

All plugins in this repository strictly adhere to:
- **Zero-Surgery & Provenance**: Remote storage preserves byte-level integrity so native hashes (`oshash`, `pHash`, `MD5`) match upon restore without custom surgery.
- **Human-First Presentation**: Media uploaded to Telegram remains playable in-app with native video presentation (duration, dimensions, streaming flags, cover artwork) and clean UTF-16 budgeted captions.
- **First-Class Auditing**: Physical platform limits (such as Telegram's 2 GB / 4 GB ceiling) are handled with explicit upfront gatekeepers and coverage reports, not silent failures or destructive splitting.

See [plugins/tgDrive/README.md](plugins/tgDrive/README.md) for full architectural trade-offs and disaster recovery procedures.

---

## License

Licensed under [AGPL-3.0](LICENCE).