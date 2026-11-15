"""Generate the README's SVG figures from the committed results, so the numbers in the
pictures can never drift from the numbers in the data.

Run after `python analysis/weather_effect.py`:  python docs/figures.py
Standard library only. Every SVG carries its own background, so it reads the same in
GitHub's light and dark themes, and uses system font fallbacks."""

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
R = json.loads((ROOT / "docs" / "data" / "results.json").read_text())
IMG = ROOT / "docs" / "img"
IMG.mkdir(exist_ok=True)

# Palette: Ledger (same family as churn-roi), plus taxi amber and one rain blue
PAPER, SURF, SURF2 = "#f6f5f0", "#fdfdfb", "#eeede6"
INK, INK2, MUTED, LINE = "#14261f", "#3f4f48", "#5f6b65", "#dcdcd3"
ACC, ACC_INK, AMBER, CP, RAIN = "#0f8f62", "#0b6e4b", "#f2b632", "#b26d00", "#2f6fa6"
SANS = "Archivo, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

CONDS = list(R["meta"]["conditions"].items())


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pct(v, d=1):
    s = f"{abs(v):.{d}f}"
    if float(s) == 0:
        return f"{0:.{d}f}%"
    return ("+" if v > 0 else "−") + s + "%"


def svg_open(w, h, title, desc):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-labelledby="t d">',
        f'<title id="t">{esc(title)}</title><desc id="d">{esc(desc)}</desc>',
        f'<rect width="{w}" height="{h}" rx="18" fill="{PAPER}"/>',
    ]


def write(name, parts):
    parts.append("</svg>")
    (IMG / name).write_text("\n".join(parts) + "\n")


