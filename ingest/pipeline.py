"""
Orchestrates the full ingestion pipeline:

  1. download every tax roll PDF from nola.gov/tax-rolls/ (owner, values, tax)
  2. parse them into the `properties` table
  3. pull parcel polygons from the City's open parcel dataset -> `parcels`
     table, compute centroid lat/lng
  4. pull neighborhood polygons from the City's open dataset -> `neighborhoods`
  5. spatial join: assign each parcel's neighborhood via point-in-polygon

Run with: python -m ingest.pipeline --db data/db/nola_tax.sqlite
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ingest import db, open_data
from ingest.download_tax_rolls import download_all
from ingest.parse_tax_roll import has_extractable_text, parse_pdf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def load_tax_rolls(conn, raw_dir: Path, years: set[str] | None) -> None:
    pdf_paths = sorted(raw_dir.glob("*/*.pdf")) + sorted(raw_dir.glob("*/*.PDF"))
    pdf_paths = [p for p in pdf_paths if "taxroll" in p.name.lower()]
    if years is not None:
        pdf_paths = [p for p in pdf_paths if p.parent.name in years]
    log.info("parsing %d tax roll PDFs", len(pdf_paths))
    for i, path in enumerate(pdf_paths):
        year = int(path.parent.name)
        if not has_extractable_text(path):
            log.warning(
                "[%d/%d] %s has no extractable text (scanned image, pre-digital roll) - skipping",
                i + 1, len(pdf_paths), path,
            )
            continue
        log.info("[%d/%d] parsing %s", i + 1, len(pdf_paths), path)
        records = list(parse_pdf(path, year))
        n = db.insert_properties(conn, records)
        log.info("  -> %d records", n)


def load_parcels(conn) -> None:
    log.info("fetching parcel geometry from data.nola.gov ...")
    n = open_data.fetch_parcels(lambda pid, geopin, geojson: db.upsert_parcel_geometry(conn, pid, geopin, geojson))
    conn.commit()
    log.info("loaded %d parcels, computing centroids ...", n)
    db.compute_centroids(conn)


def load_neighborhoods(conn) -> None:
    log.info("fetching neighborhood boundaries from data.nola.gov ...")
    n = open_data.fetch_neighborhoods(lambda nid, name, geojson: db.upsert_neighborhood(conn, nid, name, geojson))
    conn.commit()
    log.info("loaded %d neighborhoods, running spatial join ...", n)
    updated = db.assign_neighborhoods(conn)
    log.info("assigned neighborhoods to %d parcels", updated)


def load_zoning(conn) -> None:
    log.info("fetching zoning districts from data.nola.gov ...")
    n = open_data.fetch_zoning(lambda zc, zd, geojson: db.upsert_zoning(conn, zc, zd, geojson))
    conn.commit()
    log.info("loaded %d zoning districts, running spatial join ...", n)
    updated = db.assign_zoning(conn)
    log.info("assigned zoning to %d parcels", updated)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/db/nola_tax.sqlite")
    parser.add_argument("--raw-dir", default="data/raw/tax_rolls")
    parser.add_argument("--years", nargs="*", help="restrict to these tax-roll years")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--skip-tax-rolls", action="store_true")
    parser.add_argument("--skip-parcels", action="store_true")
    parser.add_argument("--skip-neighborhoods", action="store_true")
    parser.add_argument("--skip-zoning", action="store_true")
    args = parser.parse_args()

    years = set(args.years) if args.years else None
    raw_dir = Path(args.raw_dir)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    if not args.skip_download:
        download_all(raw_dir, years=years)

    conn = db.connect(args.db)
    db.init_schema(conn)

    if not args.skip_tax_rolls:
        load_tax_rolls(conn, raw_dir, years)
    if not args.skip_parcels:
        load_parcels(conn)
    if not args.skip_neighborhoods:
        load_neighborhoods(conn)
    if not args.skip_zoning:
        load_zoning(conn)

    conn.close()
    log.info("pipeline complete: %s", args.db)


if __name__ == "__main__":
    main()
