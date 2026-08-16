"""
SQLite + SpatiaLite database layer.

Three tables:
  - properties:    one row per (tax_year, parcel_id) tax roll entry
                    (owner, values, tax amounts - from nola.gov PDFs)
  - parcels:        one row per parcel_id (polygon geometry + centroid lat/lng
                    - from the City's open parcel dataset)
  - neighborhoods:  neighborhood polygons (from the City's open dataset)

`parcels.neighborhood` is filled in by a spatial join (point-in-polygon on
the parcel centroid) after both parcels and neighborhoods are loaded.
"""
from __future__ import annotations

import glob
import sqlite3
from pathlib import Path

SPATIALITE_CANDIDATES = [
    "/opt/homebrew/lib/mod_spatialite.dylib",
    "/opt/homebrew/lib/mod_spatialite.so",
    "/usr/local/lib/mod_spatialite.dylib",
    "mod_spatialite",  # let sqlite search the loader path (Linux: mod_spatialite.so)
]


def _find_spatialite() -> str:
    for candidate in SPATIALITE_CANDIDATES:
        if candidate.startswith("/"):
            if Path(candidate).exists():
                return candidate
            continue
        matches = glob.glob(candidate)
        if matches:
            return matches[0]
    # last resort: hand back the bare module name and let sqlite3 try to resolve it
    return "mod_spatialite"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    conn.load_extension(_find_spatialite())
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT count(*) FROM sqlite_master WHERE name='spatial_ref_sys'")
    if cur.fetchone()[0] == 0:
        conn.execute("SELECT InitSpatialMetaData(1)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tax_year INTEGER NOT NULL,
            district_file TEXT NOT NULL,
            source_page INTEGER NOT NULL,
            mun_dist TEXT,
            asst_dist TEXT,
            book TEXT,
            key_no TEXT,
            parcel_id TEXT NOT NULL,
            owner_name TEXT,
            mailing_address TEXT,
            legal_description TEXT,
            land_value INTEGER,
            improvement_value INTEGER,
            gross_assessment INTEGER,
            homestead_exemption_amount INTEGER,
            total_tax REAL,
            homestead_tax_amount REAL,
            net_tax REAL,
            fees INTEGER,
            UNIQUE(tax_year, district_file, source_page, book, key_no)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_properties_parcel ON properties(parcel_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_properties_year ON properties(tax_year)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS parcels (
            parcel_id TEXT PRIMARY KEY,
            geopin TEXT,
            lat REAL,
            lng REAL,
            neighborhood TEXT,
            zone_class TEXT,
            zone_desc TEXT
        )
        """
    )
    cur = conn.execute("PRAGMA table_info(parcels)")
    cols = {row["name"] for row in cur.fetchall()}
    for col in ("neighborhood", "zone_class", "zone_desc"):
        if col not in cols:
            conn.execute(f"ALTER TABLE parcels ADD COLUMN {col} TEXT")

    cur = conn.execute("PRAGMA table_info(parcels)")
    cols = {row["name"] for row in cur.fetchall()}
    if "geom" not in cols:
        conn.execute("SELECT AddGeometryColumn('parcels', 'geom', 4326, 'MULTIPOLYGON', 'XY')")
    if "centroid" not in cols:
        conn.execute("SELECT AddGeometryColumn('parcels', 'centroid', 4326, 'POINT', 'XY')")
    conn.execute("SELECT CreateSpatialIndex('parcels', 'geom')")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS neighborhoods (
            id INTEGER PRIMARY KEY,
            neigh_id TEXT,
            name TEXT
        )
        """
    )
    cur = conn.execute("PRAGMA table_info(neighborhoods)")
    cols = {row["name"] for row in cur.fetchall()}
    if "geom" not in cols:
        conn.execute("SELECT AddGeometryColumn('neighborhoods', 'geom', 4326, 'MULTIPOLYGON', 'XY')")
    conn.execute("SELECT CreateSpatialIndex('neighborhoods', 'geom')")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS zoning_districts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            zone_class TEXT,
            zone_desc TEXT
        )
        """
    )
    cur = conn.execute("PRAGMA table_info(zoning_districts)")
    cols = {row["name"] for row in cur.fetchall()}
    if "geom" not in cols:
        conn.execute("SELECT AddGeometryColumn('zoning_districts', 'geom', 4326, 'MULTIPOLYGON', 'XY')")
    conn.execute("SELECT CreateSpatialIndex('zoning_districts', 'geom')")

    conn.commit()


def insert_properties(conn: sqlite3.Connection, records) -> int:
    rows = [
        (
            r.tax_year, r.district_file, r.source_page, r.mun_dist, r.asst_dist,
            r.book, r.key_no, r.parcel_id, r.owner_name, r.mailing_address,
            r.legal_description, r.land_value, r.improvement_value, r.gross_assessment,
            r.homestead_exemption_amount, r.total_tax, r.homestead_tax_amount,
            r.net_tax, r.fees,
        )
        for r in records
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT OR IGNORE INTO properties (
            tax_year, district_file, source_page, mun_dist, asst_dist,
            book, key_no, parcel_id, owner_name, mailing_address,
            legal_description, land_value, improvement_value, gross_assessment,
            homestead_exemption_amount, total_tax, homestead_tax_amount,
            net_tax, fees
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_parcel_geometry(conn: sqlite3.Connection, parcel_id: str, geopin: str, geojson: str) -> None:
    conn.execute(
        """
        INSERT INTO parcels (parcel_id, geopin, geom)
        VALUES (?, ?, CastToMulti(SetSRID(GeomFromGeoJSON(?), 4326)))
        ON CONFLICT(parcel_id) DO UPDATE SET geopin=excluded.geopin, geom=excluded.geom
        """,
        (parcel_id, geopin, geojson),
    )


def compute_centroids(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE parcels
        SET centroid = ST_Centroid(geom),
            lat = ST_Y(ST_Centroid(geom)),
            lng = ST_X(ST_Centroid(geom))
        WHERE geom IS NOT NULL
        """
    )
    conn.commit()


