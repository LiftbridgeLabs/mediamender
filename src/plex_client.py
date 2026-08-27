import time

import requests
import defusedxml.ElementTree as ET
from typing import Optional, List, Dict

from src.version import __version__
from src.branding import PRODUCT_NAME, PRODUCT_SLUG


# Plex media type IDs
_MOVIE_TYPES = [1]        # movie
_TV_TYPES    = [2, 3, 4]  # show, season, episode

# Human-readable labels for type IDs
_TYPE_LABELS = {
    1: "movie",
    2: "show",
    3: "season",
    4: "episode",
}


def trash_item_key(item: Dict) -> tuple:
    """Return the most stable identity available for a Plex trash item."""
    rating_key = str(item.get("rating_key", ""))
    if rating_key:
        return ("rating", rating_key)
    return (
        "composite",
        item.get("media_type_id", ""),
        item.get("type", ""),
        item.get("title", ""),
        item.get("year", ""),
        item.get("index", ""),
        item.get("parent_title", ""),
        item.get("parent_index", ""),
        item.get("grandparent_title", ""),
    )


def _deleted_item(element, type_id: int, deleted_at: int, media=None) -> Dict:
    return {
        "title":             element.get("title", "Unknown"),
        "year":              element.get("year", ""),
        "type":              _TYPE_LABELS.get(type_id, "item"),
        "deleted_at":        deleted_at,
        "media_type_id":     type_id,
        "index":             element.get("index", ""),
        "parent_title":      element.get("parentTitle", ""),
        "parent_index":      element.get("parentIndex", ""),
        "grandparent_title": element.get("grandparentTitle", ""),
        "rating_key":        element.get("ratingKey", ""),
        "media_id":          media.get("id", "") if media is not None else "",
    }


def _append_unique(items: List[Dict], seen: set, candidate: Dict) -> None:
    key = trash_item_key(candidate)
    if key not in seen:
        items.append(candidate)
        seen.add(key)


