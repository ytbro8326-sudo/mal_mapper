"""
MAL ID Mapper for AnimeGG  ·  v3 (non-blocking background saves)
═════════════════════════════════════════════════════════════════
Fetches animegg_all_series1.json from a GitHub raw URL, queries the
MyAnimeList API v2 for every title, and writes four result files:

    results/exact_matching.json          ← exact title / alt-title match
    results/fuzzy_matching.json          ← substring / partial match
    results/first_results_fallback.json  ← top MAL result used as fallback
    results/not_matching.json            ← nothing found on MAL

── How saves work ───────────────────────────────────────────────────────────
A dedicated background writer thread runs alongside the main loop.
Every time the main loop finishes processing entry #100, #200, #300 … it
snapshots the four lists and drops the snapshot into a queue — then
immediately continues fetching the next anime without waiting for disk I/O.

The writer thread picks up snapshots from the queue and rewrites the four
JSON files on disk.  If a snapshot is already waiting when a new one arrives,
the older one is silently replaced (no point writing stale data).

On a crash the last successfully written checkpoint is on disk.  The
progress.jsonl file also records every single entry so the run can be resumed
from exactly where it stopped.

── Resume logic ─────────────────────────────────────────────────────────────
Every processed entry is immediately appended (line by line) to:

    results/progress.jsonl

On restart the script reads that file, rebuilds the four buckets, and skips
every title already recorded.

── Flags ────────────────────────────────────────────────────────────────────
    results/.done   written only on clean finish; re-runs exit immediately
                    (use --force to override)

Environment variables:
    MAL_CLIENT_ID      – MAL API v2 client id
    INPUT_JSON_URL     – raw GitHub URL to animegg_all_series1.json

Usage:
    pip install requests
    MAL_CLIENT_ID=xxx INPUT_JSON_URL=https://... python mal_mapper.py
    python mal_mapper.py --force   # wipe progress, start over
"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CLIENT_ID = os.environ.get("MAL_CLIENT_ID", "b0f57250436db633080e10767f2dab54").strip()
INPUT_JSON_URL = os.environ.get(
    "INPUT_JSON_URL",
    "https://raw.githubusercontent.com/ytbro8326-sudo/mal_mapper/refs/heads/main/animegg_all_series1.json",
).strip()

MAL_SEARCH_URL   = "https://api.myanimelist.net/v2/anime"
RATE_LIMIT_DELAY = 0.5   # seconds between MAL requests (~2 req/s, limit is 3)
MAX_RETRIES      = 3
RETRY_DELAY      = 6     # seconds to wait after 429 / 5xx
CHECKPOINT_EVERY = 100   # write result files every N processed entries

RESULTS_DIR   = Path("results")
OUT_EXACT     = RESULTS_DIR / "exact_matching.json"
OUT_FUZZY     = RESULTS_DIR / "fuzzy_matching.json"
OUT_FIRST     = RESULTS_DIR / "first_results_fallback.json"
OUT_NONE      = RESULTS_DIR / "not_matching.json"
PROGRESS_FILE = RESULTS_DIR / "progress.jsonl"
DONE_FLAG     = RESULTS_DIR / ".done"
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════════════════════════
#  Background writer thread
# ══════════════════════════════════════════════════════════════════════════════

# Sentinel object: when the main thread puts this into the queue the writer
# thread knows the run is finished and it should exit.
_STOP = object()


def _writer_thread(write_queue: queue.Queue) -> None:
    """
    Runs in a daemon thread.  Pulls snapshots off write_queue and writes the
    four JSON result files to disk.

    A snapshot is a dict:
        {
            "exact":    [...],
            "fuzzy":    [...],
            "first":    [...],
            "not_found":[...],
            "label":    "checkpoint N/total"   # for the log line
        }

    If multiple snapshots pile up (disk is slow) we discard stale ones and
    only write the most recent — there's no value in writing outdated data.
    """
    while True:
        snapshot = write_queue.get()   # blocks until something arrives

        if snapshot is _STOP:
            break                      # main thread is done; exit cleanly

        # Drain any additional pending snapshots — keep only the latest.
        latest = snapshot
        while True:
            try:
                candidate = write_queue.get_nowait()
                if candidate is _STOP:
                    # Put the stop sentinel back so the outer loop sees it.
                    write_queue.put(_STOP)
                    break
                latest = candidate     # discard older snapshot
            except queue.Empty:
                break

        snapshot = latest

        label = snapshot.get("label", "checkpoint")
        print(f"\n    ── {label} — writing result files …")
        _write_json(OUT_EXACT, snapshot["exact"])
        _write_json(OUT_FUZZY, snapshot["fuzzy"])
        _write_json(OUT_FIRST, snapshot["first"])
        _write_json(OUT_NONE,  snapshot["not_found"])
        print(f"    ── {label} — done ✓\n", flush=True)


def _write_json(path: Path, records: list[dict]) -> None:
    """Write *records* to *path* as pretty JSON (strips internal _ fields)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
    # Write to a temp file first, then rename — atomic on POSIX filesystems.
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    print(f"        💾  {path}  ({len(clean)} entries)")


