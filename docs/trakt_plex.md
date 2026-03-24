# Plan to Incorporate Trakt Into a Plex Install

## Objective
Incorporate Trakt into the Plex environment so that:

- Plex remains the source of local library and watch activity
- Trakt receives synced playback/rating activity where supported
- the recommendation engine can use Trakt as a community-signal provider
- the system remains functional even if Trakt is unavailable

This plan covers both:
- operational setup between Plex and Trakt
- application-level integration for the recommender

---

## Integration model

### Core principle
Do not think of Trakt as replacing Plex.

Instead:

- **Plex** = source of local truth
- **Trakt** = external community signal and profile mirror
- **your app** = hybrid recommendation layer

### Practical implication
Use Plex for:
- what exists in the local library
- what has been watched locally
- what is available to recommend

Use Trakt for:
- community recommendation signals
- external recommendation relationships
- optional backup history/rating enrichment

---

## Phase 1: Confirm prerequisites

### Plex requirements
- Plex server owner/admin account access
- Plex Pass if webhook-based event forwarding is required
- stable metadata matching for movies and shows
- clean library configuration

### Trakt requirements
- a Trakt account
- API application credentials if your app will call Trakt directly
- a decision on whether you also want Trakt profile sync/scrobbling enabled

### Tasks
- verify Plex Pass status
- confirm which Plex libraries will be included
- create or confirm Trakt account
- review Trakt account tier/features if needed
- verify that your Plex metadata is reasonably clean before syncing

### Deliverables
- known-good admin access
- validated accounts and prerequisites

---

## Phase 2: Clean Plex metadata and IDs

### Goal
Make sure external matching works reliably.

### Why
Trakt integration quality depends heavily on accurate media IDs.

### Tasks
- inspect movie and show GUIDs in Plex
- refresh metadata on badly matched titles
- fix obvious title/year mismatches
- check that popular titles resolve to external IDs consistently
- decide whether problematic libraries should be excluded initially

### Important identifiers to support
- Plex GUID
- IMDb ID
- TMDb ID
- TVDb ID
- media type

### Deliverables
- cleaner Plex metadata baseline
- fewer ID mismatches later

---

## Phase 3: Decide sync scope

### Goal
Define exactly what Trakt should receive from Plex.

### Options
You may choose to sync some or all of:
- watched history
- playback/scrobble activity
- ratings
- collection/library state

### Recommended starting scope
Start with:
- watched history
- scrobble/playback activity
- ratings if you use ratings consistently

Avoid adding more until the core flow is verified.

### Deliverables
- clear sync policy
- smaller surface area for troubleshooting

---

## Phase 4: Connect Plex to Trakt operationally

### Goal
Enable Plex activity to reach Trakt.

### Recommended setup
Use Trakt’s supported Plex integration flow first rather than inventing a custom bridge immediately.

### Tasks
- enable webhook support in Plex if required
- configure the Trakt Plex integration according to Trakt’s current setup path
- authorize Trakt access where required
- trigger playback events from Plex
- verify that Trakt receives and records the events

### What to verify
- watched events
- scrobbled events
- rating updates
- movie matching
- TV episode matching

### Deliverables
- working Plex-to-Trakt sync path
- confirmed event flow

---

## Phase 5: Validate sync behavior

### Goal
Make sure synced activity behaves correctly before using Trakt as part of recommendations.

### Test cases
- start a movie and stop early
- watch beyond scrobble threshold
- rate a movie in Plex
- rate an episode or show if applicable
- rewatch an item
- test a movie with a straightforward metadata match
- test a show with multiple seasons and episodes
- test one awkwardly matched title

### Validation checklist
- no duplicate watches unexpectedly created
- ratings appear correctly
- TV episodes map correctly
- newly played items appear in Trakt within expected time
- bad metadata cases are logged for later cleanup

### Deliverables
- known sync quality baseline
- a list of metadata problems to correct

---

## Phase 6: Add Trakt to your application architecture

### Goal
Use Trakt from your recommender app, not just as a passive sync target.

### Components to add
- `TraktProvider`
- `TraktAuthManager`
- `IdNormalizer`
- `CollaborativeScorer`
- `ExternalRecommendationCache`

### Responsibilities

#### `TraktProvider`
- talks to Trakt APIs
- fetches related/recommended items
- normalizes result payloads
- handles retries and rate limits

#### `TraktAuthManager`
- stores and refreshes credentials if user-authenticated endpoints are needed
- keeps secrets out of the scoring code

#### `CollaborativeScorer`
- computes Trakt-based score boosts
- combines seed item relationships with local candidate lists

#### `ExternalRecommendationCache`
- caches Trakt responses
- reduces repeated API calls
- supports degraded mode during outages

### Deliverables
- app-level Trakt integration layer
- no direct Trakt calls scattered across the recommender

---

## Phase 7: Build media ID matching between Trakt and Plex