class PlexClient:
    def __init__(self, url: str, token: str):
        self.url   = url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "X-Plex-Token":             token,
            "X-Plex-Product":           PRODUCT_NAME,
            "X-Plex-Version":           __version__,
            "X-Plex-Client-Identifier": PRODUCT_SLUG,
            "Accept":                   "application/json",
        })
        self._sections_cache: List[Dict] | None = None
        self._sections_cached_at = 0.0

    def _get(self, path: str, params: dict = None, timeout: int = 15):
        return self.session.get(f"{self.url}{path}", params=params, timeout=timeout)

    @staticmethod
    def _metadata(response) -> List[Dict]:
        container = response.json().get("MediaContainer", {})
        return list(container.get("Metadata", [])) or [
            *container.get("Directory", []), *container.get("Video", []),
        ]

    def list_tv_shows(self, section_id: str) -> List[Dict]:
        return self.list_tv_shows_page(section_id, 0, 100000)["shows"]

    def list_tv_shows_page(self, section_id: str, start: int = 0,
                           size: int = 24) -> Dict:
        """Return one Plex-native page of shows without loading the library."""
        response = self._get(
            f"/library/sections/{section_id}/all",
            params={"type": 2, "sort": "titleSort:asc",
                    "X-Plex-Container-Start": max(0, int(start)),
                    "X-Plex-Container-Size": max(1, min(int(size), 100000))},
            timeout=30,
        )
        response.raise_for_status()
        container = response.json().get("MediaContainer", {})
        items = self._metadata(response)
        shows = [{
            "rating_key": str(item.get("ratingKey", "")),
            "title": str(item.get("title", "Unknown")),
            "year": item.get("year"),
            "thumb": str(item.get("thumb", "")),
            "leaf_count": int(item.get("leafCount", 0) or 0),
            "viewed_leaf_count": int(item.get("viewedLeafCount", 0) or 0),
        } for item in items if item.get("ratingKey")]
        total = int(
            container.get("totalSize", container.get("totalLeafCount", len(shows)))
            or len(shows)
        )
        return {"shows": shows, "total": total, "start": max(0, int(start))}

    def list_show_seasons(self, show_rating_key: str) -> List[Dict]:
        response = self._get(f"/library/metadata/{show_rating_key}/children", timeout=30)
        response.raise_for_status()
        return [{
            "rating_key": str(item.get("ratingKey", "")),
            "index": int(item.get("index", 0) or 0),
            "title": str(item.get("title", "Season")),
            "thumb": str(item.get("thumb", "")),
            "leaf_count": int(item.get("leafCount", 0) or 0),
            "viewed_leaf_count": int(item.get("viewedLeafCount", 0) or 0),
        } for item in self._metadata(response)
          if item.get("type") == "season" and item.get("ratingKey")]

    def find_episode(self, section_id: str, show_title: str,
                     season: int, episode: int) -> Optional[Dict]:
        response = self._get(
            f"/library/sections/{section_id}/all",
            params={"type": 4, "grandparentTitle": show_title,
                    "parentIndex": int(season), "index": int(episode)},
            timeout=30,
        )
        response.raise_for_status()
        expected = show_title.strip().casefold()
        for item in self._metadata(response):
            if str(item.get("grandparentTitle", "")).strip().casefold() != expected:
                continue
            if int(item.get("parentIndex", -1)) != int(season):
                continue
            if int(item.get("index", -1)) != int(episode):
                continue
            rating_key = str(item.get("ratingKey", ""))
            show_key = str(item.get("grandparentRatingKey", ""))
            if rating_key and show_key:
                return {
                    "rating_key": rating_key,
                    "show_rating_key": show_key,
                    "season_rating_key": str(item.get("parentRatingKey", "")),
                    "season_index": int(season),
                    "episode_index": int(episode),
                    "title": str(item.get("title", "")),
                }
        return None

    def _scrobble_endpoint(self) -> tuple[str, str]:
        response = self._get("/media/providers")
        response.raise_for_status()
        container = response.json().get("MediaContainer", {})
        providers = container.get("MediaProvider", container.get("Metadata", []))
        for provider in providers:
            identifier = str(provider.get("identifier", ""))
            for feature in provider.get("Feature", []):
                endpoint = feature.get("scrobbleKey")
                if feature.get("type") == "timeline" and endpoint:
                    return str(endpoint), identifier or "com.plexapp.plugins.library"
        raise RuntimeError("Plex did not advertise a scrobble endpoint")

    def mark_watched(self, rating_key: str) -> None:
        endpoint, identifier = self._scrobble_endpoint()
        response = self._get(endpoint, params={
            "key": str(rating_key), "identifier": identifier,
        })
        response.raise_for_status()

    def get_artwork(self, artwork_key: str):
        if not artwork_key.startswith("/") or artwork_key.startswith("//"):
            raise ValueError("Invalid Plex artwork key")
        response = self._get(artwork_key, timeout=10)
        response.raise_for_status()
        return response

    def check_reachable(self) -> Dict:
        try:
            r = self._get("/identity")
            if r.status_code == 200:
                version = r.json().get("MediaContainer", {}).get("version", "?")
                return {"pass": True, "detail": f"Plex reachable (v{version}) at {self.url}"}
            return {"pass": False, "detail": f"Plex returned HTTP {r.status_code}"}
        except requests.exceptions.Timeout:
            return {"pass": False, "detail": f"Plex timed out: {self.url}"}
        except Exception as e:
            return {"pass": False, "detail": f"Plex unreachable ({self.url}): {e}"}

    def get_sections(self, refresh: bool = False) -> List[Dict]:
        if (not refresh and self._sections_cache is not None
                and time.monotonic() - self._sections_cached_at < 60):
            return [dict(section) for section in self._sections_cache]
        r = self._get("/library/sections")
        r.raise_for_status()
        sections = [
            {"id": str(s["key"]), "title": s["title"], "type": s["type"]}
            for s in r.json().get("MediaContainer", {}).get("Directory", [])
        ]
        self._sections_cache = sections
        self._sections_cached_at = time.monotonic()
        return [dict(section) for section in sections]

    def get_machine_identifier(self) -> Optional[str]:
        try:
            response = self._get("/identity")
            response.raise_for_status()
            return str(
                response.json().get("MediaContainer", {}).get(
                    "machineIdentifier", "",
                )
            ) or None
        except Exception:
            return None

    def get_unmatched_items(self, section_id: str) -> Dict:
        """Return top-level library items whose primary GUID is local-only."""
        response = self._get(
            f"/library/sections/{section_id}/all",
            params={
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": 100000,
            },
            timeout=60,
        )
        response.raise_for_status()
        container = response.json().get("MediaContainer", {})
        entries = list(container.get("Metadata", []))
        if not entries:
            entries = [
                *container.get("Video", []),
                *container.get("Directory", []),
            ]
        unmatched = []
        seen = set()
        for item in entries:
            guid = str(item.get("guid", ""))
            rating_key = str(item.get("ratingKey", ""))
            if not guid.startswith("local://") or not rating_key:
                continue
            if rating_key in seen:
                continue
            seen.add(rating_key)
            unmatched.append({
                "title": str(item.get("title", "Unknown")),
                "year": item.get("year", ""),
                "type": str(item.get("type", "item")),
                "rating_key": rating_key,
                "metadata_key": str(
                    item.get("key") or f"/library/metadata/{rating_key}"
                ),
                "guid": guid,
            })
        return {
            "total_items": int(
                container.get("totalSize", container.get("size", len(entries)))
            ),
            "items": sorted(
                unmatched,
                key=lambda item: (item["title"].casefold(), item["rating_key"]),
            ),
        }

    def find_section_id(self, library_name: str) -> Optional[str]:
        try:
            for s in self.get_sections():
                if s["title"].lower() == library_name.lower():
                    return s["id"]
        except Exception:
            pass
        return None

    def get_section_type(self, section_id: str) -> Optional[str]:
        try:
            for s in self.get_sections():
                if s["id"] == section_id:
                    return s["type"]
        except Exception:
            pass
        return None

    def get_library_item_count(self, section_id: str) -> Optional[int]:
        """Return a count comparable to media files, or None when Plex fails.

        TV section `/all` counts shows, while paths normally contain episodes.
        Requesting episode type 4 gives a useful leaf-item safety threshold.
        """
        try:
            section_type = self.get_section_type(section_id)
            if section_type is None:
                return None
            params = {
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": 0,
            }
            if section_type == "show":
                params["type"] = 4
            r = self._get(f"/library/sections/{section_id}/all",
                          params=params)
            r.raise_for_status()
            return int(r.json().get("MediaContainer", {}).get("totalSize", 0))
        except Exception:
            return None

    def _fetch_deleted_xml(self, section_id: str,
                           type_id: int) -> Optional[List[Dict]]:
        """
        Fetch items with deletedAt using XML (required — JSON omits deletedAt
        on Media children for episodes). Checks both item-level and
        Media child-level deletedAt.
        Returns list of {title, year, type, deleted_at, media_type_id}.
        """
        try:
            r = requests.get(
                f"{self.url}/library/sections/{section_id}/all",
                params={
                    "checkFiles":   1,
                    "type":         type_id,
                    "X-Plex-Token": self.token,
                },
                timeout=120,
            )
            if r.status_code != 200:
                return None
            root    = ET.fromstring(r.text)
            deleted = []
            for item in root:
                if item.get("deletedAt"):
                    deleted.append(_deleted_item(
                        item, type_id, int(item.get("deletedAt", 0))
                    ))
                else:
                    # Check deletedAt on <Media> children (episodes with
                    # unavailable/replaced file versions)
                    for media in item.findall("Media"):
                        if media.get("deletedAt"):
                            deleted.append(_deleted_item(
                                item, type_id, int(media.get("deletedAt", 0)),
                                media=media,
                            ))
                            break  # one entry per episode
            return deleted
        except Exception:
            return None

    def _legacy_trash_items(self, section_id: str) -> List[Dict]:
        response = self._get(
            f"/library/sections/{section_id}/all",
            params={"trash": 1},
        )
        if response.status_code != 200:
            return []
        return [
            {
                "title": item.get("title", "Unknown"),
                "year": item.get("year", ""),
                "type": item.get("type", ""),
                "media_type_id": 0,
                "rating_key": item.get("ratingKey", ""),
                "media_id": "",
            }
            for item in response.json().get("MediaContainer", {}).get("Metadata", [])
        ]

    def get_trash_items(self, section_id: str) -> Optional[List[Dict]]:
        """
        Get all items that will be removed by emptyTrash.
        Returns list of items with type info for breakdown reporting.
        """
        try:
            section_type = self.get_section_type(section_id)
            if section_type is None:
                return None
            type_ids     = _TV_TYPES if section_type == "show" else _MOVIE_TYPES

            all_items = []
            seen_items = set()
            for type_id in type_ids:
                fetched = self._fetch_deleted_xml(section_id, type_id)
                if fetched is None:
                    return None
                for item in fetched:
                    _append_unique(all_items, seen_items, item)
            try:
                for item in self._legacy_trash_items(section_id):
                    _append_unique(all_items, seen_items, item)
            except Exception:
                pass
            return all_items
        except Exception:
            return None

    def clean_bundles(self) -> Dict:
        """
        Ask Plex to perform its server-wide Clean Bundles maintenance action.
        The runner keeps this opt-in because it is broader than a single
        library's Empty Trash operation.
        """
        try:
            r = self.session.put(
                f"{self.url}/library/clean",
                timeout=60
            )
            if r.status_code in (200, 202, 204):
                return {"ok": True}
            return {"ok": False, "http": r.status_code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def scan_path(self, section_id: str, path: str) -> Dict:
        """Request Plex's supported path-limited library scan."""
        try:
            response = self._get(
                f"/library/sections/{section_id}/refresh",
                params={"path": path},
                timeout=30,
            )
            if response.status_code in (200, 201, 202, 204):
                return {"ok": True, "http": response.status_code}
            return {"ok": False, "http": response.status_code}
        except Exception as exc:
            return {"ok": False, "http": None, "error": type(exc).__name__}

    def refresh_section(self, section_id: str) -> Dict:
        """Request a normal full refresh of one Plex library section."""
        try:
            response = self.session.post(
                f"{self.url}/library/sections/{section_id}/refresh",
                timeout=30,
            )
            if response.status_code in (200, 201, 202, 204):
                return {"ok": True, "http": response.status_code}
            return {"ok": False, "http": response.status_code}
        except Exception as exc:
            return {"ok": False, "http": None, "error": type(exc).__name__}

    def empty_trash(self, section_id: str) -> Dict:
        try:
            r = self.session.put(
                f"{self.url}/library/sections/{section_id}/emptyTrash",
                timeout=30
            )
            if r.status_code in (200, 204):
                return {"ok": True,  "http": r.status_code}
            return {"ok": False, "http": r.status_code, "error": r.text[:200]}
        except Exception as e:
            return {"ok": False, "http": None, "error": str(e)}
