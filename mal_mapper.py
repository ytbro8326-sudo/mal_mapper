"""
MAL ID Mapper for AnimeGG  ·  v2 (crash-safe, resumable)
══════════════════════════════════════════════════════════
Fetches animegg_all_series1.json from a GitHub raw URL, queries the
MyAnimeList API v2 for every title, and writes four result files:

    results/exact_matching.json          ← exact title / alt-title match
    results/fuzzy_matching.json          ← substring / partial match
    results/first_results_fallback.json  ← top MAL result used as fallback
    results/not_matching.json            ← nothing found on MAL

── Crash-safe design ────────────────────────────────────────────────────────
Every processed entry is immediately appended as a single JSON line to:

    results/progress.jsonl

On restart the script reads that file to rebuild the four in-memory buckets
and skips every title already recorded — so zero work is ever repeated or
lost, even if the process is killed mid-run (power cut, OOM, timeout, etc.).

When the run completes cleanly a sentinel file is written:

    results/.done

Re-running after a clean finish prints a summary and exits immediately
(pass --force to override and start fresh).

Environment variables (set as GitHub Actions secrets / vars):
    MAL_CLIENT_ID      – MAL API v2 client id
    INPUT_JSON_URL     – raw GitHub URL to animegg_all_series1.json

Usage (local):
    pip install requests
    MAL_CLIENT_ID=xxx INPUT_JSON_URL=https://raw.githubusercontent.com/... python mal_mapper.py
    python mal_mapper.py --force      # wipe progress and start over
"""

import argparse
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
    "https://raw.githubusercontent.com/ytbro8326-sudo/mal_mapper/refs/heads/main/animegg_all_series1.json",
).strip()

MAL_SEARCH_URL   = "https://api.myanimelist.net/v2/anime"
RATE_LIMIT_DELAY = 0.5   # seconds between MAL requests  (~2 req/s, limit is 3)
MAX_RETRIES      = 3
RETRY_DELAY      = 6     # seconds to wait after 429 / 5xx

RESULTS_DIR   = Path("results")
OUT_EXACT     = RESULTS_DIR / "exact_matching.json"
OUT_FUZZY     = RESULTS_DIR / "fuzzy_matching.json"
OUT_FIRST     = RESULTS_DIR / "first_results_fallback.json"
OUT_NONE      = RESULTS_DIR / "not_matching.json"
PROGRESS_FILE = RESULTS_DIR / "progress.jsonl"   # one JSON object per line
DONE_FLAG     = RESULTS_DIR / ".done"            # written only on clean finish
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
            resp = session.get(
                MAL_SEARCH_URL, params=params, headers=headers, timeout=10
            )

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
    """Merge original anime fields with MAL result fields into one output record."""
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


# ── Progress file I/O ─────────────────────────────────────────────────────────

