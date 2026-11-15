#!/usr/bin/env python3
"""Does weather change taxi demand at JFK and Central Park? A per-day, like-for-like answer.

Inputs (public, no accounts needed; `--download` fetches whatever is missing):
  * NYC TLC yellow taxi trip records, monthly parquet, Jan 2023 to Dec 2025
    https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_YYYY-MM.parquet
  * NOAA GHCN-Daily, NY City Central Park station USW00094728 (the weather used everywhere)
  * NOAA GHCN-Daily for JFK, LaGuardia and Newark too, only to show how much taking the
    max across stations (what Spark/spark-job.py did) inflates the rain buckets

Outputs (committed, small):
  * docs/data/daily.csv     one row per date and pickup zone: trips, fare, tip, distance, weather
  * docs/data/results.json  controlled effects with intervals, naive totals, regression, data notes

Method, in one paragraph: trips are cleaned with the same row filters as Spark/spark-job.py,
counted per pickup date for JFK (TLC zone 132) and Central Park (zone 43), and joined to one
weather station. A weather day is compared only with dry days in the same calendar month,
same year and same day of week (federal holidays left out), and the interval comes from a
cluster bootstrap that resamples whole months. A log-linear regression with year-month and
day-of-week fixed effects and HAC standard errors is reported as a cross-check.

Run:  python analysis/weather_effect.py --download
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
OUT_DIR = ROOT / "docs" / "data"

YEARS = (2023, 2024, 2025)
ZONES = {132: "JFK", 43: "Central Park"}
STATION = "USW00094728"
STATION_NAME = "NY City Central Park"
OTHER_STATIONS = ["USW00094789", "USW00014732", "USW00014734"]  # JFK, LaGuardia, Newark

TLC_URL = "https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_{y}-{m:02d}.parquet"
NOAA_URL = (
    "https://www.ncei.noaa.gov/access/services/data/v1?dataset=daily-summaries"
    "&stations={stations}&startDate=2023-01-01&endDate=2025-12-31"
    "&dataTypes={types}&units=standard&format=csv&includeStationName=true"
)

# A data-gap day: the raw TLC file (all zones, before any filter) has fewer than GAP_MIN_RECORDS
# pickups in at least one hour between 08:00 and 21:59. Real quiet days never get close: the
# quietest daytime hour of Christmas 2023 still logged 894 records citywide.
GAP_HOURS = range(8, 22)
GAP_MIN_RECORDS = 300
BOOT_SEED = 405
BOOT_REPS = 2000

# Conditions the dashboard offers. Each compares "treated" days with dry control days
# in the same year, month and weekday. Kept in sync with docs/app.js (CONDITIONS).
CONDITIONS = {
    "any_rain": "Any rain",
    "light_rain": "Light rain",
    "moderate_rain": "Moderate rain",
    "heavy_rain": "Heavy rain",
    "snow": "Snow",
    "freezing": "Freezing",
    "hot": "Hot",
}
METRICS = {"trips": "Trips per day", "avg_fare": "Average fare", "tip_pct": "Tip % (card)"}
ZONE_KEYS = {"jfk": ["JFK"], "cp": ["Central Park"], "both": ["JFK", "Central Park"]}


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------
def taxi_files() -> list[Path]:
    return [RAW / "taxi" / f"yellow_tripdata_{y}-{m:02d}.parquet" for y in YEARS for m in range(1, 13)]


def download_inputs() -> None:
    (RAW / "taxi").mkdir(parents=True, exist_ok=True)
    (RAW / "weather").mkdir(parents=True, exist_ok=True)
    for path in taxi_files():
        if path.exists() and path.stat().st_size > 0:
            continue
        y, m = path.stem.split("_")[-1].split("-")
        _fetch(TLC_URL.format(y=y, m=int(m)), path)
    cp = RAW / "weather" / "central_park_daily.csv"
    if not cp.exists():
        _fetch(NOAA_URL.format(stations=STATION, types="PRCP,SNOW,SNWD,TMAX,TMIN,TAVG,AWND"), cp)
    multi = RAW / "weather" / "nyc_first_order_daily.csv"
    if not multi.exists():
        stations = ",".join([STATION, *OTHER_STATIONS])
        _fetch(NOAA_URL.format(stations=stations, types="PRCP,SNOW,TMAX,TMIN"), multi)


def _fetch(url: str, dest: Path) -> None:
    print(f"downloading {url}", file=sys.stderr)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=300) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    tmp.rename(dest)


# --------------------------------------------------------------------------------------
# Taxi: same row filters as Spark/spark-job.py, exact-duplicate removal, per-day sums
# --------------------------------------------------------------------------------------
# Every condition below is copied from clean_taxi_df() in Spark/spark-job.py. SQL and Spark
# share NULL semantics here: a comparison against NULL is not true, so the row is dropped.
VALID_TRIP = """
    passenger_count BETWEEN 1 AND 6
    AND trip_distance > 0 AND trip_distance <= 100
    AND fare_amount BETWEEN 0 AND 500
    AND total_amount BETWEEN 0 AND 500
    AND (fare_amount = 0 OR tip_amount <= fare_amount)
    AND pulocationid IS NOT NULL AND dolocationid IS NOT NULL
    AND tpep_pickup_datetime IS NOT NULL
    AND year(tpep_pickup_datetime) BETWEEN 2023 AND 2025
    AND (tip_amount IS NULL OR tip_amount BETWEEN 0 AND 200)
    AND (tolls_amount IS NULL OR tolls_amount BETWEEN 0 AND 200)
    AND (extra IS NULL OR extra BETWEEN 0 AND 50)
    AND (mta_tax IS NULL OR mta_tax BETWEEN 0 AND 10)
    AND (improvement_surcharge IS NULL OR improvement_surcharge BETWEEN 0 AND 10)
    AND (congestion_surcharge IS NULL OR congestion_surcharge BETWEEN 0 AND 20)
    AND (airport_fee IS NULL OR airport_fee BETWEEN 0 AND 20)
    AND (cbd_congestion_fee IS NULL OR cbd_congestion_fee BETWEEN 0 AND 20)
