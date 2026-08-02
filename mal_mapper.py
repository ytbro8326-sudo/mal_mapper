MAL ID Mapper for AnimeGG  ·  v4 (save after every single entry)
═════════════════════════════════════════════════════════════════
Writes results/exact_matching.json, results/fuzzy_matching.json,
results/first_results_fallback.json, results/not_matching.json
after EVERY processed anime — no batching, no threads, no loss.
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
CLIENT_ID = os.environ.get("MAL_CLIENT_ID", "b0f57250436db633080e10767f2dab54").strip()
INPUT_JSON_URL = os.environ.get(
    "INPUT_JSON_URL",
    "https://raw.githubusercontent.com/ytbro8326-sudo/mal_mapper/refs/heads/main/animegg_all_series1.json",
).strip()

MAL_SEARCH_URL   = "https://api.myanimelist.net/v2/anime"
RATE_LIMIT_DELAY = 0.5
MAX_RETRIES      = 3
RETRY_DELAY      = 6

RESULTS_DIR = Path("results")
OUT_EXACT   = RESULTS_DIR / "exact_matching.json"
OUT_FUZZY   = RESULTS_DIR / "fuzzy_matching.json"
OUT_FIRST   = RESULTS_DIR / "first_results_fallback.json"
OUT_NONE    = RESULTS_DIR / "not_matching.json"
# ──────────────────────────────────────────────────────────────────────────────


def normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


def load_anime_list(url: str, session: requests.Session) -> list[dict]:
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
    titles = [node.get("title", "")]
    alt = node.get("alternative_titles", {})
    titles += alt.get("synonyms", [])
    if alt.get("en"):
        titles.append(alt["en"])
    if alt.get("ja"):
        titles.append(alt["ja"])
    return [t for t in titles if t]


def search_mal(title: str, session: requests.Session) -> dict | None:
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

            for item in data:
                node = item["node"]
                if any(normalize(t) == norm_q for t in collect_all_titles(node)):
                    return {"id": node["id"], "mal_title": node["title"], "confidence": "exact"}

            for item in data:
                node = item["node"]
                if any(norm_q in normalize(t) or normalize(t) in norm_q
                       for t in collect_all_titles(node)):
                    return {"id": node["id"], "mal_title": node["title"], "confidence": "fuzzy"}

            first = data[0]["node"]
            return {"id": first["id"], "mal_title": first["title"], "confidence": "first"}

        except requests.RequestException as exc:
            print(f"    ⚠  Network error (attempt {attempt}): {exc}")
            time.sleep(RETRY_DELAY)

    return None


def build_entry(anime: dict, result: dict | None) -> dict:
    base = {
        "title":         anime.get("title"),
        "uri":           anime.get("uri"),
        "url":           anime.get("url"),
        "thumbnail_url": anime.get("thumbnail_url"),
        "episode_count": anime.get("episode_count"),
        "status":        anime.get("status"),
        "genres":        anime.get("genres", []),
        "description":   anime.get("description"),
        "mal_id":        result["id"]    if result else None,
        "mal_title":     result["mal_title"] if result else None,
    }
    return base


def save_all(exact: list, fuzzy: list, first: list, none: list) -> None:
    """
    Overwrite all four result JSON files right now.
    Uses write-to-tmp-then-rename so files are never half-written.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for path, records in (
        (OUT_EXACT, exact),
        (OUT_FUZZY, fuzzy),
        (OUT_FIRST, first),
        (OUT_NONE,  none),
    ):
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        tmp.replace(path)   # atomic on Linux/macOS


def load_existing() -> tuple[list, list, list, list, set]:
    """
    Read the four result files from disk to resume a previous run.
    Returns the four lists + a set of already-seen titles.
    """
    exact, fuzzy, first, none = [], [], [], []
    seen: set[str] = set()

    for path, bucket in (
        (OUT_EXACT, exact),
        (OUT_FUZZY, fuzzy),
        (OUT_FIRST, first),
        (OUT_NONE,  none),
    ):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                bucket.extend(data)
            except (json.JSONDecodeError, OSError):
                print(f"    ⚠  Could not read {path} — starting that bucket fresh.")

    for record in exact + fuzzy + first + none:
        t = (record.get("title") or "").strip()
        if t:
            seen.add(t)

    return exact, fuzzy, first, none, seen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Ignore existing results and start from scratch.")
    args = parser.parse_args()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.force:
        for p in (OUT_EXACT, OUT_FUZZY, OUT_FIRST, OUT_NONE):
            if p.exists():
                p.unlink()
        print("🔄  --force: cleared all result files.\n")

    # ── Resume: load whatever is already on disk ───────────────────────────────
    exact_matches, fuzzy_matches, first_fallback, not_found, seen_titles = load_existing()

    if seen_titles:
        print(
            f"🔁  Resuming — {len(seen_titles)} titles already in result files, skipping them.\n"
            f"    exact={len(exact_matches)}  fuzzy={len(fuzzy_matches)}  "
            f"fallback={len(first_fallback)}  not_found={len(not_found)}\n"
        )

    session    = requests.Session()
    anime_list = load_anime_list(INPUT_JSON_URL, session)
    total      = len(anime_list)
    print(f"📋  {total} anime entries loaded.\n{'─'*60}")

    for i, anime in enumerate(anime_list, 1):
        title = (anime.get("title") or "").strip()

        if not title:
            print(f"[{i:>5}/{total}] ⚠  Skipping — no title.")
            continue

        if title in seen_titles:
            print(f"[{i:>5}/{total}] ⏭   Already done: '{title}'")
            continue

        print(f"[{i:>5}/{total}] 🔎  '{title}' …", end=" ", flush=True)

        result = search_mal(title, session)
        entry  = build_entry(anime, result)

        if result is None:
            not_found.append(entry)
            print("❌  not found")

        elif result["confidence"] == "exact":
            exact_matches.append(entry)
            print(f"✅  exact      mal_id={result['id']}  → '{result['mal_title']}'")

        elif result["confidence"] == "fuzzy":
            fuzzy_matches.append(entry)
            print(f"🟡  fuzzy      mal_id={result['id']}  → '{result['mal_title']}'")

        else:
            first_fallback.append(entry)
            print(f"🟠  fallback   mal_id={result['id']}  → '{result['mal_title']}'")

        seen_titles.add(title)

        # ── Write all 4 files to disk RIGHT NOW, every single anime ───────────
        save_all(exact_matches, fuzzy_matches, first_fallback, not_found)

        time.sleep(RATE_LIMIT_DELAY)

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
