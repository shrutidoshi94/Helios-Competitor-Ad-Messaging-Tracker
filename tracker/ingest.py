"""Turn raw Glasser/ScrapeCreators Meta responses into rows in `ads`."""
import re
import shutil
from datetime import date, datetime, timezone

from . import db
from .glasser import BudgetExceeded, Glasser, RAW

SEED = RAW / "seed_notion_meta.json"


def _d(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else None


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def extract(ad):
    """Pull the fields we need out of one Meta ad_library record."""
    snap = ad.get("snapshot") or {}
    text = ((snap.get("body") or {}).get("text") or "").strip()
    if not text and snap.get("cards"):                      # carousel ads keep copy on the cards
        parts = []
        for c in snap["cards"][:3]:
            t = ((c.get("body") or "") + " " + (c.get("title") or "")).strip()
            if t and t not in parts:                       # carousel cards often repeat the same line
                parts.append(t)
        text = " / ".join(parts)
    return dict(
        ext_id=str(ad["ad_archive_id"]), text=text, title=snap.get("title") or "",
        cta=snap.get("cta_text") or "",
        first_seen=_d(ad.get("start_date")),
        last_seen=_d(ad.get("end_date")) or date.today().isoformat(),
        is_active=bool(ad.get("is_active")), page_name=ad.get("page_name") or "",
        placements=",".join(x.lower() for x in (ad.get("publisher_platform") or [])) or "facebook")


def ingest_meta(con, competitor_id, competitor_name, response, aliases=()):
    """Keep only ads published by the competitor's own Page — a keyword search also
    returns every third-party ad that merely mentions the brand."""
    names = {_norm(competitor_name), *map(_norm, aliases)}
    kept = 0
    for ad in response.get("searchResults", []):
        r = extract(ad)
        if _norm(r["page_name"]) not in names:
            continue
        db.upsert_ad(con, competitor_id, "meta", r["ext_id"], r["text"], r["title"], r["cta"],
                     r["first_seen"], r["last_seen"], r["is_active"], ad)
        kept += 1
    return kept


def seed_cache_from_first_call():
    """The one live call made during setup is stored under the cache key the client will look up."""
    if SEED.exists():
        target = Glasser.cache_path("scrapecreators", "/v1/facebook/adLibrary/search/ads",
                                    {"query": "Notion", "status": "ACTIVE", "country": "US",
                                     "ad_type": "all", "trim": True})
        if not target.exists():
            shutil.copy(SEED, target)


def collect(con, glasser, competitors):
    """competitors: list of dicts {name, aliases?, domains?}. Returns {name: ads_kept}."""
    out = {}
    for c in competitors:
        cid = db.upsert_competitor(con, c["name"], c.get("domains", ""))
        try:
            resp = glasser.meta_ads(c.get("query", c["name"]))
        except BudgetExceeded as e:           # not cached / over budget: skip this brand, keep going
            out[c["name"]] = f"skipped ({e})"
            continue
        out[c["name"]] = ingest_meta(con, cid, c["name"], resp, c.get("aliases", ()))
    return out


def log_setup_spend(con):
    """Record the one live call made during setup ($0.00188) so the cost meter is honest."""
    if not con.execute("SELECT 1 FROM spend_log WHERE cached=0").fetchone():
        con.execute("INSERT INTO spend_log(run_label,provider,endpoint,charge,run_url,cached) VALUES(?,?,?,?,?,0)",
                    ("setup", "scrapecreators", "/v1/facebook/adLibrary/search/ads", 0.00188,
                     "https://app.glasser.ai/runs/bdfaf688-359e-42d4-9913-e28d55863bf2"))
        con.commit()


def collect_pages(con, glasser, pages, max_pages=2, status="ACTIVE"):
    """pages: [{name, page_id, is_self?}]. Follows the cursor up to max_pages; stops on budget."""
    out = {}
    for c in pages:
        cid = db.upsert_competitor(con, c["name"], c.get("domains", ""), c.get("is_self", False), c["page_id"])
        kept, cursor = 0, None
        try:
            for _ in range(max_pages):
                resp = glasser.meta_company_ads(c["page_id"], cursor=cursor, status=status)
                for ad in resp.get("results") or resp.get("searchResults") or []:   # shape differs by endpoint
                    r = extract(ad)
                    db.upsert_ad(con, cid, "meta", r["ext_id"], r["text"], r["title"], r["cta"],
                                 r["first_seen"], r["last_seen"], r["is_active"], ad, placements=r["placements"])
                    kept += 1
                cursor = resp.get("cursor")
                if not cursor:
                    break
            out[c["name"]] = kept
        except BudgetExceeded as e:
            out[c["name"]] = f"{kept} (stopped: {e})"
    return out


UNDATED = "2000-01-01"   # LinkedIn search results usually carry no start date: treat those ads as "already running"


def extract_linkedin(ad):
    text = "\n".join(x.strip() for x in (ad.get("headline"), ad.get("description")) if x and x.strip())
    start, end = (ad.get("startDate") or "")[:10], (ad.get("endDate") or "")[:10]
    today = date.today().isoformat()
    return dict(ext_id=str(ad["id"]), text=text, title=ad.get("headline") or "", cta=ad.get("cta") or "",
                first_seen=start or UNDATED, last_seen=end or today, is_active=(not end) or end >= today,
                advertiser=ad.get("advertiser") or "")


def ingest_linkedin(con, competitor_id, brand, response):
    """Keep ads whose advertiser is the brand itself (a company search also returns look-alike advertisers)."""
    kept = 0
    for ad in response.get("ads", []):
        r = extract_linkedin(ad)
        if _norm(r["advertiser"]) != _norm(brand):
            continue
        db.upsert_ad(con, competitor_id, "linkedin", r["ext_id"], r["text"], r["title"], r["cta"], r["first_seen"],
                     r["last_seen"], r["is_active"], ad, placements="linkedin")
        kept += 1
    return kept


def collect_linkedin(con, glasser, brands, max_pages=2):
    """brands: [name]. Follows paginationToken; a missing cache entry or budget stop ends that brand quietly."""
    out = {}
    for name in brands:
        cid = db.upsert_competitor(con, name)
        kept, cursor = 0, None
        try:
            for _ in range(max_pages):
                resp = glasser.linkedin_ads(name, cursor=cursor)
                kept += ingest_linkedin(con, cid, name, resp)
                cursor = resp.get("paginationToken")
                if not cursor or resp.get("isLastPage"):
                    break
            out[name] = kept
        except BudgetExceeded as e:
            out[name] = f"{kept} (stopped: {e})"
    return out
