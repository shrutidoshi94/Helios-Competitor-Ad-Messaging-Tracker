"""Evaluation: how often do the classifier's tags match a human, and does confidence mean anything?

Workflow (blind: the sheet does NOT show the model's tags, so labeling can't be biased by them):
  1. python -m tracker.evaluate export          -> eval/labels.csv  (50 real ads, spread across brands)
  2. fill in true_theme and true_audience for each row (valid values are printed by the command)
  3. python -m tracker.evaluate score            -> accuracy overall and by confidence band

The calibration check is the point: if Jev is calibrated, tags it is confident about should be right
more often than tags it is unsure about."""
import csv
import random
import sys
from pathlib import Path

from . import classify, db

PATH = Path(__file__).resolve().parent.parent / "eval" / "labels.csv"
HIGH_CONF = 0.60          # same cut-off the dashboard uses for its "Review" bucket


def accuracy_by_band(pairs, cut=HIGH_CONF):
    """pairs: [(pred, true, prob)] -> overall + high/low confidence accuracy."""
    def acc(xs):
        return (sum(p == t for p, t, _ in xs) / len(xs)) if xs else None
    hi, lo = [x for x in pairs if x[2] >= cut], [x for x in pairs if x[2] < cut]
    return dict(n=len(pairs), overall=acc(pairs), high_conf=acc(hi), n_high=len(hi),
                low_conf=acc(lo), n_low=len(lo))


def export(con, n=50, seed=7, path=PATH):
    """Random sample of real, text-bearing ads, round-robin across brands so no single brand dominates."""
    rows = con.execute(
        """SELECT a.id, c.name brand, a.text FROM ads a JOIN competitors c ON c.id=a.competitor_id
           JOIN ad_tags t ON t.ad_id=a.id
           WHERE t.analyzable=1 AND a.synthetic=0 AND length(a.text) >= 20 ORDER BY a.id""").fetchall()
    rng = random.Random(seed)
    by_brand = {}
    for r in rows:
        by_brand.setdefault(r["brand"], []).append(r)
    for v in by_brand.values():
        rng.shuffle(v)
    picked, i = [], 0
    while len(picked) < n and any(by_brand.values()):
        for v in by_brand.values():
            if v and len(picked) < n:
                picked.append(v.pop())
        i += 1
    rng.shuffle(picked)
    Path(path).parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ad_id", "brand", "text", "true_theme", "true_audience"])
        for r in picked:
            w.writerow([r["id"], r["brand"], " ".join((r["text"] or "").split())[:500], "", ""])
    return len(picked)


def score(path=PATH, con=None):
    """Join hand labels with the stored model tags and report accuracy by confidence band."""
    con = con or db.connect(db.DB_PATH.with_name("real.db"))
    labeled = [r for r in csv.DictReader(open(path)) if r["true_theme"].strip()]
    bad = [r["ad_id"] for r in labeled if r["true_theme"].strip() not in classify.THEMES
           or (r["true_audience"].strip() and r["true_audience"].strip() not in classify.AUDIENCES)]
    if bad:
        raise ValueError(f"Unknown label on ad_id {bad[:5]}. Themes: {list(classify.THEMES)}; "
                         f"audiences: {list(classify.AUDIENCES)}")
    th, au = [], []
    for r in labeled:
        t = con.execute("SELECT theme,theme_prob,audience,audience_prob FROM ad_tags WHERE ad_id=?",
                        (int(r["ad_id"]),)).fetchone()
        if t is None:
            continue
        th.append((t["theme"], r["true_theme"].strip(), t["theme_prob"]))
        if r["true_audience"].strip():
            au.append((t["audience"], r["true_audience"].strip(), t["audience_prob"]))
    return {"theme": accuracy_by_band(th), "audience": accuracy_by_band(au)}


def _fmt(x):
    return "n/a" if x is None else f"{x:.0%}"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "score"
    if cmd == "export":
        con = db.connect(db.DB_PATH.with_name("real.db"))
        print(f"wrote {export(con)} ads to {PATH}")
        print("true_theme    one of:", ", ".join(classify.THEMES))
        print("true_audience one of:", ", ".join(classify.AUDIENCES))
    else:
        for k, v in score().items():
            print(f"{k:9} n={v['n']:>3}  overall {_fmt(v['overall'])}  |  "
                  f"confident (>={HIGH_CONF:.0%}) {_fmt(v['high_conf'])} (n={v['n_high']})  |  "
                  f"unsure {_fmt(v['low_conf'])} (n={v['n_low']})")
