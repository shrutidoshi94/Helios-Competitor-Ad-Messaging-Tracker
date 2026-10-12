"""Helios — competitor ad & messaging tracker (Streamlit dashboard).

Run:  .venv/bin/streamlit run app.py
Real Meta ads fetched through Glasser; live calls only when enabled, always under a budget cap."""
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from tracker import classify, db, diff, digest, glasser, ingest

LOGO = "assets/helios_logo_small.png"
st.set_page_config(page_title="Helios", page_icon=LOGO, layout="wide")
st.logo("assets/helios_wordmark.png", icon_image=LOGO, size="large")

con = db.connect(db.DB_PATH.with_name("real.db"))
clf = classify.get_classifier()

# ------------------------------------------------------------------ sidebar
with st.sidebar.expander("Spend controls", expanded=False):
    allow_live = st.checkbox("Allow live Glasser calls", value=False,
                             help="Off: only saved data is used and nothing is billed.")
    budget = st.number_input("Budget cap per action ($)", 0.0, 5.0, 0.05, 0.005, format="%.3f")
    st.caption(f"Classifier: {clf.name}")
    n_stale = len(classify.stale_tags(con, clf))
    if n_stale:                                       # tags left over from a previously active classifier
        st.caption(f"{n_stale} ads are still tagged by an older classifier.")
        if st.button(f"Re-tag {n_stale} ads with {clf.name}", use_container_width=True,
                     help="One classifier call per ad. Only needed after switching classifier."):
            with st.spinner(f"Re-tagging {n_stale} ads with {clf.name}..."):
                done, bad = classify.retag_stale(con, clf)
            st.success(f"Re-tagged {done} ads." + (f" {bad} failed and keep their old tag." if bad else ""))
            st.rerun()

ingest.seed_cache_from_first_call()
ingest.log_setup_spend(con)
# Notion is left out on purpose: its cache is still seeded (ingest.SEED) and searchable, but every
# one of its ads lands in a single theme, which makes a poor opening view. Search for it to track it.
DEFAULTS = [("ChatGPT", "616148004918000"), ("Claude", "989603317562933"),
            ("Perplexity AI", "109760398882363")]
if not con.execute("SELECT 1 FROM competitors WHERE page_id IS NOT NULL").fetchone():
    for n, pid in DEFAULTS:                           # first run only; after that the user's picks win
        db.upsert_competitor(con, n, page_id=pid)
con.execute("UPDATE competitors SET is_self=0")       # every tracked brand is just a watched brand
con.commit()
g = glasser.Glasser(budget, allow_live=allow_live, con=con, run_label=f"ui-{date.today()}")
cached_g = glasser.Glasser(budget, allow_live=False, con=con)


def tracked():
    return [dict(name=r["name"], page_id=r["page_id"])
            for r in con.execute("SELECT * FROM competitors WHERE page_id IS NOT NULL ORDER BY name")]


st.sidebar.markdown("### Platforms")
platforms = st.sidebar.multiselect(
    "Platforms to watch", ["Facebook", "Instagram", "LinkedIn"], default=["Facebook", "Instagram"],
    label_visibility="collapsed",
    help="Facebook and Instagram come from the same Meta ad library pull. LinkedIn is a separate pull "
         f"(about ${glasser.ENDPOINTS['linkedin_ads'][2]} per page, 2 pages per brand).")
PL = {p.lower() for p in platforms}


def fetch(pages, client):
    """Pull ads for the selected platforms. Meta covers Facebook + Instagram; LinkedIn is its own library."""
    try:
        if PL & {"facebook", "instagram"}:
            ingest.collect_pages(con, client, pages, max_pages=3, status="ALL")
        if "linkedin" in PL:
            res = ingest.collect_linkedin(con, client, [p["name"] for p in pages], max_pages=2)
            stopped = [f"{n}: {v}" for n, v in res.items() if isinstance(v, str)]
            if stopped and client.allow_live:
                st.sidebar.warning("LinkedIn: " + stopped[0])
    except glasser.BudgetExceeded as e:
        st.sidebar.error(str(e))


