"""Data-quality checks on the committed analysis output (docs/data). No raw data needed."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))
import weather_effect as we  # noqa: E402

DAILY = pd.read_csv(ROOT / "docs" / "data" / "daily.csv", parse_dates=["date"])
RESULTS = json.loads((ROOT / "docs" / "data" / "results.json").read_text())
GAPS = {pd.Timestamp(g["date"]) for g in RESULTS["data_quality"]["data_gap_dates"]}


def test_one_row_per_date_and_zone():
    assert not DAILY.duplicated(["date", "zone"]).any()
    assert set(DAILY.zone) == {"JFK", "Central Park"}
    assert dict(zip(DAILY.location_id, DAILY.zone)) == {132: "JFK", 43: "Central Park"}


def test_no_null_keys_or_measures():
    cols = ["date", "zone", "location_id", "trips", "fare_sum", "prcp_in", "snow_in", "tavg_f", "rain_level"]
    assert not DAILY[cols].isna().any().any()


def test_every_day_covered_except_documented_gaps():
    expected = set(pd.date_range("2023-01-01", "2025-12-31")) - GAPS
    for zone, g in DAILY.groupby("zone"):
        assert set(g.date) == expected, zone
    assert len(GAPS) <= 7, "more than a week of data gaps: check the TLC files"


def test_sensible_ranges():
    d = DAILY
    assert (d.trips > 0).all()
    assert (d.card_trips <= d.trips).all()
    avg_fare = d.fare_sum / d.trips
    assert avg_fare.between(5, 120).all()
    tip_pct = d.card_tip_sum / d.card_fare_sum
    assert tip_pct.between(0, 0.5).all()
    miles = d.distance_sum / d.trips
    assert miles.between(0.5, 40).all()
    assert d.prcp_in.between(0, 10).all() and d.snow_in.between(0, 40).all()
    assert (d.tmin_f <= d.tavg_f).all() and (d.tavg_f <= d.tmax_f).all()
    assert d.dow.between(0, 6).all()
    # JFK trips are airport runs: longer and pricier than Central Park pickups, every year
    by = d.assign(year=d.date.dt.year).groupby(["year", "zone"])[["fare_sum", "trips"]].sum()
    fare = by.fare_sum / by.trips
    for year in (2023, 2024, 2025):
        assert fare[(year, "JFK")] > 2 * fare[(year, "Central Park")]


def test_weather_buckets_follow_thresholds():
    assert (DAILY.rain_level == DAILY.prcp_in.map(we.rain_level)).all()
    assert (DAILY.snow_level == DAILY.snow_in.map(we.snow_level)).all()
    assert (DAILY.temp_level == DAILY.tavg_f.map(we.temp_level)).all()
    one = DAILY.drop_duplicates("date")
    assert one.rain_level.value_counts().to_dict() == {
        k: v for k, v in RESULTS["day_counts"]["rain_level"].items() if v
    }


def test_results_totals_match_the_table():
    for zone, t in RESULTS["totals"].items():
        assert t["trips"] == DAILY.loc[DAILY.zone == zone, "trips"].sum()


def test_effect_intervals_are_ordered():
    for zone in RESULTS["effects"].values():
        for cond in zone.values():
            for e in cond.values():
                assert e["n_matched"] <= e["n_treated"]
                if e["estimate"] is not None:
                    assert e["ci_low"] <= e["estimate"] <= e["ci_high"]


def test_mulberry32_matches_javascript():
    # First three draws of mulberry32(405), as printed by the same function in docs/app.js
    rng = we.Mulberry32(405)
    draws = [rng() for _ in range(3)]
    assert draws == pytest.approx([0.9057318824343383, 0.24359855754300952, 0.8087025540880859], abs=1e-15)


@pytest.mark.parametrize(
    "zone,cond,metric",
    [("jfk", "any_rain", "trips"), ("cp", "any_rain", "trips"), ("both", "heavy_rain", "trips"), ("cp", "snow", "avg_fare")],
)
def test_headline_effects_recompute_from_the_committed_table(zone, cond, metric):
    """The committed results.json is reproducible from daily.csv alone."""
    d = DAILY.copy()
    d["holiday"] = d.holiday.astype(bool)
    got = we.matched_effect(we.zone_series(d, we.ZONE_KEYS[zone]), cond, metric)
    want = RESULTS["effects"][zone][cond][metric]
    assert got["n_matched"] == want["n_matched"]
    for k in ("estimate", "ci_low", "ci_high"):
        assert got[k] == pytest.approx(want[k], abs=0.01)

