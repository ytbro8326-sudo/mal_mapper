"""
MAL ID Mapper for AnimeGG
═════════════════════════
Fetches animegg_all_series1.json from a GitHub raw URL, queries the
MyAnimeList API v2 for every title, and writes four result files:

    results/exact_matching.json          ← exact title / alt-title match
    results/fuzzy_matching.json          ← substring / partial match
    results/first_results_fallback.json  ← top MAL result used as fallback
    results/not_matching.json            ← nothing found on MAL

Resume / Crash-safety
─────────────────────
Every 100 processed entries the script atomically saves all four result
files AND writes results/progress.json:

    { "last_index": 4200, "processed": ["One Piece", "Naruto", ...] }

If the run is interrupted (crash, timeout, Ctrl+C) the NEXT run:
  1. Reads progress.json → learns last_index + already-processed titles.
  2. Reloads all four result files back into memory.
  3. Skips titles already in the processed set.
  4. Continues from exactly where it stopped.

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
CHECKPOINT_EVERY = 100   # flush every N *newly processed* entries

RESULTS_DIR  = Path("results")
OUT_EXACT    = RESULTS_DIR / "exact_matching.json"
OUT_FUZZY    = RESULTS_DIR / "fuzzy_matching.json"
OUT_FIRST    = RESULTS_DIR / "first_results_fallback.json"
OUT_NONE     = RESULTS_DIR / "not_matching.json"
OUT_PROGRESS = RESULTS_DIR / "progress.json"   # ← new: resume tracker
# ──────────────────────────────────────────────────────────────────────────────


# ── Helpers (unchanged from original) ────────────────────────────────────────
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
# ──────────────────────────────────────────────────────────────────────────────


# ── Resume helpers (new) ──────────────────────────────────────────────────────
def _read_json(path: Path) -> list | dict | None:
    """Safely read a JSON file; return None if missing or corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _atomic_save(path: Path, data: list | dict) -> None:
    """
    Write *data* to a .tmp file then rename it over *path*.
    On Linux (GitHub Actions) os.replace() is atomic — the destination
    file is either the old version or the new version, never half-written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_progress() -> tuple[set[str], list, list, list, list]:
    """
    Read progress.json + the four result files.

    Returns:
        processed_titles  – set of title strings already handled
        exact, fuzzy, first_fb, none_found  – lists restored from disk
                                              (all empty on a fresh run)
    """
    prog = _read_json(OUT_PROGRESS)
    if not prog:
        # Fresh run
        return set(), [], [], [], []

    processed_titles = set(prog.get("processed", []))

    def _load_list(path: Path) -> list:
        data = _read_json(path)
        return data if isinstance(data, list) else []

    exact    = _load_list(OUT_EXACT)
    fuzzy    = _load_list(OUT_FUZZY)
    first_fb = _load_list(OUT_FIRST)
    none_fnd = _load_list(OUT_NONE)

    print(
        f"♻️   Resume detected — {len(processed_titles)} titles already processed.\n"
        f"    Restored  ✅{len(exact)}  🟡{len(fuzzy)}  🟠{len(first_fb)}  ❌{len(none_fnd)}\n"
        f"{'─'*60}"
    )
    return processed_titles, exact, fuzzy, first_fb, none_fnd


def flush(
    exact: list, fuzzy: list, first_fb: list, none_fnd: list,
    processed_titles: set[str], checkpoint_label: str,
) -> None:
    """
    Atomically write all four result files + progress.json, then print a
    one-line summary.  Called every CHECKPOINT_EVERY entries and at end-of-run.
    """
    _atomic_save(OUT_EXACT, exact)
    _atomic_save(OUT_FUZZY, fuzzy)
    _atomic_save(OUT_FIRST, first_fb)
    _atomic_save(OUT_NONE,  none_fnd)
    _atomic_save(OUT_PROGRESS, {
        "processed": sorted(processed_titles),   # sorted = human-readable diffs
    })
    total = len(exact) + len(fuzzy) + len(first_fb) + len(none_fnd)
    print(
        f"\n    💾  [{checkpoint_label}] saved {total} records total"
        f"  (✅{len(exact)} 🟡{len(fuzzy)} 🟠{len(first_fb)} ❌{len(none_fnd)})\n"
    )
# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    session    = requests.Session()
    anime_list = load_anime_list(INPUT_JSON_URL, session)
    total      = len(anime_list)
    print(f"📋  {total} anime entries loaded.\n{'─'*60}")

    # ── Resume or fresh start ─────────────────────────────────────────────────
    processed_titles, exact_matches, fuzzy_matches, first_fallback, not_found = \
        load_progress()

    if not processed_titles:
        print(f"🆕  Fresh run — no previous progress found.\n{'─'*60}")

    # ── Main loop ─────────────────────────────────────────────────────────────
    #
    # `new_this_run` counts entries processed IN THIS EXECUTION only.
    # This keeps the checkpoint cadence correct even on a resume:
    # e.g. if we resume at entry 4150, the first checkpoint still fires
    # after 100 new entries (at 4250), not at the next multiple of 100
    # from the source-list index.
    #
    new_this_run = 0

    for i, anime in enumerate(anime_list, 1):
        title = (anime.get("title") or "").strip()

        if not title:
            print(f"[{i:>5}/{total}] ⚠  Skipping entry with no title.")
            continue

        # ── Skip already-processed titles (resume path) ───────────────────────
        if title in processed_titles:
            continue

        # ── Query MAL ─────────────────────────────────────────────────────────
        print(f"[{i:>5}/{total}] 🔎  '{title}' …", end=" ", flush=True)
        result = search_mal(title, session)

        if result is None:
            not_found.append(build_entry(anime, None))
            print("❌  not found")

        elif result["confidence"] == "exact":
            exact_matches.append(build_entry(anime, result))
            print(f"✅  exact      mal_id={result['id']}  → '{result['mal_title']}'")

        elif result["confidence"] == "fuzzy":
            fuzzy_matches.append(build_entry(anime, result))
            print(f"🟡  fuzzy      mal_id={result['id']}  → '{result['mal_title']}'")

        else:  # "first"
            first_fallback.append(build_entry(anime, result))
            print(f"🟠  fallback   mal_id={result['id']}  → '{result['mal_title']}'")

        processed_titles.add(title)
        new_this_run += 1

        # ── Checkpoint every 100 NEW entries ──────────────────────────────────
        if new_this_run % CHECKPOINT_EVERY == 0:
            flush(
                exact_matches, fuzzy_matches, first_fallback, not_found,
                processed_titles,
                checkpoint_label=f"{i}/{total}",
            )

        time.sleep(RATE_LIMIT_DELAY)

    # ── Final save ────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("💾  Writing final result files …")
    flush(
        exact_matches, fuzzy_matches, first_fallback, not_found,
        processed_titles,
        checkpoint_label="FINAL",
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"{'═'*60}")
    print(f"  ✅  Exact matches          : {len(exact_matches)}")
    print(f"  🟡  Fuzzy matches          : {len(fuzzy_matches)}")
    print(f"  🟠  First-result fallbacks : {len(first_fallback)}")
    print(f"  ❌  Not found              : {len(not_found)}")
    print(f"  ─────────────────────────────")
    print(f"  📋  Total processed        : {total}")
    print(f"  🔄  New this run           : {new_this_run}")
    print(f"{'═'*60}")


if __name__ == "__main__":
    main()
