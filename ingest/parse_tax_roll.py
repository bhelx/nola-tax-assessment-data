"""
Parses Orleans Parish "Real Estate Assessment Roll" PDFs (as published at
nola.gov/tax-rolls/) into structured property records.

These are fixed-width, COBOL-report-style tables with no ruling lines and
wrapped multi-line fields (owner name / mailing address / legal description
all share one text column, distinguished only by content and order). We
work at the *character* level (not pdfplumber's word tokens): the parcel-id
continuation column and the name/address/legal column sit close enough
together (as little as ~0.8pt gap) that pdfplumber's word-join heuristic
sometimes merges text across the column boundary, e.g. "POYDRASST" +
"COMMISSIONERS" becoming one glued word. Building each column's text
independently from characters filtered by a fixed x-range avoids that.

Column x ranges were reverse-engineered from the 2025 1st District roll and
validated against ~10,500 records with a well under 0.1% unparsed rate
(large multi-tract institutional parcels, e.g. the convention center, are
the known failure mode - they are skipped and logged, not fabricated).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pdfplumber

log = logging.getLogger(__name__)

CODE_X = (20, 62)     # mun_dist / asst_dist / ward - informational only, unreliable as a row marker
BOOK_X = (63, 84)
KEY_X = (84, 99)
PARCEL_X = (99, 158)  # parcel-id (first line + continuation lines)
INFO_X = (158, 265)   # shared owner-name / mailing-address / legal-description column
NUMERIC_X0 = 265       # continuation-line boundary; record-start rows use content-based splitting (see _split_name_and_nums)
RECORD_ROW_RIGHT = 800

NUM_TOKEN_RE = re.compile(r"^-?[\d,]+(\.\d{1,2})?$")

HEADER_BOTTOM = 175
FOOTER_TOP = 990
LINE_CLUSTER_TOL = 3.0
WORD_GAP_THRESHOLD = 2.0  # x-gap between chars beyond which we insert a space

PARCEL_START_RE = re.compile(r"^\d+-")
ADDR_START_RE = re.compile(r"^(\d|PO\b|P\.?O\.?\s*BOX|C/O)", re.IGNORECASE)
ZIP_RE = re.compile(r"\b\d{5}(-\d{4})?\b")
NUM_CLEAN_RE = re.compile(r"[^\d.\-]")


@dataclass
class TaxRollRecord:
    tax_year: int
    district_file: str
    source_page: int
    mun_dist: str
    asst_dist: str
    book: str
    key_no: str
    parcel_id: str
    owner_name: str
    mailing_address: str
    legal_description: str
    land_value: int | None
    improvement_value: int | None
    gross_assessment: int | None
    homestead_exemption_amount: int | None
    total_tax: float | None
    homestead_tax_amount: float | None
    net_tax: float | None
    fees: int | None


def _group_lines(chars, tol: float = LINE_CLUSTER_TOL):
    """Clusters characters into visual rows by 'top', tolerant of the
    sub-point jitter pdfplumber reports for glyphs on the same printed line."""
    cs_sorted = sorted(chars, key=lambda c: c["top"])
    lines, cur, cur_tops = [], [], []
    for c in cs_sorted:
        if cur and abs(c["top"] - (sum(cur_tops) / len(cur_tops))) > tol:
            lines.append((sum(cur_tops) / len(cur_tops), sorted(cur, key=lambda x: x["x0"])))
            cur, cur_tops = [], []
        cur.append(c)
        cur_tops.append(c["top"])
    if cur:
        lines.append((sum(cur_tops) / len(cur_tops), sorted(cur, key=lambda x: x["x0"])))
    lines.sort(key=lambda l: l[0])
    return lines


def _zone_text(chars_sorted_by_x0, x_min: float, x_max: float, gap: float = WORD_GAP_THRESHOLD) -> str:
    """Reconstructs text for one column zone from characters, inserting a
    space wherever the horizontal gap between consecutive glyphs exceeds
    `gap` (i.e. an actual inter-word space, not just letter kerning)."""
    zone = [c for c in chars_sorted_by_x0 if x_min <= c["x0"] < x_max]
    if not zone:
        return ""
    out = [zone[0]["text"]]
    prev_x1 = zone[0]["x1"]
    for c in zone[1:]:
        if c["x0"] - prev_x1 > gap and out[-1] != " ":
            out.append(" ")
        out.append(c["text"])
        prev_x1 = c["x1"]
    return "".join(out).strip()


def _zone_tokens(chars_sorted_by_x0, x_min: float, x_max: float, gap: float = WORD_GAP_THRESHOLD) -> list[str]:
    text = _zone_text(chars_sorted_by_x0, x_min, x_max, gap)
    return text.split() if text else []


NUM_FIND_RE = re.compile(r"-?\d[\d,]*(?:\.\d{1,2})?")


def _split_trailing_numbers(line_text: str, want: int = 8) -> tuple[list[str], list[str]]:
    """Splits a record-start row's text into (name_tokens, value_tokens).

    Two wrinkles in the source data rule out simple whitespace tokenizing:
      - owner names sometimes contain pure-digit tokens (e.g. "1542
        CONSTANCE STREET INC" - LLCs are often named after their own
        address), so a token can't be classified as name-vs-number in
        isolation; only the *trailing run* of `want` numbers is reliably
        numeric.
      - a "Special District" annotation is occasionally printed with zero
        gap right after the FEES value (e.g. "876.42Special Dis[trict]"),
        so we extract numbers as regex matches within the text rather than
        requiring a whole whitespace-delimited token to be purely numeric -
        trailing garbage fused onto a number is simply ignored, and a
        record with only 7 recoverable trailing numbers (FEES obscured by
        the annotation) is still accepted with fees left blank.
    """
    matches = list(NUM_FIND_RE.finditer(line_text))
    if len(matches) < want - 1:
        return line_text.split(), [m.group() for m in matches]
    take = matches[-want:]
    # A digit-leading name (e.g. "638 RACE, LLC") can coincidentally bring
    # the total match count up to exactly `want`, fooling a plain
    # last-N slice into treating the name's leading number as a value
    # field. Real value fields have nothing but whitespace between them;
    # if letters appear between two "taken" matches, the leftmost one
    # isn't a value at all - trim it back into the name.
    while len(take) > 1 and re.search(r"[A-Za-z]", line_text[take[0].end() : take[1].start()]):
        take = take[1:]
    name_text = line_text[: take[0].start()]
    return name_text.split(), [m.group() for m in take]


def _is_record_start(cs) -> bool:
    """A real record-start row has book + key_no + a dashed parcel-id token
    together. The leading mun/asst/ward digits alone are NOT reliable -
    they're occasionally repeated on continuation lines too."""
    book = _zone_text(cs, *BOOK_X)
    key_no = _zone_text(cs, *KEY_X)
    parcel = _zone_text(cs, *PARCEL_X)
    return bool(book) and book.isdigit() and bool(key_no) and key_no.isdigit() and bool(PARCEL_START_RE.match(parcel))


