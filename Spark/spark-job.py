import argparse
import os
from functools import reduce

from pyspark.sql import SparkSession, DataFrame
from pyspark.sql import functions as F


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gcs-bucket")
    parser.add_argument("--raw-taxi-path")
    parser.add_argument("--raw-weather-path")
    parser.add_argument("--fact-output-name")
    parser.add_argument("--dim-time-output-name")
    parser.add_argument("--dim-weather-output-name")
    parser.add_argument("--dim-location-output-name")
    parser.add_argument("--weather-station")
    return parser.parse_args()


ARGS = parse_args()

# ----------------------------
# Config
# ----------------------------
RAW_TAXI_PATH = ARGS.raw_taxi_path or os.getenv("RAW_TAXI_PATH", "NYC-taxi")
RAW_WEATHER_PATH = ARGS.raw_weather_path or os.getenv("RAW_WEATHER_PATH", "Weather.csv")

FACT_OUTPUT_NAME = ARGS.fact_output_name or os.getenv("FACT_OUTPUT_NAME", "fact_taxi_demand")
DIM_TIME_OUTPUT_NAME = ARGS.dim_time_output_name or os.getenv("DIM_TIME_OUTPUT_NAME", "dim_time")
DIM_WEATHER_OUTPUT_NAME = ARGS.dim_weather_output_name or os.getenv("DIM_WEATHER_OUTPUT_NAME", "dim_weather")
DIM_LOCATION_OUTPUT_NAME = ARGS.dim_location_output_name or os.getenv("DIM_LOCATION_OUTPUT_NAME", "dim_location")

# One weather station for every day (NY City Central Park). Taking the max across every
# station in a NOAA export turned any shower anywhere in the region into a "heavy rain" day.
WEATHER_STATION = ARGS.weather_station or os.getenv("WEATHER_STATION", "USW00094728")

# Data-gap days: the raw TLC files are nearly empty on a few dates (2023-09-21 to 2023-09-24).
# A day is a gap if any hour from 08:00 to 21:59 has fewer than 300 raw records citywide.
# Same rule as analysis/weather_effect.py.
GAP_HOURS = list(range(8, 22))
GAP_MIN_RECORDS = 300


def require_env(var_name: str) -> str:
    value = os.getenv(var_name)
    if not value:
        raise ValueError(f"Missing required environment variable: {var_name}")
    return value


def build_gcs_path(bucket: str, path: str) -> str:
    bucket = bucket.rstrip("/")
    path = path.lstrip("/")
    return f"{bucket}/{path}"

BUCKET = ARGS.gcs_bucket or require_env("GCS_BUCKET")

TAXI_INPUT = build_gcs_path(BUCKET, RAW_TAXI_PATH)
WEATHER_INPUT = build_gcs_path(BUCKET, RAW_WEATHER_PATH)

FACT_OUTPUT = build_gcs_path(BUCKET, FACT_OUTPUT_NAME)
DIM_TIME_OUTPUT = build_gcs_path(BUCKET, DIM_TIME_OUTPUT_NAME)
DIM_WEATHER_OUTPUT = build_gcs_path(BUCKET, DIM_WEATHER_OUTPUT_NAME)
DIM_LOCATION_OUTPUT = build_gcs_path(BUCKET, DIM_LOCATION_OUTPUT_NAME)

# ----------------------------
# Spark Session
# ----------------------------
def create_spark_session() -> SparkSession:
    return (
        SparkSession.builder
        .appName("nyc-taxi-spark-pipeline")
        .getOrCreate()
    )


# ----------------------------
# Taxi extraction + standardization
# ----------------------------
def get_taxi_files(spark: SparkSession, taxi_input: str) -> list[str]:
    return sorted(spark.read.parquet(taxi_input).inputFiles())


def standardize_taxi_schema(df: DataFrame) -> DataFrame:
    df = df.toDF(*[c.lower() for c in df.columns])

    # Add missing 2025 column if absent
    if "cbd_congestion_fee" not in df.columns:
        df = df.withColumn("cbd_congestion_fee", F.lit(0.0))

    # Cast columns to a common schema seen in the notebook
    df = (
        df.withColumn("vendorid", F.col("vendorid").cast("bigint"))
          .withColumn("passenger_count", F.col("passenger_count").cast("double"))
          .withColumn("ratecodeid", F.col("ratecodeid").cast("double"))
          .withColumn("pulocationid", F.col("pulocationid").cast("bigint"))
          .withColumn("dolocationid", F.col("dolocationid").cast("bigint"))
          .withColumn("payment_type", F.col("payment_type").cast("bigint"))
          .withColumn("cbd_congestion_fee", F.col("cbd_congestion_fee").cast("double"))
    )

    return df


