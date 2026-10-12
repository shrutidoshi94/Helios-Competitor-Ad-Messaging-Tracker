"""SQLite schema + helpers. Core tables follow the spec; a few columns/tables
are added for dedup (ad_archive_id), cost metering (spend_log) and tag provenance."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitors(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, domains TEXT DEFAULT '',
  is_self INTEGER DEFAULT 0, page_id TEXT);
CREATE TABLE IF NOT EXISTS ads(
  id INTEGER PRIMARY KEY, competitor_id INTEGER NOT NULL REFERENCES competitors(id),
  platform TEXT NOT NULL, ext_id TEXT NOT NULL, text TEXT, title TEXT, cta TEXT,
  first_seen TEXT, last_seen TEXT, is_active INTEGER, synthetic INTEGER DEFAULT 0,
  raw_json TEXT, placements TEXT, UNIQUE(platform, ext_id));
CREATE TABLE IF NOT EXISTS ad_tags(
  ad_id INTEGER PRIMARY KEY REFERENCES ads(id), theme TEXT, theme_prob REAL,
  audience TEXT, audience_prob REAL, claim_verifiable_prob REAL,
  confidence REAL, needs_review INTEGER, analyzable INTEGER DEFAULT 1,
  classifier TEXT, theme_probs TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
  week TEXT, competitor_id INTEGER, theme_distribution TEXT, n_ads INTEGER,
  PRIMARY KEY(week, competitor_id));
CREATE TABLE IF NOT EXISTS spend_log(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, run_label TEXT,
  provider TEXT, endpoint TEXT, charge REAL, run_url TEXT, cached INTEGER);
"""


def connect(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if "page_id" not in {r[1] for r in con.execute("PRAGMA table_info(competitors)")}:
        con.execute("ALTER TABLE competitors ADD COLUMN page_id TEXT")      # migrate older DBs
    if "placements" not in {r[1] for r in con.execute("PRAGMA table_info(ads)")}:
        con.execute("ALTER TABLE ads ADD COLUMN placements TEXT")
    _backfill_placements(con)
    return con


def _backfill_placements(con):
    """Older rows: derive where a Meta ad runs (facebook, instagram, ...) from the stored raw record."""
    rows = con.execute("SELECT id, raw_json FROM ads WHERE placements IS NULL AND platform='meta'").fetchall()
    for r in rows:
        try:
            pl = json.loads(r["raw_json"] or "{}").get("publisher_platform") or []
        except ValueError:
            pl = []
        con.execute("UPDATE ads SET placements=? WHERE id=?", (",".join(x.lower() for x in pl) or "facebook", r["id"]))
    if rows:
        con.commit()


def upsert_competitor(con, name, domains="", is_self=False, page_id=None):
    con.execute("INSERT INTO competitors(name,domains,is_self,page_id) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET domains=excluded.domains, is_self=excluded.is_self, "
                "page_id=COALESCE(excluded.page_id, competitors.page_id)",
                (name, domains, int(is_self), page_id))
    con.commit()
    return con.execute("SELECT id FROM competitors WHERE name=?", (name,)).fetchone()[0]


def upsert_ad(con, competitor_id, platform, ext_id, text, title, cta, first_seen,
              last_seen, is_active, raw, synthetic=False, placements=None):
    """Dedup on (platform, ext_id): re-seeing an ad only extends last_seen/status."""
    con.execute(
        """INSERT INTO ads(competitor_id,platform,ext_id,text,title,cta,first_seen,last_seen,
           is_active,synthetic,raw_json,placements) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(platform,ext_id) DO UPDATE SET
             last_seen=MAX(ads.last_seen, excluded.last_seen), is_active=excluded.is_active,
             placements=COALESCE(excluded.placements, ads.placements)""",
        (competitor_id, platform, ext_id, text, title, cta, first_seen, last_seen,
         int(is_active), int(synthetic), json.dumps(raw), placements))
    con.commit()