"""

COLUMNS = [
    "tpep_pickup_datetime", "tpep_dropoff_datetime", "passenger_count", "trip_distance",
    "pulocationid", "dolocationid", "payment_type", "fare_amount", "extra", "mta_tax",
    "tip_amount", "tolls_amount", "improvement_surcharge", "total_amount",
    "congestion_surcharge", "airport_fee", "cbd_congestion_fee",
]


def _select_list(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    """Lower-case the file's columns and add cbd_congestion_fee where it is missing (pre-2025)."""
    have = {r[0].lower(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{path}'").fetchall()}
    parts = []
    for col in COLUMNS:
        if col in have:
            parts.append(f'"{have[col]}" AS {col}')
        elif col == "cbd_congestion_fee":
            parts.append("0.0::DOUBLE AS cbd_congestion_fee")
        else:
            raise KeyError(f"{path.name} has no column {col}")
    return ", ".join(parts)


def build_taxi_daily() -> tuple[pd.DataFrame, dict]:
    con = duckdb.connect()
    frames, citywide, dedupe = [], [], {"rows_valid": 0, "zone_trips_exact_dedupe": {}, "zone_trips_spark_date_dedupe": {}}
    for z in ZONES.values():
        dedupe["zone_trips_exact_dedupe"][z] = 0
        dedupe["zone_trips_spark_date_dedupe"][z] = 0
    for path in taxi_files():
        if not path.exists():
            raise FileNotFoundError(f"{path} missing; run with --download")
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE t AS SELECT {_select_list(con, path)} "
            f"FROM '{path}' WHERE TRUE"
        )
        citywide.append(
            con.execute(
                "SELECT CAST(tpep_pickup_datetime AS DATE) AS date, hour(tpep_pickup_datetime) AS hour, "
                "count(*) AS records FROM t GROUP BY 1, 2"
            ).df()
        )
        con.execute(f"CREATE OR REPLACE TEMP TABLE v AS SELECT DISTINCT * FROM t WHERE {VALID_TRIP}")
        dedupe["rows_valid"] += con.execute("SELECT count(*) FROM v").fetchone()[0]

        # What the original Spark job did: drop duplicates on the pickup DATE (not timestamp),
        # dropoff date, distance, fare, tip and total, across all zones, before keeping 132/43.
        # Spark keeps an arbitrary row per group; here the earliest pickup is kept, so this is
        # one realisation of that rule rather than the exact rows Dataproc kept.
        spark_like = con.execute(
            """
            SELECT pulocationid, count(*) FROM (
                SELECT pulocationid FROM v
                QUALIFY row_number() OVER (
                    PARTITION BY CAST(tpep_pickup_datetime AS DATE), CAST(tpep_dropoff_datetime AS DATE),
                                 trip_distance, fare_amount, tip_amount, total_amount
                    ORDER BY tpep_pickup_datetime, tpep_dropoff_datetime, pulocationid, dolocationid
                ) = 1
            ) WHERE pulocationid IN (132, 43) GROUP BY 1
            """
        ).fetchall()
        for loc, n in spark_like:
            dedupe["zone_trips_spark_date_dedupe"][ZONES[loc]] += n

        day = con.execute(
            """
            SELECT CAST(tpep_pickup_datetime AS DATE) AS date,
                   pulocationid AS location_id,
                   count(*) AS trips,
                   sum(CASE WHEN payment_type = 1 THEN 1 ELSE 0 END) AS card_trips,
                   sum(fare_amount) AS fare_sum,
                   sum(tip_amount) AS tip_sum,
                   sum(CASE WHEN payment_type = 1 THEN fare_amount ELSE 0 END) AS card_fare_sum,
                   sum(CASE WHEN payment_type = 1 THEN tip_amount ELSE 0 END) AS card_tip_sum,
                   sum(trip_distance) AS distance_sum,
                   sum(total_amount) AS total_sum
            FROM v WHERE pulocationid IN (132, 43)
            GROUP BY 1, 2
            """
        ).df()
        frames.append(day)
        print(f"{path.name}: {int(day.trips.sum()):,} trips at JFK + Central Park", file=sys.stderr)

    daily = pd.concat(frames, ignore_index=True)
    # A month file can hold a few stray trips from neighbouring months (or years): sum them in.
    keys = ["date", "location_id"]
    daily = daily.groupby(keys, as_index=False).sum(numeric_only=True)
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily[(daily.date >= "2023-01-01") & (daily.date <= "2025-12-31")]
    daily["zone"] = daily.location_id.map(ZONES)
    # Parallel float sums can differ in the last bits between runs; round money and miles to
    # cents so the committed table is exactly what every later step reads (and reruns match).
    money = ["fare_sum", "tip_sum", "card_fare_sum", "card_tip_sum", "distance_sum", "total_sum"]
    daily[money] = daily[money].round(2)
    daily[["trips", "card_trips"]] = daily[["trips", "card_trips"]].astype(int)

    # Data gaps: the TLC files themselves are nearly empty on a few days (all zones, before any
    # filter). Those days say nothing about weather, so they are dropped and listed in results.
    hourly = pd.concat(citywide).groupby(["date", "hour"], as_index=False).records.sum()
    hourly["date"] = pd.to_datetime(hourly.date)
    hourly = hourly[(hourly.date >= "2023-01-01") & (hourly.date <= "2025-12-31")]
    city = hourly.groupby("date").records.sum()
    good_hours = (
        hourly[hourly.hour.isin(GAP_HOURS) & (hourly.records >= GAP_MIN_RECORDS)].groupby("date").hour.nunique()
    ).reindex(city.index, fill_value=0)
    gaps = sorted(good_hours[good_hours < len(GAP_HOURS)].index)
    dedupe["data_gap_rule"] = (
        f"fewer than {GAP_MIN_RECORDS} raw records citywide in any hour from "
        f"{GAP_HOURS.start:02d}:00 to {GAP_HOURS.stop - 1:02d}:59"
    )
    dedupe["data_gap_dates"] = [
        {"date": d.strftime("%Y-%m-%d"), "citywide_records": int(city[d]), "full_daytime_hours": int(good_hours[d])}
        for d in gaps
    ]
    for z in ZONES.values():  # counted before gap days go, to compare with the Spark-style count
        dedupe["zone_trips_exact_dedupe"][z] = int(daily.loc[daily.zone == z, "trips"].sum())
    daily = daily[~daily.date.isin(gaps)]
    return daily, dedupe


# --------------------------------------------------------------------------------------
# Weather: one station, original bucket thresholds
# --------------------------------------------------------------------------------------
def rain_level(p: float) -> str:
    if p == 0:
        return "no rain"
    if p <= 0.1:
        return "light rain"
    if p <= 0.3:
        return "moderate rain"
    return "heavy rain"


def snow_level(s: float) -> str:
    if s == 0:
        return "no snow"
    if s <= 1:
        return "light snow"
    if s <= 3:
        return "moderate snow"
    return "heavy snow"


def temp_level(t: float) -> str:
    if t < 32:
        return "freezing"
    if t < 50:
        return "cold"
    if t < 70:
        return "mild"
    return "warm"


def build_weather() -> pd.DataFrame:
    w = pd.read_csv(RAW / "weather" / "central_park_daily.csv")
    w = w[w.STATION == STATION].copy()
    w["date"] = pd.to_datetime(w.DATE)
    # Central Park does not publish TAVG for these years, so use the (TMAX + TMIN) / 2
    # convention NOAA itself uses for daily normals.
    w["tavg_f"] = (w.TMAX + w.TMIN) / 2
    out = pd.DataFrame(
        {
            "date": w.date,
            "prcp_in": w.PRCP.astype(float),
            "snow_in": w.SNOW.astype(float),
            "tmax_f": w.TMAX.astype(float),
            "tmin_f": w.TMIN.astype(float),
            "tavg_f": w.tavg_f.astype(float),
        }
    )
    if out[["prcp_in", "snow_in", "tmax_f", "tmin_f"]].isna().any().any():
        raise ValueError("Central Park weather has missing PRCP, SNOW, TMAX or TMIN")
    out["rain_level"] = out.prcp_in.map(rain_level)
    out["snow_level"] = out.snow_in.map(snow_level)
    out["temp_level"] = out.tavg_f.map(temp_level)
    return out


def max_across_stations() -> pd.DataFrame:
    m = pd.read_csv(RAW / "weather" / "nyc_first_order_daily.csv")
    m["date"] = pd.to_datetime(m.DATE)
    g = m.groupby("date").agg(prcp_max=("PRCP", "max"), n_stations=("STATION", "nunique")).reset_index()
    g["rain_level_max"] = g.prcp_max.map(rain_level)
    return g


# --------------------------------------------------------------------------------------
# Like-for-like comparison (mirrored in docs/app.js so the dashboard recomputes it live)
# --------------------------------------------------------------------------------------
class Mulberry32:
    """Tiny seeded PRNG, bit-for-bit identical to the JavaScript version in docs/app.js."""

    def __init__(self, seed: int) -> None:
        self.a = seed & 0xFFFFFFFF

    def __call__(self) -> float:
        self.a = (self.a + 0x6D2B79F5) & 0xFFFFFFFF
        t = self.a
        t = _imul(t ^ (t >> 15), t | 1)
        t ^= (t + _imul(t ^ (t >> 7), t | 61)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296


def _imul(a: int, b: int) -> int:
    return (a * b) & 0xFFFFFFFF


def quantile(sorted_vals: list[float], p: float) -> float:
    """Linear interpolation between order statistics (numpy's default), as in app.js."""
    pos = p * (len(sorted_vals) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def is_treated(row: pd.Series, cond: str) -> bool:
    p, s, t = row.prcp_in, row.snow_in, row.tavg_f
    dry = p == 0 and s == 0
    return {
        "any_rain": p > 0 and s == 0,
        "light_rain": 0 < p <= 0.1 and s == 0,
        "moderate_rain": 0.1 < p <= 0.3 and s == 0,
        "heavy_rain": p > 0.3 and s == 0,
        "snow": s > 0,
        "freezing": dry and t < 32,
        "hot": dry and t >= 80,
    }[cond]


def is_control(row: pd.Series, cond: str) -> bool:
    dry = row.prcp_in == 0 and row.snow_in == 0
    if cond == "freezing":
        return dry and row.tavg_f >= 32
    if cond == "hot":
        return dry and row.tavg_f < 80
    return dry


def zone_series(daily: pd.DataFrame, zones: list[str]) -> pd.DataFrame:
    """Sum the chosen zones per date, then derive the three metrics from the sums."""
    d = daily[daily.zone.isin(zones)]
    sums = d.groupby("date", as_index=False)[
        ["trips", "fare_sum", "card_fare_sum", "card_tip_sum"]
    ].sum()
    weather = d.drop_duplicates("date").set_index("date")[
        ["prcp_in", "snow_in", "tavg_f", "holiday", "dow"]
    ]
    s = sums.set_index("date").join(weather).reset_index()
    s["avg_fare"] = s.fare_sum / s.trips
    s["tip_pct"] = 100 * s.card_tip_sum / s.card_fare_sum
    s["ym"] = s.date.dt.strftime("%Y-%m")
    return s.sort_values("date").reset_index(drop=True)


def matched_effect(s: pd.DataFrame, cond: str, metric: str, seed: int = BOOT_SEED, reps: int = BOOT_REPS) -> dict:
    s = s[~s.holiday]
    treated = s[s.apply(is_treated, axis=1, cond=cond)]
    control = s[s.apply(is_control, axis=1, cond=cond)]
    ctrl_mean = control.groupby(["ym", "dow"])[metric].mean()
    clusters: dict[str, list[float]] = {}
    n_unmatched = 0
    for _, row in treated.iterrows():
        key = (row.ym, row.dow)
        if key not in ctrl_mean.index:
            n_unmatched += 1
            continue
        clusters.setdefault(row.ym, []).append(row[metric] / ctrl_mean[key] - 1)
    keys = sorted(clusters)
    n = sum(len(clusters[k]) for k in keys)
    out = {"n_treated": int(len(treated)), "n_matched": int(n), "n_unmatched": int(n_unmatched), "n_months": len(keys)}
    if n == 0:
        return {**out, "estimate": None, "ci_low": None, "ci_high": None}
    est = sum(sum(clusters[k]) for k in keys) / n
    rng = Mulberry32(seed)
    boots = []
    for _ in range(reps):
        tot, cnt = 0.0, 0
        for _ in keys:
            vals = clusters[keys[math.floor(rng() * len(keys))]]
            tot += sum(vals)
            cnt += len(vals)
        boots.append(tot / cnt)
    boots.sort()
    return {
        **out,
        "estimate": 100 * est,
        "ci_low": 100 * quantile(boots, 0.025),
        "ci_high": 100 * quantile(boots, 0.975),
    }


# --------------------------------------------------------------------------------------
# Regression cross-check
# --------------------------------------------------------------------------------------
def regression(s: pd.DataFrame, metric: str) -> dict:
    import statsmodels.formula.api as smf

    d = s.copy()
    d["y"] = np.log(d[metric])
    d["rain"] = pd.Categorical(
        d.prcp_in.map(rain_level), categories=["no rain", "light rain", "moderate rain", "heavy rain"]
    )
    d["snowy"] = (d.snow_in > 0).astype(int)
    d["freezing"] = (d.tavg_f < 32).astype(int)
    d["hot"] = (d.tavg_f >= 80).astype(int)
    d["hol"] = d.holiday.astype(int)
    fit = smf.ols("y ~ C(rain) + snowy + freezing + hot + hol + C(ym) + C(dow)", data=d).fit(
        cov_type="HAC", cov_kwds={"maxlags": 7}
    )
    terms = {
        "light_rain": "C(rain)[T.light rain]",
        "moderate_rain": "C(rain)[T.moderate rain]",
        "heavy_rain": "C(rain)[T.heavy rain]",
        "snow": "snowy",
        "freezing": "freezing",
        "hot": "hot",
    }
    ci = fit.conf_int()
    coefs = {}
    for k, term in terms.items():
        b = fit.params[term]
        coefs[k] = {
            "estimate": 100 * (math.exp(b) - 1),
            "ci_low": 100 * (math.exp(ci.loc[term, 0]) - 1),
            "ci_high": 100 * (math.exp(ci.loc[term, 1]) - 1),
            "p_value": float(fit.pvalues[term]),
        }
    return {"n_days": int(fit.nobs), "r2": float(fit.rsquared), "effects_pct": coefs}


# --------------------------------------------------------------------------------------
# The naive view the original dashboard took
# --------------------------------------------------------------------------------------
RAIN_ORDER = ["no rain", "light rain", "moderate rain", "heavy rain"]


def naive_tables(daily: pd.DataFrame, maxw: pd.DataFrame) -> dict:
    out = {}
    d = daily.merge(maxw[["date", "rain_level_max"]], on="date", how="left")
    for zkey, zones in ZONE_KEYS.items():
        sub = d[d.zone.isin(zones)]
        per_day = sub.groupby("date").agg(
            trips=("trips", "sum"), rain_level=("rain_level", "first"), rain_level_max=("rain_level_max", "first")
        )
        res = {}
        for col, label in (("rain_level_max", "max_of_4_stations"), ("rain_level", "central_park")):
            g = per_day.groupby(col)["trips"].agg(["sum", "count", "mean"]).reindex(RAIN_ORDER)
            res[label] = [
                {"rain_level": lvl, "total_trips": int(r["sum"]), "days": int(r["count"]), "trips_per_day": float(r["mean"])}
                for lvl, r in g.iterrows()
            ]
        out[zkey] = res
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download", action="store_true", help="fetch any missing TLC and NOAA inputs first")
    args = ap.parse_args()
    if args.download:
        download_inputs()

    taxi, dedupe = build_taxi_daily()
    weather = build_weather()
    from pandas.tseries.holiday import USFederalHolidayCalendar

    hols = set(USFederalHolidayCalendar().holidays("2023-01-01", "2025-12-31"))

    daily = taxi.merge(weather, on="date", how="left", validate="many_to_one")
    if daily.prcp_in.isna().any():
        raise ValueError("a taxi date has no weather row")
    daily["dow"] = daily.date.dt.dayofweek  # 0 = Monday
    daily["holiday"] = daily.date.isin(hols)
    daily = daily.sort_values(["date", "location_id"]).reset_index(drop=True)

    # Max precipitation across four first-order stations: only to replay the original
    # max-across-stations bucketing in the dashboard's naive view.
    maxw = max_across_stations()
    maxw = maxw[maxw.date.isin(daily.date)]  # same analysis days as everything else
    daily = daily.merge(maxw[["date", "prcp_max"]].rename(columns={"prcp_max": "prcp_max4_in"}), on="date", how="left")

    # ---- per-day table for the dashboard and the tests
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    table = daily[
        [
            "date", "zone", "location_id", "trips", "card_trips", "fare_sum", "tip_sum", "card_fare_sum",
            "card_tip_sum", "distance_sum", "total_sum", "prcp_in", "prcp_max4_in", "snow_in", "tmax_f", "tmin_f", "tavg_f",
            "rain_level", "snow_level", "temp_level", "dow", "holiday",
        ]
    ].copy()
    table["date"] = table.date.dt.strftime("%Y-%m-%d")
    table["holiday"] = table.holiday.astype(int)
    table.to_csv(OUT_DIR / "daily.csv", index=False)

    # ---- effects
    effects, regs = {}, {}
    for zkey, zones in ZONE_KEYS.items():
        s = zone_series(daily, zones)
        effects[zkey] = {c: {m: matched_effect(s, c, m) for m in METRICS} for c in CONDITIONS}
        regs[zkey] = {m: regression(s, m) for m in METRICS}

    one = daily.drop_duplicates("date")
    day_counts = {
        "rain_level": one.rain_level.value_counts().reindex(RAIN_ORDER).astype(int).to_dict(),
        "rain_level_max_of_4_stations": maxw.rain_level_max.value_counts().reindex(RAIN_ORDER).astype(int).to_dict(),
        "snow_days": int((one.snow_in > 0).sum()),
        "freezing_days": int((one.tavg_f < 32).sum()),
        "hot_days": int((one.tavg_f >= 80).sum()),
        "holidays": int(one.holiday.sum()),
        "days": int(len(one)),
    }
    totals = {
        z: {
            "trips": int(daily.loc[daily.zone == z, "trips"].sum()),
            "trips_per_day": float(daily.loc[daily.zone == z, "trips"].mean()),
        }
        for z in ZONES.values()
    }
    dedupe["spark_date_dedupe_share_lost"] = {
        z: 1 - dedupe["zone_trips_spark_date_dedupe"][z] / dedupe["zone_trips_exact_dedupe"][z] for z in ZONES.values()
    }

    results = {
        "meta": {
            "period": ["2023-01-01", "2025-12-31"],
            "zones": {str(k): v for k, v in ZONES.items()},
            "weather_station": {"id": STATION, "name": STATION_NAME},
            "comparison": "each weather day vs dry days in the same year, month and weekday; federal holidays excluded",
            "interval": f"95% cluster bootstrap over months, {BOOT_REPS} draws, mulberry32 seed {BOOT_SEED}",
            "regression": "OLS on log(metric) with year-month and weekday fixed effects, holiday dummy, HAC(7) SEs",
            "tip_pct": "card trips only (cash tips are not recorded): sum of tips / sum of fares",
            "conditions": CONDITIONS,
            "metrics": METRICS,
        },
        "day_counts": day_counts,
        "totals": totals,
        "effects": effects,
        "regression": regs,
        "naive": naive_tables(daily, maxw),
        "data_quality": dedupe,
    }
    (OUT_DIR / "results.json").write_text(json.dumps(results, indent=1, default=_json_default) + "\n")
    print(json.dumps({z: effects[z]["any_rain"]["trips"] for z in effects}, indent=1, default=_json_default))


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    raise TypeError(type(o))


if __name__ == "__main__":
    main()