def clean_taxi_df(df: DataFrame) -> DataFrame:
    # Initial duplicate removal + broad row filters from notebook logic
    df = df.dropDuplicates()

    df = df.filter(
        (F.col("passenger_count") >= 1) &
        (F.col("passenger_count") <= 6) &
        (F.col("trip_distance") > 0) &
        (F.col("trip_distance") <= 100) &
        (F.col("fare_amount") >= 0) &
        (F.col("fare_amount") <= 500) &
        (F.col("total_amount") >= 0) &
        (F.col("total_amount") <= 500) &
        ((F.col("fare_amount") == 0) | (F.col("tip_amount") <= F.col("fare_amount"))) &
        (F.col("pulocationid").isNotNull()) &
        (F.col("dolocationid").isNotNull())
    )

    # Stronger final cleaning step from notebook
    condition = (
        F.col("tpep_pickup_datetime").isNotNull() &
        (F.year("tpep_pickup_datetime") >= 2023) &
        (F.year("tpep_pickup_datetime") <= 2025) &
        F.col("pulocationid").isNotNull() &
        F.col("dolocationid").isNotNull() &
        F.col("passenger_count").isNotNull() &
        (F.col("passenger_count") >= 1) & (F.col("passenger_count") <= 6) &
        F.col("trip_distance").isNotNull() &
        (F.col("trip_distance") > 0) & (F.col("trip_distance") <= 100) &
        F.col("fare_amount").isNotNull() &
        (F.col("fare_amount") >= 0) & (F.col("fare_amount") <= 500) &
        F.col("total_amount").isNotNull() &
        (F.col("total_amount") >= 0) & (F.col("total_amount") <= 500) &
        (F.col("tip_amount").isNull() | ((F.col("tip_amount") >= 0) & (F.col("tip_amount") <= 200))) &
        (F.col("tolls_amount").isNull() | ((F.col("tolls_amount") >= 0) & (F.col("tolls_amount") <= 200))) &
        (F.col("extra").isNull() | ((F.col("extra") >= 0) & (F.col("extra") <= 50))) &
        (F.col("mta_tax").isNull() | ((F.col("mta_tax") >= 0) & (F.col("mta_tax") <= 10))) &
        (F.col("improvement_surcharge").isNull() | ((F.col("improvement_surcharge") >= 0) & (F.col("improvement_surcharge") <= 10))) &
        (F.col("congestion_surcharge").isNull() | ((F.col("congestion_surcharge") >= 0) & (F.col("congestion_surcharge") <= 20))) &
        (F.col("airport_fee").isNull() | ((F.col("airport_fee") >= 0) & (F.col("airport_fee") <= 20))) &
        (F.col("cbd_congestion_fee").isNull() | ((F.col("cbd_congestion_fee") >= 0) & (F.col("cbd_congestion_fee") <= 20)))
    )

    # Duplicates are exact repeated records only (handled by dropDuplicates() above). An
    # earlier version converted the timestamps to dates first and then dropped duplicates on
    # (pickup date, dropoff date, distance, fare, tip, total) across every zone. That treated
    # different trips on the same day with the same fare as one trip and silently removed
    # about 45% of Central Park pickups and 9% of JFK pickups in 2023 to 2025.
    df = df.filter(condition)

    # Convert timestamps to dates only after cleaning and de-duplication
    df = (
        df.withColumn("tpep_pickup_datetime", F.to_date("tpep_pickup_datetime"))
          .withColumn("tpep_dropoff_datetime", F.to_date("tpep_dropoff_datetime"))
    )
    return df


def find_data_gap_dates(raw: DataFrame) -> DataFrame:
    """Dates where the raw feed (all zones, before cleaning) is missing whole daytime hours."""
    hourly = (
        raw.withColumn("date", F.to_date("tpep_pickup_datetime"))
           .withColumn("hour", F.hour("tpep_pickup_datetime"))
           .groupBy("date", "hour")
           .count()
    )
    all_dates = hourly.select("date").distinct()
    full_hours = (
        hourly.filter(F.col("hour").isin(GAP_HOURS) & (F.col("count") >= GAP_MIN_RECORDS))
              .groupBy("date")
              .agg(F.countDistinct("hour").alias("full_hours"))
    )
    return (
        all_dates.join(full_hours, on="date", how="left")
                 .filter(F.coalesce(F.col("full_hours"), F.lit(0)) < len(GAP_HOURS))
                 .select("date")
    )


