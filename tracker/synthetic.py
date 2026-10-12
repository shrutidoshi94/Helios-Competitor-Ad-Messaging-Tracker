"""SYNTHETIC demo data — clearly flagged (ads.synthetic=1, shown as such in the UI).

Real ad data costs money and the setup budget was $0.002, so the weekly-diff demo
runs on fictional brands with ads written from templates. One fictional competitor
(Brightly) deliberately pivots from feature ads to price/urgency ads in the last
3 weeks so the shift detector, digest and counter-messaging have something to find.
Nothing here describes a real company."""
import random
from datetime import date, timedelta

from . import db

AS_OF = "2026-10-11"

TEMPLATES = {
    "features": ["{b} now connects with Slack, Notion and Google Calendar so every task lives in one workflow.",
                 "Custom dashboards, automations and 40+ integrations. See what {b} can build for your team.",
                 "Meet the new {b} AI assistant: it drafts, tags and routes tasks for you.",
                 "Gantt views, workload charts and API access: the full {b} feature set."],
    "price": ["Switch to {b} and save 30% on your first year. Plans from $8/user.",
              "Stop overpaying for project software. {b} is half the price of the big names.",
              "Free forever for teams under 10. Upgrade only when you grow. {b}.",
              "Get 2 months free when you go annual with {b}."],
    "urgency": ["Limited time: 40% off {b} ends Friday. Don't miss it.",
                "Last chance to lock in launch pricing on {b}. Offer expires tonight.",
                "Only 48 hours left to claim your {b} bonus."],
    "trust": ["Trusted by 20,000 teams and rated #1 for ease of use. {b}.",
              "SOC 2 certified, 99.9% uptime. Teams trust {b} with their work.",
              "The best project tool, according to our customers. Try {b}."],
    "speed": ["Set up {b} in 5 minutes and get your team moving today.",
              "Ship projects 2x faster with {b}. No training required."],
    "education": ["Free webinar: how to run weekly planning that works. Hosted by {b}.",
                  "Download the {b} guide to cutting meeting time in half."],
}
BRANDS = {"Lumio": None, "Brightly": "pivot", "Quillbase": {"features": .55, "trust": .2, "speed": .15, "price": .1},
          "Taskwise": {"price": .4, "speed": .3, "features": .2, "trust": .1}}
BASE_FEATURES = {"features": .7, "speed": .1, "trust": .1, "price": .1}
PIVOT = {"price": .55, "features": .2, "urgency": .15, "trust": .1}


def _weights(brand, week_idx, n_weeks):
    spec = BRANDS[brand]
    if spec == "pivot":
        return BASE_FEATURES if week_idx < n_weeks - 3 else PIVOT
    return spec or {"features": .4, "speed": .25, "trust": .2, "price": .15}


def load(con, as_of=AS_OF, n_weeks=8, seed=7):
    rng = random.Random(seed)
    end = date.fromisoformat(as_of)
    for brand in BRANDS:
        cid = db.upsert_competitor(con, brand, f"{brand.lower()}.example", is_self=(brand == "Lumio"))
        for w in range(n_weeks):
            start = end - timedelta(weeks=n_weeks - 1 - w)
            w_ = _weights(brand, w, n_weeks)
            for i in range(rng.randint(5, 7)):
                theme = rng.choices(list(w_), list(w_.values()))[0]
                text = rng.choice(TEMPLATES[theme]).format(b=brand)
                fs = start + timedelta(days=rng.randint(0, 6))
                # most ads live 1-3 weeks; a few "winners" run for the whole window
                life = n_weeks * 7 if rng.random() < .08 else rng.randint(7, 21)
                ls = min(fs + timedelta(days=life), end)
                active = ls >= end
                db.upsert_ad(con, cid, "meta", f"syn-{brand}-{w}-{i}", text, "", rng.choice(["Learn more", "Sign up", "Get offer"]),
                             fs.isoformat(), ls.isoformat(), active, {"synthetic": True}, synthetic=True)
    return as_of