# ---------------------------------------------------------------- results.svg
def results_figure():
    eff = R["effects"]
    rows = [(k, label, eff["jfk"][k]["trips"], eff["cp"][k]["trips"]) for k, label in CONDS]
    W, H = 1200, 690
    alt = "; ".join(
        f"{label}: JFK {pct(j['estimate'])} ({pct(j['ci_low'])} to {pct(j['ci_high'])}), "
        f"Central Park {pct(c['estimate'])} ({pct(c['ci_low'])} to {pct(c['ci_high'])}), {j['n_matched']} days"
        for _, label, j, c in rows
    )
    s = svg_open(W, H, "Rain barely moves taxi demand at JFK or Central Park",
                 f"Change in trips per day against dry days in the same month and weekday, with 95% intervals. {alt}.")
    s.append(f'<text x="48" y="62" font-family="{MONO}" font-size="15" fill="{ACC_INK}">Trips per day vs dry days in the same month and weekday · 95% month-cluster bootstrap · 2023 to 2025</text>')
    s.append(f'<text x="48" y="104" font-family="{SANS}" font-size="34" font-weight="800" letter-spacing="-1" fill="{INK}">Rain barely moves taxi demand</text>')
    # legend
    lx = 830
    s.append(f'<circle cx="{lx}" cy="94" r="7" fill="{INK}"/><text x="{lx + 14}" y="99" font-family="{MONO}" font-size="15" fill="{INK2}">JFK</text>')
    s.append(f'<circle cx="{lx + 80}" cy="94" r="7" fill="{CP}"/><text x="{lx + 94}" y="99" font-family="{MONO}" font-size="15" fill="{INK2}">Central Park</text>')

    top, row_h, label_w, right = 140, 66, 300, 64
    x0, x1 = 48 + label_w, W - right
    lo = min(min(j["ci_low"], c["ci_low"]) for _, _, j, c in rows)
    hi = max(max(j["ci_high"], c["ci_high"]) for _, _, j, c in rows)
    lo, hi = 5 * ((lo - 1) // 5), 5 * (-(-(hi + 1) // 5))
    X = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)
    bottom = top + len(rows) * row_h
    v = lo
    while v <= hi:
        zero = v == 0
        s.append(f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="{top - 6}" y2="{bottom}" stroke="{INK2 if zero else LINE}" stroke-width="{1.6 if zero else 1}"/>')
        s.append(f'<text x="{X(v):.1f}" y="{bottom + 26}" text-anchor="middle" font-family="{MONO}" font-size="14" fill="{MUTED}">{"0%" if zero else pct(v, 0)}</text>')
        v += 5
    for i, (key, label, j, c) in enumerate(rows):
        y = top + i * row_h
        if key == "any_rain":
            s.append(f'<rect x="32" y="{y + 2}" width="{W - 64}" height="{row_h - 4}" rx="10" fill="{ACC}" fill-opacity="0.08"/>')
        if i and key == "snow":
            s.append(f'<line x1="48" x2="{W - 48}" y1="{y}" y2="{y}" stroke="{LINE}" stroke-dasharray="4 5"/>')
        s.append(f'<text x="48" y="{y + 30}" font-family="{SANS}" font-size="18" font-weight="{700 if key == "any_rain" else 500}" fill="{INK}">{esc(label)}</text>')
        s.append(f'<text x="48" y="{y + 52}" font-family="{MONO}" font-size="13.5" fill="{MUTED}">JFK {pct(j["estimate"])} · CP {pct(c["estimate"])} · {j["n_matched"]} days</text>')
        for k, (e, col) in enumerate(((j, INK), (c, CP))):
            yy = y + row_h / 2 + (k - 0.5) * 16
            s.append(f'<line x1="{X(e["ci_low"]):.1f}" x2="{X(e["ci_high"]):.1f}" y1="{yy}" y2="{yy}" stroke="{col}" stroke-width="2.5" stroke-linecap="round"/>')
            for vv in (e["ci_low"], e["ci_high"]):
                s.append(f'<line x1="{X(vv):.1f}" x2="{X(vv):.1f}" y1="{yy - 5}" y2="{yy + 5}" stroke="{col}" stroke-width="2.5" stroke-linecap="round"/>')
            s.append(f'<circle cx="{X(e["estimate"]):.1f}" cy="{yy}" r="6.5" fill="{col}" stroke="{PAPER}" stroke-width="2.5"/>')
    s.append(f'<text x="48" y="{H - 30}" font-family="{SANS}" font-size="15" fill="{MUTED}">Federal holidays and four TLC data-gap days excluded. Weather: NOAA station USW00094728 (Central Park). Few snow, freezing and hot days, so wide intervals.</text>')
    write("results.svg", s)


# ---------------------------------------------------------------- naive.svg
def naive_figure():
    both = R["naive"]["both"]
    cp = {r["rain_level"]: r for r in both["central_park"]}
    mx = {r["rain_level"]: r for r in both["max_of_4_stations"]}
    order = [("no rain", "Dry"), ("light rain", "Light"), ("moderate rain", "Moderate"), ("heavy rain", "Heavy")]
    eff = R["effects"]["both"]
    effk = {"light rain": "light_rain", "moderate rain": "moderate_rain", "heavy rain": "heavy_rain"}
    W, H = 1200, 560
    tot = sum(r["total_trips"] for r in cp.values())
    days = sum(r["days"] for r in cp.values())
    desc = (
        "Three readings of the same days, JFK and Central Park combined, Central Park weather. "
        + "Total trips: " + ", ".join(f"{lab} {cp[k]['total_trips']:,} over {cp[k]['days']} days" for k, lab in order)
        + ". Trips per day: " + ", ".join(f"{lab} {cp[k]['trips_per_day']:,.0f}" for k, lab in order)
        + ". Against dry days in the same month and weekday: "
        + ", ".join(f"{lab} {pct(eff[effk[k]]['trips']['estimate'])}" for k, lab in order[1:])
        + f". Taking the max of four stations instead labels {mx['heavy rain']['days']} days heavy rain, not {cp['heavy rain']['days']}."
    )
    s = svg_open(W, H, "Totals count days, not demand", desc)
    s.append(f'<text x="48" y="62" font-family="{MONO}" font-size="15" fill="{ACC_INK}">JFK + Central Park · rain buckets from Central Park · {days:,} days · {tot / 1e6:.2f}M trips</text>')
    s.append(f'<text x="48" y="104" font-family="{SANS}" font-size="34" font-weight="800" letter-spacing="-1" fill="{INK}">Totals count days, not demand</text>')

    pw, gap, py, ph = 344, 36, 150, 300
    panels = [
        ("1  What the old dashboard charted", "Total trips by bucket", [cp[k]["total_trips"] for k, _ in order], lambda v: f"{v / 1e6:.2f}M", "#a3aaa6"),
        ("2  Divide by the days in each bucket", "Trips per day", [cp[k]["trips_per_day"] for k, _ in order], lambda v: f"{v:,.0f}", INK2),
    ]
    for p, (step, title, vals, fmt, col) in enumerate(panels):
        x = 48 + p * (pw + gap)
        s.append(f'<rect x="{x - 16}" y="{py - 24}" width="{pw + 8}" height="{ph + 92}" rx="14" fill="{SURF}" stroke="{LINE}"/>')
        s.append(f'<text x="{x}" y="{py + 4}" font-family="{MONO}" font-size="13.5" fill="{MUTED}">{esc(step)}</text>')
        s.append(f'<text x="{x}" y="{py + 30}" font-family="{SANS}" font-size="19" font-weight="700" fill="{INK}">{title}</text>')
        base, top_ = py + ph - 10, py + 70
        m = max(vals)
        bw = 58
        for i, ((k, lab), v) in enumerate(zip(order, vals)):
            bx = x + 12 + i * 80
            h = (base - top_) * v / m
            s.append(f'<rect x="{bx}" y="{base - h:.1f}" width="{bw}" height="{h:.1f}" rx="4" fill="{col}"/>')
            s.append(f'<text x="{bx + bw / 2}" y="{base - h - 8:.1f}" text-anchor="middle" font-family="{MONO}" font-size="13" fill="{INK}">{fmt(v)}</text>')
            s.append(f'<text x="{bx + bw / 2}" y="{base + 22}" text-anchor="middle" font-family="{SANS}" font-size="14" fill="{INK}">{lab}</text>')
            s.append(f'<text x="{bx + bw / 2}" y="{base + 40}" text-anchor="middle" font-family="{MONO}" font-size="12" fill="{MUTED}">{cp[k]["days"]} days</text>')

    # Panel 3: controlled effects
    x = 48 + 2 * (pw + gap)
    s.append(f'<rect x="{x - 16}" y="{py - 24}" width="{pw + 8}" height="{ph + 92}" rx="14" fill="#e3f1ea" stroke="{ACC}" stroke-opacity="0.5"/>')
    s.append(f'<text x="{x}" y="{py + 4}" font-family="{MONO}" font-size="13.5" fill="{ACC_INK}">3  Compare like with like</text>')
    s.append(f'<text x="{x}" y="{py + 30}" font-family="{SANS}" font-size="19" font-weight="700" fill="{INK}">vs dry days, same month and weekday</text>')
    lo, hi = -6, 6
    ax0, ax1 = x + 96, x + pw - 16
    X = lambda v: ax0 + (v - lo) / (hi - lo) * (ax1 - ax0)
    yb = py + ph - 10
    for v in (-6, -3, 0, 3, 6):
        s.append(f'<line x1="{X(v):.1f}" x2="{X(v):.1f}" y1="{py + 70}" y2="{yb}" stroke="{INK2 if v == 0 else LINE}" stroke-width="{1.5 if v == 0 else 1}"/>')
        s.append(f'<text x="{X(v):.1f}" y="{yb + 22}" text-anchor="middle" font-family="{MONO}" font-size="12" fill="{MUTED}">{"0%" if v == 0 else pct(v, 0)}</text>')
    for i, (k, lab) in enumerate(order[1:]):
        e = eff[effk[k]]["trips"]
        y = py + 108 + i * 62
        s.append(f'<text x="{x}" y="{y - 4}" font-family="{SANS}" font-size="15" fill="{INK}">{lab}</text>')
        s.append(f'<text x="{x}" y="{y + 15}" font-family="{MONO}" font-size="12.5" fill="{ACC_INK}">{pct(e["estimate"])}</text>')
        s.append(f'<line x1="{X(e["ci_low"]):.1f}" x2="{X(e["ci_high"]):.1f}" y1="{y}" y2="{y}" stroke="{ACC}" stroke-width="2.5" stroke-linecap="round"/>')
        for vv in (e["ci_low"], e["ci_high"]):
            s.append(f'<line x1="{X(vv):.1f}" x2="{X(vv):.1f}" y1="{y - 5}" y2="{y + 5}" stroke="{ACC}" stroke-width="2.5" stroke-linecap="round"/>')
        s.append(f'<circle cx="{X(e["estimate"]):.1f}" cy="{y}" r="6" fill="{ACC}" stroke="#e3f1ea" stroke-width="2"/>')

    s.append(f'<text x="48" y="{H - 30}" font-family="{SANS}" font-size="15" fill="{MUTED}">Dry days hold {cp["no rain"]["total_trips"] / tot:.0%} of all trips because {cp["no rain"]["days"] / days:.0%} of days were dry. Taking the wettest of four NYC stations would call {mx["heavy rain"]["days"]} days heavy rain, not {cp["heavy rain"]["days"]}.</text>')
    write("naive.svg", s)


# ---------------------------------------------------------------- architecture.svg
def box(x, y, w, h, title, lines, kind="plain"):
    fill, stroke, tcol, lcol = {
        "plain": (SURF, LINE, INK, INK2),
        "dark": (INK, INK, PAPER, "#c4d0ca"),
        "accent": ("#e3f1ea", ACC, ACC_INK, INK2),
        "amber": ("#fdf1d2", "#ecc970", INK, INK2),
    }[kind]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}"/>',
           f'<text x="{x + 16}" y="{y + 30}" font-family="{SANS}" font-size="17" font-weight="700" fill="{tcol}">{esc(title)}</text>']
    for n, ln in enumerate(lines):
        out.append(f'<text x="{x + 16}" y="{y + 54 + n * 20}" font-family="{MONO}" font-size="12.5" fill="{lcol}">{esc(ln)}</text>')
    return out


