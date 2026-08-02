# MAL ID Mapper

Fetches `animegg_all_series1.json` from a GitHub raw URL, queries the
**MyAnimeList API v2** for every title, and writes four categorised result
files back to this repository automatically via GitHub Actions.

---

## Output files

| File | Description |
|------|-------------|
| `results/exact_matching.json` | Title matched exactly against MAL main / English / Japanese / synonym titles |
| `results/fuzzy_matching.json` | One title is a substring of the other |
| `results/first_results_fallback.json` | Top MAL search result used as fallback |
| `results/not_matching.json` | No MAL result found at all (`mal_id: null`) |

Every entry keeps **all original fields** plus two new ones:

```json
{
  "title": "Gabriel DropOut",
  "uri": "gabriel-dropout",
  "url": "https://www.animegg.org/series/gabriel-dropout",
  "thumbnail_url": "...",
  "episode_count": 12,
  "status": "Ongoing",
  "genres": ["Supernatural", "Comedy", "Shounen", "School"],
  "description": "...",
  "mal_id": 33731,
  "mal_title": "Gabriel DropOut"
}
```

---

## Setup

### 1 — Add secrets & variables

Go to **Settings → Secrets and variables → Actions** in your GitHub repo.

| Type | Name | Value |
|------|------|-------|
| **Secret** | `MAL_CLIENT_ID` | Your MAL API v2 Client ID |
| **Variable** | `INPUT_JSON_URL` | Raw GitHub URL to `animegg_all_series1.json` |

Example variable value:
```
https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/animegg_all_series1.json
```

> The Client Secret is **not** needed — MAL v2 read-only search only requires
> the Client ID sent as the `X-MAL-CLIENT-ID` header.

### 2 — Run the workflow

**Automatically:**
- Every **Sunday at 02:00 UTC** (cron schedule)
- Whenever `animegg_all_series1.json` is pushed to this repo

**Manually:**
1. Go to **Actions → MAL ID Mapper → Run workflow**
2. Optionally paste a different raw JSON URL in the input box
3. Click **Run workflow**

The workflow will commit and push the four result files back to `main`.

---

## Run locally

```bash
pip install requests

export MAL_CLIENT_ID="your_client_id_here"
export INPUT_JSON_URL="https://raw.githubusercontent.com/YOU/REPO/main/animegg_all_series1.json"

python mal_mapper.py
```

Results are written to `results/` in the current directory.

---

## Match confidence levels

| Symbol | Level | How it's decided |
|--------|-------|-----------------|
| ✅ | **Exact** | `normalize(query) == normalize(mal_title)` across all title variants |
| 🟡 | **Fuzzy** | Query is a substring of a MAL title, or vice-versa |
| 🟠 | **Fallback** | None of the above matched — top MAL result used |
| ❌ | **Not found** | MAL returned no results at all |

Entries in `first_results_fallback.json` and `fuzzy_matching.json` should be
manually reviewed for correctness.