def _to_int(s: str) -> int | None:
    s = NUM_CLEAN_RE.sub("", s)
    return int(float(s)) if s not in ("", "-") else None


def _to_float(s: str) -> float | None:
    s = NUM_CLEAN_RE.sub("", s)
    return float(s) if s not in ("", "-") else None


def _finalize(raw: dict, tax_year: int, district_file: str, page_no: int) -> TaxRollRecord | None:
    nums = raw["nums"]
    if not raw["book"] or not raw["key_no"] or not raw["parcel_id"] or len(nums) not in (7, 8):
        log.warning(
            "unparsed record on %s p%d (book=%r key=%r parcel=%r nums=%d) - skipping",
            district_file, page_no, raw["book"], raw["key_no"], raw["parcel_id"], len(nums),
        )
        return None
    if len(nums) == 7:
        # FEES obscured by a "Special District" annotation glued onto it - leave blank
        nums = nums + [""]
    codes = raw["codes"]
    return TaxRollRecord(
        tax_year=tax_year,
        district_file=district_file,
        source_page=page_no,
        mun_dist=codes[0] if len(codes) > 0 else "",
        asst_dist=codes[1] if len(codes) > 1 else "",
        book=raw["book"],
        key_no=raw["key_no"],
        parcel_id=raw["parcel_id"],
        owner_name=" ".join(raw["name_parts"]).strip(),
        mailing_address=" ".join(raw["address_parts"]).strip(),
        legal_description=" ".join(raw["legal_parts"]).strip(),
        land_value=_to_int(nums[0]),
        improvement_value=_to_int(nums[1]),
        gross_assessment=_to_int(nums[2]),
        homestead_exemption_amount=_to_int(nums[3]),
        total_tax=_to_float(nums[4]),
        homestead_tax_amount=_to_float(nums[5]),
        net_tax=_to_float(nums[6]),
        fees=_to_int(nums[7]),
    )