def build_taxi_dataset(spark: SparkSession, taxi_input: str) -> DataFrame:
    files = get_taxi_files(spark, taxi_input)

    if not files:
        raise FileNotFoundError(f"No parquet files found in taxi input path: {taxi_input}")

    dfs, raws = [], []
    for f in files:
        df = standardize_taxi_schema(spark.read.parquet(f))
        raws.append(df.select("tpep_pickup_datetime"))
        dfs.append(clean_taxi_df(df))

    taxi = reduce(lambda a, b: a.unionByName(b, allowMissingColumns=True), dfs)

    gap_dates = find_data_gap_dates(reduce(lambda a, b: a.unionByName(b), raws))
    taxi = taxi.join(
        F.broadcast(gap_dates.withColumnRenamed("date", "tpep_pickup_datetime")),
        on="tpep_pickup_datetime",
        how="left_anti",
    )

    # In notebook you further narrowed to JFK (132) and Central Park (43)
    taxi = taxi.filter(F.col("pulocationid").isin(132, 43)).dropDuplicates()

    taxi = taxi.withColumn(
        "location",
        F.when(F.col("pulocationid") == 132, "JFK")
         .when(F.col("pulocationid") == 43, "Central Park")
         .otherwise(None)
    )

    taxi = taxi.withColumnRenamed("tpep_pickup_datetime", "date")

    return taxi


# ----------------------------
# Weather transformation
# ----------------------------
def build_weather_daily(spark: SparkSession, weather_input: str) -> DataFrame:
    weather = (
        spark.read
        .option("header", True)
        .option("inferSchema", True)
        .csv(weather_input)
    )

    cols = {c.upper(): c for c in weather.columns}

    # Keep a single station. A NOAA export usually holds many stations per date, and taking
    # the max of PRCP across all of them labels a day "heavy rain" if any one gauge saw it.
    if "STATION" in cols:
        weather = weather.filter(F.col(cols["STATION"]) == WEATHER_STATION)

    # Central Park publishes TMAX and TMIN but not TAVG for these years, so fall back to the
    # (TMAX + TMIN) / 2 convention when TAVG is missing.
    tavg_parts = []
    if "TAVG" in cols:
        tavg_parts.append(F.col(cols["TAVG"]).cast("double"))
    if "TMAX" in cols and "TMIN" in cols:
        tavg_parts.append((F.col(cols["TMAX"]).cast("double") + F.col(cols["TMIN"]).cast("double")) / 2)
    if not tavg_parts:
        raise ValueError("Weather file needs TAVG or both TMAX and TMIN")

    weather_small = weather.select(
        F.to_date(F.col(cols["DATE"])).alias("date"),
        F.col(cols["PRCP"]).cast("double").alias("PRCP"),
        F.col(cols["SNOW"]).cast("double").alias("SNOW"),
        F.coalesce(*tavg_parts).alias("TAVG"),
    )

    # One station gives one row per date already; the groupBy guards against repeats.
    weather_daily = (
        weather_small
        .groupBy("date")
        .agg(
            F.first("PRCP", ignorenulls=True).alias("PRCP"),
            F.first("SNOW", ignorenulls=True).alias("SNOW"),
            F.first("TAVG", ignorenulls=True).alias("TAVG")
        )
    )

    weather_daily = (
        weather_daily
        .withColumn(
            "rain_level",
            F.when(F.col("PRCP") == 0, "no rain")
             .when(F.col("PRCP") <= 0.1, "light rain")
             .when(F.col("PRCP") <= 0.3, "moderate rain")
             .otherwise("heavy rain")
        )
        .withColumn(
            "snow_level",
            F.when(F.col("SNOW") == 0, "no snow")
             .when(F.col("SNOW") <= 1, "light snow")
             .when(F.col("SNOW") <= 3, "moderate snow")
             .otherwise("heavy snow")
        )
        .withColumn(
            "temp_level",
            F.when(F.col("TAVG") < 32, "freezing")
             .when(F.col("TAVG") < 50, "cold")
             .when(F.col("TAVG") < 70, "mild")
             .otherwise("warm")
        )
        .select("date", "rain_level", "snow_level", "temp_level")
    )

    return weather_daily


