"""Deterministic snapshot / diff logic: no model calls, fully reproducible."""
import json
from collections import Counter
from datetime import date, timedelta

MIN_ADS = 5            # need at least this many tagged ads in both periods to call a "shift"
SHIFT_THRESHOLD = 0.15  # absolute change in theme share that counts as a shift
LONG_RUNNING_DAYS = 28


def week_start(d):
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return (d - timedelta(days=d.weekday())).isoformat()


def active_ads(con, competitor_id, on, placements=None):
    """Ads live on date `on` (inclusive), analyzable only, with tags. `placements` (e.g. {"facebook","linkedin"})
    keeps ads that run on at least one of those."""
    rows = con.execute(
        """SELECT a.*, t.theme, t.theme_prob, t.audience, t.audience_prob, t.claim_verifiable_prob,
                  t.needs_review, t.classifier
           FROM ads a JOIN ad_tags t ON t.ad_id=a.id
           WHERE a.competitor_id=? AND t.analyzable=1 AND a.first_seen<=? AND a.last_seen>=?""",
        (competitor_id, on, on)).fetchall()
    if placements is None:
        return rows
    return [r for r in rows if set((r["placements"] or "facebook").split(",")) & set(placements)]


def theme_share(rows):
    n = len(rows)
    c = Counter(r["theme"] for r in rows)
    return {k: v / n for k, v in c.items()} if n else {}


def compare(con, competitor_id, prev, cur, urgency_tag="urgency", placements=None):
    """Compare the active set on `prev` to the active set on `cur`."""
    a, b = active_ads(con, competitor_id, prev, placements), active_ads(con, competitor_id, cur, placements)
    ida, idb = {r["id"] for r in a}, {r["id"] for r in b}
    sp, sc = theme_share(a), theme_share(b)
    deltas = {t: sc.get(t, 0) - sp.get(t, 0) for t in set(sp) | set(sc)}
    enough = len(a) >= MIN_ADS and len(b) >= MIN_ADS
    shifts = sorted(((t, d) for t, d in deltas.items() if enough and abs(d) >= SHIFT_THRESHOLD),
                    key=lambda x: -abs(x[1]))
    new = [r for r in b if r["id"] not in ida]
    return dict(prev=prev, cur=cur, n_prev=len(a), n_cur=len(b), share_prev=sp, share_cur=sc,
                deltas=deltas, shifts=shifts, enough_data=enough, new=new,
                retired=[r for r in a if r["id"] not in idb],
                new_urgency=sum(1 for r in new if r["theme"] == urgency_tag))


def longest_running(con, competitor_id, on, min_days=LONG_RUNNING_DAYS, limit=3, placements=None):
    rows = []
    for r in active_ads(con, competitor_id, on, placements):
        if r["first_seen"] <= "2001-01-01":       # start date unknown (see ingest.UNDATED): cannot rank by age
            continue
        age = (date.fromisoformat(on) - date.fromisoformat(r["first_seen"])).days
        if age >= min_days:
            rows.append((age, r))
    return sorted(rows, key=lambda x: -x[0])[:limit]


def weak_claims(rows, max_verifiable=0.4):
    return [r for r in rows if r["claim_verifiable_prob"] is not None and r["claim_verifiable_prob"] < max_verifiable]


def save_snapshot(con, competitor_id, on):
    rows = active_ads(con, competitor_id, on)
    con.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?)",
                (week_start(on), competitor_id, json.dumps(theme_share(rows)), len(rows)))
    con.commit()
