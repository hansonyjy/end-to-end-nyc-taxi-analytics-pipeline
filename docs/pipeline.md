# Pipeline Reference

How the cloud pipeline works, stage by stage. The [README](../README.md) has the finding and the setup steps; this page is the detail behind them.

```
./pipeline.py  ->  Luigi: RunSparkJob  ->  LoadToSnowflake  ->  FullPipeline
                          |                     |
              Dataproc runs Spark/spark-job.py  SnowSQL runs Snowflake/snowflake_load.sql
              GCS raw -> GCS star schema (CSV)  GCS star schema -> NYCTAXI_DB.STAR_SCHEMA
```

## Spark: `Spark/spark-job.py`

Runs on a Dataproc cluster. Reads every monthly parquet file under `GCS_BUCKET/RAW_TAXI_PATH` and the NOAA CSV at `GCS_BUCKET/RAW_WEATHER_PATH`.

**1. Standardize each file.** Lower-case the column names, add `cbd_congestion_fee` (new in 2025) as 0 where it is missing, and cast IDs to one schema so the 36 files union cleanly.

**2. Clean trips.** Keep a trip only if all of these hold (NULLs fail the test and are dropped):

| Field | Rule |
|---|---|
| `passenger_count` | 1 to 6 |
| `trip_distance` | above 0, at most 100 miles |
| `fare_amount`, `total_amount` | 0 to 500 |
| `tip_amount` | 0 to 200, and no more than the fare unless the fare is 0 |
| `tolls_amount` | 0 to 200 |
| `extra` | 0 to 50 |
| `mta_tax`, `improvement_surcharge` | 0 to 10 |
| `congestion_surcharge`, `airport_fee`, `cbd_congestion_fee` | 0 to 20 |
| pickup | not NULL, year 2023 to 2025 |
| pickup and dropoff zone | not NULL |

**3. Remove duplicates.** Exact repeated records only. An earlier version first truncated the timestamps to dates and then de-duplicated on (pickup date, dropoff date, distance, fare, tip, total) across every zone. Two different trips on the same day with the same fare and distance counted as one, which removed 45% of Central Park pickups and 9% of JFK pickups over 2023 to 2025 (measured in `docs/data/results.json`, `data_quality`).

**4. Drop data-gap days.** The raw TLC files are nearly empty from 2023-09-21 to 2023-09-24 for every zone. A day counts as a gap when any hour from 08:00 to 21:59 has fewer than 300 raw records citywide. Real quiet days never come close: the quietest daytime hour on Christmas 2023 still logged 894.

**5. Keep two pickup zones.** JFK Airport (TLC zone 132) and Central Park (zone 43).

**6. Build daily weather from one station.** `WEATHER_STATION`, default `USW00094728` (NY City Central Park). The old job took the max precipitation across every station in the export, so a shower at any one gauge made the whole city a rain day. Central Park does not publish `TAVG` for these years, so the job falls back to `(TMAX + TMIN) / 2`.

| Bucket | Rule |
|---|---|
| Rain | none: 0 · light: up to 0.1 in · moderate: up to 0.3 in · heavy: above 0.3 in |
| Snow | none: 0 · light: up to 1 in · moderate: up to 3 in · heavy: above 3 in |
| Temperature | freezing: below 32°F · cold: below 50°F · mild: below 70°F · warm: 70°F and up |

**7. Aggregate and write.** One fact row per date and zone, plus three dimensions, written as CSV with headers to `GCS_BUCKET/<name>/`.

## Snowflake: `Snowflake/snowflake_load.sql`

Run by Luigi through SnowSQL after `__GCS_BUCKET_NO_PREFIX__` is replaced with your bucket.

1. Creates `NYCTAXI_DB.STAR_SCHEMA`, a GCS storage integration (`gcs_taxi_int`) and an external stage (`gcp_taxi_stage`, CSV, header skipped). The integration lets Snowflake read the bucket without embedding credentials; grant its service account read access in GCP the first time.
2. Recreates the four tables (`CREATE OR REPLACE`), so a rerun replaces rather than appends.
3. Loads each table with `COPY INTO` from its folder in the stage.
4. Creates `V_TRIPS_PER_DAY_BY_RAIN`, which reports days, total trips and trips per day for each zone and rain bucket. Chart `TRIPS_PER_DAY`: a bucket with more days always has more total trips.
5. Runs validation queries: row counts, months covered, a check that the fact table has exactly one row per date and location, and a sample star join.

| Table | Grain and columns |
|---|---|
| `FACT_TAXI_DEMAND` | one row per date and location: `DATE_ID`, `LOCATION_ID`, `WEATHER_ID`, `TRIP_COUNT`, `AVG_FARE`, `AVG_TIP`, `AVG_TIP_PCT`, `AVG_TOTAL`, `AVG_DISTANCE`, `CARD_TIP_PCT` |
| `DIM_TIME` | `DATE_ID`, `YEAR`, `QUARTER`, `MONTH`, `DAY`, `DAY_OF_WEEK` (Spark's 1 = Sunday) |
| `DIM_LOCATION` | `LOCATION_ID`, `LOCATION_NAME` |
| `DIM_WEATHER` | `WEATHER_ID`, `RAIN_LEVEL`, `SNOW_LEVEL`, `TEMP_LEVEL` |

`AVG_TIP_PCT` is the mean of tip / fare over all trips, as originally built. `CARD_TIP_PCT` is sum of card tips / sum of card fares, the comparable rate, because cash tips are not recorded.

## Luigi: `Luigi/pipeline.py` and `./pipeline.py`

Three tasks, each writing a marker file in `Luigi/flags/` when it succeeds:

| Task | Does | Marker |
|---|---|---|
| `RunSparkJob` | `gcloud dataproc jobs submit pyspark Spark/spark-job.py` with the GCS paths and station | `spark_completed.marker` |
| `LoadToSnowflake` | renders the SQL with your bucket, runs it with SnowSQL | `snowflake_completed.marker` |
| `FullPipeline` | wrapper that requires the other two | `pipeline_completed.marker` |

Luigi skips any task whose marker exists. `./pipeline.py` clears the markers first, so a plain run redoes everything; `./pipeline.py --keep-flags` keeps them, so after a Snowflake failure it retries only the load. Before anything runs, `./pipeline.py` checks that `gcloud`, `snowsql` and every required variable in `.env` are present, and lists all that are missing at once.

If `gcloud` or `snowsql` is not on your `PATH`, point `GCLOUD_BIN` or `SNOWSQL_BIN` at it in `.env`.

## The Local Re-Analysis: `analysis/weather_effect.py`

Reads the same public files with DuckDB and applies the same rules as steps 1 to 6, without any cloud account. It writes `docs/data/daily.csv` (the fact table with sums instead of averages, plus raw weather values) and `docs/data/results.json`. The trip totals per date and zone are what the fixed Spark job produces; the warehouse simply stores averages where the CSV stores sums.
