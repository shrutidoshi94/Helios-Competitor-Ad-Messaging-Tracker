"""Weekly 'what changed' digest + counter-messaging suggestions.

Insight ranking, thresholds and numbers are deterministic (diff.py). Counter-messaging
angles are template-based placeholders: swap `suggest_angles` for an LLM draft and
score each draft's relevance with a Jev Noul question when keys are available."""
from datetime import date, timedelta

from . import diff

ANGLES = {
    "price": ["Lead with total cost of ownership instead of sticker price.",
              "Show a side-by-side of what's included at each price point."],
    "urgency": ["Counter fake scarcity with 'no countdown timers, same price all year'.",
                "Offer a risk-free trial rather than a deadline."],
    "features": ["Reframe from feature lists to one outcome customers get in week one.",
                 "Show a workflow demo rather than an integrations list."],
    "trust": ["Back claims with a named customer result and a number.",
              "Publish verifiable proof (uptime page, certifications)."],
    "speed": ["Prove setup time with a timed walkthrough."],
    "education": ["Out-teach them: publish the template they only tease."],
    "brand": ["Test a concrete offer; vague brand ads rarely convert."],
}


SURGE_MIN_NEW = 10      # new ads in the window that count as a launch surge


def suggest_angles(theme):
    return ANGLES.get(theme, [])


def _uniq(texts, n=2, width=90):
    """First n distinct snippets (ads from one campaign often share identical copy)."""
    seen, out = set(), []
    for t in texts:
        k = " ".join((t or "").lower().split())[:width]
        if k and k not in seen:
            seen.add(k)
            out.append(" ".join((t or "").split())[:width])
        if len(out) == n:
            break
    return out


def _one_line(t, width=90):
    return " ".join((t or "").split())[:width]


def pct(x):
    return f"{round(x * 100)}%"


def build(con, as_of, spend=0.0, budget=None, weeks_back=3, placements=None):
    prev = (date.fromisoformat(as_of) - timedelta(weeks=weeks_back)).isoformat()
    comps = con.execute("SELECT * FROM competitors ORDER BY name").fetchall()
    insights, winners, weak, review = [], [], [], 0
    for c in comps:
        d = diff.compare(con, c["id"], prev, as_of, placements=placements)
        review += sum(1 for r in diff.active_ads(con, c["id"], as_of, placements) if r["needs_review"])
        for theme, delta in d["shifts"][:2]:
            frm, to = d["share_prev"].get(theme, 0), d["share_cur"].get(theme, 0)
            ev = _uniq(r["text"] for r in d["new"] if r["theme"] == theme)
            extra = f" and launched {d['new_urgency']} new urgency ads" if theme != "urgency" and d["new_urgency"] else ""
            insights.append(dict(
                competitor=c["name"], theme=theme, delta=delta,
                headline=f"{c['name']}: {theme} ads {'rose' if delta > 0 else 'fell'} from {pct(frm)} to {pct(to)} "
                         f"over {weeks_back} weeks{extra}.",
                evidence=ev, angles=suggest_angles(theme) if delta > 0 else []))
        if len(d["new"]) >= SURGE_MIN_NEW and d["n_cur"] >= 1.5 * max(d["n_prev"], 1):
            mix = diff.theme_share(d["new"])
            top = sorted(mix.items(), key=lambda x: -x[1])[:3]
            insights.append(dict(
                competitor=c["name"], theme="surge", delta=len(d["new"]) / 100,
                headline=f"{c['name']}: launched {len(d['new'])} new ads in {weeks_back} weeks "
                         f"({d['n_prev']} -> {d['n_cur']} live). New-ad mix: "
                         + ", ".join(f"{t} {pct(v)}" for t, v in top) + ".",
                evidence=_uniq(r["text"] for r in d["new"]),
                angles=suggest_angles(top[0][0]) if top else []))
        for age, r in diff.longest_running(con, c["id"], as_of, limit=1, placements=placements):
            winners.append((c["name"], age, r))
        weak += [(c["name"], r) for r in diff.weak_claims(diff.active_ads(con, c["id"], as_of, placements))][:2]
    insights.sort(key=lambda i: -abs(i["delta"]))
    md = [f"# Competitor messaging brief — week of {diff.week_start(as_of)}", ""]
    md.append("## What changed")
    md += [f"- **{i['headline']}**" + "".join(f"\n  - evidence: “{e}…”" for e in i["evidence"])
           for i in insights[:4]] or ["- No theme shifts crossed the threshold this period."]
    angles = [(i["competitor"], a) for i in insights if i["angles"] for a in i["angles"][:1]]
    if angles:
        md += ["", "## Counter-messaging ideas (template-based)"] + [f"- vs {c}: {a}" for c, a in angles[:3]]
    if winners:
        md += ["", "## Likely winners (longest-running live ads)"]
        md += [f"- {n}: live {age} days — “{_one_line(r['text'])}…”" for n, age, r in sorted(winners, key=lambda x: -x[1])[:3]]
    if weak:
        md += ["", "## Weak claims (low verifiability)"] + [f"- {n}: “{_one_line(r['text'])}…” (verifiable p={r['claim_verifiable_prob']:.2f})" for n, r in weak[:3]]
    md += ["", f"_{review} active ads flagged for human review. Glasser spend this run: ${spend:.5f}"
           + (f" of ${budget:.3f} cap" if budget else "") + "._"]
    return "\n".join(md), insights
