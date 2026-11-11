#!/usr/bin/env python3
"""Load significant USGS earthquakes into the local PostGIS database."""

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg
from dotenv import dotenv_values
from psycopg.conninfo import make_conninfo


def database_conninfo(config: dict[str, str]) -> str:
    """Build the PostgreSQL connection string from PG* settings."""
    return make_conninfo(
        host=config["PGHOST"],
        port=config["PGPORT"],
        dbname=config["PGDATABASE"],
        user=config["PGUSER"],
        password=config["PGPASSWORD"],
    )


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_page(params: dict[str, str], conf: dict[str, str]) -> dict[str, Any]:
    """Fetch one GeoJSON page, retrying temporary HTTP and network failures."""
    url = f"{conf['API_URL']}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "GeoPulse/1.0"})
    max_attempts = int(conf["MAX_REQUEST_ATTEMPTS"])

    for attempt in range(max_attempts):
        try:
            with urlopen(request, timeout=float(conf["REQUEST_TIMEOUT_SECONDS"])) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(
                    f"USGS request failed with HTTP {error.code}: {error.reason}"
                ) from error
            if attempt == max_attempts - 1:
                raise RuntimeError(
                    f"USGS request failed with HTTP {error.code} after "
                    f"{max_attempts} attempts"
                ) from error
        except (URLError, TimeoutError) as error:
            if attempt == max_attempts - 1:
                raise RuntimeError(
                    f"USGS request failed after {max_attempts} attempts: "
                    f"{error}"
                ) from error
        time.sleep(min(2**attempt, 8))

    raise RuntimeError("USGS request failed without a response")


def ensure_schema(connection: psycopg.Connection[Any]) -> None:
    """Create the PostGIS table and indexes if they are not already present."""
    with connection.cursor() as cursor:
        # Enable spatial types/functions required for the earthquake point column.
        cursor.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        cursor.execute("""
            -- Keep searchable event fields in columns and the full USGS payload in JSONB.
            -- The geometry uses WGS 84 (longitude/latitude), matching the USGS GeoJSON.
            CREATE TABLE IF NOT EXISTS earthquakes (
                event_id text PRIMARY KEY,
                event_time timestamptz NOT NULL,
                magnitude double precision,
                significance integer,
                place text,
                depth_km double precision,
                event_url text,
                event_updated_at timestamptz,
                properties jsonb NOT NULL,
                geom geometry(Point, 4326) NOT NULL,
                ingested_at timestamptz NOT NULL DEFAULT now()
            )
            """)
        # Older versions of the table may have required depth; USGS depth can be null.
        cursor.execute("ALTER TABLE earthquakes ALTER COLUMN depth_km DROP NOT NULL")
        # Support time-range queries and spatial searches efficiently.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS earthquakes_event_time_idx "
            "ON earthquakes (event_time)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS earthquakes_geom_gix "
            "ON earthquakes USING GIST (geom)"
        )
    connection.commit()


