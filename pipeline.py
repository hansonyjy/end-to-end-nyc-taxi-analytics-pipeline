#!/usr/bin/env python3

import argparse
import os
import shutil
from pathlib import Path

import luigi
from dotenv import load_dotenv

from Luigi.pipeline import FLAG_DIR, FullPipeline, command_override

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

REQUIRED_COMMANDS = ("gcloud", "snowsql")
REQUIRED_ENV_VARS = (
    "GCP_PROJECT",
    "GCP_REGION",
    "GCP_CLUSTER",
    "GCS_BUCKET",
    "RAW_TAXI_PATH",
    "RAW_WEATHER_PATH",
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
    "SNOWFLAKE_ROLE",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the NYC Taxi pipeline using GCP-hosted raw data and Snowflake."
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of Luigi workers to use. Default: 1.",
    )
    parser.add_argument(
        "--keep-flags",
        action="store_true",
        help="Keep existing Luigi marker files instead of clearing them before the run.",
    )
    return parser.parse_args()


def clear_flags() -> None:
    if not FLAG_DIR.exists():
        return

    for marker in FLAG_DIR.glob("*.marker"):
        marker.unlink()


def validate_environment() -> None:
    missing_commands = []
    for command in REQUIRED_COMMANDS:
        override = command_override(command)  # optional GCLOUD_BIN / SNOWSQL_BIN
        if override is not None:
            if not override.exists():
                missing_commands.append(f"{command} (override {override} not found)")
            continue
        if not shutil.which(command):
            missing_commands.append(command)
    missing_env_vars = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]

    errors = []
    if missing_commands:
        errors.append(
            "Missing required commands: " + ", ".join(missing_commands)
        )
    if missing_env_vars:
        errors.append(
            "Missing required environment variables in .env: "
            + ", ".join(missing_env_vars)
        )

    if errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise SystemExit(f"Pipeline preflight failed:\n{joined}")


def main() -> int:
    args = parse_args()
    validate_environment()

    if not args.keep_flags:
        clear_flags()

    success = luigi.build(
        [FullPipeline()],
        local_scheduler=True,
        workers=args.workers,
        detailed_summary=True,
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