def arrow(x1, y1, x2, y2):
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{INK2}" stroke-width="2" marker-end="url(#ar)"/>'


def architecture_figure():
    W, H = 1200, 600
    s = svg_open(W, H, "Architecture: a cloud pipeline and a local re-analysis that produce the same numbers",
                 "Cloud path, orchestrated by Luigi: TLC parquet and NOAA weather in Google Cloud Storage go to a PySpark job on Dataproc, "
                 "which cleans trips, removes exact duplicates, keeps one weather station, drops data-gap days and aggregates per day and zone; "
                 "it writes a fact table and three dimensions back to GCS, which Snowflake loads into a star schema read by the Tableau dashboard. "
                 "Local path: analysis/weather_effect.py reads the same public files with DuckDB, applies the same filters and writes daily.csv and results.json, "
                 "which feed the static dashboard and the README figures.")
    s.append(f'<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{INK2}"/></marker></defs>')
    s.append(f'<text x="48" y="58" font-family="{SANS}" font-size="28" font-weight="800" letter-spacing="-0.8" fill="{INK}">One set of rules, two ways to run it</text>')

    s.append(f'<rect x="32" y="88" width="{W - 64}" height="250" rx="14" fill="none" stroke="{LINE}" stroke-dasharray="6 6"/>')
    s.append(f'<text x="52" y="80" font-family="{MONO}" font-size="13.5" fill="{MUTED}">Cloud pipeline · ./pipeline.py · Luigi tasks with marker files</text>')
    s += box(52, 112, 214, 112, "Raw data in GCS", ["TLC yellow parquet", "36 months, 2023-2025", "NOAA daily CSV"])
    s += box(306, 112, 296, 196, "PySpark on Dataproc", ["Spark/spark-job.py", "same trip filters", "exact-duplicate removal", "one station: Central Park", "drop TLC data-gap days", "aggregate per day x zone"], "dark")
    s += box(642, 112, 214, 112, "Star schema in GCS", ["fact_taxi_demand", "dim_time, dim_weather", "dim_location (CSV)"])
    s += box(896, 112, 252, 112, "Snowflake", ["COPY INTO from a stage", "NYCTAXI_DB.STAR_SCHEMA", "V_TRIPS_PER_DAY_BY_RAIN"])
    s += box(896, 246, 252, 72, "Tableau Public", ["team dashboard (Zihan Ye)"], "amber")
    s.append(arrow(266, 168, 302, 168))
    s.append(arrow(602, 168, 638, 168))
    s.append(arrow(856, 168, 892, 168))
    s.append(arrow(1022, 224, 1022, 242))

    s.append(f'<rect x="32" y="380" width="{W - 64}" height="190" rx="14" fill="none" stroke="{ACC}" stroke-opacity="0.5" stroke-dasharray="6 6"/>')
    s.append(f'<text x="52" y="372" font-family="{MONO}" font-size="13.5" fill="{ACC_INK}">Local re-analysis · no cloud accounts · reruns byte for byte</text>')
    s += box(52, 404, 214, 112, "Public downloads", ["TLC CloudFront parquet", "NOAA GHCN-Daily API", "data/raw, gitignored"])
    s += box(306, 404, 296, 140, "analysis/weather_effect.py", ["DuckDB, same filters", "like-for-like comparison", "month-cluster bootstrap", "fixed-effects regression"], "accent")
    s += box(642, 404, 214, 112, "Committed outputs", ["docs/data/daily.csv", "docs/data/results.json", "checked by pytest"])
    s += box(896, 404, 252, 140, "Static dashboard", ["docs/index.html", "recomputes every effect", "in the browser and checks", "itself against results.json"], "accent")
    s.append(arrow(266, 460, 302, 460))
    s.append(arrow(602, 460, 638, 460))
    s.append(arrow(856, 460, 892, 460))
    write("architecture.svg", s)


# ---------------------------------------------------------------- star-schema.svg
def table_box(x, y, w, name, cols, kind="plain"):
    head_fill = {"plain": INK2, "fact": INK}[kind]
    h = 46 + 24 * len(cols) + 10
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{SURF}" stroke="{LINE}"/>',
           f'<path d="M{x} {y + 12} a12 12 0 0 1 12 -12 h{w - 24} a12 12 0 0 1 12 12 v28 h-{w} z" fill="{head_fill}"/>',
           f'<text x="{x + 16}" y="{y + 27}" font-family="{MONO}" font-size="14.5" font-weight="600" fill="{PAPER}">{name}</text>']
    for i, (col, typ, key) in enumerate(cols):
        yy = y + 64 + i * 24
        colr = ACC_INK if key else INK
        out.append(f'<text x="{x + 16}" y="{yy}" font-family="{MONO}" font-size="13" fill="{colr}" font-weight="{600 if key else 400}">{col}</text>')
        out.append(f'<text x="{x + w - 16}" y="{yy}" text-anchor="end" font-family="{MONO}" font-size="12" fill="{MUTED}">{typ}{"  " + key if key else ""}</text>')
    return out, h