# ══════════════════════════════════════════════════════════════════════════════
#  MAL helpers
# ══════════════════════════════════════════════════════════════════════════════

def normalize(text: str) -> str:
    """Lowercase + strip punctuation for title comparison."""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def load_anime_list(url: str, session: requests.Session) -> list[dict]:
    """Download the source JSON and return a flat list of anime dicts."""
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
        raise ValueError(f"No anime list found. Keys: {list(raw.keys())}")

    raise ValueError(f"Unexpected JSON root type: {type(raw)}")


def collect_all_titles(node: dict) -> list[str]:
    """Return every title string available for a MAL search result node."""
    titles = [node.get("title", "")]
    alt = node.get("alternative_titles", {})
    titles += alt.get("synonyms", [])
    if alt.get("en"):
        titles.append(alt["en"])
    if alt.get("ja"):
        titles.append(alt["ja"])
    return [t for t in titles if t]


def search_mal(title: str, session: requests.Session) -> dict | None:
    """
    Query MAL for *title*.
    Returns {"id", "mal_title", "confidence"} or None after exhausting retries.
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

            # 1. Exact match
            for item in data:
                node = item["node"]
                if any(normalize(t) == norm_q for t in collect_all_titles(node)):
                    return {"id": node["id"], "mal_title": node["title"],
                            "confidence": "exact"}

            # 2. Fuzzy (substring) match
            for item in data:
                node = item["node"]
                if any(
                    norm_q in normalize(t) or normalize(t) in norm_q
                    for t in collect_all_titles(node)
                ):
                    return {"id": node["id"], "mal_title": node["title"],
                            "confidence": "fuzzy"}

            # 3. First-result fallback
            first = data[0]["node"]
            return {"id": first["id"], "mal_title": first["title"],
                    "confidence": "first"}

        except requests.RequestException as exc:
            print(f"    ⚠  Network error (attempt {attempt}): {exc}")
            time.sleep(RETRY_DELAY)

    return None


def build_entry(anime: dict, result: dict | None) -> dict:
    """Merge source anime fields with MAL result into one output record."""
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


# ══════════════════════════════════════════════════════════════════════════════
#  Progress file (resume support)
# ══════════════════════════════════════════════════════════════════════════════

def load_progress() -> tuple[set[str], list[dict], list[dict], list[dict], list[dict]]:
    """
    Read progress.jsonl and rebuild the four in-memory buckets + seen set.
    The '_confidence' field stored in each line drives bucket assignment.
    """
    seen:      set[str]   = set()
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
        print(f"    ⚠  {corrupted} corrupted line(s) ignored in progress file.")

    return seen, exact, fuzzy, first_fb, not_found


def append_progress(entry: dict, confidence: str | None) -> None:
    """
    Append one processed record to progress.jsonl immediately.
    Stores '_confidence' for bucket reconstruction on resume.
    Uses fsync so data survives a hard crash.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    record = dict(entry)
    record["_confidence"] = confidence  # None → not_found

    with open(PROGRESS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _snapshot(exact, fuzzy, first_fb, not_found, label):
    """Return a shallow-copy snapshot of the four buckets for the writer thread."""
    return {
        "exact":     list(exact),
        "fuzzy":     list(fuzzy),
        "first":     list(first_fb),
        "not_found": list(not_found),
        "label":     label,
    }


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


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="MAL ID Mapper (non-blocking saves)")
    parser.add_argument("--force", action="store_true",
                        help="Wipe existing progress and start from scratch.")
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
        _, exact, fuzzy, first_fb, not_found = load_progress()
        total = len(exact) + len(fuzzy) + len(first_fb) + len(not_found)
        print_summary(exact, fuzzy, first_fb, not_found, total)
        return

    # ── Load previous progress (resume) ───────────────────────────────────────
    seen_titles, exact_matches, fuzzy_matches, first_fallback, not_found = load_progress()
    if seen_titles:
        print(
            f"🔁  Resuming from entry #{len(seen_titles) + 1}  "
            f"({len(exact_matches)} exact / {len(fuzzy_matches)} fuzzy / "
            f"{len(first_fallback)} fallback / {len(not_found)} not-found already saved)\n"
        )

    # ── Start background writer thread ────────────────────────────────────────
    write_queue: queue.Queue = queue.Queue()
    writer = threading.Thread(target=_writer_thread, args=(write_queue,), daemon=True)
    writer.start()

    # ── Fetch source list ─────────────────────────────────────────────────────
    session    = requests.Session()
    anime_list = load_anime_list(INPUT_JSON_URL, session)
    total      = len(anime_list)
    print(f"📋  {total} anime entries loaded.\n{'─'*60}")

    # How many were already done before this run started (for checkpoint math).
    already_done = len(seen_titles)
    new_this_run = 0   # counts only entries processed in this run

    # ── Main loop — never pauses for disk I/O ─────────────────────────────────
    for i, anime in enumerate(anime_list, 1):
        title = (anime.get("title") or "").strip()

        if not title:
            print(f"[{i:>5}/{total}] ⚠  Skipping entry with no title.")
            continue

        # Skip titles already recorded in a previous run
        if title in seen_titles:
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
        new_this_run += 1

        # ── Every CHECKPOINT_EVERY NEW entries: push snapshot to writer thread ─
        # The main loop does NOT wait here — it continues immediately.
        total_processed = already_done + new_this_run
        if new_this_run % CHECKPOINT_EVERY == 0:
            label = f"Checkpoint {total_processed}/{total}"
            write_queue.put(_snapshot(
                exact_matches, fuzzy_matches, first_fallback, not_found, label
            ))
            # The writer thread will log "writing …" and "done ✓" asynchronously.

        time.sleep(RATE_LIMIT_DELAY)

    # ── Signal writer thread to finish, then wait for it ──────────────────────
    # Push the final snapshot so the very last partial batch is also written.
    write_queue.put(_snapshot(
        exact_matches, fuzzy_matches, first_fallback, not_found,
        f"Final save ({already_done + new_this_run}/{total})"
    ))
    write_queue.put(_STOP)   # tell the writer thread to exit after flushing
    writer.join()            # wait for the last write to finish before exiting

    # Mark clean completion
    DONE_FLAG.write_text("done\n", encoding="utf-8")
    print(f"    🏁  {DONE_FLAG}  (clean-finish sentinel)")

    print_summary(exact_matches, fuzzy_matches, first_fallback, not_found, total)


if __name__ == "__main__":
    main()
