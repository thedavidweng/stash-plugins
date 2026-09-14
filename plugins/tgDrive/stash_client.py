"""
Stash GraphQL API client with SQLite fallback.
Communicates with Stash server via GraphQL queries and mutations.
Supports API key, SessionCookie, and direct SQLite fallback.
"""
import json
import os
import sqlite3
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional


class StashClient:
    def __init__(
        self,
        base_url: str = "http://localhost:9999",
        api_key: Optional[str] = None,
        session_cookie: Optional[Any] = None,
        sqlite_path: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.graphql_url = f"{self.base_url}/graphql"
        self.api_key = api_key
        self.session_cookie = session_cookie
        self.sqlite_path = sqlite_path

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if self.session_cookie:
            if isinstance(self.session_cookie, dict):
                cookie_val = self.session_cookie.get("Value", "")
            else:
                cookie_val = str(self.session_cookie)
            if cookie_val:
                headers["Cookie"] = f"session={cookie_val}"
        return headers

    def execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        req = urllib.request.Request(self.graphql_url, data=payload, headers=self._headers(), method="POST")

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Stash GraphQL HTTP {e.code}: {err_body}") from e
        except Exception as e:
            raise RuntimeError(f"Stash GraphQL connection error: {e}") from e

        if "errors" in data and data["errors"]:
            msg = data["errors"][0].get("message", "Unknown GraphQL error")
            raise RuntimeError(f"Stash GraphQL error: {msg}")

        return data.get("data", {})

    def get_version(self) -> str:
        try:
            q = "{ version { version } }"
            res = self.execute(q)
            return res.get("version", {}).get("version", "unknown")
        except Exception:
            return "unknown"

    def find_scenes(self, page: int = 1, per_page: int = 50, updated_after: Optional[str] = None) -> Dict[str, Any]:
        """Find scenes with files and presentation attributes, with SQLite fallback."""
        try:
            filter_str = f"page: {page}, per_page: {per_page}, sort: \"updated_at\", direction: ASC"
            crit_str = ""
            if updated_after:
                crit_str = f', scene_filter: {{ updated_at: {{ value: "{updated_after}", modifier: GREATER_THAN }} }}'

            q = f"""
            {{
                findScenes(filter: {{ {filter_str} }}{crit_str}) {{
                    count
                    scenes {{
                        id
                        title
                        code
                        date
                        updated_at
                        paths {{
                            screenshot
                            preview
                            stream
                        }}
                        studio {{
                            id
                            name
                        }}
                        performers {{
                            id
                            name
                        }}
                        tags {{
                            id
                            name
                        }}
                        files {{
                            id
                            path
                            size
                            duration
                            width
                            height
                            video_codec
                            audio_codec
                            frame_rate
                            bit_rate
                        }}
                    }}
                }}
            }}
            """
            res = self.execute(q)
            return res.get("findScenes", {"count": 0, "scenes": []})
        except Exception as e:
            if self.sqlite_path and os.path.exists(self.sqlite_path):
                return self.find_scenes_sqlite(page, per_page, updated_after)
            raise e

    def find_scenes_sqlite(self, page: int = 1, per_page: int = 50, updated_after: Optional[str] = None) -> Dict[str, Any]:
        """Direct SQLite query fallback when GraphQL auth is not configured."""
        offset = (page - 1) * per_page
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        count_query = "SELECT count(*) FROM scenes s JOIN scenes_files sf ON s.id = sf.scene_id"
        total = cur.execute(count_query).fetchone()[0]

        q = """
        SELECT s.id, s.title, s.date, s.code, s.updated_at,
               folders.path || '/' || f.basename AS full_path,
               f.id AS file_id, f.size,
               vf.duration, vf.width, vf.height,
               st.name as studio_name,
               (SELECT GROUP_CONCAT(p.name, ':::') FROM performers_scenes ps JOIN performers p ON ps.performer_id = p.id WHERE ps.scene_id = s.id) as performers_str,
               (SELECT GROUP_CONCAT(t.name, ':::') FROM scenes_tags stg JOIN tags t ON stg.tag_id = t.id WHERE stg.scene_id = s.id) as tags_str
        FROM scenes s
        JOIN scenes_files sf ON s.id = sf.scene_id
        JOIN files f ON sf.file_id = f.id
        JOIN folders ON f.parent_folder_id = folders.id
        JOIN video_files vf ON f.id = vf.file_id
        LEFT JOIN studios st ON s.studio_id = st.id
        ORDER BY s.updated_at ASC
        LIMIT ? OFFSET ?
        """
        rows = cur.execute(q, (per_page, offset)).fetchall()
        scenes = []
        for r in rows:
            performers = [{"name": p} for p in (r["performers_str"] or "").split(":::") if p]
            tags = [{"name": t} for t in (r["tags_str"] or "").split(":::") if t]
            scenes.append({
                "id": str(r["id"]),
                "title": r["title"] or f"Scene {r['id']}",
                "date": r["date"],
                "code": r["code"],
                "updated_at": r["updated_at"],
                "studio": {"name": r["studio_name"]} if r["studio_name"] else None,
                "performers": performers,
                "tags": tags,
                "files": [{
                    "id": r["file_id"],
                    "path": r["full_path"],
                    "size": r["size"],
                    "duration": r["duration"],
                    "width": r["width"],
                    "height": r["height"],
                }],
                "paths": {"screenshot": f"/scene/{r['id']}/screenshot"}
            })
        conn.close()
        return {"count": total, "scenes": scenes}

    def download_image(self, url_or_path: str, dest_path: str) -> bool:
        """Download scene cover or image to a local file."""
        if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
            fetch_url = url_or_path
        else:
            fetch_url = f"{self.base_url}{url_or_path}"

        req = urllib.request.Request(fetch_url, headers=self._headers(), method="GET")
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp, open(dest_path, "wb") as f:
                f.write(resp.read())
            return True
        except Exception:
            return False

    def export_metadata_bundle(self, output_file: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        try:
            q = """
            {
                allPerformers {
                    id
                    name
                    gender
                    birthdate
                    ethnicity
                    country
                    eye_color
                    height_cm
                    measurements
                    fake_tits
                    career_length
                    tattoos
                    piercings
                    aliases
                    favorite
                    details
                }
                allStudios {
                    id
                    name
                    url
                    details
                    rating100
                }
                allTags {
                    id
                    name
                    description
                    aliases
                }
            }
            """
            res = self.execute(q)
            bundle = {
                "version": "stash-tg-drive:v1",
                "exported_at": os.path.basename(output_file),
                "studios": res.get("allStudios", []),
                "performers": res.get("allPerformers", []),
                "tags": res.get("allTags", []),
            }
        except Exception:
            bundle = {"version": "stash-tg-drive:v1", "exported_at": os.path.basename(output_file)}

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)

    def trigger_scan(self, paths: Optional[List[str]] = None) -> None:
        q = """
        mutation MetadataScan($input: ScanMetadataInput!) {
            metadataScan(input: $input)
        }
        """
        self.execute(q, {"input": {"paths": paths or []}})
