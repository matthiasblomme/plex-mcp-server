import json
import asyncio
from typing import Optional, Dict, List, Any
from urllib.parse import urljoin, urlencode
import aiohttp
from modules import mcp, connect_to_plex


# ---------------------------------------------------------------------------
# HTTP helpers (mirrors the pattern in modules/library.py)
# ---------------------------------------------------------------------------

def _get_plex_headers(plex) -> Dict[str, str]:
    return {
        "X-Plex-Token": plex._token,
        "Accept": "application/json",
    }


async def _async_get_json(session: aiohttp.ClientSession, url: str, headers: Dict) -> Any:
    async with session.get(url, headers=headers) as response:
        if response.status != 200:
            try:
                body = await response.text()
                msg = body[:200]
            except Exception:
                msg = "Could not read error body"
            raise Exception(f"Plex API error {response.status}: {msg}")
        return await response.json()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_sections_for_type(plex, content_type: str) -> List[Dict]:
    """Return list of {section_id, section_type} dicts filtered by content_type."""
    type_map = {"movie": "movie", "show": "show"}
    target = type_map.get(content_type.lower())
    sections = []
    for section in plex.library.sections():
        if target is None or section.type == target:
            sections.append({"id": section.key, "type": section.type, "title": section.title})
    return sections


async def _fetch_candidate_pool(
    plex,
    section_id: str,
    max_items: int = 200,
    batch_size: int = 50,
) -> List[Dict]:
    """Fetch lightweight stubs of unwatched items from a library section."""
    candidates = []
    base_url = plex._baseurl
    headers = _get_plex_headers(plex)

    async with aiohttp.ClientSession() as session:
        offset = 0
        while len(candidates) < max_items:
            params = {"unwatched": "1", "start": offset, "size": batch_size}
            request_headers = {
                **headers,
                "X-Plex-Container-Start": str(offset),
                "X-Plex-Container-Size": str(batch_size),
            }
            url = urljoin(base_url, f"library/sections/{section_id}/all?{urlencode(params)}")
            data = await _async_get_json(session, url, request_headers)
            items = data.get("MediaContainer", {}).get("Metadata", [])

            if not items:
                break

            for item in items:
                candidates.append({
                    "ratingKey": item.get("ratingKey"),
                    "title": item.get("title", ""),
                    "year": item.get("year"),
                    "rating": item.get("rating"),
                })

            offset += batch_size
            if len(items) < batch_size:
                break  # exhausted the section

    return candidates[:max_items]


async def _build_preference_profile(history_items: List, plex) -> Dict:
    """
    Analyze watch history items and build a weighted taste profile.

    Weights use linear recency decay (1.0 → 0.1) multiplied by a userRating
    amplifier (0.6–1.5 when rated, 1.0 otherwise).

    Weight multipliers per attribute:
      genres    × 1.0  (base)
      directors × 1.5  (stronger taste signal)
      actors    × 0.8  (top 5 billed only)
      studios   × 0.5  (weakest — tiebreaker only)
    """
    profile: Dict[str, Any] = {
        "genres": {},
        "directors": {},
        "actors": {},
        "studios": {},
        "analyzed_count": 0,
        "rated_count": 0,
    }

    n = len(history_items)
    if n == 0:
        return profile

    loop = asyncio.get_event_loop()

    # Fetch full metadata for all history items concurrently
    async def _safe_fetch(rk):
        try:
            return await loop.run_in_executor(None, plex.fetchItem, rk)
        except Exception:
            return None

    tasks = [_safe_fetch(getattr(h, "ratingKey", None)) for h in history_items
             if getattr(h, "ratingKey", None)]
    fetched = await asyncio.gather(*tasks)

    for i, item in enumerate(fetched):
        if item is None:
            continue

        recency_weight = 1.0 - 0.9 * (i / max(n - 1, 1))

        user_rating = getattr(item, "userRating", None)
        if user_rating is not None:
            rating_multiplier = 0.5 + (float(user_rating) / 10.0)
            profile["rated_count"] += 1
        else:
            rating_multiplier = 1.0

        w = recency_weight * rating_multiplier

        # For episodes, prefer show-level metadata (richer genre/director data)
        metadata_item = item
        if getattr(item, "type", "") == "episode":
            grandparent_key = getattr(item, "grandparentRatingKey", None)
            if grandparent_key:
                try:
                    metadata_item = await loop.run_in_executor(None, plex.fetchItem, grandparent_key)
                except Exception:
                    metadata_item = item

        for genre in getattr(metadata_item, "genres", []):
            tag = getattr(genre, "tag", None)
            if tag:
                profile["genres"][tag] = profile["genres"].get(tag, 0.0) + w

        for director in getattr(metadata_item, "directors", []):
            tag = getattr(director, "tag", None)
            if tag:
                profile["directors"][tag] = profile["directors"].get(tag, 0.0) + w * 1.5

        for actor in getattr(metadata_item, "roles", [])[:5]:
            tag = getattr(actor, "tag", None)
            if tag:
                profile["actors"][tag] = profile["actors"].get(tag, 0.0) + w * 0.8

        studio = getattr(metadata_item, "studio", None)
        if studio:
            profile["studios"][studio] = profile["studios"].get(studio, 0.0) + w * 0.5

        profile["analyzed_count"] += 1

    return profile


