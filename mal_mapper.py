"""
MAL ID Mapper for AnimeGG
═════════════════════════
Fetches animegg_all_series1.json from a GitHub raw URL, queries the
MyAnimeList API v2 for every title, and writes four result files:

    results/exact_matching.json          ← exact title / alt-title match
    results/fuzzy_matching.json          ← substring / partial match
    results/first_results_fallback.json  ← top MAL result used as fallback
    results/not_matching.json            ← nothing found on MAL

Progress & Resume
─────────────────
Every 100 processed entries the script atomically flushes all four result
files AND writes results/progress.json:

    {
      "last_index":  <int>,   ← last i that was fully processed (1-based)
      "processed":   [...]    ← sorted list of titles already handled
    }

On the next run (or after a crash) the script:
  1. Reads progress.json to find last_index.
  2. Re-loads the four result files to rebuild in-memory state.
  3. Skips every anime whose title is already in the processed set.
  4. Continues from where it left off.

Environment variables:
    MAL_CLIENT_ID      – MAL API v2 client id
    INPUT_JSON_URL     – raw GitHub URL to animegg_all_series1.json
    RESUME             – set to "true" to force resume mode (default: auto)

Usage (local):
    pip install requests
    MAL_CLIENT_ID=xxx INPUT_JSON_URL=https://... python mal_mapper.py
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
INPUT_JSON_URL = os.environ.get(
    "INPUT_JSON_URL",
    "https://raw.githubusercontent.com/ytbro8326-sudo/mal_mapper/refs/heads/main/animegg_all_series1.json"
).strip()

MAL_SEARCH_URL   = "https://api.myanimelist.net/v2/anime"
RATE_LIMIT_DELAY = 0.5   # seconds between MAL requests  (~2 req/s, limit is 3)
MAX_RETRIES      = 3
RETRY_DELAY      = 6     # seconds to wait after 429 / 5xx
CHECKPOINT_EVERY = 100   # save progress + flush result files every N entries

RESULTS_DIR = Path("results")
OUT_EXACT    = RESULTS_DIR / "exact_matching.json"
OUT_FUZZY    = RESULTS_DIR / "fuzzy_matching.json"
OUT_FIRST    = RESULTS_DIR / "first_results_fallback.json"
OUT_NONE     = RESULTS_DIR / "not_matching.json"
OUT_PROGRESS = RESULTS_DIR / "progress.json"
# ──────────────────────────────────────────────────────────────────────────────


# ── Helpers ───────────────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    """Lowercase, strip punctuation – used for title comparison."""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def atomic_write_json(path: Path, data: list | dict) -> None:
    """
    Write *data* as JSON to a temporary file then atomically rename it to
    *path*.  This guarantees the destination file is never in a half-written
    state even if the process is killed mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)   # atomic on POSIX; near-atomic on Windows


