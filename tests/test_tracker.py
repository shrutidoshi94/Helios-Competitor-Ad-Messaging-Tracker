import json

import pytest

from tracker import classify, db, diff, digest, evaluate, glasser, ingest, synthetic


@pytest.fixture
def con(tmp_path):
    return db.connect(tmp_path / "t.db")


def _ad(i, page, text="Save 20% today", start=1790000000, end=1791000000):
    return {"ad_archive_id": i, "page_name": page, "start_date": start, "end_date": end, "is_active": True,
            "snapshot": {"body": {"text": text}, "title": "t", "cta_text": "Shop now", "cards": []}}


def test_ingest_keeps_only_competitor_page_and_dedups(con):
    cid = db.upsert_competitor(con, "Notion")
    resp = {"searchResults": [_ad(1, "Notion"), _ad(2, "Some Reseller"), _ad(1, "Notion")]}
    assert ingest.ingest_meta(con, cid, "Notion", resp) == 2          # both Notion rows parsed...
    assert con.execute("SELECT COUNT(*) FROM ads").fetchone()[0] == 1  # ...but deduplicated to one ad


def test_carousel_text_fallback():
    ad = _ad(9, "X", text="")
    ad["snapshot"]["cards"] = [{"body": "Card one", "title": "Deal"}]
    assert "Card one" in ingest.extract(ad)["text"]


def test_budget_cap_blocks_live_call(con, tmp_path):
    g = glasser.Glasser(budget_usd=0.001, allow_live=True)
    with pytest.raises(glasser.BudgetExceeded):
        g.call("meta_ads", {"query": "never-cached-xyz"})


def test_uncached_call_refused_when_offline():
    g = glasser.Glasser(budget_usd=1, allow_live=False)
    with pytest.raises(glasser.BudgetExceeded):
        g.call("meta_ads", {"query": "never-cached-abc"})


def test_unanalyzable_ads_are_skipped_not_guessed():
    t = classify.HeuristicClassifier().classify("", "")
    assert not t.analyzable and t.needs_review


def test_heuristic_theme_and_review_bucket():
    c = classify.HeuristicClassifier()
    assert c.classify("Limited time: 40% off ends Friday. Don't miss it.").theme == "urgency"
    assert c.classify("A quiet morning light over the water and the city.").needs_review


def test_jev_parsing_with_fake_http():
    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"answers": {
                "theme": {"choice": "price", "probabilities": {"price": 0.9, "speed": 0.1}, "confidence": 0.8},
                "audience": {"choice": "consumer", "probabilities": {"consumer": 0.7, "general": 0.3}, "confidence": 0.6},
                "verifiable": {"noul": 0.2}}}
    class S:
        def post(self, *a, **k):
            self.body = k["json"]; return R()
    s = S()
    t = classify.JevClassifier(api_key="k", session=s).classify("Save big on shoes today only")
    assert (t.theme, t.theme_prob, t.claim_verifiable_prob, t.classifier) == ("price", 0.9, 0.2, "jev")
    assert set(s.body["questions"]) == {"theme", "audience", "verifiable"}


def test_shift_detector_finds_the_planted_pivot(con):
    asof = synthetic.load(con)
    classify.classify_new_ads(con, classify.HeuristicClassifier())
    brightly = con.execute("SELECT id FROM competitors WHERE name='Brightly'").fetchone()[0]
    d = diff.compare(con, brightly, "2026-09-13", asof)   # 4 weeks back: still pre-pivot
    assert d["enough_data"]
    moved = dict(d["shifts"])
    assert moved.get("price", 0) > 0 and moved.get("features", 0) < 0


def test_no_shift_claimed_without_enough_ads(con):
    cid = db.upsert_competitor(con, "Tiny")
    db.upsert_ad(con, cid, "meta", "a", "Save 20% now", "", "", "2026-01-01", "2026-12-31", True, {})
    classify.classify_new_ads(con, classify.HeuristicClassifier())
    assert not diff.compare(con, cid, "2026-02-01", "2026-03-01")["enough_data"]


def test_digest_mentions_pivot_and_spend(con):
    asof = synthetic.load(con)
    classify.classify_new_ads(con, classify.HeuristicClassifier())
    md, ins = digest.build(con, asof, spend=0.00188, budget=0.002, weeks_back=4)
    assert "Brightly" in md and "$0.00188" in md


def test_accuracy_by_confidence_band():
    r = evaluate.accuracy_by_band([("a", "a", .9), ("a", "a", .8), ("a", "b", .3), ("b", "a", .2)])
    assert r["high_conf"] == 1.0 and r["low_conf"] == 0.0 and r["overall"] == 0.5


def test_seed_response_parses_if_present(con):
    if not ingest.SEED.exists():
        pytest.skip("seed not present")
    cid = db.upsert_competitor(con, "Notion")
    n = ingest.ingest_meta(con, cid, "Notion", json.loads(ingest.SEED.read_text()))
    assert n == 1   # exactly one of the 30 returned ads is Notion's own Page


def test_find_brands_parses_company_search(monkeypatch):
    g = glasser.Glasser(budget_usd=0, allow_live=False)
    monkeypatch.setattr(g, "call", lambda kind, p: {"searchResults": [
        {"name": "Asana", "page_id": 180143175668, "likes": 5, "category": "Software", "page_is_deleted": False},
        {"name": "Gone", "page_id": 1, "page_is_deleted": True}]})
    out = g.find_brands(" Asana ")
    assert out == [dict(name="Asana", page_id="180143175668", likes=5, category="Software", ig="")]


