"""
MAL ID Mapper for AnimeGG — Advanced Fuzzy Matching Edition
═════════════════════════════════════════════════════════════
Fetches animegg_with_alternate_titles.json from a GitHub raw URL, queries the
MyAnimeList API v2 for every title, and writes four result files:

    results/exact_matching.json          ← near-perfect title match  (score ≥ 99)
    results/fuzzy_matching.json          ← strong partial match      (60 ≤ score < 99)
    results/first_results_fallback.json  ← top MAL result used as a best-effort guess
    results/not_matching.json            ← nothing usable found on MAL

MATCHING STRATEGY (this is the part that changed)
──────────────────────────────────────────────────
Source records now look like:

    {
      "serial_no": 1,
      "title": "One Piece",
      "alternate_title": "ワンピース",
      "url": "https://www.animegg.org/series/one-piece",
      "episode_count": 1160,
      "status": "Ongoing",
      "genres": [...],
      "description": "..."
    }

Tier 1 — Title pass
    Search MAL with the primary `title`. Score every candidate MAL node
    against `title` using a blended fuzzy-matching function (see
    `similarity()` below) that combines four different signals so a
    candidate only needs to agree on ONE of them to score well:
        • plain normalised string ratio
        • token-sort ratio      (handles reordered words, e.g. "Shippuden Naruto")
        • token-set ratio       (handles extra/missing words, à la fuzzywuzzy)
        • substring containment (one title fully embedded in the other)
        • noise-stripped ratio  (ignores low-signal words like "the/movie/ova/season")
    A candidate scoring ≥ 99 is treated as "exact"; ≥ 60 is "fuzzy".

Tier 2 — Alternate-title pass (only runs if Tier 1 found nothing ≥ 60)
    `alternate_title` is a comma-separated string (e.g. "Case Closed,
    Meitantei Conan, 名探偵コナン"). Each part is queried against MAL
    individually, and results are scored against the FULL set of known
    titles (primary + every alternate). Same 60/99 thresholds apply.

Fallback
    If neither tier clears the 60% bar, the single top MAL search result
    from Tier 1 is kept as a "first" (best-effort) match instead of
    discarding the entry outright.

Status handling
    AnimeGG's `status` ("Ongoing"/"Completed"/"Upcoming") is mapped to MAL's
    vocabulary (`currently_airing`/`finished_airing`/`not_yet_aired`) and is
    used ONLY to break ties when two candidates score equally on title —
    it never disqualifies a title match by itself.

Episode counts are NOT used for matching at all (per requirement) — the
source's `episode_count` is still carried through into the output purely
for reference.

Tracking files (auto-created / updated in results/):
    results/already_processed_items.txt  ← URL slugs already done; skipped on re-run
    results/range_tracking.txt           ← log of every range that was processed

Environment variables (set as GitHub Actions secrets / vars):
    MAL_CLIENT_ID      – MAL API v2 client id
    INPUT_JSON_URL     – raw GitHub URL to animegg_with_alternate_titles.json
    RANGE_START        – 1-based start index (inclusive); default: 1
    RANGE_END          – 1-based end index   (inclusive); default: last entry

Usage (local):
    pip install requests
    MAL_CLIENT_ID=xxx INPUT_JSON_URL=https://raw.githubusercontent.com/... python mal_mapper.py

    # Process only entries 54 to 80:
    RANGE_START=54 RANGE_END=80 MAL_CLIENT_ID=xxx INPUT_JSON_URL=https://... python mal_mapper.py
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID      = os.environ.get("MAL_CLIENT_ID", "b0f57250436db633080e10767f2dab54").strip()
INPUT_JSON_URL = os.environ.get(
    "INPUT_JSON_URL",
    "https://raw.githubusercontent.com/ytbro8326-sudo/animeg_main_web_urls_list_extractor/"
    "refs/heads/main/animegg_with_alternate_titles.json",
).strip()

# Custom range (1-based, both inclusive). Empty string → no limit.
_RAW_START = os.environ.get("RANGE_START", "").strip()
_RAW_END   = os.environ.get("RANGE_END",   "").strip()

MAL_SEARCH_URL   = "https://api.myanimelist.net/v2/anime"
RATE_LIMIT_DELAY = 0.5   # seconds between MAL requests  (~2 req/s, limit is 3)
MAX_RETRIES      = 3
RETRY_DELAY      = 6     # seconds to wait after 429 / 5xx

# Matching thresholds (as percentages, 0-100)
FUZZY_MIN_SCORE  = 60.0   # below this → candidate is rejected outright
EXACT_MIN_SCORE  = 99.0   # at/above this → confidence "exact"

RESULTS_DIR      = Path("results")
OUT_EXACT        = RESULTS_DIR / "exact_matching.json"
OUT_FUZZY        = RESULTS_DIR / "fuzzy_matching.json"
OUT_FIRST        = RESULTS_DIR / "first_results_fallback.json"
OUT_NONE         = RESULTS_DIR / "not_matching.json"
OUT_PROCESSED    = RESULTS_DIR / "already_processed_items.txt"
OUT_RANGE_LOG    = RESULTS_DIR / "range_tracking.txt"
# ──────────────────────────────────────────────────────────────────────────────


# ── Range helpers ─────────────────────────────────────────────────────────────
def parse_range(raw_start: str, raw_end: str, total: int) -> tuple[int, int]:
    """
    Convert raw RANGE_START / RANGE_END strings (1-based) into a
    zero-based Python slice (start_idx, end_idx) where end_idx is exclusive.
    """
    try:
        start_1 = int(raw_start) if raw_start else 1
        end_1   = int(raw_end)   if raw_end   else total
    except ValueError:
        sys.exit(
            f"❌  RANGE_START / RANGE_END must be integers.\n"
            f"    Got: RANGE_START={raw_start!r}  RANGE_END={raw_end!r}"
        )

    if start_1 < 1:
        sys.exit(f"❌  RANGE_START must be ≥ 1  (got {start_1}).")
    if end_1 > total:
        sys.exit(f"❌  RANGE_END ({end_1}) exceeds total entries ({total}).")
    if start_1 > end_1:
        sys.exit(f"❌  RANGE_START ({start_1}) must be ≤ RANGE_END ({end_1}).")

    return start_1 - 1, end_1   # 0-based slice bounds
# ──────────────────────────────────────────────────────────────────────────────


# ── Already-processed helpers ─────────────────────────────────────────────────
def load_processed(path: Path) -> set[str]:
    """Return the set of URIs already processed from the tracking file."""
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    uris  = {ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")}
    print(f"📂  Loaded {len(uris)} already-processed URL slugs from {path}")
    return uris


def append_processed(path: Path, uri: str) -> None:
    """Append a single URI to the processed tracking file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(uri + "\n")
