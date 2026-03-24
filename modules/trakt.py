"""
Trakt integration for the Plex MCP recommendation engine.

Provides community-driven "people who liked X also liked Y" signals
via the Trakt public API. Uses API-key auth only (no OAuth).

All functions gracefully return empty results when TRAKT_CLIENT_ID is not set.
"""
import os
import time
import asyncio
from typing import Dict, List, Any, Optional, Tuple
import aiohttp


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRAKT_BASE_URL = "https://api.trakt.tv"
TRAKT_TIMEOUT = aiohttp.ClientTimeout(total=5)

# Cache: {cache_key: (expiry_timestamp, data)}
_cache: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL = 900  # 15 minutes


def _get_client_id() -> str:
    return os.environ.get("TRAKT_CLIENT_ID", "")


def _is_available() -> bool:
    return bool(_get_client_id())


def _trakt_headers() -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": _get_client_id(),
    }


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_get(key: str) -> Optional[Any]:
    entry = _cache.get(key)
    if entry is None:
        return None
    expiry, data = entry
    if time.time() > expiry:
        del _cache[key]
        return None
    return data


def _cache_set(key: str, data: Any) -> None:
    _cache[key] = (time.time() + _CACHE_TTL, data)


# ---------------------------------------------------------------------------
# GUID parsing (Plex → external IDs)
# ---------------------------------------------------------------------------

def parse_plex_guids(item) -> Dict[str, str]:
    """Extract external IDs from a Plex item's guids attribute.

    plexapi items have a .guids property returning Guid objects with .id like
    'imdb://tt1234567', 'tmdb://12345', 'tvdb://12345'.

    Returns: {"imdb": "tt1234567", "tmdb": "12345", "tvdb": "12345"}
    """
    ids: Dict[str, str] = {}
    for guid in getattr(item, "guids", []):
        guid_str = getattr(guid, "id", "")
        if guid_str.startswith("imdb://"):
            ids["imdb"] = guid_str[7:]
        elif guid_str.startswith("tmdb://"):
            ids["tmdb"] = guid_str[7:]
        elif guid_str.startswith("tvdb://"):
            ids["tvdb"] = guid_str[7:]
    return ids


# ---------------------------------------------------------------------------
# Trakt API client
# ---------------------------------------------------------------------------

async def _trakt_get(session: aiohttp.ClientSession, path: str) -> Optional[List]:
    """Make a GET request to the Trakt API. Returns parsed JSON or None on failure."""
    cache_key = f"trakt:{path}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        url = f"{TRAKT_BASE_URL}{path}"
        async with session.get(url, headers=_trakt_headers(), timeout=TRAKT_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            _cache_set(cache_key, data)
            return data
    except Exception:
        return None


async def get_trakt_related(media_type: str, trakt_id: str, limit: int = 20) -> List[Dict]:
    """Fetch related items from Trakt for a given ID.

    Args:
        media_type: "movie" or "show"
        trakt_id: An IMDb ID (tt...), TMDb ID, or Trakt slug
        limit: Max results to return

    Returns: List of dicts with {imdb, tmdb, tvdb, title, year}
    """
    if not _is_available():
        return []

    plural = "movies" if media_type == "movie" else "shows"
    path = f"/{plural}/{trakt_id}/related?limit={limit}"

    async with aiohttp.ClientSession() as session:
        data = await _trakt_get(session, path)

    if not data:
        return []

    results = []
    for item in data[:limit]:
        ids = item.get("ids", {})
        results.append({
            "imdb": ids.get("imdb"),
            "tmdb": str(ids.get("tmdb", "")) if ids.get("tmdb") else None,
            "tvdb": str(ids.get("tvdb", "")) if ids.get("tvdb") else None,
            "title": item.get("title", ""),
            "year": item.get("year"),
        })
    return results


async def get_trakt_trending(media_type: str, limit: int = 20) -> List[Dict]:
    """Fetch trending items from Trakt (fallback when no seed items).

    Returns: List of dicts with {imdb, tmdb, tvdb, title, year}
    """
    if not _is_available():
        return []

    plural = "movies" if media_type == "movie" else "shows"
    path = f"/{plural}/trending?limit={limit}"

    async with aiohttp.ClientSession() as session:
        data = await _trakt_get(session, path)

    if not data:
        return []

    results = []
    for entry in data[:limit]:
        item = entry.get(media_type, entry)
        ids = item.get("ids", {})
        results.append({
            "imdb": ids.get("imdb"),
            "tmdb": str(ids.get("tmdb", "")) if ids.get("tmdb") else None,
            "tvdb": str(ids.get("tvdb", "")) if ids.get("tvdb") else None,
            "title": item.get("title", ""),
            "year": item.get("year"),
        })
    return results


# ---------------------------------------------------------------------------
# Score computation
# ---------------------------------------------------------------------------

async def compute_trakt_scores(
    seed_items: List,
    candidates: List,
    media_type: str,
) -> Dict[str, float]:
    """Compute Trakt-based scores for candidate items.

    For each seed item, fetch Trakt /related. Build a set of external IDs that
    Trakt considers related. For each candidate, check if its IDs overlap.

    Args:
        seed_items: Plex items (with .guids) to use as seeds
        candidates: Plex items (with .guids and .ratingKey) to score
        media_type: "movie" or "show"

    Returns: {str(ratingKey): normalized_score} for candidates that matched
    """
    if not _is_available() or not seed_items:
        return {}

    # 1. Collect seed IDs and fetch related items for each
    related_id_sets: List[set] = []

    async with aiohttp.ClientSession() as session:
        for seed in seed_items:
            seed_ids = parse_plex_guids(seed)
            # Prefer IMDb ID for Trakt lookups, fall back to TMDb
            trakt_id = seed_ids.get("imdb") or seed_ids.get("tmdb")
            if not trakt_id:
                continue

            plural = "movies" if media_type == "movie" else "shows"
            path = f"/{plural}/{trakt_id}/related?limit=30"
            data = await _trakt_get(session, path)
            if not data:
                continue

            # Collect all external IDs from related items
            related_ids = set()
            for rel_item in data:
                ids = rel_item.get("ids", {})
                if ids.get("imdb"):
                    related_ids.add(("imdb", ids["imdb"]))
                if ids.get("tmdb"):
                    related_ids.add(("tmdb", str(ids["tmdb"])))
                if ids.get("tvdb"):
                    related_ids.add(("tvdb", str(ids["tvdb"])))
            related_id_sets.append(related_ids)

    if not related_id_sets:
        return {}

    # 2. Score each candidate by how many seed-related sets include it
    scores: Dict[str, float] = {}
    max_seeds = len(related_id_sets)

    for candidate in candidates:
        candidate_ids = parse_plex_guids(candidate)
        if not candidate_ids:
            continue

        # Build candidate's ID tuples for matching
        candidate_id_tuples = set()
        for id_type, id_val in candidate_ids.items():
            if id_val:
                candidate_id_tuples.add((id_type, id_val))

        # Count how many seeds' related lists include this candidate
        hit_count = 0
        for related_ids in related_id_sets:
            if candidate_id_tuples & related_ids:
                hit_count += 1

        if hit_count > 0:
            # Normalize to 0.0–1.0 based on number of seed matches
            # Then scale to be comparable with content scores
            normalized = hit_count / max_seeds
            rk = str(getattr(candidate, "ratingKey", ""))
            if rk:
                scores[rk] = normalized * 10.0  # Scale to content-score range

    return scores
