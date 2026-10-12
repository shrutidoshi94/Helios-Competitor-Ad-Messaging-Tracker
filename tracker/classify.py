"""Ad classification: theme, audience, claim-verifiability — each with a probability.

Two backends share one interface and one output shape:
  * JevClassifier       — TypeSafe's Jev decision model via OpenRouter's Decisions API
                          (needs OPENROUTER_API_KEY). Choice + Noul questions in one call.
  * HeuristicClassifier — keyword baseline used when no Jev key is set. Tagged
                          classifier='heuristic' so it can never be mistaken for Jev.
The fixed label set is deliberately small (7 themes) so tags stay consistent."""
import json
import math
import os
import re
from dataclasses import dataclass, field

THEMES = {
    "price": "Leads with cost: discounts, free tiers, savings, cheaper than alternatives",
    "speed": "Leads with speed or ease: fast setup, save time, instant results",
    "trust": "Leads with credibility: ratings, reviews, security, guarantees, brand reputation",
    "features": "Leads with product capabilities: what it does, integrations, AI, tools",
    "urgency": "Leads with scarcity or deadlines: limited time, ends soon, last chance, launch offer",
    "education": "Leads with learning or content: course, guide, webinar, newsletter, how-to",
    "brand": "General awareness or lifestyle with no concrete offer or capability",
}
AUDIENCES = {
    "consumer": "Individual consumers shopping for themselves",
    "small_business": "Founders, small teams, freelancers, local businesses",
    "enterprise": "Large companies, IT/security buyers, procurement",
    "creator_marketer": "Creators, marketers, sellers, agencies growing an audience",
    "general": "Broad or unclear audience",
}
REVIEW_THEME_PROB = 0.60      # below this the ad goes to the human-review bucket
REVIEW_AUDIENCE_PROB = 0.50
SUPERLATIVE = re.compile(r"#\s?1\b|\bnumber (one|1)\b|\bbest\b|\bleading\b|\bworld'?s\b|\bmost (loved|trusted|popular)\b|\btop[- ]rated\b|\b100%|\bguarantee", re.I)


@dataclass
class Tag:
    theme: str
    theme_prob: float
    theme_probs: dict
    audience: str
    audience_prob: float
    claim_verifiable_prob: float
    confidence: float
    classifier: str
    analyzable: bool = True
    needs_review: bool = field(init=False)

    def __post_init__(self):
        self.needs_review = (not self.analyzable) or self.theme_prob < REVIEW_THEME_PROB \
            or self.audience_prob < REVIEW_AUDIENCE_PROB


def not_analyzable():
    return Tag("brand", 0.0, {}, "general", 0.0, 0.5, 0.0, "skipped", analyzable=False)


def _normalize(d):
    s = sum(d.values()) or 1.0
    return {k: v / s for k, v in d.items()}


# ---------------------------------------------------------------- heuristic
_KW = {
    "price": r"\b(\$\d+|\d+% off|off\b|discount|free|save|cheap|affordable|pricing|deal|sale|price|only \$)",
    "speed": r"\b(fast|faster|instant|minutes|in seconds|quick|save time|effortless|easy|simple|automat)",
    "trust": r"\b(trusted|rated|reviews?|secure|security|guarantee|award|million (users|customers|teams)|loved by|#\s?1|certified)",
    "features": r"\b(feature|integrat|ai\b|dashboard|workflow|template|tool|plugin|analytics|sync|api|build|connect)",
    "urgency": r"\b(limited|ends (soon|today|tonight)|last chance|hurry|today only|don'?t miss|now or never|launch offer|expires|عرض)",
    "education": r"\b(course|learn|guide|webinar|masterclass|newsletter|tutorial|how to|lesson|ebook|training|دروس)",
}
_AUD = {
    "enterprise": r"\b(enterprise|compliance|soc ?2|sso|security team|cto|ciso|organization)",
    "small_business": r"\b(small business|startup|founder|freelanc|your team|payroll|invoice|local business)",
    "creator_marketer": r"\b(creator|marketer|content|audience|followers|agency|brand|newsletter|ecommerce|sellers?)",
    "consumer": r"\b(shop now|you'?ll love|for you|your (home|style|trip)|wear|gift|family)",
}