st.sidebar.markdown("### Brands")
q = st.sidebar.text_input("Find a brand", placeholder="e.g. Asana", key="brand_q")
if st.sidebar.button("Search Glasser", use_container_width=True,
                     help=f"A new search costs ${glasser.ENDPOINTS['meta_find'][2]}; repeats are free.") and q.strip():
    try:
        st.session_state["found"] = g.find_brands(q)
    except glasser.BudgetExceeded:
        st.sidebar.error("Not saved yet. Turn on 'Allow live Glasser calls' under Spend controls.")
found = st.session_state.get("found", [])
if found:
    label = lambda f: f"{f['name']} · {f['category'] or 'Page'} · {f['likes']:,} likes"
    picks = st.sidebar.multiselect("Results", found, format_func=label)
    if st.sidebar.button("Track selected", type="primary", use_container_width=True) and picks:
        for f in picks:
            db.upsert_competitor(con, f["name"], page_id=f["page_id"])
        res = fetch([dict(name=f["name"], page_id=f["page_id"]) for f in picks], g)
        st.sidebar.success(f"Fetched ads. Spent ${g.spent:.4f}")
cur = tracked()
drop = st.sidebar.multiselect("Stop tracking", [c["name"] for c in cur])
if drop and st.sidebar.button("Remove selected", use_container_width=True):
    for n in drop:
        cid = con.execute("SELECT id FROM competitors WHERE name=?", (n,)).fetchone()[0]
        con.execute("DELETE FROM ad_tags WHERE ad_id IN (SELECT id FROM ads WHERE competitor_id=?)", (cid,))
        con.execute("DELETE FROM ads WHERE competitor_id=?", (cid,))
        con.execute("DELETE FROM competitors WHERE id=?", (cid,))
    con.commit()
    st.rerun()
if st.sidebar.button("Refresh ads", use_container_width=True,
                     help="Re-fetches every tracked brand. Uses saved data unless live calls are on."):
    fetch(tracked(), g)
    st.sidebar.success(f"Done. Spent ${g.spent:.4f}")

as_of = date.today().isoformat()
empty = [c for c in tracked() if not con.execute(
    "SELECT 1 FROM ads a JOIN competitors k ON k.id=a.competitor_id WHERE k.name=? LIMIT 1", (c["name"],)).fetchone()]
if empty:                                             # replay saved responses for new brands (free)
    fetch(empty, cached_g)
    ingest.collect_pages(con, cached_g, empty, max_pages=2, status="ACTIVE")
if "linkedin" in PL:                                  # replay any saved LinkedIn pulls (free)
    ingest.collect_linkedin(con, cached_g, [c["name"] for c in tracked()], max_pages=2)
with st.spinner("Tagging new ads with " + clf.name + "..."):
    n_tagged, n_failed = classify.classify_new_ads(con, clf)
if n_failed:
    st.sidebar.warning(f"{n_failed} ads could not be tagged just now; they will be retried on the next refresh.")

comps = con.execute("SELECT * FROM competitors WHERE page_id IS NOT NULL ORDER BY name").fetchall()

# ------------------------------------------------------------------ header
if not PL:
    st.info("Pick at least one platform in the sidebar.")
    st.stop()
if not comps:
    st.info("Search for a brand in the sidebar.")
    st.stop()
st.markdown("<style>.block-container{padding-top:3.5rem;}</style>", unsafe_allow_html=True)

spend = con.execute("SELECT COALESCE(SUM(charge),0) FROM spend_log WHERE cached=0").fetchone()[0]
c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="center")
weeks = c1.select_slider("Show the ads running", options=list(range(13)), value=3,
                         format_func=lambda w: "now" if w == 0 else f"{w}w ago")
snap = (date.fromisoformat(as_of) - timedelta(weeks=weeks)).isoformat()      # the day the snapshot is taken
snap_txt = "now" if weeks == 0 else f"{weeks}w ago ({date.fromisoformat(snap):%b %d})"
ads_at = {c["name"]: diff.active_ads(con, c["id"], snap, PL) for c in comps}  # ads running on that day, per brand
order = sorted(ads_at, key=lambda n: -len(ads_at[n]))
c2.metric("Need review", sum(1 for rows in ads_at.values() for r in rows if r["needs_review"]))
c3.metric("Glasser spend", f"${spend:.4f}")