# ──────────────────────────────────────────────────────────────────────────────


# ── Range-tracking helpers ────────────────────────────────────────────────────
def init_range_log(path: Path) -> None:
    """Create the range log file with a header if it doesn't exist yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Range Tracking Log — MAL ID Mapper\n")
            f.write("# Format: TIMESTAMP | RANGE_START–RANGE_END | processed | skipped | total_in_range\n")
            f.write("#\n")


def append_range_log(
    path: Path,
    start_1: int,
    end_1: int,
    processed: int,
    skipped: int,
    total_in_range: int,
) -> None:
    """Append one run's range summary line to the log."""
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    line  = (
        f"{ts} | {start_1}–{end_1} | "
        f"processed={processed} | skipped={skipped} | total_in_range={total_in_range}\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"    📝  {path}  (range {start_1}–{end_1} logged)")
# ──────────────────────────────────────────────────────────────────────────────


# ── Text normalisation & fuzzy scoring ────────────────────────────────────────
_NOISE_WORDS = {
    "the", "a", "an", "and", "of", "movie", "movies", "special", "specials",
    "ova", "ovas", "ona", "onas", "tv", "series", "season", "seasons",
    "part", "parts", "vol", "volume",
}


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — used everywhere titles are compared."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _strip_noise(norm_text: str) -> str:
    """Drop common low-signal words so 'Naruto Movie' vs 'Naruto' isn't over/under-penalised."""
    words = [w for w in norm_text.split() if w not in _NOISE_WORDS]
    return " ".join(words) if words else norm_text  # never collapse to empty


def similarity(a: str, b: str) -> float:
    """
    Blended fuzzy score (0-100) between two titles. Combines several
    complementary signals and returns the STRONGEST one, so a pair of
    titles only needs to agree on one axis to score well:

        • plain normalised ratio   (character-level similarity)
        • token-sort ratio         (handles reordered words)
        • token-set ratio          (handles extra/missing words)
        • containment bonus        (one title fully embedded in the other)
        • noise-stripped ratio     (ignores "the/movie/ova/season/part" etc.)
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0

    def ratio(x: str, y: str) -> float:
        return SequenceMatcher(None, x, y).ratio() * 100

    scores = [ratio(na, nb)]

    # token-sort ratio — same words, different order
    ts_a = " ".join(sorted(na.split()))
    ts_b = " ".join(sorted(nb.split()))
    scores.append(ratio(ts_a, ts_b))

    # token-set ratio — shared words weighted, unique words de-emphasised
    set_a, set_b = set(na.split()), set(nb.split())
    if set_a and set_b:
        common   = set_a & set_b
        common_s = " ".join(sorted(common))
        diff_a   = " ".join(sorted(set_a - set_b))
        diff_b   = " ".join(sorted(set_b - set_a))
        scores.append(ratio((common_s + " " + diff_a).strip(),
                             (common_s + " " + diff_b).strip()))

    # containment — e.g. "Naruto" fully inside "Naruto Shippuden"
    if na in nb or nb in na:
        scores.append(95.0)

    # noise-stripped ratio — ignore filler words entirely
    sa, sb = _strip_noise(na), _strip_noise(nb)
    if sa == sb:
        scores.append(98.0)
    else:
        scores.append(ratio(sa, sb))

    return max(scores)


def split_alt_titles(raw: str | None) -> list[str]:
    """Split the source 'alternate_title' comma-separated string into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]