def optional_number(value: Any, field: str) -> float | None:
    """Convert an optional numeric API value, rejecting invalid or infinite data."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"USGS event has an invalid {field}: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"USGS event has a non-finite {field}: {value!r}")
    return number


def event_to_row(feature: dict[str, Any]) -> tuple[Any, ...]:
    """Validate a USGS GeoJSON feature and map it to the database column order."""
    event_id = feature.get("id")
    properties = feature.get("properties")
    geometry = feature.get("geometry")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("USGS event is missing its ID")
    if not isinstance(properties, dict):
        raise ValueError(f"USGS event {event_id} has invalid properties")
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        raise ValueError(f"USGS event {event_id} has invalid point geometry")

    coordinates = geometry.get("coordinates")
    if (
        not isinstance(coordinates, list)
        or len(coordinates) < 2
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in coordinates[:2]
        )
    ):
        raise ValueError(f"USGS event {event_id} has invalid coordinates")

    longitude, latitude = (float(value) for value in coordinates[:2])
    depth = optional_number(coordinates[2], "depth") if len(coordinates) > 2 else None
    if not all(math.isfinite(value) for value in (longitude, latitude)):
        raise ValueError(f"USGS event {event_id} has non-finite coordinates")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise ValueError(f"USGS event {event_id} has out-of-range coordinates")

    occurred_ms = properties.get("time")
    if isinstance(occurred_ms, bool) or not isinstance(occurred_ms, (int, float)):
        raise ValueError(f"USGS event {event_id} has an invalid origin time")
    occurred_at = datetime.fromtimestamp(occurred_ms / 1000, tz=timezone.utc)

    updated_ms = properties.get("updated")
    updated_at = None
    if updated_ms is not None:
        if isinstance(updated_ms, bool) or not isinstance(updated_ms, (int, float)):
            raise ValueError(f"USGS event {event_id} has an invalid update time")
        updated_at = datetime.fromtimestamp(updated_ms / 1000, tz=timezone.utc)

    significance = properties.get("sig")
    if significance is not None and (
        isinstance(significance, bool) or not isinstance(significance, int)
    ):
        raise ValueError(f"USGS event {event_id} has an invalid significance score")

    return (
        event_id,
        occurred_at,
        optional_number(properties.get("mag"), "magnitude"),
        significance,
        properties.get("place"),
        depth,
        properties.get("url"),
        updated_at,
        json.dumps(properties),
        longitude,
        latitude,
    )


def upsert_events(
    connection: psycopg.Connection[Any], features: list[dict[str, Any]]
) -> int:
    """Insert only unseen USGS event IDs and return the number inserted."""
    if not features:
        return 0

    # Convert and validate the whole page before sending it to the database.
    rows = [event_to_row(feature) for feature in features]
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            -- Build a WGS 84 point from each feature's longitude and latitude.
            -- The event ID primary key makes reruns idempotent: existing rows
            -- are left unchanged rather than duplicated or updated.
            INSERT INTO earthquakes (
                event_id, event_time, magnitude, significance, place, depth_km,
                event_url, event_updated_at, properties, geom
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)
            )
            ON CONFLICT (event_id) DO NOTHING
            """,
            rows,
        )
        inserted = cursor.rowcount
    connection.commit()
    return inserted


def main() -> None:
    # Resolve .env next to this script so running it from another directory works.
    config = dotenv_values(".env")
    min_significance = int(config["USGS_MIN_SIG"])
    if not 0 <= min_significance <= 1000:
        raise ValueError("USGS_MIN_SIG must be between 0 and 1000")

    start = parse_utc(config["USGS_STARTTIME"])
    end = datetime.now(timezone.utc)
    if start > end:
        raise ValueError("USGS_STARTTIME must not be in the future")

    page_size = int(config["PAGE_SIZE"])
    if page_size <= 0:
        raise ValueError("PAGE_SIZE must be greater than zero")

    total = 0  # Number of new rows inserted across all pages.
    offset = 1  # USGS API offsets are one-based.

    start_text = start.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_text = end.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    with psycopg.connect(database_conninfo(config)) as connection:
        ensure_schema(connection)
        while True:
            payload = fetch_page(
                {
                    # Request GeoJSON points, significance-filtered and sorted so
                    # successive offset pages traverse the catalog consistently.
                    "format": "geojson",
                    "minsig": str(min_significance),
                    "starttime": start_text,
                    "endtime": end_text,
                    "orderby": "time-asc",
                    "limit": str(page_size),
                    "offset": str(offset),
                },
                config,
            )
            features = payload.get("features")
            if not isinstance(features, list):
                raise RuntimeError("USGS response does not contain an event list")

            total += upsert_events(connection, features)
            inserted = upsert_events(connection, features)
            total += inserted
            print(
                f"Offset {offset}: {len(features)} events received, "
                f"{inserted} new events inserted"
            )
            if len(features) < page_size:
                break
            offset += len(features)

    print(f"Finished: {total} new events inserted into geopulse.public.earthquakes")


if __name__ == "__main__":
    main()