def star_figure():
    W, H = 1200, 545
    s = svg_open(W, H, "Star schema: one fact table, three dimensions",
                 "FACT_TAXI_DEMAND holds one row per date and pickup location with trip_count, avg_fare, avg_tip, avg_tip_pct, avg_total, "
                 "avg_distance and card_tip_pct. It joins DIM_TIME on DATE_ID, DIM_LOCATION on LOCATION_ID (132 JFK, 43 Central Park) and "
                 "DIM_WEATHER on WEATHER_ID (rain, snow and temperature level).")
    s.append(f'<text x="48" y="58" font-family="{SANS}" font-size="28" font-weight="800" letter-spacing="-0.8" fill="{INK}">Star schema in Snowflake</text>')
    s.append(f'<text x="48" y="86" font-family="{MONO}" font-size="13.5" fill="{MUTED}">NYCTAXI_DB.STAR_SCHEMA · grain: one row per date and pickup zone</text>')
    fact, fh = table_box(420, 170, 360, "FACT_TAXI_DEMAND", [
        ("DATE_ID", "DATE", "FK"), ("LOCATION_ID", "INT", "FK"), ("WEATHER_ID", "INT", "FK"),
        ("TRIP_COUNT", "INT", ""), ("AVG_FARE", "FLOAT", ""), ("AVG_TIP", "FLOAT", ""), ("AVG_TIP_PCT", "FLOAT", ""),
        ("AVG_TOTAL", "FLOAT", ""), ("AVG_DISTANCE", "FLOAT", ""), ("CARD_TIP_PCT", "FLOAT", ""),
    ], "fact")
    t, th = table_box(48, 130, 300, "DIM_TIME", [("DATE_ID", "DATE", "PK"), ("YEAR", "INT", ""), ("QUARTER", "INT", ""), ("MONTH", "INT", ""), ("DAY", "INT", ""), ("DAY_OF_WEEK", "INT", "")])
    l, lh = table_box(852, 130, 300, "DIM_LOCATION", [("LOCATION_ID", "INT", "PK"), ("LOCATION_NAME", "STRING", "")])
    w, wh = table_box(852, 340, 300, "DIM_WEATHER", [("WEATHER_ID", "INT", "PK"), ("RAIN_LEVEL", "STRING", ""), ("SNOW_LEVEL", "STRING", ""), ("TEMP_LEVEL", "STRING", "")])
    s += fact + t + l + w
    # connectors from FK rows to dimension PKs
    s.append(f'<path d="M420 234 C 384 234, 384 194, 348 194" fill="none" stroke="{ACC}" stroke-width="2"/>')
    s.append(f'<path d="M780 258 C 816 258, 816 194, 852 194" fill="none" stroke="{ACC}" stroke-width="2"/>')
    s.append(f'<path d="M780 282 C 816 282, 816 404, 852 404" fill="none" stroke="{ACC}" stroke-width="2"/>')
    s.append(f'<text x="852" y="{130 + lh + 26}" font-family="{MONO}" font-size="12.5" fill="{MUTED}">132 JFK · 43 Central Park</text>')
    s.append(f'<text x="48" y="{130 + th + 30}" font-family="{SANS}" font-size="14.5" fill="{INK2}">Ask the question per day:</text>')
    s.append(f'<text x="48" y="{130 + th + 54}" font-family="{MONO}" font-size="13" fill="{INK}">AVG(TRIP_COUNT) GROUP BY RAIN_LEVEL</text>')
    s.append(f'<text x="48" y="{130 + th + 76}" font-family="{MONO}" font-size="13" fill="{MUTED}">not SUM(TRIP_COUNT), which counts days</text>')
    write("star-schema.svg", s)


results_figure()
naive_figure()
architecture_figure()
star_figure()
print("wrote", ", ".join(sorted(p.name for p in IMG.glob("*.svg"))))