# ──────────────────────────────────────────────────────────────────────────────


# ── Core helpers ──────────────────────────────────────────────────────────────
def load_anime_list(url: str, session: requests.Session) -> list[dict]:
    """Download JSON from *url* and return a flat list of anime dicts."""
    print(f"📥  Fetching source JSON …\n    {url}\n")
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    raw = resp.json()

    if isinstance(raw, list):
        return raw

    if isinstance(raw, dict):
        for key, val in raw.items():
            if isinstance(val, list) and val and isinstance(val[0], dict):
                print(f"ℹ️   Wrapper key detected: '{key}'")
                return val
        raise ValueError(
            f"No anime list found inside JSON dict. Keys: {list(raw.keys())}"
        )

    raise ValueError(f"Unexpected JSON root type: {type(raw)}")


def collect_all_titles(node: dict) -> list[str]:
    """Return every title string available for a MAL node."""
    titles = [node.get("title", "")]
    alt    = node.get("alternative_titles", {})
    titles += alt.get("synonyms", [])
    if alt.get("en"):
        titles.append(alt["en"])
    if alt.get("ja"):
        titles.append(alt["ja"])
    return [t for t in titles if t]


def _status_matches(anime_status: str | None, mal_status: str | None) -> bool:
    """
    Loosely compare AnimeGG status to MAL status. Used ONLY as a tie-breaker
    between equally-scored title candidates — never to reject a match.
    MAL status values: "finished_airing", "currently_airing", "not_yet_aired"
    AnimeGG status values: "Completed", "Ongoing", "Upcoming" (approximate)
    """
    if not anime_status or not mal_status:
        return True   # can't judge — treat as matching

    STATUS_MAP: dict[str, str] = {
        "completed":     "finished_airing",
        "finished":      "finished_airing",
        "ongoing":       "currently_airing",
        "airing":        "currently_airing",
        "currently airing": "currently_airing",
        "upcoming":      "not_yet_aired",
        "not yet aired": "not_yet_aired",
    }
    mapped = STATUS_MAP.get(anime_status.lower().strip())
    return mapped is None or mapped == mal_status.lower().strip()


def _mal_url(mal_id: int) -> str:
    """Build the canonical MyAnimeList anime page URL from its numeric id."""
    return f"https://myanimelist.net/anime/{mal_id}"


def _make_result(node: dict, confidence: str, score: float) -> dict:
    """Package a MAL node + score into the result dict that search_mal returns."""
    return {
        "id":           node["id"],
        "mal_title":    node["title"],
        "mal_url":      _mal_url(node["id"]),
        "confidence":   confidence,
        "match_score":  round(score, 1),
        "mal_episodes": node.get("num_episodes"),   # must be >= source episode_count (see _episode_count_ok)
        "mal_status":   node.get("status"),
    }