def upsert_neighborhood(conn: sqlite3.Connection, neigh_id: str, name: str, geojson: str) -> None:
    conn.execute(
        "INSERT INTO neighborhoods (neigh_id, name, geom) VALUES (?, ?, CastToMulti(SetSRID(GeomFromGeoJSON(?), 4326)))",
        (neigh_id, name, geojson),
    )


def assign_neighborhoods(conn: sqlite3.Connection) -> int:
    """Point-in-polygon spatial join: for each parcel centroid, find the
    containing neighborhood polygon using the RTree spatial index."""
    cur = conn.execute(
        """
        UPDATE parcels
        SET neighborhood = (
            SELECT n.name FROM neighborhoods n
            WHERE n.ROWID IN (
                SELECT ROWID FROM SpatialIndex
                WHERE f_table_name = 'neighborhoods' AND search_frame = parcels.centroid
            )
            AND ST_Contains(n.geom, parcels.centroid)
            LIMIT 1
        )
        WHERE centroid IS NOT NULL
        """
    )
    conn.commit()
    return cur.rowcount


def upsert_zoning(conn: sqlite3.Connection, zone_class: str, zone_desc: str, geojson: str) -> None:
    conn.execute(
        "INSERT INTO zoning_districts (zone_class, zone_desc, geom) VALUES (?, ?, CastToMulti(SetSRID(GeomFromGeoJSON(?), 4326)))",
        (zone_class, zone_desc, geojson),
    )


def assign_zoning(conn: sqlite3.Connection) -> int:
    """Point-in-polygon spatial join: for each parcel centroid, find the
    containing zoning district polygon using the RTree spatial index."""
    cur = conn.execute(
        """
        UPDATE parcels
        SET zone_class = (
            SELECT z.zone_class FROM zoning_districts z
            WHERE z.ROWID IN (
                SELECT ROWID FROM SpatialIndex
                WHERE f_table_name = 'zoning_districts' AND search_frame = parcels.centroid
            )
            AND ST_Contains(z.geom, parcels.centroid)
            LIMIT 1
        ),
        zone_desc = (
            SELECT z.zone_desc FROM zoning_districts z
            WHERE z.ROWID IN (
                SELECT ROWID FROM SpatialIndex
                WHERE f_table_name = 'zoning_districts' AND search_frame = parcels.centroid
            )
            AND ST_Contains(z.geom, parcels.centroid)
            LIMIT 1
        )
        WHERE centroid IS NOT NULL
        """
    )
    conn.commit()
    return cur.rowcount
