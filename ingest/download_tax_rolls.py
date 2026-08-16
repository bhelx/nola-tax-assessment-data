"""
Downloads the City of New Orleans' official tax roll PDFs from nola.gov/tax-rolls/.

This is a plain, freely-published government page (not the Beacon/Schneider
Corp system) - no robots.txt restriction, no bot-blocking observed. We still
throttle requests to be a polite bulk consumer.
"""
import logging
import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TAX_ROLLS_PAGE = "https://nola.gov/tax-rolls/"
USER_AGENT = "nola-tax-assessor-scraper/0.1 (civic-data project; contact via github)"
REQUEST_DELAY_SECONDS = 1.5

PDF_LINK_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)
YEAR_RE = re.compile(r"(20\d{2})")

# The tax-rolls page also links a few non-real-estate-roll PDFs that don't
# match our parser's layout: "City-Wide-PP-*" is the Personal Property
# roll (business equipment, different columns entirely - would burn a lot
# of time generating parse warnings for zero usable records), and the
# "CNO_*"/scanned files are publication-proof images with no extractable
# text. Only keep files that look like the real estate assessment roll.
REAL_ESTATE_ROLL_RE = re.compile(r"taxroll", re.IGNORECASE)


def discover_pdf_links(session: requests.Session) -> list[str]:
    resp = session.get(TAX_ROLLS_PAGE, timeout=30)
    resp.raise_for_status()
    hrefs = PDF_LINK_RE.findall(resp.text)
    all_urls = sorted({urljoin(TAX_ROLLS_PAGE, unquote(h)) for h in hrefs})
    urls = [u for u in all_urls if REAL_ESTATE_ROLL_RE.search(u)]
    skipped = len(all_urls) - len(urls)
    log.info("discovered %d real estate tax roll PDF links (skipped %d non-roll PDFs)", len(urls), skipped)
    return urls


def year_for_url(url: str) -> str | None:
    m = YEAR_RE.search(url)
    return m.group(1) if m else None


def download_all(dest_dir: Path, years: set[str] | None = None) -> list[Path]:
    """Downloads every tax roll PDF (optionally filtered to `years`) into
    dest_dir/<year>/<filename>.pdf, skipping files already on disk."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    urls = discover_pdf_links(session)
    saved: list[Path] = []
    for i, url in enumerate(urls):
        year = year_for_url(url)
        if year is None:
            log.warning("skipping url with no discernible year: %s", url)
            continue
        if years is not None and year not in years:
            continue

        year_dir = dest_dir / year
        year_dir.mkdir(parents=True, exist_ok=True)
        filename = unquote(url.rsplit("/", 1)[-1])
        out_path = year_dir / filename

        if out_path.exists() and out_path.stat().st_size > 0:
            log.info("[%d/%d] already have %s", i + 1, len(urls), out_path.name)
            saved.append(out_path)
            continue

        log.info("[%d/%d] downloading %s -> %s", i + 1, len(urls), url, out_path)
        resp = session.get(url, timeout=120)
        resp.raise_for_status()
        tmp_path = out_path.with_suffix(".pdf.part")
        tmp_path.write_bytes(resp.content)
        tmp_path.rename(out_path)
        saved.append(out_path)
        time.sleep(REQUEST_DELAY_SECONDS)

    return saved


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="data/raw/tax_rolls", help="output directory")
    parser.add_argument("--years", nargs="*", help="only download these years, e.g. --years 2024 2025")
    args = parser.parse_args()

    year_filter = set(args.years) if args.years else None
    files = download_all(Path(args.dest), years=year_filter)
    log.info("done. %d files on disk under %s", len(files), args.dest)