tab_brand, tab_heat, tab_trend, tab_ads, tab_digest = st.tabs(["Brands", "Heatmap", "Trend", "Ads", "Brief"])
themes = list(classify.THEMES)
BLUE = "#1f77b4"
BUCKET_COLORS = {"Price": "#2f8fe0", "Speed": "#ff8f3d", "Trust": "#ffd54f", "Features": "#8ecbfb",
                 "Urgency": "#ef6f6c", "Education": "#5cc9a7", "Brand": "#b39ddb"}

# ------------------------------------------------------------------ brands: ads running that week, by theme
with tab_brand:
    rows_all = [{"Brand": n, "Theme": r["theme"].title(), "Example": " ".join((r["text"] or "").split())[:110]}
                for n in order for r in ads_at[n]]
    if not rows_all:
        st.info("No ads were running then.")
        st.stop()
    ldf = pd.DataFrame(rows_all)
    t_order = [t.title() for t in themes]               # fixed buckets, even when empty
    cnt = ldf.groupby(["Brand", "Theme"]).agg(Ads=("Example", "size"), Example=("Example", "first")).reset_index()

    sel = st.session_state.get("brand_sel")
    if sel not in order:
        sel = st.session_state["brand_sel"] = order[0]
    i_sel = order.index(sel)
    d = cnt[cnt.Brand == sel]
    st.markdown("<style>.st-key-car_prev button,.st-key-car_next button{height:160px;}</style>", unsafe_allow_html=True)
    st.markdown(f"# {sel}")
    st.markdown(f"{len(ads_at[sel])} ads running  ·  {snap_txt}")
    left_c, mid_c, right_c = st.columns([1, 18, 1], vertical_alignment="center")
    if left_c.button(":material/chevron_left:", key="car_prev", width="stretch", help="Previous brand"):
        st.session_state["brand_sel"] = order[(i_sel - 1) % len(order)]
        st.rerun()
    if right_c.button(":material/chevron_right:", key="car_next", width="stretch", help="Next brand"):
        st.session_state["brand_sel"] = order[(i_sel + 1) % len(order)]
        st.rerun()
    base = alt.Chart(d).encode(
        x=alt.X("Theme:N", sort=t_order, title=None, scale=alt.Scale(domain=t_order),
                axis=alt.Axis(labelAngle=0, labelFontSize=14)),
        y=alt.Y("Ads:Q", title=None, axis=alt.Axis(tickMinStep=1)),
        tooltip=[alt.Tooltip("Theme:N"), alt.Tooltip("Ads:Q"), alt.Tooltip("Example:N", title="Example ad")])
    chart = base.mark_bar(color=BLUE) + base.mark_text(dy=-9, fontSize=15, fontWeight="bold", color="#1f2937").encode(text="Ads:Q")
    mid_c.altair_chart(chart.properties(height=400), width="stretch")

