"""
MAL ID Mapper for AnimeGG
═════════════════════════
Fetches animegg_all_series1.json from a GitHub raw URL, queries the
MyAnimeList API v2 for every title, and writes four result files:

    results/exact_matching.json          ← exact title / alt-title match
    results/fuzzy_matching.json          ← substring / partial match
    results/first_results_fallback.json  ← top MAL result used as fallback
    results/not_matching.json            ← nothing found on MAL

Tracking files (auto-created / updated in results/):
    results/already_processed_items.txt  ← URL slugs already done; skipped on re-run
    results/range_tracking.txt           ← log of every range that was processed

Environment variables (set as GitHub Actions secrets / vars):
    MAL_CLIENT_ID      – MAL API v2 client id
    INPUT_JSON_URL     – raw GitHub URL to animegg_all_series1.json
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
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID      = os.environ.get("MAL_CLIENT_ID", "b0f57250436db633080e10767f2dab54").strip()
INPUT_JSON_URL = os.environ.get("INPUT_JSON_URL", "https://raw.githubusercontent.com/ytbro8326-sudo/mal_mapper/refs/heads/main/animegg_all_series1.json").strip()

# Custom range (1-based, both inclusive). Empty string → no limit.
_RAW_START = os.environ.get("RANGE_START", "").strip()
_RAW_END   = os.environ.get("RANGE_END",   "").strip()

MAL_SEARCH_URL   = "https://api.myanimelist.net/v2/anime"
RATE_LIMIT_DELAY = 0.5   # seconds between MAL requests  (~2 req/s, limit is 3)
MAX_RETRIES      = 3
RETRY_DELAY      = 6     # seconds to wait after 429 / 5xx

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


# ── Core helpers ──────────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    """Lowercase, strip punctuation – used for title comparison."""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


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
    Loosely compare AnimeGG status to MAL status.
    Returns True when they agree, or when either side is unknown.
    MAL status values: "finished_airing", "currently_airing", "not_yet_aired"
    AnimeGG status values: "Completed", "Ongoing", "Upcoming" (approximate)
    """
    if not anime_status or not mal_status:
        return True   # can't judge — treat as matching

    STATUS_MAP: dict[str, str] = {
        "completed":  "finished_airing",
        "finished":   "finished_airing",
        "ongoing":    "currently_airing",
        "airing":     "currently_airing",
        "upcoming":   "not_yet_aired",
        "not yet aired": "not_yet_aired",
    }
    mapped = STATUS_MAP.get(anime_status.lower().strip())
    return mapped is None or mapped == mal_status.lower().strip()


def _make_result(node: dict, confidence: str) -> dict:
    """Package a MAL node into the result dict that search_mal returns."""
    return {
        "id":           node["id"],
        "mal_title":    node["title"],
        "confidence":   confidence,
        "mal_episodes": node.get("num_episodes"),   # 0 = unknown on MAL side
        "mal_status":   node.get("status"),
    }