# ----------------------------
# Fact + dimensions
# ----------------------------
def build_fact_and_dims(taxi: DataFrame, weather_daily: DataFrame):
    joined = taxi.join(weather_daily, on="date", how="left")

    joined = joined.withColumn(
        "tip_pct",
        F.when(F.col("fare_amount") > 0, F.col("tip_amount") / F.col("fare_amount"))
    )

    # Tips are only recorded for card payments (cash tips show as 0), so the comparable tip
    # rate is sum(card tips) / sum(card fares), matching analysis/weather_effect.py.
    is_card = F.col("payment_type") == 1

    fact_taxi_demand = (
        joined.groupBy("date", "location", "rain_level", "snow_level", "temp_level")
        .agg(
            F.count("*").alias("trip_count"),
            F.avg("fare_amount").alias("avg_fare"),
            F.avg("tip_amount").alias("avg_tip"),
            F.avg("tip_pct").alias("avg_tip_pct"),
            F.avg("total_amount").alias("avg_total"),
            F.avg("trip_distance").alias("avg_distance"),
            (
                F.sum(F.when(is_card, F.col("tip_amount")))
                / F.sum(F.when(is_card, F.col("fare_amount")))
            ).alias("card_tip_pct")
        )
    )

    dim_time = (
        fact_taxi_demand
        .select("date")
        .distinct()
        .withColumn("year", F.year("date"))
        .withColumn("quarter", F.quarter("date"))
        .withColumn("month", F.month("date"))
        .withColumn("day", F.dayofmonth("date"))
        .withColumn("day_of_week", F.dayofweek("date"))
    )

    dim_weather = (
        fact_taxi_demand
        .select("rain_level", "snow_level", "temp_level")
        .distinct()
        .withColumn("weather_id", F.monotonically_increasing_id())
    )

    dim_location = (
        joined
        .select(
            F.col("pulocationid").alias("location_id"),
            F.col("location").alias("location_name")
        )
        .distinct()
    )

    fact_taxi_demand_final = (
        fact_taxi_demand
        .join(dim_weather, on=["rain_level", "snow_level", "temp_level"], how="left")
        .join(dim_location, fact_taxi_demand["location"] == dim_location["location_name"], how="left")
        .select(
            fact_taxi_demand["date"],
            dim_location["location_id"],
            dim_weather["weather_id"],
            fact_taxi_demand["trip_count"],
            fact_taxi_demand["avg_fare"],
            fact_taxi_demand["avg_tip"],
            fact_taxi_demand["avg_tip_pct"],
            fact_taxi_demand["avg_total"],
            fact_taxi_demand["avg_distance"],
            fact_taxi_demand["card_tip_pct"]
        )
    )

    return fact_taxi_demand_final, dim_time, dim_weather, dim_location


# ----------------------------
# Load outputs
# ----------------------------
def write_outputs(
    fact_taxi_demand_final: DataFrame,
    dim_time: DataFrame,
    dim_weather: DataFrame,
    dim_location: DataFrame
) -> None:
    (
        fact_taxi_demand_final.write.mode("overwrite")
        .option("header", True)
        .csv(FACT_OUTPUT)
    )

    (
        dim_time.write.mode("overwrite")
        .option("header", True)
        .csv(DIM_TIME_OUTPUT)
    )

    (
        dim_weather.write.mode("overwrite")
        .option("header", True)
        .csv(DIM_WEATHER_OUTPUT)
    )

    (
        dim_location.write.mode("overwrite")
        .option("header", True)
        .csv(DIM_LOCATION_OUTPUT)
    )


# ----------------------------
# Main
# ----------------------------
def main():
    spark = create_spark_session()

    taxi = build_taxi_dataset(spark, TAXI_INPUT)
    weather_daily = build_weather_daily(spark, WEATHER_INPUT)

    fact_taxi_demand_final, dim_time, dim_weather, dim_location = build_fact_and_dims(
        taxi, weather_daily
    )

    write_outputs(fact_taxi_demand_final, dim_time, dim_weather, dim_location)

    print("Pipeline completed successfully.")
    print("fact_taxi_demand_final schema:")
    fact_taxi_demand_final.printSchema()

    spark.stop()


if __name__ == "__main__":
    main()