def parse_page(page, tax_year: int, district_file: str, page_no: int) -> list[TaxRollRecord]:
    chars = [c for c in page.chars if HEADER_BOTTOM <= c["top"] <= FOOTER_TOP and c["text"].strip()]
    lines = _group_lines(chars)

    records: list[TaxRollRecord] = []
    cur: dict | None = None
    stage: str | None = None

    def flush():
        if cur is not None:
            rec = _finalize(cur, tax_year, district_file, page_no)
            if rec is not None:
                records.append(rec)

    for _top, cs in lines:
        if _is_record_start(cs):
            flush()
            codes = _zone_text(cs, *CODE_X).split()
            book = _zone_text(cs, *BOOK_X)
            key_no = _zone_text(cs, *KEY_X)
            parcel = _zone_text(cs, *PARCEL_X)
            row_text = _zone_text(cs, INFO_X[0], RECORD_ROW_RIGHT)
            name_toks, num_toks = _split_trailing_numbers(row_text)
            cur = {
                "codes": codes,
                "book": book,
                "key_no": key_no,
                "parcel_id": parcel,
                "name_parts": name_toks,
                "address_parts": [],
                "legal_parts": [],
                "nums": num_toks,
            }
            stage = "name"
            continue

        if cur is None:
            continue

        pcont = _zone_text(cs, *PARCEL_X)
        if pcont:
            cur["parcel_id"] += pcont

        line_text = _zone_text(cs, INFO_X[0], NUMERIC_X0)
        if not line_text:
            continue

        if stage == "name":
            if ADDR_START_RE.match(line_text):
                stage = "address"
                cur["address_parts"].append(line_text)
            else:
                cur["name_parts"].append(line_text)
        elif stage == "address":
            cur["address_parts"].append(line_text)
            if ZIP_RE.search(line_text):
                stage = "legal"
        else:
            cur["legal_parts"].append(line_text)

    flush()
    return records


def has_extractable_text(path: Path, sample_pages: int = 6, min_chars: int = 200) -> bool:
    """Some older tax roll years are scanned images with a corrupted/empty
    text layer (glyphs present in `page.chars` but with blank `text` and
    font 'unknown') - unusable without OCR. A single page can carry a
    handful of stray non-blank characters (scan noise, a stamp) even in an
    otherwise all-image file, so this samples several pages spread across
    the document and requires a real amount of text on at least one -
    cheap relative to committing to a full (very slow, thousands of pages)
    parse attempt that would yield nothing."""
    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        step = max(1, n // sample_pages)
        for i in range(0, n, step):
            nonblank = sum(1 for c in pdf.pages[i].chars if c["text"].strip())
            if nonblank >= min_chars:
                return True
    return False


def parse_pdf(path: Path, tax_year: int) -> Iterator[TaxRollRecord]:
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            yield from parse_page(page, tax_year, path.name, i + 1)


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    for n, rec in enumerate(parse_pdf(args.pdf, args.year)):
        print(rec)
        if n + 1 >= args.limit:
            break