def _build_similarity_profile(source_item) -> Dict:
    """Build a taste profile from a single item for use with media_get_similar."""
    profile: Dict[str, Any] = {
        "genres": {},
        "directors": {},
        "actors": {},
        "studios": {},
        "analyzed_count": 1,
        "rated_count": 0,
    }
    for genre in getattr(source_item, "genres", []):
        tag = getattr(genre, "tag", None)
        if tag:
            profile["genres"][tag] = 1.0

    for director in getattr(source_item, "directors", []):
        tag = getattr(director, "tag", None)
        if tag:
            profile["directors"][tag] = 1.5

    for actor in getattr(source_item, "roles", [])[:5]:
        tag = getattr(actor, "tag", None)
        if tag:
            profile["actors"][tag] = 0.8

    studio = getattr(source_item, "studio", None)
    if studio:
        profile["studios"][studio] = 0.5

    return profile


async def _score_and_rank(
    candidates: List[Dict],
    profile: Dict,
    plex,
    top_n: int,
    min_rating: Optional[float],
    exclude_key: Optional[int] = None,
) -> List[Dict]:
    """
    Enrich candidate stubs with full metadata, score against the profile, and
    return the top_n results sorted by score descending.
    """
    # Pre-filter by min_rating using stub data to reduce fetchItem calls
    if min_rating is not None:
        candidates = [
            c for c in candidates
            if c.get("rating") is None or c["rating"] >= min_rating
        ]

    if exclude_key is not None:
        candidates = [c for c in candidates if str(c.get("ratingKey")) != str(exclude_key)]

    loop = asyncio.get_event_loop()

    async def _safe_fetch(rk):
        try:
            return await loop.run_in_executor(None, plex.fetchItem, rk)
        except Exception:
            return None

    tasks = [_safe_fetch(c["ratingKey"]) for c in candidates if c.get("ratingKey")]
    fetched_items = await asyncio.gather(*tasks)

    scored = []
    for item in fetched_items:
        if item is None:
            continue

        item_rating = getattr(item, "rating", None)

        # Post-fetch min_rating check (in case stub was missing rating)
        if min_rating is not None and item_rating is not None:
            if item_rating < min_rating:
                continue

        score = 0.0
        reasons = []

        # Genre matching
        item_genres = [getattr(g, "tag", "") for g in getattr(item, "genres", [])]
        matched_genres = [g for g in item_genres if g in profile["genres"]]
        if matched_genres:
            genre_score = sum(profile["genres"][g] for g in matched_genres)
            score += genre_score
            reasons.append(f"Genres: {', '.join(matched_genres[:3])}")

        # Director matching
        item_directors = [getattr(d, "tag", "") for d in getattr(item, "directors", [])]
        matched_dirs = [d for d in item_directors if d in profile["directors"]]
        if matched_dirs:
            dir_score = sum(profile["directors"][d] for d in matched_dirs)
            score += dir_score
            reasons.append(f"Director: {', '.join(matched_dirs)}")

        # Actor matching
        item_actors = [getattr(a, "tag", "") for a in getattr(item, "roles", [])[:10]]
        matched_actors = [a for a in item_actors if a in profile["actors"]]
        if matched_actors:
            actor_score = sum(profile["actors"][a] for a in matched_actors)
            score += actor_score
            reasons.append(f"Cast: {', '.join(matched_actors[:2])}")

        # Studio matching
        studio = getattr(item, "studio", None)
        if studio and studio in profile["studios"]:
            score += profile["studios"][studio]
            reasons.append(f"Studio: {studio}")

        # Small tiebreaker boost from item's own rating
        if item_rating:
            score += float(item_rating) * 0.1

        if score > 0:
            scored.append({
                "ratingKey": item.ratingKey,
                "title": item.title,
                "year": getattr(item, "year", None),
                "rating": item_rating,
                "score": round(score, 2),
                "matchReasons": " | ".join(reasons) if reasons else "General match",
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_n]


def _make_profile_summary(profile: Dict) -> Dict:
    """Return the top genres/directors/actors from a profile for display."""
    top_genres = sorted(profile["genres"], key=lambda k: profile["genres"][k], reverse=True)[:5]
    top_directors = sorted(profile["directors"], key=lambda k: profile["directors"][k], reverse=True)[:3]
    top_actors = sorted(profile["actors"], key=lambda k: profile["actors"][k], reverse=True)[:3]
    return {
        "topGenres": top_genres,
        "topDirectors": top_directors,
        "topActors": top_actors,
    }


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def media_get_recommendations(
    content_type: str = "movie",
    count: int = 10,
    history_limit: int = 50,
    min_rating: Optional[float] = None,
    account_id: int = 1,
) -> str:
    """Recommend unwatched media based on the user's watch history.

    Analyzes recent watch history to build a taste profile (preferred genres,
    directors, actors, studios) and scores unwatched library items against it.

    Args:
        content_type: Type of content to recommend — "movie" or "show" (default: "movie")
        count: Number of recommendations to return (default: 10, max: 50)
        history_limit: Number of recent watched items to analyze (default: 50)
        min_rating: Minimum Plex audience rating to include (e.g. 7.0). None = no filter
        account_id: Plex accountID to pull history for. 1 = server owner (default: 1)
    """
    try:
        plex = connect_to_plex()
        count = min(count, 50)

        # 1. Fetch watch history
        history = plex.history(maxresults=history_limit, accountID=account_id)

        # 2. Filter history to the requested content type
        type_filter = {"movie": ("movie",), "show": ("episode", "show")}
        allowed_types = type_filter.get(content_type.lower(), ("movie",))
        filtered_history = [
            h for h in history if getattr(h, "type", "") in allowed_types
        ]

        if not filtered_history:
            return json.dumps({
                "status": "error",
                "message": f"No {content_type} watch history found for account_id={account_id}. "
                           "Try watching some content first, or check the account_id.",
            })

        # 3. Build preference profile from history
        profile = await _build_preference_profile(filtered_history, plex)

        if profile["analyzed_count"] == 0:
            return json.dumps({
                "status": "error",
                "message": "Could not retrieve metadata for history items. Check Plex connectivity.",
            })

        # 4. Gather candidate pool from all matching library sections
        sections = _get_sections_for_type(plex, content_type)
        if not sections:
            return json.dumps({
                "status": "error",
                "message": f"No {content_type} library sections found on this Plex server.",
            })

        all_candidates: List[Dict] = []
        seen_keys = set()
        for section in sections:
            pool = await _fetch_candidate_pool(plex, section["id"])
            for c in pool:
                rk = c.get("ratingKey")
                if rk and rk not in seen_keys:
                    seen_keys.add(rk)
                    all_candidates.append(c)

        if not all_candidates:
            return json.dumps({
                "status": "success",
                "message": f"No unwatched {content_type} items found in your library.",
                "count": 0,
                "recommendations": [],
            })

        # 5. Score and rank
        recommendations = await _score_and_rank(all_candidates, profile, plex, count, min_rating)

        return json.dumps({
            "status": "success",
            "contentType": content_type,
            "analyzedHistory": profile["analyzed_count"],
            "profileSummary": _make_profile_summary(profile),
            "count": len(recommendations),
            "recommendations": recommendations,
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Error generating recommendations: {str(e)}"})


@mcp.tool()
async def media_get_similar(
    rating_key: int,
    count: int = 10,
    min_rating: Optional[float] = None,
) -> str:
    """Find unwatched items similar to a specific piece of media.

    Uses the source item's genres, directors, and cast to find matching
    unwatched content in your library.

    Args:
        rating_key: The ratingKey of the item to base similarity on
        count: Number of similar items to return (default: 10)
        min_rating: Minimum Plex audience rating to include (e.g. 7.0). None = no filter
    """
    try:
        plex = connect_to_plex()
        loop = asyncio.get_event_loop()

        # 1. Fetch source item metadata
        try:
            source_item = await loop.run_in_executor(None, plex.fetchItem, rating_key)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Could not find item with ratingKey={rating_key}: {str(e)}",
            })

        source_type = getattr(source_item, "type", "movie")
        # Map episode → show for candidate search
        content_type = "show" if source_type in ("episode", "show", "season") else "movie"

        # 2. Build similarity profile from source item
        profile = _build_similarity_profile(source_item)

        # 3. Fetch candidate pool
        sections = _get_sections_for_type(plex, content_type)
        if not sections:
            return json.dumps({
                "status": "error",
                "message": f"No {content_type} library sections found on this Plex server.",
            })

        all_candidates: List[Dict] = []
        seen_keys = set()
        for section in sections:
            pool = await _fetch_candidate_pool(plex, section["id"])
            for c in pool:
                rk = c.get("ratingKey")
                if rk and rk not in seen_keys:
                    seen_keys.add(rk)
                    all_candidates.append(c)

        if not all_candidates:
            return json.dumps({
                "status": "success",
                "message": f"No unwatched {content_type} items found in your library.",
                "count": 0,
                "similarItems": [],
            })

        # 4. Score and rank, excluding the source item itself
        similar = await _score_and_rank(
            all_candidates, profile, plex, count, min_rating,
            exclude_key=rating_key,
        )

        return json.dumps({
            "status": "success",
            "sourceItem": {
                "ratingKey": source_item.ratingKey,
                "title": source_item.title,
                "year": getattr(source_item, "year", None),
                "type": source_type,
            },
            "count": len(similar),
            "similarItems": similar,
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Error finding similar items: {str(e)}"})