def load_json_file(path: Path) -> list | dict | None:
    """Load a JSON file, returning None if it does not exist or is corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"    ⚠  Could not read {path}: {exc}")
        return None


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
            resp = session.get(
                MAL_SEARCH_URL, params=params, headers=headers, timeout=10
            )

            if resp.status_code == 429:
                wait = RETRY_DELAY * attempt
                print(f"\n    ⚠  Rate-limited — waiting {wait}s …", end=" ")
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                sys.exit("❌  Unauthorised — check MAL_CLIENT_ID secret.")

            if not resp.ok:
                print(f"\n    ⚠  HTTP {resp.status_code} (attempt {attempt}/{MAX_RETRIES})", end=" ")
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
                    return {
                        "id": node["id"],
                        "mal_title": node["title"],
                        "confidence": "exact",
                    }

            # ── 2. Fuzzy (substring) match ────────────────────────────────────
            for item in data:
                node = item["node"]
                if any(
                    norm_q in normalize(t) or normalize(t) in norm_q
                    for t in collect_all_titles(node)
                ):
                    return {
                        "id": node["id"],
                        "mal_title": node["title"],
                        "confidence": "fuzzy",
                    }

            # ── 3. First-result fallback ──────────────────────────────────────
            first = data[0]["node"]
            return {
                "id": first["id"],
                "mal_title": first["title"],
                "confidence": "first",
            }

        except requests.RequestException as exc:
            print(f"\n    ⚠  Network error (attempt {attempt}): {exc}", end=" ")
            time.sleep(RETRY_DELAY)

    return None


def build_entry(anime: dict, result: dict | None) -> dict:
    """Merge original anime fields with MAL result into one output record."""
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


# ── Progress / Resume ─────────────────────────────────────────────────────────
def load_progress() -> tuple[int, set[str]]:
    """
    Read progress.json.
    Returns (last_index, processed_title_set).
    last_index = 0 means nothing processed yet.
    """
    raw = load_json_file(OUT_PROGRESS)
    if not raw or not isinstance(raw, dict):
        return 0, set()

    last_index = int(raw.get("last_index", 0))
    processed  = set(raw.get("processed", []))
    return last_index, processed


def restore_buckets() -> tuple[list, list, list, list]:
    """
    Re-load the four result files into memory so we can continue appending.
    Returns (exact, fuzzy, first, not_found) as lists (empty if file missing).
    """
    def _load(path: Path) -> list:
        data = load_json_file(path)
        return data if isinstance(data, list) else []

    return (
        _load(OUT_EXACT),
        _load(OUT_FUZZY),
        _load(OUT_FIRST),
        _load(OUT_NONE),
    )


def flush_checkpoint(
    exact:   list[dict],
    fuzzy:   list[dict],
    first:   list[dict],
    none:    list[dict],
    last_index: int,
    processed:  set[str],
) -> None:
    """
    Atomically write all four result files + progress.json.
    Called every CHECKPOINT_EVERY entries and at end-of-run.
    """
    atomic_write_json(OUT_EXACT, exact)
    atomic_write_json(OUT_FUZZY, fuzzy)
    atomic_write_json(OUT_FIRST, first)
    atomic_write_json(OUT_NONE,  none)
    atomic_write_json(
        OUT_PROGRESS,
        {
            "last_index": last_index,
            "processed":  sorted(processed),   # sorted for human readability
        },
    )
    total_saved = len(exact) + len(fuzzy) + len(first) + len(none)
    print(
        f"\n    💾  Checkpoint @ entry {last_index} — "
        f"saved {total_saved} records total  "
        f"(✅{len(exact)} 🟡{len(fuzzy)} 🟠{len(first)} ❌{len(none)})"
    )
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    session    = requests.Session()
    anime_list = load_anime_list(INPUT_JSON_URL, session)
    total      = len(anime_list)
    print(f"📋  {total} anime entries loaded.\n{'─'*60}")

    # ── Resume detection ──────────────────────────────────────────────────────
    last_index, processed_titles = load_progress()

    if last_index > 0:
        print(
            f"♻️   Resume detected — {last_index} entries previously processed "
            f"({len(processed_titles)} unique titles).\n"
            f"    Restoring result buckets from disk …"
        )
        exact_matches, fuzzy_matches, first_fallback, not_found = restore_buckets()
        print(
            f"    Restored  ✅{len(exact_matches)}  🟡{len(fuzzy_matches)}  "
            f"🟠{len(first_fallback)}  ❌{len(not_found)}\n{'─'*60}"
        )
    else:
        print("🆕  Fresh run — no previous progress found.\n" + "─"*60)
        exact_matches:  list[dict] = []
        fuzzy_matches:  list[dict] = []
        first_fallback: list[dict] = []
        not_found:      list[dict] = []

    # ── Main loop ─────────────────────────────────────────────────────────────
    #
    # Design decisions:
    #   • We iterate with the *original* index `i` (1-based) so that
    #     last_index stored in progress.json always matches the source list
    #     position regardless of skips.
    #   • `entries_this_run` counts only NEW work done in this run so that
    #     the CHECKPOINT_EVERY cadence fires correctly even after a resume.
    #   • The `processed_titles` set is the dedup guard; title is the natural
    #     key because that is what we query MAL with anyway.
    #
    entries_this_run = 0

    for i, anime in enumerate(anime_list, 1):

        # ── Skip already-processed entries (resume) ───────────────────────────
        title = (anime.get("title") or "").strip()
        if not title:
            print(f"[{i:>5}/{total}] ⚠  Skipping entry with no title.")
            continue

        if title in processed_titles:
            # Already handled in a previous run — skip silently.
            continue

        # ── Query MAL ─────────────────────────────────────────────────────────
        print(f"[{i:>5}/{total}] 🔎  '{title}' …", end=" ", flush=True)
        result = search_mal(title, session)

        if result is None:
            not_found.append(build_entry(anime, None))
            print("❌  not found")

        elif result["confidence"] == "exact":
            exact_matches.append(build_entry(anime, result))
            print(f"✅  exact     mal_id={result['id']}  → '{result['mal_title']}'")

        elif result["confidence"] == "fuzzy":
            fuzzy_matches.append(build_entry(anime, result))
            print(f"🟡  fuzzy     mal_id={result['id']}  → '{result['mal_title']}'")

        else:   # "first"
            first_fallback.append(build_entry(anime, result))
            print(f"🟠  fallback  mal_id={result['id']}  → '{result['mal_title']}'")

        # Mark as processed
        processed_titles.add(title)
        last_index        = i
        entries_this_run += 1

        # ── Periodic checkpoint every CHECKPOINT_EVERY new entries ────────────
        #
        # Why track `entries_this_run` and not `i`?
        #   On a resume run `i` starts higher than 0, so `i % 100 == 0` would
        #   only fire at multiples of 100 from the *source list* index, not
        #   after every 100 *new* entries processed this run.  We want the
        #   checkpoint cadence to be consistent regardless of resume offset.
        #
        if entries_this_run % CHECKPOINT_EVERY == 0:
            flush_checkpoint(
                exact_matches, fuzzy_matches, first_fallback, not_found,
                last_index, processed_titles,
            )
            print()   # blank line for readability

        time.sleep(RATE_LIMIT_DELAY)

    # ── Final flush ───────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("💾  Writing final result files …")
    flush_checkpoint(
        exact_matches, fuzzy_matches, first_fallback, not_found,
        last_index, processed_titles,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print(f"  ✅  Exact matches          : {len(exact_matches)}")
    print(f"  🟡  Fuzzy matches          : {len(fuzzy_matches)}")
    print(f"  🟠  First-result fallbacks : {len(first_fallback)}")
    print(f"  ❌  Not found              : {len(not_found)}")
    print(f"  ─────────────────────────────────────────")
    print(f"  📋  Total processed        : {total}")
    print(f"  🔄  New this run           : {entries_this_run}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