def search_mal(
    title: str,
    session: requests.Session,
    episode_count: int | None = None,
    anime_status: str | None = None,
) -> dict | None:
    """
    Query MAL for *title*. Returns:
        {
            "id":           int,
            "mal_title":    str,
            "confidence":   "exact" | "fuzzy" | "first",
            "mal_episodes": int | None,
            "mal_status":   str | None,
        }
    or None if nothing found after all retries.

    Fuzzy and first-result confidence levels require the MAL episode count to
    match *episode_count* (when both sides are known and non-zero).
    Status is used as a secondary signal: a status mismatch downgrades a
    fuzzy candidate but does not discard it outright.
    """
    params  = {"q": title, "limit": 5, "fields": "id,title,alternative_titles,num_episodes,status"}
    headers = {"X-MAL-CLIENT-ID": CLIENT_ID}

    def episodes_ok(node: dict) -> bool:
        """True when episode counts are compatible (missing/0 on either side = OK)."""
        mal_eps = node.get("num_episodes") or 0
        src_eps = episode_count or 0
        if mal_eps == 0 or src_eps == 0:
            return True           # unknown on one side — can't disqualify
        return mal_eps == src_eps

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

            data = resp.json().get("data", [])
            if not data:
                return None

            norm_q = normalize(title)

            # ── 1. Exact title match (episode count not required) ─────────────
            for item in data:
                node = item["node"]
                if any(normalize(t) == norm_q for t in collect_all_titles(node)):
                    return _make_result(node, "exact")

            # ── 2. Fuzzy (substring) match — episode count MUST match ─────────
            #
            # Priority order within fuzzy:
            #   a) title substring match  +  episodes match  +  status match   → best fuzzy
            #   b) title substring match  +  episodes match  (status optional)  → accept
            #   c) title substring match  +  episodes mismatch                  → skip
            #
            fuzzy_candidates = []
            for item in data:
                node = item["node"]
                title_hit = any(
                    norm_q in normalize(t) or normalize(t) in norm_q
                    for t in collect_all_titles(node)
                )
                if not title_hit:
                    continue
                if not episodes_ok(node):
                    # Title matched but episode count is wrong — reject
                    print(
                        f"    ↳  skipping '{node['title']}' "
                        f"(ep mismatch: source={episode_count} mal={node.get('num_episodes')})"
                    )
                    continue
                status_hit = _status_matches(anime_status, node.get("status"))
                fuzzy_candidates.append((node, status_hit))

            if fuzzy_candidates:
                # Prefer candidates where status also matches
                fuzzy_candidates.sort(key=lambda x: (0 if x[1] else 1))
                best_node, _ = fuzzy_candidates[0]
                return _make_result(best_node, "fuzzy")

            # ── 3. First-result fallback — episode count MUST match ───────────
            #
            # Walk through results in order; use the first one whose episode
            # count agrees.  If none agree, return None (→ not_matching).
            #
            for item in data:
                node = item["node"]
                if episodes_ok(node):
                    return _make_result(node, "first")

            # All candidates have mismatched episode counts
            print(
                f"    ↳  all {len(data)} MAL results have episode-count mismatches "
                f"(source={episode_count}) — marking not found"
            )
            return None

        except requests.RequestException as exc:
            print(f"    ⚠  Network error (attempt {attempt}): {exc}")
            time.sleep(RETRY_DELAY)

    return None


def build_entry(anime: dict, result: dict | None) -> dict:
    """
    Merge original anime fields + MAL result into one record.
    serial_no is read directly from the source JSON (already present there).
    """
    base = {
        "serial_no":     anime.get("serial_no"),
        "title":         anime.get("title"),
        "url":           anime.get("url"),
        "episode_count": anime.get("episode_count"),
        "status":        anime.get("status"),
        "genres":        anime.get("genres", []),
        "description":   anime.get("description"),
    }
    if result:
        base["mal_id"]       = result["id"]
        base["mal_title"]    = result["mal_title"]
        base["mal_episodes"] = result.get("mal_episodes")
        base["mal_status"]   = result.get("mal_status")
    else:
        base["mal_id"]       = None
        base["mal_title"]    = None
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

        # Derive a stable dedup key from the URL path (no uri field in new schema)
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

        episode_count = anime.get("episode_count")
        anime_status  = anime.get("status")
        result = search_mal(title, session, episode_count=episode_count, anime_status=anime_status)
        actually_processed += 1

        if result is None:
            entry = build_entry(anime, None)
            not_found.append(entry)
            print("❌  not found")

        elif result["confidence"] == "exact":
            entry = build_entry(anime, result)
            exact_matches.append(entry)
            print(f"✅  exact      mal_id={result['id']}  → '{result['mal_title']}'")

        elif result["confidence"] == "fuzzy":
            entry = build_entry(anime, result)
            fuzzy_matches.append(entry)
            print(
                f"🟡  fuzzy      mal_id={result['id']}  → '{result['mal_title']}'"
                f"  [eps src={episode_count} mal={result.get('mal_episodes')}]"
            )

        else:  # "first"
            entry = build_entry(anime, result)
            first_fallback.append(entry)
            print(
                f"🟠  fallback   mal_id={result['id']}  → '{result['mal_title']}'"
                f"  [eps src={episode_count} mal={result.get('mal_episodes')}]"
            )

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