def test_page_id_persists_and_old_db_migrates(tmp_path):
    import sqlite3
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.execute("CREATE TABLE competitors(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, domains TEXT DEFAULT '', is_self INTEGER DEFAULT 0)")
    c.commit(); c.close()
    con = db.connect(old)                      # must add page_id without losing the table
    db.upsert_competitor(con, "Asana", page_id="180143175668")
    db.upsert_competitor(con, "Asana", is_self=True)     # later upsert must not wipe the page_id
    assert con.execute("SELECT page_id FROM competitors WHERE name='Asana'").fetchone()[0] == "180143175668"


def test_placements_backfill_and_platform_filter(tmp_path):
    con = db.connect(tmp_path / "p.db")
    cid = db.upsert_competitor(con, "Acme")
    both = {"publisher_platform": ["FACEBOOK", "INSTAGRAM"]}
    fb = {"publisher_platform": ["FACEBOOK"]}
    db.upsert_ad(con, cid, "meta", "1", "Save 20% today on shoes", "", "", "2026-01-01", "2026-12-31", True, both, placements="facebook,instagram")
    db.upsert_ad(con, cid, "meta", "2", "Fast setup in minutes", "", "", "2026-01-01", "2026-12-31", True, fb, placements="facebook")
    db.upsert_ad(con, cid, "linkedin", "3", "Trusted by 5000 teams", "", "", "2026-01-01", "2026-12-31", True, {}, placements="linkedin")
    classify.classify_new_ads(con, classify.HeuristicClassifier())
    ids = lambda pl: {r["ext_id"] for r in diff.active_ads(con, cid, "2026-06-01", pl)}
    assert ids({"instagram"}) == {"1"}                       # only the ad that runs on Instagram
    assert ids({"facebook"}) == {"1", "2"}
    assert ids({"linkedin"}) == {"3"}
    assert ids({"facebook", "linkedin"}) == {"1", "2", "3"}
    # old DB row with no placements gets them derived from the stored raw record
    con.execute("UPDATE ads SET placements=NULL WHERE ext_id='1'"); con.commit()
    con2 = db.connect(tmp_path / "p.db")
    assert con2.execute("SELECT placements FROM ads WHERE ext_id='1'").fetchone()[0] == "facebook,instagram"


def test_linkedin_ingest_filters_advertiser_and_handles_missing_dates(con):
    cid = db.upsert_competitor(con, "Notion")
    resp = {"ads": [
        {"id": "10", "advertiser": "Notion", "headline": "Your agent knowledge layer", "description": "Work with agents", "cta": "Learn more", "startDate": None, "endDate": None},
        {"id": "11", "advertiser": "Notion Studios Inc.", "headline": "Marketing agency", "description": "x"},
        {"id": "12", "advertiser": "Notion", "headline": "Dated ad", "description": "y", "startDate": "2026-09-01T00:00:00.000Z", "endDate": "2026-09-10T00:00:00.000Z"}]}
    assert ingest.ingest_linkedin(con, cid, "Notion", resp) == 2          # look-alike advertiser dropped
    rows = {r["ext_id"]: r for r in con.execute("SELECT * FROM ads")}
    assert rows["10"]["first_seen"] == ingest.UNDATED and rows["10"]["is_active"] == 1
    assert rows["12"]["first_seen"] == "2026-09-01" and rows["12"]["is_active"] == 0
    assert rows["10"]["placements"] == "linkedin"


def test_linkedin_cached_probe_parses_if_present():
    payload = {"company": "Notion", "keyword": "Notion"}
    path = glasser.Glasser.cache_path("scrapecreators", "/v1/linkedin/ads/search", payload)
    if not path.exists():
        pytest.skip("probe not cached")
    con = db.connect(":memory:")
    cid = db.upsert_competitor(con, "Notion")
    n = ingest.ingest_linkedin(con, cid, "Notion", json.loads(path.read_text()))
    assert n > 0 and all(r[0] == "linkedin" for r in con.execute("SELECT platform FROM ads"))


def test_eval_export_is_blind_and_score_joins_db_tags(tmp_path):
    import csv
    con = db.connect(tmp_path / "e.db")
    for brand in ("A", "B"):
        cid = db.upsert_competitor(con, brand)
        for i in range(6):
            db.upsert_ad(con, cid, "meta", f"{brand}{i}", f"Limited time: 40% off ends Friday {brand}{i}", "", "",
                         "2026-01-01", "2026-12-31", True, {}, placements="facebook")
    classify.classify_new_ads(con, classify.HeuristicClassifier())
    path = tmp_path / "labels.csv"
    assert evaluate.export(con, n=8, path=path) == 8
    rows = list(csv.DictReader(open(path)))
    assert set(rows[0]) == {"ad_id", "brand", "text", "true_theme", "true_audience"}   # no model tags leak into the sheet
    assert {r["brand"] for r in rows} == {"A", "B"}                                 # spread across brands
    for r in rows:
        r["true_theme"], r["true_audience"] = "urgency", "general"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    res = evaluate.score(path, con)
    assert res["theme"]["n"] == 8 and res["theme"]["overall"] == 1.0          # heuristic tags these as urgency
    rows[0]["true_theme"] = "pricee"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    with pytest.raises(ValueError):
        evaluate.score(path, con)


def test_failed_classifier_call_does_not_crash_and_is_retried(con):
    cid = db.upsert_competitor(con, "X")
    db.upsert_ad(con, cid, "meta", "1", "Save 20% today on shoes", "", "", "2026-01-01", "2026-12-31", True, {})

    class Flaky:
        def classify(self, *a, **k):
            raise RuntimeError("rate limited")
    assert classify.classify_new_ads(con, Flaky()) == (0, 1)
    assert classify.classify_new_ads(con, classify.HeuristicClassifier()) == (1, 0)     # untagged ad is picked up next time