### Goal
Map Trakt items back to actual local Plex items.

### Matching order
1. IMDb ID
2. TMDb ID
3. TVDb ID
4. GUID equivalence
5. title + year fallback only if necessary

### Rules
- if a Trakt item does not match a Plex item, ignore it
- if it matches a watched Plex item, exclude or down-rank it
- if it matches a non-library item, ignore it unless you want out-of-library suggestions

### Tasks
- create normalized media identity objects for both systems
- add matching logs for every failed match
- maintain statistics for mismatch causes

### Deliverables
- reliable bridge between Trakt recommendations and Plex candidates

---

## Phase 8: Define seed strategies for Trakt scoring

### Goal
Choose how your app decides which items to use as Trakt community seeds.

### Candidate seed strategies
- most recently watched highly rated Plex items
- top weighted items from the preference profile
- a single currently viewed item for "similar to this" flows
- a rolling profile seed set from the last N meaningful watches

### Recommended first approach
Use:
- the top 3–5 recent highly rated items
- filtered by media type
- excluding items with poor metadata quality

### Why
This keeps Trakt calls focused and avoids a noisy seed set.

### Deliverables
- deterministic seed selection policy
- predictable Trakt request pattern

---

## Phase 9: Blend Trakt with the local Plex recommender

### Goal
Use Trakt as an enrichment layer, not the sole recommendation source.

### Score model
Recommended starting weights:
- content score from Plex metadata: `0.70`
- Trakt community score: `0.30`

Tune later after testing.

### Rules
- local library availability is mandatory
- unwatched status is mandatory unless a different mode is desired
- Trakt can boost a candidate but should not override obvious local irrelevance
- if Trakt data is missing, fall back to content-only scoring

### Deliverables
- hybrid scoring logic
- stable fallback behavior

---

## Phase 10: Add operational safeguards

### Goal
Make the integration robust in production.

### Safeguards to implement
- request timeouts for Trakt calls
- retry with backoff where appropriate
- local caching of recommendation responses
- feature flag to disable Trakt scoring quickly
- logging for:
  - API failures
  - rate limits
  - ID mismatches
  - unmatched titles
  - scoring distribution

### Why
External APIs fail. The recommendation engine should remain usable regardless.

### Deliverables
- resilient external integration
- easier troubleshooting

---

## Phase 11: Decide what not to sync or not to use

### Goal
Prevent the integration from becoming noisy or misleading.

### Recommendations
Initially avoid:
- obscure or low-quality libraries
- heavily mismatched titles
- specials/extras unless episode mapping is solid
- old ratings imported from messy historical metadata without validation

### Deliverables
- cleaner initial rollout
- reduced sync noise

---

## Phase 12: Test end-to-end recommendation behavior

### Goal
Confirm that the Trakt integration actually improves recommendations.

### End-to-end scenarios
- recent sci-fi watches should surface community-adjacent sci-fi titles already in Plex
- a selected seed item should produce sensible "similar to this" unwatched results
- results should not collapse into one actor/director/franchise
- hybrid mode should outperform content-only mode on discovery
- Trakt downtime should still yield content-only results

### Metrics to observe
- match rate from Trakt item to Plex item
- fraction of recommendations receiving Trakt boosts
- duplicate recommendation patterns
- latency impact
- user-perceived quality improvement

### Deliverables
- validation that Trakt adds real value
- tuning inputs for score weights

---

## Phase 13: Rollout strategy

### Step 1
Enable Plex-to-Trakt sync and validate it operationally.

### Step 2
Integrate Trakt into the app behind a feature flag.

### Step 3
Run content-only and hybrid recommendations side by side.

### Step 4
Tune score blending and reranking rules.

### Step 5
Promote hybrid mode to default once stable.

### Deliverables
- controlled rollout
- minimized production risk

---

## Suggested implementation order

### Sprint 1
- confirm prerequisites
- clean Plex metadata
- configure Plex-to-Trakt sync
- validate watch/rating flow

### Sprint 2
- implement `TraktProvider`
- implement media ID matching
- add caching and logging

### Sprint 3
- add collaborative scoring
- blend with existing content scorer
- add fallback logic

### Sprint 4
- test recommendation quality
- tune weights
- enable by default if stable

---

## Success criteria

The Trakt incorporation is successful if:

- Plex remains the authoritative source for local recommendations
- Trakt receives reliable playback/rating activity where intended
- Trakt items can be matched back to Plex with a high success rate
- hybrid recommendations improve discovery without breaking relevance
- Trakt outages or API issues do not break the recommender
- troubleshooting data is sufficient to debug mismatch and sync issues

---

## Final design principle

Use Trakt to answer:
- what the wider community tends to like around item X

Use Plex to answer:
- what this user actually has
- what this user actually watched
- what this user can be recommended right now

Keep those responsibilities separate and the system will stay much easier to reason about.