# ------------------------------------------------------------------ heatmap: mix of ads running that week
with tab_heat:
    cells, hl = [], {}
    for n in order:
        total = len(ads_at[n])
        share = diff.theme_share(ads_at[n])
        hl[n] = f"{n} · {total} ads"
        for t in themes:
            v = share.get(t, 0.0)
            cells.append({"Brand": hl[n], "Theme": t.title(), "Share": v, "Ads": round(v * total),
                          "Text": f"{v:.0%}" if v else ""})
    rows2 = [hl[n] for n in order]
    cdf = pd.DataFrame(cells)
    theme_order = [t.title() for t in themes]            # fixed columns: an empty bucket stays as an empty column
    heat = alt.Chart(cdf).encode(
        x=alt.X("Theme:N", sort=theme_order, title=None, axis=alt.Axis(orient="top", labelAngle=0, labelFontSize=13)),
        y=alt.Y("Brand:N", sort=rows2, title=None, axis=alt.Axis(labelLimit=220, labelFontSize=13)))
    rect = heat.mark_rect(stroke="white", strokeWidth=2).encode(
        color=alt.Color("Share:Q", scale=alt.Scale(scheme="blues", domain=[0, 1]), legend=None),
        tooltip=[alt.Tooltip("Brand:N"), alt.Tooltip("Theme:N"), alt.Tooltip("Ads:Q"),
                 alt.Tooltip("Share:Q", format=".0%", title="Share of ads running")])
    txt = heat.mark_text(fontSize=15, fontWeight="bold").encode(
        text="Text:N", color=alt.condition("datum.Share > 0.45", alt.value("white"), alt.value("#1f2937")))
    left = (rect + txt).properties(width=560, height=62 * len(order),
                                   title=alt.TitleParams(f"Share of ads running {snap_txt}", anchor="start", fontSize=14))
    vol = pd.DataFrame([{"Brand": hl[n], "Ads": len(ads_at[n])} for n in order])
    vb = alt.Chart(vol).encode(y=alt.Y("Brand:N", sort=rows2, title=None, axis=None), x=alt.X("Ads:Q", title=None, axis=None))
    right = (vb.mark_bar(color=BLUE, size=26) + vb.mark_text(align="left", dx=4, fontSize=14).encode(text="Ads:Q")).properties(
        width=240, height=62 * len(order), title=alt.TitleParams("Ads running", anchor="start", fontSize=14))
    st.altair_chart(alt.hconcat(left, right, spacing=10), width="stretch")

# ------------------------------------------------------------------ trend: ads per bucket, week by week
with tab_trend:
    pick = st.selectbox("Brand", ["All brands"] + order, key="trend_brand", label_visibility="collapsed")
    chosen = [c for c in comps if pick == "All brands" or c["name"] == pick]
    weekly = []
    for w in range(12, -1, -1):
        day = (date.fromisoformat(as_of) - timedelta(weeks=w)).isoformat()
        counts = {}
        for c in chosen:
            for r in diff.active_ads(con, c["id"], day, PL):
                counts[r["theme"]] = counts.get(r["theme"], 0) + 1
        for t in themes:
            weekly.append({"Day": day, "Bucket": t.title(), "Ads": counts.get(t, 0)})
    tdf = pd.DataFrame(weekly)
    seen = [t.title() for t in themes if tdf[tdf.Bucket == t.title()].Ads.sum() > 0]
    if not seen:
        st.info("No ads in this period.")
    else:
        tdf = tdf[tdf.Bucket.isin(seen)]
        # largest bucket is drawn first (at the back) so smaller translucent areas stay visible in front of it
        by_size = tdf.groupby("Bucket").Ads.sum().sort_values(ascending=False).index.tolist()
        tdf["Layer"] = tdf.Bucket.map({b: i for i, b in enumerate(by_size)})
        enc = dict(
            x=alt.X("Day:T", title=None, axis=alt.Axis(format="%Y-%m-%d", labelFontSize=12, tickCount=13, grid=False,
                                                       labelAngle=-30, domain=True)),
            y=alt.Y("Ads:Q", stack=None, title="Ads running", axis=alt.Axis(tickMinStep=1, gridColor="#e5e7eb", domain=False)),
            color=alt.Color("Bucket:N", sort=seen, title=None,
                            scale=alt.Scale(domain=list(BUCKET_COLORS), range=list(BUCKET_COLORS.values())),
                            legend=alt.Legend(orient="top", labelFontSize=14, symbolType="square", symbolSize=140, columns=7)),
            order=alt.Order("Layer:Q"),
            tooltip=[alt.Tooltip("Day:T", format="%b %d", title="Week"), alt.Tooltip("Bucket:N"), alt.Tooltip("Ads:Q")])
        area = alt.Chart(tdf).mark_area(opacity=0.7, line={"strokeWidth": 2}, interpolate="monotone").encode(**enc)
        mark = alt.Chart(pd.DataFrame({"Day": [snap]})).mark_rule(strokeDash=[4, 4], color="#9ca3af").encode(x="Day:T")
        st.altair_chart((area + mark).properties(
            height=440, title=alt.TitleParams("Ads running by bucket" + ("" if pick == "All brands" else f" · {pick}"),
                                              anchor="middle", color="#6b7280", fontSize=18, fontWeight="normal")),
            width="stretch")
        st.caption("Dashed line = the week picked on the slider. Earlier weeks can be undercounted: only each brand's "
                   "most recent ~50 ads are stored.")

