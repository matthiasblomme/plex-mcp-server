import json
import asyncio
import os
import time as _time
from typing import Optional, Dict, List, Any, Tuple
from urllib.parse import urljoin, urlencode
import aiohttp
from modules import mcp, connect_to_plex
from modules.trakt import compute_trakt_scores, get_trakt_related, parse_plex_guids


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
# In-memory metadata cache (lives for process lifetime)
# ---------------------------------------------------------------------------

_metadata_cache: Dict[int, Any] = {}


async def _cached_fetch_item(plex, rating_key):
    """Fetch a Plex item by ratingKey, using an in-memory cache."""
    # Normalize to int — fetchItem treats strings as URL paths
    try:
        rk = int(rating_key)
    except (ValueError, TypeError):
        return None
    if rk in _metadata_cache:
        return _metadata_cache[rk]
    loop = asyncio.get_running_loop()
    try:
        item = await loop.run_in_executor(None, plex.fetchItem, rk)
        _metadata_cache[rk] = item
        return item
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _actor_rank_weight(rank: int) -> float:
    """Return a decay weight based on billing position (0-indexed)."""
    if rank < 5:
        return 1.0
    if rank < 10:
        return 0.6
    if rank < 20:
        return 0.3
    return 0.0


def _year_to_decade(year) -> Optional[int]:
    """Convert a year to its decade (e.g. 2017 → 2010)."""
    if year is None:
        return None
    try:
        return (int(year) // 10) * 10
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_sections_for_type(plex, content_type: str) -> List[Dict]:
    """Return list of {section_id, section_type} dicts filtered by content_type.

    Skips non-media sections (e.g. photo, camera libraries tagged as 'movie').
    """
    type_map = {"movie": "movie", "show": "show"}
    target = type_map.get(content_type.lower())
    # Section titles that are clearly not real media libraries
    skip_titles = {"cameras", "camera", "photos", "home videos"}
    sections = []
    for section in plex.library.sections():
        if target is None or section.type == target:
            if section.title.lower() not in skip_titles:
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
      writers   × 1.0  (same as genres)
      actors    × 0.8  (rank-decayed by billing position)
      studios   × 0.5  (weakest — tiebreaker only)
      decades   × 0.3  (era preference)
    """
    profile: Dict[str, Any] = {
        "genres": {},
        "directors": {},
        "writers": {},
        "actors": {},
        "studios": {},
        "decades": {},
        "analyzed_count": 0,
        "rated_count": 0,
    }

    n = len(history_items)
    if n == 0:
        return profile

    # Fetch full metadata for all history items concurrently
    tasks = [_cached_fetch_item(plex, getattr(h, "ratingKey", None))
             for h in history_items if getattr(h, "ratingKey", None)]
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
                show = await _cached_fetch_item(plex, grandparent_key)
                if show is not None:
                    metadata_item = show

        for genre in getattr(metadata_item, "genres", []):
            tag = getattr(genre, "tag", None)
            if tag:
                profile["genres"][tag] = profile["genres"].get(tag, 0.0) + w

        for director in getattr(metadata_item, "directors", []):
            tag = getattr(director, "tag", None)
            if tag:
                profile["directors"][tag] = profile["directors"].get(tag, 0.0) + w * 1.5

        for writer in getattr(metadata_item, "writers", []):
            tag = getattr(writer, "tag", None)
            if tag:
                profile["writers"][tag] = profile["writers"].get(tag, 0.0) + w

        for rank, actor in enumerate(getattr(metadata_item, "roles", [])):
            rw = _actor_rank_weight(rank)
            if rw == 0.0:
                break
            tag = getattr(actor, "tag", None)
            if tag:
                profile["actors"][tag] = profile["actors"].get(tag, 0.0) + w * 0.8 * rw

        studio = getattr(metadata_item, "studio", None)
        if studio:
            profile["studios"][studio] = profile["studios"].get(studio, 0.0) + w * 0.5

        decade = _year_to_decade(getattr(metadata_item, "year", None))
        if decade is not None:
            profile["decades"][decade] = profile["decades"].get(decade, 0.0) + w * 0.3

        profile["analyzed_count"] += 1

    return profile


def _build_similarity_profile(source_item) -> Dict:
    """Build a taste profile from a single item for use with media_get_similar."""
    profile: Dict[str, Any] = {
        "genres": {},
        "directors": {},
        "writers": {},
        "actors": {},
        "studios": {},
        "decades": {},
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

    for writer in getattr(source_item, "writers", []):
        tag = getattr(writer, "tag", None)
        if tag:
            profile["writers"][tag] = 1.0

    for rank, actor in enumerate(getattr(source_item, "roles", [])):
        rw = _actor_rank_weight(rank)
        if rw == 0.0:
            break
        tag = getattr(actor, "tag", None)
        if tag:
            profile["actors"][tag] = 0.8 * rw

    studio = getattr(source_item, "studio", None)
    if studio:
        profile["studios"][studio] = 0.5

    decade = _year_to_decade(getattr(source_item, "year", None))
    if decade is not None:
        profile["decades"][decade] = 0.3

    return profile


async def _score_and_rank(
    candidates: List[Dict],
    profile: Dict,
    plex,
    top_n: int,
    min_rating: Optional[float],
    exclude_key: Optional[int] = None,
    trakt_scores: Optional[Dict[str, float]] = None,
) -> List[Dict]:
    """
    Enrich candidate stubs with full metadata, score against the profile, and
    return the top_n results sorted by score descending.

    If trakt_scores is provided, blends content score (70%) with Trakt score (30%).
    """
    # Pre-filter by min_rating using stub data to reduce fetchItem calls
    if min_rating is not None:
        candidates = [
            c for c in candidates
            if c.get("rating") is None or c["rating"] >= min_rating
        ]

    if exclude_key is not None:
        candidates = [c for c in candidates if str(c.get("ratingKey")) != str(exclude_key)]

    tasks = [_cached_fetch_item(plex, c["ratingKey"]) for c in candidates if c.get("ratingKey")]
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

        content_score = 0.0
        reasons = []

        # Genre matching
        item_genres = [getattr(g, "tag", "") for g in getattr(item, "genres", [])]
        matched_genres = [g for g in item_genres if g in profile["genres"]]
        if matched_genres:
            genre_score = sum(profile["genres"][g] for g in matched_genres)
            content_score += genre_score
            reasons.append(f"Genres: {', '.join(matched_genres[:3])}")

        # Director matching
        item_directors = [getattr(d, "tag", "") for d in getattr(item, "directors", [])]
        matched_dirs = [d for d in item_directors if d in profile["directors"]]
        if matched_dirs:
            dir_score = sum(profile["directors"][d] for d in matched_dirs)
            content_score += dir_score
            reasons.append(f"Director: {', '.join(matched_dirs)}")

        # Writer matching
        item_writers = [getattr(w, "tag", "") for w in getattr(item, "writers", [])]
        matched_writers = [w for w in item_writers if w in profile.get("writers", {})]
        if matched_writers:
            writer_score = sum(profile["writers"][w] for w in matched_writers)
            content_score += writer_score
            reasons.append(f"Writer: {', '.join(matched_writers[:2])}")

        # Actor matching (rank-decayed)
        item_roles = getattr(item, "roles", [])
        matched_actors = []
        actor_score = 0.0
        for rank, actor in enumerate(item_roles):
            rw = _actor_rank_weight(rank)
            if rw == 0.0:
                break
            tag = getattr(actor, "tag", "")
            if tag in profile["actors"]:
                actor_score += profile["actors"][tag] * rw
                matched_actors.append(tag)
        if matched_actors:
            content_score += actor_score
            reasons.append(f"Cast: {', '.join(matched_actors[:3])}")

        # Studio matching
        studio = getattr(item, "studio", None)
        if studio and studio in profile["studios"]:
            content_score += profile["studios"][studio]
            reasons.append(f"Studio: {studio}")

        # Decade matching
        item_decade = _year_to_decade(getattr(item, "year", None))
        profile_decades = profile.get("decades", {})
        if item_decade is not None and profile_decades:
            if item_decade in profile_decades:
                content_score += profile_decades[item_decade]
                reasons.append(f"Era: {item_decade}s")
            else:
                # Adjacent decade gets half weight
                for adj in (item_decade - 10, item_decade + 10):
                    if adj in profile_decades:
                        content_score += profile_decades[adj] * 0.5
                        break

        # Small tiebreaker boost from item's own rating
        if item_rating:
            content_score += float(item_rating) * 0.1

        # Trakt blending (70% content, 30% Trakt when available)
        trakt_score = None
        rk_str = str(item.ratingKey)
        if trakt_scores and rk_str in trakt_scores:
            trakt_score = trakt_scores[rk_str]
            final_score = 0.70 * content_score + 0.30 * trakt_score
            reasons.append("Trakt: community pick")
        else:
            final_score = content_score

        if final_score > 0:
            # Store hidden fields for diversity reranking (removed before return)
            scored.append({
                "ratingKey": item.ratingKey,
                "title": item.title,
                "year": getattr(item, "year", None),
                "rating": item_rating,
                "contentScore": round(content_score, 2),
                "traktScore": round(trakt_score, 2) if trakt_score is not None else None,
                "score": round(final_score, 2),
                "matchReasons": " | ".join(reasons) if reasons else "General match",
                "_directors": item_directors,
                "_lead_actor": getattr(item_roles[0], "tag", "") if item_roles else "",
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    result = _diversify(scored, top_n)

    # Remove hidden fields
    for item in result:
        item.pop("_directors", None)
        item.pop("_lead_actor", None)

    return result


def _diversify(scored: List[Dict], top_n: int, max_per_director: int = 3, max_per_actor: int = 3) -> List[Dict]:
    """Cap same-director and same-lead-actor repetitions in final results."""
    result = []
    director_counts: Dict[str, int] = {}
    actor_counts: Dict[str, int] = {}

    for item in scored:
        if len(result) >= top_n:
            break

        # Check director cap
        directors = item.get("_directors", [])
        skip = False
        for d in directors:
            if d and director_counts.get(d, 0) >= max_per_director:
                skip = True
                break
        if skip:
            continue

        # Check lead actor cap
        lead = item.get("_lead_actor", "")
        if lead and actor_counts.get(lead, 0) >= max_per_actor:
            continue

        result.append(item)
        for d in directors:
            if d:
                director_counts[d] = director_counts.get(d, 0) + 1
        if lead:
            actor_counts[lead] = actor_counts.get(lead, 0) + 1

    return result


def _make_profile_summary(profile: Dict) -> Dict:
    """Return the top genres/directors/actors/writers from a profile for display."""
    top_genres = sorted(profile["genres"], key=lambda k: profile["genres"][k], reverse=True)[:5]
    top_directors = sorted(profile["directors"], key=lambda k: profile["directors"][k], reverse=True)[:3]
    top_actors = sorted(profile["actors"], key=lambda k: profile["actors"][k], reverse=True)[:3]
    top_writers = sorted(profile.get("writers", {}), key=lambda k: profile["writers"][k], reverse=True)[:3]
    return {
        "topGenres": top_genres,
        "topDirectors": top_directors,
        "topActors": top_actors,
        "topWriters": top_writers,
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
    use_trakt: bool = True,
) -> str:
    """Recommend unwatched media based on the user's watch history.

    Analyzes recent watch history to build a taste profile (preferred genres,
    directors, actors, writers, studios, decade) and scores unwatched library
    items against it. Optionally blends in Trakt community recommendations.

    Args:
        content_type: Type of content to recommend — "movie" or "show" (default: "movie")
        count: Number of recommendations to return (default: 10, max: 50)
        history_limit: Number of recent watched items to analyze (default: 50)
        min_rating: Minimum Plex audience rating to include (e.g. 7.0). None = no filter
        account_id: Plex accountID to pull history for. 1 = server owner (default: 1)
        use_trakt: Enable Trakt community scoring (default: True, requires TRAKT_CLIENT_ID)
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

        # 4.5. Compute Trakt community scores (if enabled)
        trakt_scores = None
        if use_trakt:
            # Select top 5 seed items from history (most recent)
            # For episodes, promote to show level (Trakt needs show IDs)
            seed_items = []
            seen_seed_keys = set()
            for h in filtered_history[:15]:
                rk = getattr(h, "ratingKey", None)
                if not rk:
                    continue
                item = await _cached_fetch_item(plex, rk)
                if item is None:
                    continue
                # Promote episodes to their parent show
                if getattr(item, "type", "") == "episode":
                    grandparent_key = getattr(item, "grandparentRatingKey", None)
                    if grandparent_key and grandparent_key not in seen_seed_keys:
                        show = await _cached_fetch_item(plex, grandparent_key)
                        if show is not None:
                            seen_seed_keys.add(grandparent_key)
                            seed_items.append(show)
                else:
                    if rk not in seen_seed_keys:
                        seen_seed_keys.add(rk)
                        seed_items.append(item)
                if len(seed_items) >= 5:
                    break

            # Fetch full metadata for candidates that need Trakt scoring
            candidate_items = []
            for c in all_candidates:
                rk = c.get("ratingKey")
                if rk:
                    item = await _cached_fetch_item(plex, rk)
                    if item is not None:
                        candidate_items.append(item)

            if seed_items and candidate_items:
                trakt_scores = await compute_trakt_scores(
                    seed_items, candidate_items, content_type
                )

        # 5. Score and rank
        recommendations = await _score_and_rank(
            all_candidates, profile, plex, count, min_rating,
            trakt_scores=trakt_scores,
        )

        return json.dumps({
            "status": "success",
            "contentType": content_type,
            "analyzedHistory": profile["analyzed_count"],
            "profileSummary": _make_profile_summary(profile),
            "traktEnabled": use_trakt and trakt_scores is not None,
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
    use_trakt: bool = True,
) -> str:
    """Find unwatched items similar to a specific piece of media.

    Uses the source item's genres, directors, writers, and cast to find matching
    unwatched content in your library. Optionally enriched with Trakt community data.

    Args:
        rating_key: The ratingKey of the item to base similarity on
        count: Number of similar items to return (default: 10)
        min_rating: Minimum Plex audience rating to include (e.g. 7.0). None = no filter
        use_trakt: Enable Trakt community scoring (default: True, requires TRAKT_CLIENT_ID)
    """
    try:
        plex = connect_to_plex()

        # 1. Fetch source item metadata
        source_item = await _cached_fetch_item(plex, rating_key)
        if source_item is None:
            return json.dumps({
                "status": "error",
                "message": f"Could not find item with ratingKey={rating_key}",
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

        # 3.5. Compute Trakt scores using the source item as sole seed
        trakt_scores = None
        if use_trakt:
            candidate_items = []
            for c in all_candidates:
                rk = c.get("ratingKey")
                if rk:
                    item = await _cached_fetch_item(plex, rk)
                    if item is not None:
                        candidate_items.append(item)

            if candidate_items:
                trakt_scores = await compute_trakt_scores(
                    [source_item], candidate_items, content_type
                )

        # 4. Score and rank, excluding the source item itself
        similar = await _score_and_rank(
            all_candidates, profile, plex, count, min_rating,
            exclude_key=rating_key,
            trakt_scores=trakt_scores,
        )

        return json.dumps({
            "status": "success",
            "sourceItem": {
                "ratingKey": source_item.ratingKey,
                "title": source_item.title,
                "year": getattr(source_item, "year", None),
                "type": source_type,
            },
            "traktEnabled": use_trakt and trakt_scores is not None,
            "count": len(similar),
            "similarItems": similar,
        })

    except Exception as e:
        return json.dumps({"status": "error", "message": f"Error finding similar items: {str(e)}"})
