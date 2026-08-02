"""
MAL ID Mapper for AnimeGG
═════════════════════════
Fetches animegg_all_series1.json from a GitHub raw URL, queries the
MyAnimeList API v2 for every title, and writes four result files:

    results/exact_matching.json          ← exact title / alt-title match
    results/fuzzy_matching.json          ← substring / partial match
    results/first_results_fallback.json  ← top MAL result used as fallback
    results/not_matching.json            ← nothing found on MAL

Environment variables (set as GitHub Actions secrets / vars):
    MAL_CLIENT_ID      – MAL API v2 client id
    INPUT_JSON_URL     – raw GitHub URL to animegg_all_series1.json

Usage (local):
    pip install requests
    MAL_CLIENT_ID=xxx INPUT_JSON_URL=https://raw.githubusercontent.com/... python mal_mapper.py
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID      = os.environ.get("MAL_CLIENT_ID", "b0f57250436db633080e10767f2dab54").strip()
INPUT_JSON_URL = os.environ.get("INPUT_JSON_URL", "https://raw.githubusercontent.com/ytbro8326-sudo/mal_mapper/refs/heads/main/animegg_all_series1.json").strip()

MAL_SEARCH_URL   = "https://api.myanimelist.net/v2/anime"
RATE_LIMIT_DELAY = 0.5   # seconds between MAL requests  (~2 req/s, limit is 3)
MAX_RETRIES      = 3
RETRY_DELAY      = 6     # seconds to wait after 429 / 5xx

RESULTS_DIR = Path("results")
OUT_EXACT   = RESULTS_DIR / "exact_matching.json"
OUT_FUZZY   = RESULTS_DIR / "fuzzy_matching.json"
OUT_FIRST   = RESULTS_DIR / "first_results_fallback.json"
OUT_NONE    = RESULTS_DIR / "not_matching.json"
# ──────────────────────────────────────────────────────────────────────────────


# ── Helpers ───────────────────────────────────────────────────────────────────
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


def search_mal(title: str, session: requests.Session) -> dict | None:
    """
    Query MAL for *title*. Returns:
        {"id": int, "mal_title": str, "confidence": "exact"|"fuzzy"|"first"}
    or None if nothing found after all retries.
    """
    params  = {"q": title, "limit": 5, "fields": "id,title,alternative_titles"}
    headers = {"X-MAL-CLIENT-ID": CLIENT_ID}

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

            # ── 1. Exact match ────────────────────────────────────────────────
            for item in data:
                node = item["node"]
                if any(normalize(t) == norm_q for t in collect_all_titles(node)):
                    return {"id": node["id"], "mal_title": node["title"],
                            "confidence": "exact"}

            # ── 2. Fuzzy (substring) match ────────────────────────────────────
            for item in data:
                node = item["node"]
                if any(
                    norm_q in normalize(t) or normalize(t) in norm_q
                    for t in collect_all_titles(node)
                ):
                    return {"id": node["id"], "mal_title": node["title"],
                            "confidence": "fuzzy"}

            # ── 3. First-result fallback ──────────────────────────────────────
            first = data[0]["node"]
            return {"id": first["id"], "mal_title": first["title"],
                    "confidence": "first"}

        except requests.RequestException as exc:
            print(f"    ⚠  Network error (attempt {attempt}): {exc}")
            time.sleep(RETRY_DELAY)

    return None


def build_entry(anime: dict, result: dict | None) -> dict:
    """
    Merge original anime fields with MAL result fields into one output record.
    """
    base = {
        "title":         anime.get("title"),
        "uri":           anime.get("uri"),
        "url":           anime.get("url"),
        "thumbnail_url": anime.get("thumbnail_url"),
        "episode_count": anime.get("episode_count"),
        "status":        anime.get("status"),
        "genres":        anime.get("genres", []),
        "description":   anime.get("description"),
    }
    if result:
        base["mal_id"]    = result["id"]
        base["mal_title"] = result["mal_title"]
    else:
        base["mal_id"]    = None
        base["mal_title"] = None
    return base


def save(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"    💾  {path}  ({len(records)} entries)")
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    # ── Pre-flight checks ─────────────────────────────────────────────────────

    session    = requests.Session()
    anime_list = load_anime_list(INPUT_JSON_URL, session)
    total      = len(anime_list)
    print(f"📋  {total} anime entries loaded.\n{'─'*60}")

    # ── Buckets ───────────────────────────────────────────────────────────────
    exact_matches:  list[dict] = []
    fuzzy_matches:  list[dict] = []
    first_fallback: list[dict] = []
    not_found:      list[dict] = []

    # ── Main loop ─────────────────────────────────────────────────────────────
    for i, anime in enumerate(anime_list, 1):
        title = (anime.get("title") or "").strip()
        if not title:
            print(f"[{i:>4}/{total}] ⚠  Skipping entry with no title.")
            continue

        print(f"[{i:>4}/{total}] 🔎  '{title}' …", end=" ", flush=True)

        result = search_mal(title, session)

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
            print(f"🟡  fuzzy      mal_id={result['id']}  → '{result['mal_title']}'")

        else:  # "first"
            entry = build_entry(anime, result)
            first_fallback.append(entry)
            print(f"🟠  fallback   mal_id={result['id']}  → '{result['mal_title']}'")

        # Save progress every 100 entries
        if i % 100 == 0:
            print(f"\n    ── Checkpoint {i}/{total} ──")
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

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  ✅  Exact matches          : {len(exact_matches)}")
    print(f"  🟡  Fuzzy matches          : {len(fuzzy_matches)}")
    print(f"  🟠  First-result fallbacks : {len(first_fallback)}")
    print(f"  ❌  Not found              : {len(not_found)}")
    print(f"  ─────────────────────────────")
    print(f"  📋  Total processed        : {total}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
