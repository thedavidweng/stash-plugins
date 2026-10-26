"""
Offline end-to-end harness: the real plugin driving the real td binary.

No Telegram account, no network: td runs against its fake Telegram client
(TD_FAKE_TELEGRAM=1, login code 12345, state persisted across invocations
via TD_FAKE_TELEGRAM_STATE), and Stash is replaced by an in-process stub
GraphQL server (stub_stash.py). This is #33's testing seam: the full
backup -> verify -> browse -> restore journey runs exactly as in
production, only the two remote sides are stubbed.

Opt-in (the default test run skips this file):
  TD_E2E_BIN=/path/to/td            use a prebuilt td binary
  TD_E2E_SOURCE=/path/to/tg-drive
                                     build td once per process with `go
                                     build ./cmd/td` (requires the Go
                                     toolchain and a warm module cache)
"""
import atexit
import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stub_stash import StubStash

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(PLUGIN_DIR, "tg_drive.py")

_TD_BUILD: str = ""


def _resolve_td() -> str:
    """Locate or build the td binary; empty string when unavailable."""
    global _TD_BUILD
    binary = os.environ.get("TD_E2E_BIN", "")
    if binary and os.path.isfile(binary):
        return binary
    source = os.environ.get("TD_E2E_SOURCE", "")
    if source and os.path.isdir(os.path.join(source, "cmd", "td")):
        if _TD_BUILD and os.path.isfile(_TD_BUILD):
            return _TD_BUILD
        build_dir = tempfile.mkdtemp(prefix="td-e2e-build-")
        target = os.path.join(build_dir, "td")
        result = subprocess.run(
            ["go", "build", "-o", target, "./cmd/td"],
            cwd=source, capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            shutil.rmtree(build_dir, ignore_errors=True)
            raise RuntimeError(f"go build failed: {result.stderr}")
        _TD_BUILD = target
        atexit.register(lambda: shutil.rmtree(build_dir, ignore_errors=True))
        return target
    return ""


def _sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class OfflineE2E(unittest.TestCase):
    """
    One sequential journey: td setup -> health -> archive backup (scenes,
    folder gallery, zip gallery, standalone image, artifacts) -> remote
    hash audit -> incremental rerun -> browse projection -> disaster
    recovery restore with byte-exact comparison.
    """

    @classmethod
    def setUpClass(cls):
        cls.td = _resolve_td()
        if not cls.td:
            raise unittest.SkipTest(
                "offline e2e is opt-in: set TD_E2E_BIN or TD_E2E_SOURCE"
            )

    def setUp(self):
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("TD_FAKE_TELEGRAM", "TD_FAKE_TELEGRAM_STATE")
        }

        self.work = tempfile.mkdtemp(prefix="tg-drive-e2e-")
        self.media_root = os.path.join(self.work, "media")
        self.td_data = os.path.join(self.work, "td-data")
        self.fake_state = os.path.join(self.work, "fake-telegram-state.json")
        self.restore_dir = os.path.join(self.work, "restore")
        os.makedirs(self.td_data, exist_ok=True)

        os.environ["TD_FAKE_TELEGRAM"] = "1"
        os.environ["TD_FAKE_TELEGRAM_STATE"] = self.fake_state

        self.stub = self._build_library()
        self.addCleanup(self.stub.stop)

    def tearDown(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.work, ignore_errors=True)

    # ------------------------------------------------------------ fixture

    def _write(self, rel_path: str, payload: bytes) -> str:
        path = os.path.join(self.media_root, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    @staticmethod
    def _md5(payload: bytes) -> str:
        return hashlib.md5(payload).hexdigest()

    @staticmethod
    def _fingerprints(payload: bytes) -> list:
        return [{"type": "md5", "value": hashlib.md5(payload).hexdigest()}]

    def _build_library(self) -> StubStash:
        scene_payload = b"SCENE-BYTES-" + b"0123456789" * 40
        scene_path = self._write("videos/scene1.mp4", scene_payload)
        gallery_dir = os.path.join(self.media_root, "gals", "trip")
        member_payloads = {
            name: f"GALLERY-{name}".encode() + b"0123456789"
            for name in ("a.jpg", "b.jpg", "c.jpg")
        }
        member_paths = {
            name: self._write(f"gals/trip/{name}", payload)
            for name, payload in member_payloads.items()
        }
        zip_bytes = b"PK-STUB-ZIP-GALLERY-BYTES"
        zip_path = self._write("gals/packed.zip", zip_bytes)
        poster_bytes = b"POSTER-BYTES-0123456789"
        poster_path = self._write("pics/poster.jpg", poster_bytes)
        zip_member_bytes = {
            "7": b"ZIP-MEMBER-01-BYTES",
            "8": b"ZIP-MEMBER-02-BYTES",
        }
        # Stub files carry md5 fingerprints (like a scanned Stash library),
        # so the plugin's content-change detection has a real signal.
        self.scene_payload = scene_payload
        self.member_payloads = member_payloads
        self.poster_bytes = poster_bytes
        self.zip_member_bytes = zip_member_bytes

        def image(image_id, path, galleries, size, payload=None, title=None, **extra):
            visual_file = {
                "id": 900 + image_id, "path": path, "size": size,
                "width": 800, "height": 600,
            }
            if payload is not None:
                visual_file["fingerprints"] = self._fingerprints(payload)
            record = {
                "id": str(image_id),
                "title": title or f"Image {image_id}",
                "code": None,
                "date": None,
                "studio": None,
                "performers": [],
                "tags": [],
                "visual_files": [visual_file],
                "paths": {"image": f"/image/{image_id}/image"},
                "galleries": [{"id": str(g)} for g in galleries],
            }
            record.update(extra)
            return record

        images = [
            image(1, member_paths["a.jpg"], [1], len(member_payloads["a.jpg"]), payload=member_payloads["a.jpg"]),
            image(2, member_paths["b.jpg"], [1], len(member_payloads["b.jpg"]), payload=member_payloads["b.jpg"]),
            image(3, member_paths["c.jpg"], [1], len(member_payloads["c.jpg"]), payload=member_payloads["c.jpg"]),
            # zip members: virtual paths inside the zip, bytes via HTTP
            image(7, os.path.join(zip_path, "01.jpg"), [3], len(zip_member_bytes["7"]), payload=zip_member_bytes["7"]),
            image(8, os.path.join(zip_path, "02.jpg"), [3], len(zip_member_bytes["8"]), payload=zip_member_bytes["8"]),
            image(42, poster_path, [], len(poster_bytes), payload=poster_bytes, title="Poster"),
        ]
        scenes = [{
            "id": "1", "title": "E2E Scene", "code": "E2E-1",
            "date": "2026-01-02", "studio": None, "performers": [], "tags": [],
            "paths": {"screenshot": "/scene/1/screenshot"},
            "files": [{
                "id": 101, "path": scene_path, "size": len(scene_payload),
                "duration": 120.0, "width": 1920, "height": 1080,
                "fingerprints": self._fingerprints(scene_payload),
            }],
        }]
        galleries = [
            {
                "id": "1", "title": "Trip Gallery", "code": None, "date": "2026-02-03",
                "image_count": 3, "studio": None, "performers": [], "tags": [],
                "folder": {"path": gallery_dir}, "files": [],
            },
            {
                "id": "3", "title": "Packed Gallery", "code": None, "date": None,
                "image_count": 2, "studio": None, "performers": [], "tags": [],
                "folder": None,
                "files": [{
                    "id": 55, "path": zip_path, "size": len(zip_bytes),
                    "fingerprints": self._fingerprints(zip_bytes),
                }],
            },
        ]
        return StubStash(scenes, images, galleries, zip_member_bytes).start()

    def _mutate_library(self):
        """Simulate a Stash rescan after scene/gallery-member bytes changed."""
        new_scene = b"SCENE-RESCAN-" + b"9999999999" * 50
        with open(os.path.join(self.media_root, "videos", "scene1.mp4"), "wb") as handle:
            handle.write(new_scene)
        self.stub.scenes[0]["files"][0]["size"] = len(new_scene)
        self.stub.scenes[0]["files"][0]["fingerprints"] = self._fingerprints(new_scene)

        new_member = b"GALLERY-SWAPPED-BYTES"
        with open(os.path.join(self.media_root, "gals", "trip", "a.jpg"), "wb") as handle:
            handle.write(new_member)
        image_1 = next(i for i in self.stub.images if i["id"] == "1")
        image_1["visual_files"][0]["size"] = len(new_member)
        image_1["visual_files"][0]["fingerprints"] = self._fingerprints(new_member)

    def _remove_gallery_member(self):
        """Simulate deleting one gallery image in Stash."""
        self.stub.images = [i for i in self.stub.images if i["id"] != "3"]
        self.stub.galleries[0]["image_count"] = 2

    def _seed_unmanaged_message(self, file_name="legacy-clip.mp4", payload=b"LEGACY-CLIP-BYTES-0123456789"):
        """Inject an already-posted (unmanaged) message into fake Telegram."""
        with open(self.fake_state) as handle:
            state = json.load(handle)
        channels = state.get("channels") or {}
        self.assertTrue(channels, "fake state has no channels after td init")
        channel_id = next(iter(channels))
        import base64
        message = {
            "ID": 9001,
            "Text": "",
            "Caption": "",
            "FileName": file_name,
            "FileSize": len(payload),
            "MIME": "video/mp4",
            "Kind": "document",
            "Data": base64.b64encode(payload).decode("ascii"),
            "ReplyTo": None,
            "NotEditable": False,
            "Date": "2026-01-01T00:00:00Z",
            "Forward": None,
            "SavedPeerID": 0,
            "SavedPeerTitle": "",
            "GroupedID": 0,
            "Video": None,
            "Thumb": None,
        }
        state.setdefault("messages", {}).setdefault(channel_id, []).append(message)
        state["next_id"] = max(state.get("next_id", 0), 100000)
        with open(self.fake_state, "w") as handle:
            json.dump(state, handle)
        return payload

    # -------------------------------------------------------------- runners

    def _td(self, *args, stdin=None, check=True):
        command = [
            self.td, *args,
            "--config", os.path.join(self.td_data, "config.toml"),
            "--session", os.path.join(self.td_data, "session.json"),
            "--db", os.path.join(self.td_data, "local_cache.db"),
        ]
        result = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=300)
        if check:
            self.assertEqual(
                result.returncode, 0,
                f"td {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}",
            )
        return result

    def _td_json(self, *args):
        result = self._td(*args, "--json")
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope.get("ok"), f"td error envelope: {result.stdout}")
        return envelope.get("data") or {}

    def _plugin(self, *args, channel: Optional[str] = None):
        """Invoke the real plugin; channel defaults to the fake drive id."""
        if channel is None:
            channel = self._drive_channel_id()
        command = [
            sys.executable, PLUGIN, *args,
            "--server-url", self.stub.url,
            "--td-path", self.td,
            "--td-data-dir", self.td_data,
            "--channel", channel,
            "--max-size-gb", "2",
            "--batch-size", "0",
        ]
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=900
        )
        self.assertEqual(
            result.returncode, 0,
            f"plugin {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}",
        )
        return json.loads(result.stdout)["output"]

    def _ledger_path(self) -> str:
        """The ledger file for the channel scope the e2e runs under."""
        return os.path.join(
            self.td_data, f"ledger-{self._drive_channel_id()}.sqlite"
        )

    def _ledger_rows(self):
        db = self._ledger_path()
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT canonical_path, kind, status, remote_path, blake3, message_id "
                "FROM ledger_items ORDER BY kind, canonical_path"
            )]

    def _remote_files(self):
        data = self._td_json("tree", "/", "--depth", "64")
        found = {}

        def walk(nodes):
            for node in nodes or []:
                if node.get("type") == "file":
                    found[node["path"]] = node.get("path")
                walk(node.get("children") or [])

        walk(data.get("tree") or [])
        return set(found)

    # ------------------------------------------- caption / manifest contract

    def _fake_state(self) -> dict:
        with open(self.fake_state) as handle:
            return json.load(handle)

    def _drive_and_discussion_ids(self):
        """(drive channel id, linked discussion group id) from fake state."""
        state = self._fake_state()
        discussion = state.get("discussion") or {}
        self.assertTrue(discussion, "fake state has no linked discussion group")
        drive_id, discussion_id = next(iter(discussion.items()))
        return str(drive_id), str(discussion_id)

    def _drive_channel_id(self) -> str:
        """The fake Telegram channel id the td drive is bound to."""
        return self._drive_and_discussion_ids()[0]

    @staticmethod
    def _b64(value: str) -> str:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")

    def _manifest_comments(self):
        """Parse td-manifest:v1 / td-album:v1 comments from the discussion group."""
        _, discussion_id = self._drive_and_discussion_ids()
        manifests, albums = [], []
        for message in self._fake_state().get("messages", {}).get(discussion_id, []):
            text = message.get("Text") or ""
            if text.startswith("td-manifest:v1"):
                kv = {}
                for line in text.split("\n")[1:]:
                    if "=" in line:
                        key, value = line.split("=", 1)
                        kv[key] = value
                manifests.append({
                    "path": self._b64(kv["p"]) if kv.get("p") else None,
                    "display_name": self._b64(kv["n"]) if kv.get("n") else None,
                    "hash": kv.get("hash"),
                    "mime": kv.get("mime"),
                    "tags": kv.get("tags"),
                })
            elif text.startswith("td-album:v1"):
                members = []
                for line in text.split("\n")[2:]:
                    if not line.strip():
                        continue
                    parts = line.split(" ")
                    kv = {"message_id": parts[0]}
                    for part in parts[1:]:
                        if "=" in part:
                            key, value = part.split("=", 1)
                            kv[key] = value
                    members.append({
                        "message_id": kv["message_id"],
                        "path": self._b64(kv["p"]) if kv.get("p") else None,
                        "hash": kv.get("h"),
                        "mime": kv.get("m"),
                    })
                albums.append({"members": members})
        return manifests, albums

    def _message_caption(self, message_id: int) -> str:
        drive_id, _ = self._drive_and_discussion_ids()
        for message in self._fake_state().get("messages", {}).get(drive_id, []):
            if message.get("ID") == message_id:
                return message.get("Caption") or ""
        self.fail(f"drive message {message_id} not found in fake state")

    def _exercise_caption_repair(self, ledger_rows, remote_path: str):
        """
        Seed the pre-v0.2.1 caption scaffold (display name + parent path
        line + #td_* chain) on one upload, then verify the documented
        cleanup: `td repair --captions --dry-run` plans the edit without
        touching Telegram, and `--continue-on-error` strips the scaffold
        while the discussion-thread machine record stays intact.
        """
        row = next(r for r in ledger_rows if r["remote_path"] == remote_path)
        target_id = int(row["message_id"])
        display = os.path.basename(remote_path)
        parent_human = os.path.dirname(remote_path).lstrip("/")
        manifests, _ = self._manifest_comments()
        manifest = next(m for m in manifests if m["path"] == remote_path)
        tags = (manifest["tags"] or "").split()
        self.assertTrue(tags, manifest)
        scaffold = f"{display}\n{parent_human}/\n\n" + "\n".join(tags)

        state = self._fake_state()
        drive_id, _ = self._drive_and_discussion_ids()
        for message in state["messages"][drive_id]:
            if message.get("ID") == target_id:
                message["Caption"] = scaffold
        with open(self.fake_state, "w") as handle:
            json.dump(state, handle)
        self.assertEqual(self._message_caption(target_id), scaffold)

        dry = self._td_json("repair", "--captions", "/stash-browse", "--dry-run")
        self.assertEqual(dry["dry_run"], True)
        self.assertGreaterEqual(dry["planned"], 1, dry)
        self.assertEqual(dry["cleaned"], 0, dry)
        self.assertIn(
            "would_clean", [item.get("action") for item in dry.get("items", [])], dry
        )
        # dry run must not have edited anything yet
        self.assertEqual(self._message_caption(target_id), scaffold)

        done = self._td_json("repair", "--captions", "/stash-browse", "--continue-on-error")
        self.assertGreaterEqual(done["cleaned"], 1, done)
        self.assertEqual(self._message_caption(target_id), display)
        # the machine record is untouched by caption cleanup
        after_manifests, _ = self._manifest_comments()
        after = next(m for m in after_manifests if m["path"] == remote_path)
        self.assertEqual(after, manifest)

    def _assert_caption_and_manifest_contract(self, ledger_rows):
        """
        v0.2.1 caption contract over the live fake Telegram state.

        - New captions are human-only: exactly the display name (the remote
          basename), never the remote path and never a path-derived #td_*
          chain.
        - Machine metadata still lives in the discussion thread: every
          published ledger item is covered by a td-manifest:v1 / td-album:v1
          comment carrying the canonical path, hash, and MIME; single-file
          manifests keep the complete tag chain (ADR 0021).
        """
        drive_id, _ = self._drive_and_discussion_ids()
        remote_by_message = {}
        for row in ledger_rows:
            if row.get("message_id"):
                remote_by_message.setdefault(int(row["message_id"]), row["remote_path"])

        # artifact slots are recorded in the ledger without message ids, but
        # their captions are still plain display names
        artifact_basenames = {
            os.path.basename("/stash-metadata/export.zip"),
            os.path.basename("/stash-backup/database/stash-backup.zip"),
        }

        for message in self._fake_state().get("messages", {}).get(drive_id, []):
            caption = message.get("Caption") or ""
            if not caption:
                continue
            self.assertNotIn("#td_", caption, caption)
            self.assertNotIn("/stash-", caption, caption)
            remote_path = remote_by_message.get(message.get("ID"))
            if remote_path:
                self.assertNotIn(remote_path, caption, caption)
                # v0.2.1 renders the display name only; caption == remote basename.
                self.assertEqual(caption, os.path.basename(remote_path), caption)
            else:
                self.assertIn(caption, artifact_basenames, f"caption without a ledger row: {caption!r}")

        manifests, albums = self._manifest_comments()
        covered = {m["path"] for m in manifests if m["path"]}
        covered |= {
            member["path"]
            for album in albums
            for member in album["members"]
            if member["path"]
        }

        published = [
            row for row in ledger_rows
            if row["status"] == "published" and row["message_id"] and row["remote_path"]
        ]
        for row in published:
            self.assertIn(row["remote_path"], covered, row)

        ledger_by_path = {row["remote_path"]: row for row in published}
        for manifest in manifests:
            self.assertTrue(manifest["path"], manifest)
            self.assertIn("hash", manifest)
            self.assertIn("mime", manifest)
            # drive uploads keep the full tag chain; td's adopt manifest
            # reconstruct carries path/hash/mime only
            if not manifest["path"].startswith("/adopted/"):
                self.assertTrue(manifest.get("tags"), manifest)
            row = ledger_by_path.get(manifest["path"])
            if row and row.get("blake3") and manifest.get("hash") not in (None, "-"):
                self.assertEqual(manifest["hash"], row["blake3"], manifest["path"])
        for album in albums:
            self.assertTrue(album["members"], album)
            for member in album["members"]:
                self.assertTrue(member["path"], member)
                self.assertIn("hash", member)
                self.assertIn("mime", member)

        # artifact slots keep their machine records too
        for artifact in ("/stash-metadata/export.zip", "/stash-backup/database/stash-backup.zip"):
            self.assertIn(artifact, covered)

    # ------------------------------------------------------------- the flow

    def test_full_offline_journey(self):
        # td config is seeded directly (phone included) so `auth login`
        # reads exactly one stdin line: piped input over-reads when a login
        # command has to prompt for the phone first.
        config_path = os.path.join(self.td_data, "config.toml")
        with open(config_path, "w") as handle:
            handle.write(
                "[telegram]\n"
                'api_id = 1\n'
                'api_hash = "e2e-api-hash"\n'
                'phone = "+15550001111"\n'
            )
        self._td("auth", "login", stdin="12345\n")
        self._td("init", self.media_root, "--create-channel")

        health = self._plugin("--mode", "health")
        self.assertTrue(health["health"]["ready"], health["health"])
        self.assertEqual(health["health"]["checks"]["version"]["status"], "pass")

        # --- archive backup: full coverage across every media type
        backup = self._plugin("--mode", "backup")
        self.assertEqual(backup["status"], "ok", backup)
        report = backup["report"]
        self.assertEqual(report["scenes_total"], 1)
        self.assertEqual(report["scenes_published"], 1)
        self.assertEqual(report["galleries_total"], 2)
        self.assertEqual(report["galleries_published"], 2)
        self.assertEqual(report["images_total"], 1)
        self.assertEqual(report["images_published"], 1)
        self.assertEqual(report["coverage_objects_percent"], 100.0)
        self.assertEqual(report["coverage_bytes_percent"], 100.0)
        self.assertEqual(report["skipped_details"], [])
        self.assertEqual(report["failed_details"], [])

        # every media unit is in the ledger with a remote path and hash
        rows = {row["canonical_path"]: row for row in self._ledger_rows()}
        expected_kinds = {
            "scene:archive": 1,
            "image:archive": 4,   # 3 folder members + 1 standalone
            "gallery:archive": 1,  # the zip itself
            "metadata": 1,
            "database": 1,
        }
        by_kind = {}
        for row in rows.values():
            if row["status"] == "published":
                by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        self.assertEqual(by_kind, expected_kinds)
        # media rows carry a hash and message id; artifacts only a remote path
        media_kinds = ("scene:", "image:", "gallery:")
        for row in rows.values():
            if row["status"] != "published":
                continue
            self.assertTrue(row["remote_path"], row)
            if row["kind"].startswith(media_kinds):
                self.assertTrue(row["blake3"], row)
                self.assertTrue(row["message_id"], row)

        # remote mirrors the library paths byte-exactly
        remote = self._remote_files()
        scene_remote = os.path.join(self.media_root, "videos/scene1.mp4")
        self.assertIn(scene_remote, remote)
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.assertIn(os.path.join(self.media_root, "gals/trip", name), remote)
        zip_remote = os.path.join(self.media_root, "gals/packed.zip")
        self.assertIn(zip_remote, remote)
        self.assertIn(os.path.join(self.media_root, "pics/poster.jpg"), remote)
        self.assertIn("/stash-metadata/export.zip", remote)
        self.assertIn("/stash-backup/database/stash-backup.zip", remote)

        # v0.2.1 caption contract: captions are human-only (display name,
        # no remote path, no #td_* chain) and every published item keeps a
        # complete machine record in the discussion thread
        self._assert_caption_and_manifest_contract(self._ledger_rows())

        # --- remote hash audit: ledger vs drive, no downloads
        verify = self._plugin("--mode", "verify")
        self.assertEqual(verify["status"], "ok")
        audit = verify["remote_audit"]
        self.assertEqual(audit["status"], "ok", audit)
        # artifacts are recorded without a ledger hash, so they fall into
        # ledger_without_hash; every hash-carrying published row verifies
        self.assertEqual(
            audit["verified"],
            sum(expected_kinds.values()) - audit["ledger_without_hash"]["count"],
        )
        self.assertEqual(audit["missing_remote"]["count"], 0)
        self.assertEqual(audit["hash_mismatch"]["count"], 0)
        self.assertEqual(audit["remote_extras"]["count"], 0)

        # --- incremental rerun publishes nothing new
        before = self._remote_files()
        second = self._plugin("--mode", "backup")
        self.assertEqual(second["report"]["scenes_published"], 1)
        self.assertEqual(second["report"]["galleries_published"], 2)
        self.assertEqual(second["report"]["images_published"], 1)
        self.assertEqual(self._remote_files(), before)

        # --- browse projection: photos, albums, and a streamable video
        browse = self._plugin("--mode", "backup", "--media-upload-mode", "browse")
        browse_report = browse["report"]
        self.assertEqual(browse_report["galleries_published"], 2, browse_report)
        self.assertEqual(browse_report["images_published"], 1)
        self.assertEqual(browse_report["scenes_published"], 1)

        # the folder gallery published as one native photo album
        album_entries = self._td_json("ls", "/stash-browse/gallery/1").get("entries") or []
        album_files = {e["name"] for e in album_entries if e.get("type") == "file"}
        self.assertEqual(len(album_files), 3)
        self.assertTrue(any("Trip Gallery" in name for name in album_files), album_files)

        # the zip gallery published from bytes fetched out of Stash
        zip_album = self._td_json("ls", "/stash-browse/gallery/3").get("entries") or []
        self.assertEqual(len([e for e in zip_album if e.get("type") == "file"]), 2)

        # browse is excluded from the archive audit's expectations but still
        # hash-verifiable (td stores the original upload's hash)
        browse_verify = self._plugin("--mode", "verify")
        self.assertEqual(browse_verify["remote_audit"]["status"], "ok")
        self.assertGreater(
            browse_verify["remote_audit"]["verified"], sum(expected_kinds.values())
        )

        # --- content-change detection: changed bytes at unchanged paths are
        # re-published (single-file cp --replace), then the audit stays green
        self._mutate_library()
        redo = self._plugin("--mode", "backup")
        redo_report = redo["report"]
        self.assertEqual(redo_report["scenes_updated"], 1, redo)
        self.assertEqual(redo_report["images_updated"], 1, redo)
        self.assertEqual(redo_report["scenes_published"], 1)
        self.assertEqual(redo_report["images_published"], 1)
        self.assertEqual(redo_report["galleries_updated"], 0, redo)
        after_redo = self._plugin("--mode", "verify")
        self.assertEqual(after_redo["remote_audit"]["status"], "ok", after_redo)
        self.assertEqual(after_redo["remote_audit"]["hash_mismatch"]["count"], 0)
        # re-published items (scene --replace, album member rm + fresh slot)
        # still honor the same caption/manifest contract
        self._assert_caption_and_manifest_contract(self._ledger_rows())

        # --- deletion reporting: a gallery image removed in Stash is
        # reported by Verify, never touched on Telegram
        self._remove_gallery_member()
        deletions = self._plugin("--mode", "verify")
        self.assertEqual(deletions["stash_deletions"]["status"], "deleted")
        deleted_c = os.path.join(self.media_root, "gals/trip/c.jpg")
        self.assertIn(
            deleted_c, deletions["stash_deletions"]["deleted"]["image"]["sample"]
        )

        # --- legacy caption cleanup: a pre-v0.2.1 scaffold caption is
        # stripped by `td repair --captions` (dry-run then apply) while the
        # discussion-thread machine record stays intact
        browse_scene_path = next(
            r["remote_path"] for r in self._ledger_rows()
            if r["kind"] == "scene:browse"
        )
        self._exercise_caption_repair(self._ledger_rows(), browse_scene_path)

        # --- adopt: an already-posted channel message joins the index (td
        # adopt, no bytes move) and the ledger records it from the drive
        adopted_payload = self._seed_unmanaged_message()
        adopt = self._plugin("--mode", "adopt")
        self.assertEqual(adopt["status"], "ok", adopt)
        # td routes adopted media by kind (video -> .../videos/) and counts
        # the post-adopt manifest reconstruction as an adopt
        adopted_remote = "/adopted/videos/legacy-clip.mp4"
        self.assertGreaterEqual(adopt["adopt"]["adopted"], 1, adopt)
        self.assertEqual(adopt["ledger_backfill"]["rows_added"], 1, adopt)
        self.assertIn(adopted_remote, self._remote_files())
        adopted_row = {
            row["canonical_path"]: row for row in self._ledger_rows()
        }[adopted_remote]
        self.assertEqual(adopted_row["kind"], "adopted:archive")
        self.assertTrue(adopted_row["blake3"], adopted_row)
        # td adopt never rewrites the original message's caption
        drive_id, _ = self._drive_and_discussion_ids()
        seeded_message = next(
            m for m in self._fake_state().get("messages", {}).get(drive_id, [])
            if m.get("ID") == 9001
        )
        self.assertEqual(seeded_message.get("Caption", ""), "")
        self.assertEqual(seeded_message.get("FileName"), "legacy-clip.mp4")
        final_verify = self._plugin("--mode", "verify")
        self.assertEqual(final_verify["remote_audit"]["status"], "ok")
        # the adopted row now audits without being an unaccounted extra
        self.assertEqual(final_verify["remote_audit"]["remote_extras"]["count"], 0)
        self.assertEqual(final_verify["stash_deletions"]["status"], "deleted")
        # the whole journey (mutations, album re-publish, adopt) keeps the
        # caption/manifest contract intact: nothing that captions lost leaks
        # and no machine metadata was dropped
        self._assert_caption_and_manifest_contract(self._ledger_rows())

        # --- disaster recovery restore: byte-exact media rebuild
        restore = self._plugin("--mode", "restore", "--restore-dir", self.restore_dir)
        self.assertEqual(restore["status"], "ok", restore)
        restore_report = restore["report"]
        self.assertEqual(restore_report["stash_scan"]["status"], "FINISHED")
        self.assertEqual(restore_report["metadata_import"]["status"], "FINISHED")
        self.assertEqual(restore_report["media"]["status"], "ok", restore_report)

        originals = {
            "videos/scene1.mp4": scene_remote,
            "gals/trip/a.jpg": os.path.join(self.media_root, "gals/trip/a.jpg"),
            "gals/trip/b.jpg": os.path.join(self.media_root, "gals/trip/b.jpg"),
            "gals/trip/c.jpg": os.path.join(self.media_root, "gals/trip/c.jpg"),
            "gals/packed.zip": zip_remote,
            "pics/poster.jpg": os.path.join(self.media_root, "pics/poster.jpg"),
        }
        for rel, original in originals.items():
            # remote paths mirror absolute library paths, so the restored
            # tree nests under restore_dir/<absolute path>
            restored = os.path.join(self.restore_dir, original.lstrip("/"))
            self.assertTrue(os.path.isfile(restored), f"missing restored {rel}")
            self.assertEqual(
                _sha256(restored), _sha256(original),
                f"restored bytes differ for {rel}",
            )

        # the adopted root restores alongside the media roots
        adopted_restored = os.path.join(self.restore_dir, "adopted", "videos", "legacy-clip.mp4")
        self.assertTrue(os.path.isfile(adopted_restored), "missing adopted restore")
        self.assertEqual(_sha256(adopted_restored), _sha256_bytes(adopted_payload))

        # recovery artifacts landed for manual application
        recovery_db = os.path.join(self.restore_dir, "stash-recovery", "database", "stash-backup.zip")
        self.assertTrue(os.path.isfile(recovery_db))
        with zipfile.ZipFile(recovery_db) as bundle:
            self.assertEqual(bundle.namelist(), ["stash-go.sqlite"])

        # the stub saw the scan paths and the metadata import upload
        self.assertTrue(self.stub.scan_paths)
        self.assertEqual(len(self.stub.imports), 1)
        self.assertIn("importObjects", self.stub.imports[0])

        # service roots never leak into the restored library
        for root in ("stash-browse", "stash-metadata", "stash-backup"):
            self.assertFalse(os.path.exists(os.path.join(self.restore_dir, root)))


if __name__ == "__main__":
    unittest.main()
