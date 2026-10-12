<img src="assets/helios_wordmark.png" alt="Helios" height="56">

# Competitor Ad & Messaging Tracker

Helios watches what your competitors are saying in their paid ads. It pulls live creatives from the
Meta and LinkedIn ad libraries, tags every ad with a **theme**, an **audience** and a
**claim-verifiability score** (each with a probability attached), diffs the mix week over week, and
writes a one-page brief on what changed and how to respond.

Paid data is metered: every API response is cached on disk, live calls are off by default, and each
run refuses to spend past a cap you set in the sidebar.

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

.venv/bin/streamlit run app.py      # dashboard
.venv/bin/python -m pytest -q       # tests
```

The dashboard opens with saved data only, so a fresh clone costs nothing. To fetch new brands, tick
**Allow live Glasser calls** under *Spend controls* and set a budget cap. Live calls need the
[`glasser`](https://glasser.ai) CLI on your `PATH`.

Set `OPENROUTER_API_KEY` (in the environment or a gitignored `.env`) to classify with Jev instead of
the keyword baseline:

```bash
export OPENROUTER_API_KEY=...
```

## How it works

```
Glasser (Meta / LinkedIn ad libraries)
   │   cached on disk by (provider, endpoint, input) · hard budget cap · spend log
   ▼
ingest  ──► SQLite (competitors, ads, ad_tags, snapshots, spend_log)
   │        dedup on (platform, ext_id); re-seeing an ad only extends last_seen
   ▼
classify ──► theme · audience · claim-verifiability, each with a probability
   │         low-confidence tags land in a "needs review" bucket instead of being trusted
   ▼
diff ──► theme share per brand per week, shifts, new/retired ads, long-runners
   ▼
digest ──► markdown brief + counter-messaging angles
```

Everything downstream of `classify` is deterministic, so the numbers in the brief are reproducible
from the database alone.

## The dashboard

| Tab | What it shows |
| --- | --- |
| **Brands** | One brand at a time: how many ads are running and how they split across themes |
| **Heatmap** | All brands at once — theme mix per brand, next to raw ad volume |
| **Trend** | Ads per theme, week by week, for one brand or all of them |
| **Ads** | Every ad with its tags, confidence bars, days live, and a link to the ad library |
| **Brief** | The generated markdown digest, downloadable |

A slider picks the week you're looking at (up to 13 weeks back); every tab reflects that date.

## Classification

Two backends behind one interface, both returning the same `Tag` shape:

- **`JevClassifier`** — TypeSafe's Jev decision model via OpenRouter's Decisions API. Two choice
  questions (theme, audience) and one noul question (is the claim objectively checkable?) in a
  single call. Used whenever `OPENROUTER_API_KEY` is set.
- **`HeuristicClassifier`** — keyword baseline used otherwise. Tagged `classifier='heuristic'` in the
  database so it can never be mistaken for a model tag.

The label set is deliberately small — 7 themes (price, speed, trust, features, urgency, education,
brand) and 5 audiences — so tags stay comparable across brands and weeks. Ads with too little text
are marked *not analyzable* rather than guessed at. Tagging is idempotent: an ad is never re-tagged,
and a failed call leaves it untagged so the next run retries it.

## Evaluating the tags

The point of the eval is calibration: tags the model is confident about should be right more often
than tags it is unsure about.

```bash
.venv/bin/python -m tracker.evaluate export   # 50 real ads -> eval/labels.csv
# fill in true_theme and true_audience (valid values are printed by the command)
.venv/bin/python -m tracker.evaluate score    # accuracy overall and by confidence band
```

The exported sheet deliberately omits the model's own tags, so hand-labelling can't be anchored by
them.

## Cost control

- Every response is cached at `data/raw/<provider>_<hash>.json`; replays are free.
- `Glasser.call` refuses any live request that would push the run past its budget cap.
- Every call — cached or billed — is written to `spend_log`, and the running total is on screen.
- Live calls are opt-in per session and default to off.

Prices below were read with `glasser inspect`:

| Source | Endpoint | Price | Wired into the UI |
| --- | --- | --- | --- |
| Meta ads | `scrapecreators /v1/facebook/adLibrary/…` | $0.00188/call | yes |
| LinkedIn ads | `scrapecreators /v1/linkedin/ads/search` | $0.00188/call | yes |
| Google ads | `serpapi google_ads_transparency_center` | $0.015/call | priced only |
| Keywords | `semrush domain_organic` | $0.00055/row | priced only |
| News | `serper /news` | $0.0011/call | priced only |

## Layout

```
app.py                  Streamlit dashboard
tracker/glasser.py      cached Glasser client, budget cap, spend log
tracker/ingest.py       raw ad-library responses -> ads rows (Meta + LinkedIn)
tracker/classify.py     JevClassifier + keyword baseline, shared Tag shape
tracker/diff.py         snapshots, theme shares, shift detection
tracker/digest.py       weekly brief + counter-messaging angles
tracker/evaluate.py     blind label export + accuracy by confidence band
tracker/synthetic.py    fictional demo brands, flagged synthetic=1
tracker/db.py           SQLite schema, upserts, migrations
data/raw/               cached API responses
tests/                  pytest suite
```

## Known limits

- Only each brand's most recent ~50 ads are stored, so weeks further back can be undercounted.
- LinkedIn search results usually carry no start date; those ads are treated as already running
  (`ingest.UNDATED`).
- Counter-messaging angles in the brief are template-based placeholders, not generated copy.
- A "shift" is only reported with at least `MIN_ADS` (5) tagged ads in both periods and an absolute
  change in theme share of `SHIFT_THRESHOLD` (15%) or more.
- `tracker/synthetic.py` brands are fictional and exist only to exercise the diff and digest logic
  without spending money. They do not describe real companies.