class HeuristicClassifier:
    name = "heuristic"

    def classify(self, text, title=""):
        body = f"{title}\n{text}".strip()
        if len(re.sub(r"\W", "", body)) < 12:
            return not_analyzable()
        low = body.lower()
        scores = {t: 2 * len(re.findall(p, low, re.I)) for t, p in _KW.items()}
        scores["brand"] = 0.35                                  # prior: nothing matched
        tp = _normalize({k: v + 0.15 for k, v in scores.items()})
        theme = max(tp, key=tp.get)
        aud_scores = {a: len(re.findall(p, low, re.I)) for a, p in _AUD.items()}
        aud_scores["general"] = 0.6
        ap = _normalize({k: v + 0.1 for k, v in aud_scores.items()})
        aud = max(ap, key=ap.get)
        verifiable = 0.15 if SUPERLATIVE.search(low) else (0.7 if re.search(r"\d", low) else 0.5)
        conf = round(1 - sum(-p * math.log(p + 1e-9) for p in tp.values()) / math.log(len(tp)), 3)
        return Tag(theme, round(tp[theme], 3), {k: round(v, 3) for k, v in tp.items()},
                   aud, round(ap[aud], 3), verifiable, conf, self.name)


# ---------------------------------------------------------------- Jev
class JevClassifier:
    name = "jev"
    URL = "https://openrouter.ai/api/alpha/decisions"

    def __init__(self, api_key=None, model="typesafe/jev-1.13", session=None):
        self.key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.key:
            raise RuntimeError("OPENROUTER_API_KEY not set")
        self.model = model
        if session is None:
            import requests
            session = requests
        self.http = session

    def request_body(self, text, title=""):
        return {
            "model": self.model,
            "state": {"ad": f"{title}\n{text}".strip()[:6000]},
            "questions": {
                "theme": {"type": "choice", "criteria": THEMES,
                          "instructions": "What is the primary persuasion theme of the ad in `ad`?"},
                "audience": {"type": "choice", "criteria": AUDIENCES,
                             "instructions": "Who is the ad in `ad` primarily aimed at?"},
                "verifiable": {"type": "noul",
                               "instructions": "Does the ad in `ad` make a specific, objectively checkable claim "
                                               "(a number, price, named feature) rather than vague or superlative boasting?"},
            },
        }

    def classify(self, text, title=""):
        if len(re.sub(r"\W", "", f"{title}{text}")) < 12:
            return not_analyzable()
        r = self.http.post(self.URL, json=self.request_body(text, title), timeout=30,
                           headers={"Authorization": f"Bearer {self.key}"})
        r.raise_for_status()
        a = r.json()["answers"]
        th, au = a["theme"], a["audience"]
        return Tag(th["choice"], th["probabilities"][th["choice"]], th["probabilities"],
                   au["choice"], au["probabilities"][au["choice"]], a["verifiable"]["noul"],
                   min(th["confidence"], au["confidence"]), self.name)


def load_env(path=None):
    """Minimal .env loader (the file is gitignored); real environment variables win."""
    from pathlib import Path
    f = Path(path) if path else Path(__file__).resolve().parent.parent / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_classifier(prefer_jev=True):
    load_env()
    if prefer_jev and os.environ.get("OPENROUTER_API_KEY"):
        return JevClassifier()
    return HeuristicClassifier()


def classify_new_ads(con, clf):
    """Tag every ad that has no tag yet (idempotent — never re-tags, never re-bills).
    A failed call (rate limit, network) leaves that ad untagged so the next run retries it.
    Returns (tagged, failed)."""
    rows = con.execute("SELECT a.id,a.text,a.title FROM ads a LEFT JOIN ad_tags t ON t.ad_id=a.id "
                       "WHERE t.ad_id IS NULL").fetchall()
    tagged = failed = 0
    for r in rows:
        try:
            t = clf.classify(r["text"] or "", r["title"] or "")
        except Exception:
            failed += 1
            continue
        con.execute("INSERT INTO ad_tags VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], t.theme, t.theme_prob, t.audience, t.audience_prob, t.claim_verifiable_prob,
                     t.confidence, int(t.needs_review), int(t.analyzable), t.classifier,
                     json.dumps(t.theme_probs)))
        tagged += 1
    con.commit()
    return tagged, failed
