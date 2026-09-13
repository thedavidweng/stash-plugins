"""
Stash GraphQL API client.
Communicates with Stash server via GraphQL queries and mutations.
Supports API key and SessionCookie authentication.
"""
import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional


class StashClient:
    def __init__(
        self,
        base_url: str = "http://localhost:9999",
        api_key: Optional[str] = None,
        session_cookie: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.graphql_url = f"{self.base_url}/graphql"
        self.api_key = api_key
        self.session_cookie = session_cookie

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["ApiKey"] = self.api_key
        if self.session_cookie:
            headers["Cookie"] = f"session={self.session_cookie}"
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
        q = "{ version { version } }"
        res = self.execute(q)
        return res.get("version", {}).get("version", "unknown")

    def find_scenes(self, page: int = 1, per_page: int = 50, updated_after: Optional[str] = None) -> Dict[str, Any]:
        """Find scenes with files and presentation attributes."""
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
                        fingerprints {{
                            type
                            value
                        }}
                    }}
                }}
            }}
        }}
        """
        res = self.execute(q)
        return res.get("findScenes", {"count": 0, "scenes": []})

    def find_galleries(self, page: int = 1, per_page: int = 50) -> Dict[str, Any]:
        """Find galleries with images and metadata."""
        q = f"""
        {{
            findGalleries(filter: {{ page: {page}, per_page: {per_page} }}) {{
                count
                galleries {{
                    id
                    title
                    date
                    updated_at
                    files {{
                        id
                        path
                        size
                    }}
                }}
            }}
        }}
        """
        res = self.execute(q)
        return res.get("findGalleries", {"count": 0, "galleries": []})

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
        """
        Export complete metadata bundle (scenes, performers, tags, studios)
        to a single JSON file for offsite disaster recovery.
        """
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        # Query overview of objects
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

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)

    def trigger_scan(self, paths: Optional[List[str]] = None) -> None:
        """Trigger Stash metadata scan."""
        q = """
        mutation MetadataScan($input: ScanMetadataInput!) {
            metadataScan(input: $input)
        }
        """
        self.execute(q, {"input": {"paths": paths or []}})