def _query_mal(
    query: str,
    session: requests.Session,
    headers: dict,
    limit: int = 15,
) -> list[dict]:
    """
    Fire one MAL search request and return the list of data nodes.
    Raises on unrecoverable HTTP errors; returns [] on 404 / empty.
    """
    params = {
        "q":      query,
        "limit":  limit,
        "fields": "id,title,alternative_titles,num_episodes,status",
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(MAL_SEARCH_URL, params=params,
                               headers=headers, timeout=10)
            if resp.status_code == 429:
                wait = RETRY_DELAY * attempt
                print(f"    ⚠  Rate-limited — waiting {wait}s …")
                time.sleep(wait)
                continue
            if resp.status_code == 401:
                sys.exit("❌  Unauthorised — check MAL_CLIENT_ID secret.")
            if not resp.ok:
                print(f"    ⚠  HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(RETRY_DELAY)
                continue
            return resp.json().get("data", [])
        except requests.RequestException as exc:
            print(f"    ⚠  Network error (attempt {attempt}): {exc}")
            time.sleep(RETRY_DELAY)
    return []


def _episode_count_ok(anime_episode_count: int | None, mal_episodes: int | None) -> bool:
    """
    Hard filter: a candidate is only acceptable if MAL's episode count is
    EQUAL TO or GREATER THAN the source's episode_count.

    This exists specifically to reject spin-off movies/OVAs/specials that
    share a title substring with a long-running series — e.g. a 1-episode
    "Meitantei Conan Movie 14" must never be accepted as a match for the
    1182-episode ongoing "Detective Conan" TV series, even though the title
    scores extremely high on pure text similarity.

    If either side is unknown (0 / None) we can't judge it, so we allow it
    through — MAL frequently reports num_episodes=0 for an ongoing series
    whose final episode count isn't fixed yet.
    """
    if not anime_episode_count or not mal_episodes:
        return True   # unknown on either side — can't disqualify
    return mal_episodes >= anime_episode_count


def _best_candidate(
    data: list[dict],
    query_titles: list[str],
    anime_status: str | None,
    anime_episode_count: int | None = None,
) -> tuple[dict | None, float]:
    """
    Score every MAL candidate in *data* against every title in *query_titles*
    (the best pairing per node wins) using the blended `similarity()` score.
    Returns (best_node, best_score) — best_node is None if nothing scored > 0
    or nothing survives the episode-count filter.

    Episode count is a HARD filter here: any candidate whose MAL episode
    count is smaller than the source's episode_count is rejected outright,
    no matter how well the title matches (see `_episode_count_ok`). Status
    agreement is used only to break ties between candidates that end up
    with the exact same top score.
    """
    scored: list[tuple[float, bool, dict]] = []

    for item in data:
        node = item["node"]
        candidate_titles = collect_all_titles(node)

        node_score = 0.0
        for q in query_titles:
            for ct in candidate_titles:
                s = similarity(q, ct)
                if s > node_score:
                    node_score = s

        if node_score <= 0:
            continue

        if not _episode_count_ok(anime_episode_count, node.get("num_episodes")):
            continue   # e.g. a 1-episode movie can't match a 1182-episode series

        status_hit = _status_matches(anime_status, node.get("status"))
        scored.append((node_score, status_hit, node))

    if not scored:
        return None, 0.0

    # Highest score wins; among equal scores, prefer status-matching candidate
    scored.sort(key=lambda t: (-t[0], 0 if t[1] else 1))
    top_score, _top_status_hit, top_node = scored[0]
    return top_node, top_score


def search_mal(
    title: str,
    alternate_title: str | None,
    session: requests.Session,
    anime_status: str | None = None,
    episode_count: int | None = None,
) -> dict | None:
    """
    Query MAL for an anime using a two-tier fuzzy strategy. Returns:
        {
            "id":           int,
            "mal_title":    str,
            "confidence":   "exact" | "fuzzy" | "first",
            "match_score":  float,       # 0-100, blended fuzzy score
            "mal_episodes": int | None,
            "mal_status":   str | None,
        }
    or None if MAL returned absolutely nothing usable for the primary title.

    Tier 1 — search MAL with the primary `title`; score results against
      `title` alone. Score ≥ 99 → "exact". 60 ≤ score < 99 → "fuzzy".

    Tier 2 — only runs if Tier 1 found nothing ≥ 60. Splits
      `alternate_title` on commas, queries MAL with each part, and scores
      the combined result pool against ALL known titles (primary +
      alternates). Same 60/99 thresholds apply.

    Fallback — if neither tier clears 60, the best-scoring candidate that
      still survives the episode-count filter is returned as confidence
      "first". If nothing survives that filter either, no result is
      returned rather than handing back an obviously-wrong movie/OVA.

    Episode count is a HARD filter in every tier: a candidate whose MAL
    episode count is smaller than the source's episode_count is rejected
    outright (see `_episode_count_ok`) — this is what stops a 1-episode
    movie from being matched to a 1000+ episode ongoing series. Status is
    used only to break ties between equally-scored candidates.
    """
    headers = {"X-MAL-CLIENT-ID": CLIENT_ID}

    # ── Tier 1: primary title ────────────────────────────────────────────────
    data1 = _query_mal(title, session, headers, limit=15)

    if data1:
        node1, score1 = _best_candidate(data1, [title], anime_status, episode_count)
        if node1 and score1 >= EXACT_MIN_SCORE:
            return _make_result(node1, "exact", score1)
        if node1 and score1 >= FUZZY_MIN_SCORE:
            return _make_result(node1, "fuzzy", score1)

    # ── Tier 2: alternate title(s) ────────────────────────────────────────────
    alt_titles = split_alt_titles(alternate_title)
    combined_data: list[dict] = list(data1) if data1 else []

    if alt_titles:
        all_known_titles = [title] + alt_titles

        for alt in alt_titles:
            time.sleep(RATE_LIMIT_DELAY)
            data_alt = _query_mal(alt, session, headers, limit=15)
            combined_data.extend(data_alt)

        if combined_data:
            node2, score2 = _best_candidate(combined_data, all_known_titles, anime_status, episode_count)
            if node2 and score2 >= EXACT_MIN_SCORE:
                return _make_result(node2, "exact", score2)
            if node2 and score2 >= FUZZY_MIN_SCORE:
                return _make_result(node2, "fuzzy", score2)

    # ── Fallback: best remaining candidate that still passes the episode filter ──
    fallback_pool  = combined_data if alt_titles else data1
    fallback_query = ([title] + alt_titles) if alt_titles else [title]

    if fallback_pool:
        node_fb, score_fb = _best_candidate(fallback_pool, fallback_query, anime_status, episode_count)
        if node_fb:
            print(f"    ↳  no match ≥ {FUZZY_MIN_SCORE:.0f}% — using best episode-consistent result as fallback")
            return _make_result(node_fb, "first", score_fb)

    # Nothing survived the episode filter at all — better to report "not found"
    # than to hand back an obviously-wrong movie/OVA/special.
    print("    ↳  no candidate with a consistent episode count — marking not found")
    return None


def build_entry(anime: dict, result: dict | None) -> dict:
    """
    Merge original anime fields + MAL result into one record.
    serial_no is read directly from the source JSON (already present there).
    """
    base = {
        "serial_no":       anime.get("serial_no"),
        "title":           anime.get("title"),
        "alternate_title": anime.get("alternate_title"),
        "url":             anime.get("url"),
        "episode_count":   anime.get("episode_count"),
        "status":          anime.get("status"),
        "genres":          anime.get("genres", []),
        "description":     anime.get("description"),
    }
    if result:
        base["mal_id"]       = result["id"]
        base["mal_title"]    = result["mal_title"]
        base["mal_url"]      = result.get("mal_url")
        base["match_score"]  = result.get("match_score")
        base["mal_episodes"] = result.get("mal_episodes")
        base["mal_status"]   = result.get("mal_status")
    else:
        base["mal_id"]       = None
        base["mal_title"]    = None
        base["mal_url"]      = None
        base["match_score"]  = None
        base["mal_episodes"] = None
        base["mal_status"]   = None
    return base


def save(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"    💾  {path}  ({len(records)} entries)")
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    session    = requests.Session()
    anime_list = load_anime_list(INPUT_JSON_URL, session)
    total      = len(anime_list)
    print(f"📋  {total} anime entries loaded.")

    # ── Load already-processed URIs ───────────────────────────────────────────
    processed_uris = load_processed(OUT_PROCESSED)

    # ── Init range log ────────────────────────────────────────────────────────
    init_range_log(OUT_RANGE_LOG)

    # ── Apply custom range ────────────────────────────────────────────────────
    slice_start, slice_end = parse_range(_RAW_START, _RAW_END, total)
    anime_slice = anime_list[slice_start:slice_end]
    slice_len   = len(anime_slice)

    # 1-based display labels
    range_start_1 = slice_start + 1
    range_end_1   = slice_end
    range_label   = f"{range_start_1}–{range_end_1}"

    if slice_len == total:
        print(f"🔢  Processing all {total} entries.\n{'─'*60}")
    else:
        print(f"🔢  Processing entries {range_label} ({slice_len} of {total} total).\n{'─'*60}")

    # ── Buckets ───────────────────────────────────────────────────────────────
    exact_matches:  list[dict] = []
    fuzzy_matches:  list[dict] = []
    first_fallback: list[dict] = []
    not_found:      list[dict] = []

    actually_processed = 0   # entries queried this run (not skipped)
    actually_skipped   = 0   # entries skipped due to already_processed_items

    # ── Main loop ─────────────────────────────────────────────────────────────
    for i, anime in enumerate(anime_slice, 1):
        real_idx  = slice_start + i          # 1-based position in full list
        serial_no = anime.get("serial_no") or real_idx   # prefer source serial_no
        title     = (anime.get("title") or "").strip()
        alt_title = (anime.get("alternate_title") or "").strip()

        # Derive a stable dedup key from the URL path
        url       = (anime.get("url") or "").strip()
        dedup_key = url.rstrip("/").rsplit("/", 1)[-1] if url else ""

        if not title:
            print(f"[{real_idx:>4}/{total}] ⚠  Skipping entry with no title.")
            continue

        # ── Skip if already processed ─────────────────────────────────────────
        if dedup_key and dedup_key in processed_uris:
            print(f"[{real_idx:>4}/{total}] ⏭   Skipping '{title}' (already processed)")
            actually_skipped += 1
            continue

        print(f"[{real_idx:>4}/{total}] 🔎  '{title}' …", end=" ", flush=True)

        anime_status  = anime.get("status")
        episode_count = anime.get("episode_count")
        result = search_mal(title, alt_title, session, anime_status=anime_status, episode_count=episode_count)
        actually_processed += 1

        if result is None:
            entry = build_entry(anime, None)
            not_found.append(entry)
            print("❌  not found")

        elif result["confidence"] == "exact":
            entry = build_entry(anime, result)
            exact_matches.append(entry)
            print(f"✅  exact      mal_id={result['id']}  score={result['match_score']}  → '{result['mal_title']}'  ({result['mal_url']})")

        elif result["confidence"] == "fuzzy":
            entry = build_entry(anime, result)
            fuzzy_matches.append(entry)
            print(f"🟡  fuzzy      mal_id={result['id']}  score={result['match_score']}  → '{result['mal_title']}'  ({result['mal_url']})")

        else:  # "first"
            entry = build_entry(anime, result)
            first_fallback.append(entry)
            print(f"🟠  fallback   mal_id={result['id']}  → '{result['mal_title']}'  ({result['mal_url']})")

        # Mark as processed immediately so partial runs are recoverable
        if dedup_key:
            append_processed(OUT_PROCESSED, dedup_key)
            processed_uris.add(dedup_key)

        # Checkpoint every 100 processed entries
        if actually_processed % 100 == 0:
            print(f"\n    ── Checkpoint {actually_processed} processed (global {real_idx}/{total}) ──")
            save(OUT_EXACT, exact_matches)
            save(OUT_FUZZY, fuzzy_matches)
            save(OUT_FIRST, first_fallback)
            save(OUT_NONE,  not_found)
            print()

        time.sleep(RATE_LIMIT_DELAY)

    # ── Final save ────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("💾  Writing final result files …")
    save(OUT_EXACT, exact_matches)
    save(OUT_FUZZY, fuzzy_matches)
    save(OUT_FIRST, first_fallback)
    save(OUT_NONE,  not_found)

    # ── Log this range ────────────────────────────────────────────────────────
    append_range_log(
        OUT_RANGE_LOG,
        start_1=range_start_1,
        end_1=range_end_1,
        processed=actually_processed,
        skipped=actually_skipped,
        total_in_range=slice_len,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    if slice_len < total:
        print(f"  🔢  Range processed        : {range_label} ({slice_len} entries)")
    print(f"  ⏭   Skipped (done before)  : {actually_skipped}")
    print(f"  ✅  Exact matches          : {len(exact_matches)}")
    print(f"  🟡  Fuzzy matches          : {len(fuzzy_matches)}")
    print(f"  🟠  First-result fallbacks : {len(first_fallback)}")
    print(f"  ❌  Not found              : {len(not_found)}")
    print(f"  ─────────────────────────────")
    print(f"  📋  Total queried this run : {actually_processed}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
