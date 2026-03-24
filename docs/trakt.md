# Recommendation Engine Implementation Plan

## Objective
Upgrade the current Plex recommendation engine from a pure metadata-overlap recommender into a hybrid recommender that combines:

- content-based scoring from Plex metadata
- improved cast handling beyond only top-billed actors
- richer metadata signals
- optional community-driven Trakt similarity
- reranking for diversity and quality

The current implementation is a good base because it already:
- builds a preference profile from watch history
- scores unwatched candidates
- explains why items matched

---

## Current limitations to address

### Content-based limitations
- actor matching is truncated to top-billed cast
- only a small metadata set is used
- candidate scoring can become repetitive
- recommendations depend heavily on metadata quality

### Collaborative limitations
- no "people who liked X also liked Y" logic
- no community signal
- no co-watch or co-like data

---

## Target architecture

The upgraded recommender should be split into the following components:

- `PreferenceProfileBuilder`
- `CandidateProvider`
- `ContentScorer`
- `TraktScorer`
- `HybridRanker`
- `RecommendationService`
- `IdNormalizer`
- `MetadataCache`

This separation keeps the current engine maintainable as more scoring logic is added.

---

## Phase 1: Refactor the current engine into modules

### Goal
Separate responsibilities so the code can support multiple scoring paths cleanly.

### Tasks
- extract watch-history profile logic into `PreferenceProfileBuilder`
- extract unwatched candidate fetching into `CandidateProvider`
- extract metadata scoring into `ContentScorer`
- move final ranking and explanations into `HybridRanker`
- use `RecommendationService` as the top-level orchestration layer

### Deliverables
- modular code structure
- no behavior change yet
- unit tests preserved or added around current behavior

---

## Phase 2: Improve actor handling

### Goal
Remove the current "top billed actors only" limitation.

### Problem
The current code only uses a limited number of actors when:
- building the preference profile
- scoring candidate items

This loses useful signal and biases recommendations too heavily toward the top cast.

### Proposed solution
Use rank-decayed cast weighting instead of a hard cutoff.

### Suggested weighting
- actors 1–5: weight `1.0`
- actors 6–10: weight `0.6`
- actors 11–20: weight `0.3`
- actors 21+: ignore or weight `0.1`

### Tasks
- implement `extract_weighted_cast(metadata_item)`
- update profile building to store weighted cast preferences
- update candidate scoring to compare against weighted cast preferences
- keep actor overlap explanations in the final recommendation output

### Deliverables
- improved actor matching
- broader cast coverage
- less overfitting to only the lead actors

---

## Phase 3: Expand metadata dimensions

### Goal
Improve content-based recommendation quality using richer signals.

### Add these metadata dimensions
- writers
- collections or franchises
- decade
- country
- language
- content rating
- release year proximity
- optional mood proxies if present in tags

### Why
These are easier to implement and tune than collaborative filtering and usually improve quality quickly.

### Tasks
- extend the preference profile to store weighted values for each new metadata dimension
- add corresponding candidate scoring functions
- tune feature weights to avoid any single feature dominating the score

### Suggested starting weights
- genres: `1.0`
- directors: `1.5`
- actors: variable by cast rank
- studios: `0.5`
- writers: `1.0`
- collections/franchises: `1.2`
- decade: `0.6`
- country: `0.4`
- language: `0.4`

### Deliverables
- richer content-based profile
- better recommendation relevance before collaborative logic is added

---

## Phase 4: Add ID normalization

### Goal
Create a reliable cross-system identity model so Plex items can be matched against external services like Trakt.

### Why
External matching based only on title and year is too fragile.

### Canonical identifiers to capture
- Plex `ratingKey`
- Plex GUID
- IMDb ID
- TMDb ID
- TVDb ID
- media type
- title/year fallback only when necessary

### Tasks
- build `IdNormalizer`
- parse Plex GUIDs and external IDs from item metadata
- store normalized identifiers in cache
- create a utility to compare Plex items and external items consistently

### Deliverables
- reusable ID normalization layer
- robust mapping foundation for Trakt integration

---

## Phase 5: Add caching

### Goal
Reduce repeated expensive API calls to Plex and external services.

### Cache targets
- Plex item metadata
- normalized IDs
- profile summaries
- candidate lists per library section
- future Trakt recommendation lookups

### Implementation options
- in-memory TTL cache for short-lived runs
- sqlite-backed cache for persistent local storage

