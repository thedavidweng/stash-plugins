# TG Drive Setup Guide

This guide configures the TG Drive Backup plugin and `tg-drive-cli` (`td`).
It is written for the environment where Stash runs. For Docker, that means
the **container**, not the NAS or host operating system.

## Important security rule

Do not enter any of the following in Stash plugin settings:

- Telegram `api_hash`
- Telegram login codes
- Telegram 2FA passwords
- The contents of `session.json`

The plugin settings only contain paths and backup preferences. Telegram
authentication is completed once from a terminal attached to the Stash
runtime. `td` then reuses its local session file.

## Short version

1. Run **Install / Update TD Core**, or place a matching release archive in
   the plugin directory.
2. Choose a persistent **container-visible** TD Data Directory.
3. Set **TD Core Path** and **TD Data Directory** in the plugin settings.
4. Run **TD Drive Setup Guide** to display the exact commands for this
   installation.
5. From a terminal attached to the Stash runtime, run `td auth setup` and
   `td auth login`.
6. Initialize one `td` drive root for each Stash media root.
7. Run **TD Drive Health Check** in Stash.
8. Start a dry run, then a backup.

## Configure the plugin

Open **Settings -> Plugins -> TG Drive Backup**.

### TD Core Path

This is optional. It accepts:

- An executable path, such as `/plugins/tgDrive/bin/td`
- A Linux/macOS `.tar.gz` or `.tgz` release archive
- A Windows `.zip` release archive
- A path to a binary placed in the plugin directory

If it is empty, the plugin checks, in order:

1. `bin/td` (or `bin/td.exe`) in the plugin directory
2. `td` in the plugin directory
3. A `td` release archive in the plugin directory
4. `td` in `PATH`

The **Install / Update TD Core** task downloads the matching OS/architecture
asset from the `tg-drive-cli` GitHub Release, verifies its SHA-256 value from
that release's `checksums.txt`, and installs it into `bin/`. It runs only when
you explicitly select the task. It does not silently update `td` during a
backup. Set **TD Core Version** to `latest` or to a release tag such as
`v1.2.3`.

If **TD Core Path** is set, the update task does not overwrite that custom
binary. Clear the setting before using the plugin-managed binary.

The configured path and release archive must be visible **inside the Stash
runtime**. The plugin extracts an archive into a private runtime directory,
checks the executable, and reports the resolved path in the health check.

### TD Data Directory

Set this to a persistent directory. The plugin passes these paths to `td`:

```text
<data-directory>/config.toml
<data-directory>/session.json
<data-directory>/local_cache.db
```

If it is empty, the plugin uses its excluded `td-data/` directory. This is
convenient when the Stash plugin directory is already on persistent storage,
but an explicit Docker volume is safer for container migrations.

For Docker and NAS installations, do not leave this in an ephemeral
container directory. Do not put it inside a release zip that you publish.

### Target Telegram Channel

Leave this empty to use the channel bound by `td init`. Set it only when the
same `td` data directory contains more than one channel and you want to
select one explicitly.

## Synology NAS with Stash Docker

### 1. Create a persistent NAS folder

For example, create this folder on the NAS:

```text
/volume1/docker/stash/tg-drive-data
```

Map it into the Stash container, for example:

```text
NAS host:  /volume1/docker/stash/tg-drive-data
Container: /td-data
```

The exact mount point is up to your Docker configuration.

### 2. Use container paths in Stash

In the plugin settings, enter:

```text
TD Data Directory: /td-data
TD Core Path:      /plugins/tgDrive/bin/td
```

Replace the TD Core Path with the actual plugin path in your container. A NAS
host path such as `/volume1/docker/...` is not valid unless that exact path is
also mounted inside the container.

### 3. Open the Stash container terminal

From the NAS host, the command is usually similar to:

```sh
docker exec -it stash sh
```

Replace `stash` with the actual container name. Run the remaining commands
inside the container.

### 4. Authenticate once

Set variables for the paths configured above:

```sh
TD=/plugins/tgDrive/bin/td
TD_DATA=/td-data
mkdir -p "$TD_DATA"
```

Store the Telegram API credentials:

```sh
"$TD" \
  --config "$TD_DATA/config.toml" \
  --session "$TD_DATA/session.json" \
  --db "$TD_DATA/local_cache.db" \
  auth setup
```

Get `api_id` and `api_hash` from
[my.telegram.org/apps](https://my.telegram.org/apps). `auth setup` stores
them in `config.toml`.

Log in:

```sh
"$TD" \
  --config "$TD_DATA/config.toml" \
  --session "$TD_DATA/session.json" \
  --db "$TD_DATA/local_cache.db" \
  auth login
```

Enter the phone number in international format. Enter the code sent by
Telegram and, if enabled, the Telegram 2FA password. This is the only
interactive login step.

Check the session:

```sh
"$TD" \
  --config "$TD_DATA/config.toml" \
  --session "$TD_DATA/session.json" \
  --db "$TD_DATA/local_cache.db" \
  auth status
```

### 5. Initialize the drive root

Use the path that Stash sees inside the container, not the NAS host path. For
example, if Stash sees media at `/data`:

```sh
"$TD" \
  --config "$TD_DATA/config.toml" \
  --session "$TD_DATA/session.json" \
  --db "$TD_DATA/local_cache.db" \
  init /data --create-channel
```

Repeat this for additional Stash media roots if necessary. The channel must
be accessible by the Telegram account used for login. A linked discussion
group is recommended for `td` machine records.

### 6. Verify in Stash

Run **TD Drive Health Check**. It should report:

- a usable `td` binary
- a writable persistent data directory
- an authenticated Telegram session
- passing `td doctor` checks
- an initialized drive root

Run **Dry Run Preview** before the first real backup.

## Other platforms

### Linux

Use the release asset matching the Stash runtime:

```text
td_linux_x86_64.tar.gz
td_linux_arm64.tar.gz
```

The platform and architecture are those of the Stash process. A Linux Docker
container needs a Linux binary even when the host is macOS or Windows.

### macOS

Use the universal macOS release when available. If macOS blocks a manually
downloaded binary, remove the quarantine attribute or install the binary
through the documented Homebrew cask. The plugin itself does not bypass
macOS security controls.

### Windows

Use the matching Windows `.zip` release containing `td.exe`. Enter the
Windows path visible to the Stash process, not a path from another machine.

### Unsupported CPU architectures

If the release does not contain the architecture used by the Stash runtime,
build `td` for that environment or provide a compatible executable through
**TD Core Path**. The health check must be able to execute `td version`.

## Troubleshooting

### `td` is not found

Run **TD Drive Setup Guide** and check the resolved path. Common causes:

- The path is a NAS host path rather than a container path.
- The binary is in `bin/` but has no execute permission.
- The archive does not match the Stash runtime OS and architecture.
- The plugin directory is not the directory you expected.

### Telegram login is required

Run `auth setup` and `auth login` in the Stash container terminal. Do not put
the code in a Stash task argument or plugin setting.

### Login disappears after a container update

The TD Data Directory is not backed by a persistent Docker volume, or the
plugin is using a different container path after the update. Reattach the
same volume and confirm `session.json` exists there.

### The drive root is not initialized

Run `td init` for the Stash-visible media root. The plugin cannot safely
choose a Telegram channel or answer an interactive channel-selection prompt
from a non-interactive Stash task.

### Never expose these files

Treat the following as account credentials:

```text
config.toml
session.json
local_cache.db
```

Keep them in a private persistent volume. Never commit or publish them, and
never upload them as part of the plugin package.