def load_progress() -> tuple[set[str], list[dict], list[dict], list[dict], list[dict]]:
    """
    Read progress.jsonl and rebuild the four in-memory buckets.

    Returns:
        seen_titles   – set of already-processed titles (for fast skip check)
        exact, fuzzy, first_fb, not_found  – rebuilt result lists
    """
    seen:      set[str]  = set()
    exact:     list[dict] = []
    fuzzy:     list[dict] = []
    first_fb:  list[dict] = []
    not_found: list[dict] = []

    if not PROGRESS_FILE.exists():
        return seen, exact, fuzzy, first_fb, not_found

    corrupted = 0
    with open(PROGRESS_FILE, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                corrupted += 1
                print(f"    ⚠  Skipping corrupted progress line {lineno}")
                continue

            title      = record.get("title") or ""
            confidence = record.get("_confidence")

            if title:
                seen.add(title)

            if confidence == "exact":
                exact.append(record)
            elif confidence == "fuzzy":
                fuzzy.append(record)
            elif confidence == "first":
                first_fb.append(record)
            else:
                not_found.append(record)

    if corrupted:
        print(f"    ⚠  {corrupted} corrupted line(s) skipped in progress file.")

    return seen, exact, fuzzy, first_fb, not_found


def append_progress(entry: dict, confidence: str | None) -> None:
    """
    Atomically append a single processed record to progress.jsonl.

    We store '_confidence' as a private field so we can rebuild buckets on
    resume without any separate index file. The field is stripped before the
    final result files are written.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    record = dict(entry)                 # shallow copy
    record["_confidence"] = confidence   # None  → not_found bucket

    # 'a' mode guarantees the write goes to the end of the file; even if the
    # process is killed, a complete JSON line is either fully written or not
    # written at all (OS kernel buffers the line before flushing).
    with open(PROGRESS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()          # push to OS buffer
        os.fsync(fh.fileno())  # force OS buffer → disk


def strip_private(records: list[dict]) -> list[dict]:
    """Remove the internal _confidence field before writing final result files."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]


def save_final(path: Path, records: list[dict]) -> None:
    """Write a clean result JSON file (strips internal fields)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = strip_private(records)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    print(f"    💾  {path}  ({len(clean)} entries)")


def print_summary(exact, fuzzy, first_fb, not_found, total) -> None:
    processed = len(exact) + len(fuzzy) + len(first_fb) + len(not_found)
    print(f"\n{'═'*60}")
    print(f"  ✅  Exact matches          : {len(exact)}")
    print(f"  🟡  Fuzzy matches          : {len(fuzzy)}")
    print(f"  🟠  First-result fallbacks : {len(first_fb)}")
    print(f"  ❌  Not found              : {len(not_found)}")
    print(f"  ─────────────────────────────")
    print(f"  📋  Processed / Total      : {processed} / {total}")
    print(f"{'═'*60}")

# ──────────────────────────────────────────────────────────────────────────────


def main() -> None:
    # ── CLI ───────────────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="MAL ID Mapper (crash-safe)")
    parser.add_argument(
        "--force", action="store_true",
        help="Wipe existing progress and start from scratch."
    )
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Force-reset ───────────────────────────────────────────────────────────
    if args.force:
        for f in (PROGRESS_FILE, DONE_FLAG, OUT_EXACT, OUT_FUZZY, OUT_FIRST, OUT_NONE):
            if f.exists():
                f.unlink()
        print("🔄  --force: wiped all previous progress. Starting fresh.\n")

    # ── Already done? ─────────────────────────────────────────────────────────
    if DONE_FLAG.exists():
        print("✅  Previous run completed successfully (.done flag found).")
        print("    Use --force to re-run from scratch.\n")
        # Still print a summary from the progress file so CI logs are useful.
        _, exact, fuzzy, first_fb, not_found = load_progress()
        total = len(exact) + len(fuzzy) + len(first_fb) + len(not_found)
        print_summary(exact, fuzzy, first_fb, not_found, total)
        return

    # ── Load previous progress ────────────────────────────────────────────────
    seen_titles, exact_matches, fuzzy_matches, first_fallback, not_found = load_progress()
    if seen_titles:
        print(
            f"🔁  Resuming: {len(seen_titles)} entries already processed "
            f"({len(exact_matches)} exact / {len(fuzzy_matches)} fuzzy / "
            f"{len(first_fallback)} fallback / {len(not_found)} not-found).\n"
        )

    # ── Fetch source list ─────────────────────────────────────────────────────
    session    = requests.Session()
    anime_list = load_anime_list(INPUT_JSON_URL, session)
    total      = len(anime_list)
    print(f"📋  {total} anime entries loaded.\n{'─'*60}")

    # ── Main loop ─────────────────────────────────────────────────────────────
    for i, anime in enumerate(anime_list, 1):
        title = (anime.get("title") or "").strip()

        if not title:
            print(f"[{i:>5}/{total}] ⚠  Skipping entry with no title.")
            continue

        # ── Skip if already processed in a prior run ──────────────────────────
        if title in seen_titles:
            # (no print to keep resume output clean; add a print here if you want verbose resume)
            continue

        print(f"[{i:>5}/{total}] 🔎  '{title}' …", end=" ", flush=True)

        result = search_mal(title, session)
        entry  = build_entry(anime, result)

        if result is None:
            not_found.append(entry)
            append_progress(entry, None)
            print("❌  not found")

        elif result["confidence"] == "exact":
            exact_matches.append(entry)
            append_progress(entry, "exact")
            print(f"✅  exact      mal_id={result['id']}  → '{result['mal_title']}'")

        elif result["confidence"] == "fuzzy":
            fuzzy_matches.append(entry)
            append_progress(entry, "fuzzy")
            print(f"🟡  fuzzy      mal_id={result['id']}  → '{result['mal_title']}'")

        else:  # "first"
            first_fallback.append(entry)
            append_progress(entry, "first")
            print(f"🟠  fallback   mal_id={result['id']}  → '{result['mal_title']}'")

        seen_titles.add(title)

        # ── Checkpoint: re-write the four result files every 100 entries ───────
        # The progress.jsonl already has everything; this just keeps the result
        # files fresh so they are readable mid-run (e.g. from another terminal).
        processed_so_far = len(exact_matches) + len(fuzzy_matches) + len(first_fallback) + len(not_found)
        if processed_so_far % 100 == 0:
            print(f"\n    ── Checkpoint {processed_so_far}/{total} ──")
            save_final(OUT_EXACT, exact_matches)
            save_final(OUT_FUZZY, fuzzy_matches)
            save_final(OUT_FIRST, first_fallback)
            save_final(OUT_NONE,  not_found)
            print()

        time.sleep(RATE_LIMIT_DELAY)

    # ── Final save ────────────────────────────────────────────────────────────
    print(f"\n{'─'*60}")
    print("💾  Writing final result files …")
    save_final(OUT_EXACT, exact_matches)
    save_final(OUT_FUZZY, fuzzy_matches)
    save_final(OUT_FIRST, first_fallback)
    save_final(OUT_NONE,  not_found)

    # Write the done sentinel — only on clean completion.
    DONE_FLAG.write_text("done\n", encoding="utf-8")
    print(f"    🏁  {DONE_FLAG}  (clean-finish sentinel)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(exact_matches, fuzzy_matches, first_fallback, not_found, total)


if __name__ == "__main__":
    main()