# ------------------------------------------------------------------ all ads
with tab_ads:
    rows = con.execute("""SELECT c.name brand, a.platform, a.placements, json_extract(a.raw_json, '$.url') url,
                          a.ext_id, a.first_seen, a.last_seen, a.is_active, a.text,
                          t.theme, t.theme_prob, t.audience, t.audience_prob, t.claim_verifiable_prob,
                          t.needs_review, t.analyzable
                          FROM ads a JOIN competitors c ON c.id=a.competitor_id JOIN ad_tags t ON t.ad_id=a.id
                          WHERE c.page_id IS NOT NULL""").fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        st.info("No ads yet.")
    else:
        df = df[df.placements.fillna("facebook").apply(lambda x: bool(set(x.split(",")) & PL))]
        df = df[(df.first_seen <= snap) & (df.last_seen >= snap)]            # running on the chosen day
        df["Platform"] = df.placements.fillna("facebook").apply(
            lambda x: ", ".join(p.title() for p in x.split(",") if p in ("facebook", "instagram", "linkedin")))
        df["Status"] = df.apply(lambda r: "Not analyzable" if not r.analyzable else ("Review" if r.needs_review else "OK"), axis=1)
        df["Days live"] = (pd.Timestamp(snap) - pd.to_datetime(df.first_seen)).dt.days.where(df.first_seen > "2001-01-01")
        df["Ad"] = df.url.fillna("https://www.facebook.com/ads/library?id=" + df.ext_id.astype(str))
        df["audience"] = df.audience.str.replace("_", " ").str.title()
        df["theme"] = df.theme.str.title()
        df["Copy"] = df.text.fillna("").str.replace("\n", " ").str.slice(0, 180)
        f1, f2, f3 = st.columns([2, 2, 2])
        brands = f1.multiselect("Brand", sorted(df.brand.unique()), default=sorted(df.brand.unique()))
        status = f2.multiselect("Status", ["OK", "Review", "Not analyzable"], default=["OK", "Review"])
        sort = f3.selectbox("Sort by", ["Newest", "Longest running", "Least confident"])
        v = df[df.brand.isin(brands) & df.Status.isin(status)]
        if st.checkbox("Only claims Jev doubts can be checked"):
            v = v[v.claim_verifiable_prob < 0.4]
        v = {"Newest": v.sort_values("first_seen", ascending=False),
             "Longest running": v.sort_values("Days live", ascending=False, na_position="last"),
             "Least confident": v.sort_values("theme_prob")}[sort]
        st.dataframe(
            v[["brand", "Platform", "Status", "theme", "theme_prob", "audience", "audience_prob", "claim_verifiable_prob",
               "Days live", "Copy", "Ad"]],
            hide_index=True, height=560, width="stretch",
            column_config={
                "brand": st.column_config.TextColumn("Brand", width="small"),
                "Platform": st.column_config.TextColumn(width="medium"),
                "Status": st.column_config.TextColumn(width="small"),
                "theme": st.column_config.TextColumn("Theme", width="small"),
                "theme_prob": st.column_config.ProgressColumn("Theme conf.", min_value=0, max_value=1, format="percent"),
                "audience": st.column_config.TextColumn("Audience", width="medium"),
                "audience_prob": st.column_config.ProgressColumn("Audience conf.", min_value=0, max_value=1, format="percent"),
                "claim_verifiable_prob": st.column_config.ProgressColumn("Claim checkable", min_value=0, max_value=1, format="percent"),
                "Days live": st.column_config.NumberColumn(width="small"),
                "Copy": st.column_config.TextColumn("Ad copy", width="large"),
                "Ad": st.column_config.LinkColumn("Open", display_text="view", width="small"),
            })

# ------------------------------------------------------------------ brief
with tab_digest:
    md, _ = digest.build(con, snap, spend=spend, budget=None, weeks_back=3, placements=PL)
    with st.container(border=True):
        st.markdown(md.replace("\n## ", "\n#### ").replace("# Competitor", "### Competitor", 1))
    st.download_button("Download brief (.md)", md, file_name="helios_brief.md")
