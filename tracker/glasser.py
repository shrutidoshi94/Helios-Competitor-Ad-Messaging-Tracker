"""Cached Glasser client with a hard per-run budget cap.

Every response is cached on disk keyed by (provider, endpoint, input), so replays
are free. Live calls go through the `glasser` CLI and are refused if they would
push the run's spend past the cap. Prices below were read with `glasser inspect`."""
import hashlib
import json
import re
import subprocess
import uuid
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
TASK = "tsk_aef18c18770e2d4b"

# worst-case price per call (USD), from `glasser inspect` — used for the pre-flight cap check
ENDPOINTS = {
    "meta_ads": ("scrapecreators", "/v1/facebook/adLibrary/search/ads", 0.00188),
    "meta_company": ("scrapecreators", "/v1/facebook/adLibrary/company/ads", 0.00188),
    "meta_find": ("scrapecreators", "/v1/facebook/adLibrary/search/companies", 0.00188),
    "linkedin_ads": ("scrapecreators", "/v1/linkedin/ads/search", 0.00188),
    "google_ads": ("serpapi", "/search?engine=google_ads_transparency_center", 0.015),
    "keywords": ("semrush", "/?type=domain_organic", 0.055),   # 0.00055/row, cap at 100 rows
    "news": ("serper", "/news", 0.0011),
}


class BudgetExceeded(RuntimeError):
    pass


class Glasser:
    def __init__(self, budget_usd, allow_live=False, con=None, run_label="run"):
        self.budget, self.allow_live, self.con, self.label = budget_usd, allow_live, con, run_label
        self.spent = 0.0

    @staticmethod
    def cache_path(provider, endpoint, payload):
        h = hashlib.sha256(json.dumps([provider, endpoint, payload], sort_keys=True).encode()).hexdigest()[:16]
        return RAW / f"{provider}_{h}.json"

    def call(self, kind, payload, worst_case=None):
        provider, endpoint, price = ENDPOINTS[kind]
        price = worst_case if worst_case is not None else price
        path = self.cache_path(provider, endpoint, payload)
        if path.exists():
            self._log(provider, endpoint, 0.0, None, True)
            return json.loads(path.read_text())
        if not self.allow_live:
            raise BudgetExceeded(f"{kind} not cached and live calls are disabled")
        if self.spent + price > self.budget:
            raise BudgetExceeded(f"{kind} (<= ${price}) would exceed budget ${self.budget} (spent ${self.spent:.5f})")
        RAW.mkdir(parents=True, exist_ok=True)
        out = subprocess.run(
            ["glasser", "run", "-p", provider, "-e", endpoint, "-i", json.dumps(payload),
             "--task", TASK, "--idempotency-key", str(uuid.uuid4()), "-o", str(path)],
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(f"glasser run failed: {out.stderr.strip() or out.stdout.strip()}")
        m = re.search(r"Charge:\s+\$([0-9.]+)", out.stdout)
        charge = float(m.group(1)) if m else price
        url = re.search(r"Run URL:\s+(\S+)", out.stdout)
        self.spent += charge
        self._log(provider, endpoint, charge, url.group(1) if url else None, False)
        return json.loads(path.read_text())

    def _log(self, provider, endpoint, charge, url, cached):
        if self.con is not None:
            self.con.execute("INSERT INTO spend_log(run_label,provider,endpoint,charge,run_url,cached) "
                             "VALUES(?,?,?,?,?,?)", (self.label, provider, endpoint, charge, url, int(cached)))
            self.con.commit()

    # --- thin endpoint wrappers (inputs verified against `glasser inspect`) ---
    def meta_ads(self, query, country="US", cursor=None):
        p = {"query": query, "status": "ACTIVE", "country": country, "ad_type": "all", "trim": True}
        if cursor:
            p["cursor"] = cursor
        return self.call("meta_ads", p)

    def find_brands(self, query):
        """Search Facebook Pages by name -> [{name, page_id, likes, category}] (one paid call, cached)."""
        resp = self.call("meta_find", {"query": query.strip()})
        return [dict(name=r["name"], page_id=str(r["page_id"]), likes=r.get("likes") or 0,
                     category=r.get("category") or "", ig=r.get("ig_username") or "")
                for r in resp.get("searchResults", []) if not r.get("page_is_deleted")]

    def meta_company_ads(self, page_id, country="US", cursor=None, status="ACTIVE"):
        """All ads of ONE Page (no keyword noise). Inputs verified against `glasser inspect`."""
        p = {"pageId": str(page_id), "status": status, "country": country, "trim": True}
        if cursor:
            p["cursor"] = cursor
        return self.call("meta_company", p)

    def linkedin_ads(self, brand, cursor=None):
        """LinkedIn Ad Library search. `keyword` is a required field of the endpoint; the brand name works as both."""
        p = {"company": brand, "keyword": brand}
        if cursor:
            p["paginationToken"] = cursor
        return self.call("linkedin_ads", p)

    def keywords(self, domain, database="us", limit=10):
        return self.call("keywords", {"domain": domain, "database": database, "display_limit": limit},
                         worst_case=0.00055 * limit)