### Tasks
- add cache abstraction
- wrap metadata fetches in cache lookups
- add expiration and invalidation strategy
- log cache hit/miss rates for tuning

### Deliverables
- lower latency
- reduced pressure on Plex and external APIs

---

## Phase 6: Add reranking and diversity control

### Goal
Prevent the top recommendations from collapsing into near-duplicates.

### Problems to address
- same franchise repeated too often
- too many results from the same lead actor
- too many results from the same director
- not enough variety in the top 10

### Reranking rules
- cap results from the same franchise/collection
- cap repeated same-director results
- cap repeated same-lead-actor results
- use Plex rating as a small tiebreak
- optionally force some genre diversity

### Tasks
- implement `HybridRanker`
- apply reranking after base scores are computed
- preserve explanations for why items ranked where they did

### Deliverables
- more balanced final lists
- recommendations that look less repetitive

---

## Phase 7: Add Trakt community scoring

### Goal
Implement the missing "people who liked X also liked Y" behavior using Trakt community data.

### Important design principle
Do not replace the current content-based recommender. Add Trakt as an additional scorer.

### Proposed approach
Use one or more seed items from the user profile, then:
- ask Trakt for related or community-recommended items
- normalize those IDs
- map them back to Plex candidates
- boost candidates that appear in Trakt’s community recommendation graph

### Seed strategies
Use one or more of:
- most recently watched highly rated items
- top weighted items in the preference profile
- a currently selected source item for item-to-item recommendations

### Score blending
Start with:

- content score: `0.70`
- Trakt community score: `0.30`

Tune later based on observed quality.

### Tasks
- implement `TraktScorer`
- define a seed-item selection strategy
- normalize Trakt results into the local media identity format
- add score blending in `RecommendationService`

### Deliverables
- collaborative/community recommendation capability
- hybrid scoring pipeline

---

## Phase 8: Add fallback behavior

### Goal
Ensure the engine still works if Trakt is unavailable.

### Rules
- if Trakt fails, return content-only recommendations
- if external IDs cannot be matched, ignore unmatched Trakt results
- if rate limits are hit, use cached Trakt results or skip Trakt scoring

### Tasks
- implement external scorer timeouts
- wrap Trakt calls in retry/fallback logic
- log every failed Trakt lookup and ID mismatch

### Deliverables
- resilient hybrid recommender
- no hard dependency on Trakt availability

---

## Phase 9: Improve explanations

### Goal
Keep recommendation output interpretable even after adding hybrid scoring.

### Explanation categories
- content reasons:
  - genres matched
  - director matched
  - cast overlap
  - franchise/collection overlap
  - writer overlap
- community reasons:
  - similar users liked this
  - related to a top seed title
  - strong Trakt recommendation confidence

### Tasks
- extend result payload schema
- include separate content score and Trakt score
- include a final combined explanation block

### Deliverables
- clearer recommendation transparency
- easier debugging and tuning

---

## Phase 10: Testing and rollout

### Goal
Validate quality before making hybrid mode the default.

### Test scenarios
- movie-to-movie recommendation sanity checks
- TV show mapping and episode-to-series normalization
- unwatched-only enforcement
- diversity constraints working as intended
- Trakt unavailable fallback path
- bad metadata edge cases
- duplicate title / remake matching edge cases

### Rollout strategy
- release behind a feature flag
- compare content-only and hybrid results
- tune weights after observation
- promote hybrid mode to default once stable

### Deliverables
- reliable rollout process
- measurable quality improvements

---

## Suggested implementation order

### Sprint 1
- refactor into modules
- improve cast handling
- add richer metadata dimensions
- add ID normalization

### Sprint 2
- add caching
- add reranking
- improve explanation structure

### Sprint 3
- add Trakt scorer
- add fallback behavior
- tune score blending

### Sprint 4
- validate with real watch history
- compare content-only vs hybrid results
- finalize production weights

---

## Success criteria

The upgrade is successful if:

- recommendations are still based on unwatched local Plex content
- actor matching is broader and less brittle
- results are more relevant and less repetitive
- hybrid recommendations meaningfully improve "X leads to Y" discovery
- Trakt outages do not break recommendations
- recommendation explanations remain understandable

---

## Final design principle

Keep Plex as the source of truth for:
- local library availability
- watch history
- personal taste profile

Use Trakt only as:
- community intelligence
- collaborative enrichment
- an additional signal in a hybrid score

That keeps the system stable, explainable, and locally grounded.