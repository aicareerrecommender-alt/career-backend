import argparse
import io
import json
import logging
import os
import re
import threading
import tempfile
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import pdfplumber
import requests
import truststore
from pdfminer.pdfparser import PDFSyntaxError

from utils.kuccps_requirements import normalize_course_title


truststore.inject_into_ssl()

BACKEND_DIR = Path(__file__).resolve().parents[1]
SOURCE_PDF = BACKEND_DIR / "utils" / "DEGREE_CUTOFFS_14-07-2025.pdf"
CUTOFFS_FILE = BACKEND_DIR / "data" / "course_cutoffs.json"
SOURCE_NAME = "KUCCPS 2025/2026 DEGREE PROGRAMME CUT OFFS"
ONLINE_SOURCE_URL = "https://statics.kuccps.net/uploads/globalFiles/DEGREE_CUTOFFS_14_07_2025.pdf"
CUTOFF_YEARS = tuple(range(2018, 2025))
ONLINE_REFRESH_TTL_SECONDS = 24 * 60 * 60
MAX_ONLINE_PDF_BYTES = 15 * 1024 * 1024
_online_refresh_lock = threading.Lock()
_online_cutoff_database = None
_online_checked_at = None


def _normalize_institution_name(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


def _parse_cutoff(value):
    value = str(value or "").strip().replace(",", ".")
    if not value or value in {"-", "—"}:
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None
    return float(value)


def _extract_cutoff_document(pdf_path=SOURCE_PDF):
    entries = []
    pdf_source = io.BytesIO(pdf_path) if isinstance(pdf_path, bytes) else pdf_path
    source_name = SOURCE_NAME
    cutoff_years = CUTOFF_YEARS
    with pdfplumber.open(pdf_source) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            if page_number == 1:
                page_text = re.sub(r"\s+", " ", page.extract_text() or "")
                source_match = re.search(
                    r"(20\d{2}\s*/\s*20\d{2}\s+DEGREE\s+PROGRAMME\s+CUT\s+OFFS)",
                    page_text,
                    re.IGNORECASE,
                )
                if source_match:
                    source_name = re.sub(r"\s+", " ", source_match.group(1)).upper()
            for table in page.extract_tables() or []:
                for header in table[:2]:
                    table_years = [
                        int(match.group(1))
                        for cell in header[4:]
                        if (match := re.search(r"\b(20\d{2})\b", str(cell or "")))
                    ]
                    if len(table_years) >= 7:
                        cutoff_years = tuple(table_years)
                        break
                for row in table:
                    if len(row) < 4 + len(cutoff_years):
                        continue
                    program_code = str(row[1] or "").strip()
                    if not re.fullmatch(r"\d{7}", program_code):
                        continue
                    institution = re.sub(r"\s+", " ", str(row[2] or "")).strip()
                    course = re.sub(r"\s+", " ", str(row[3] or "")).strip()
                    if not institution or not course:
                        continue
                    entries.append({
                        "program_code": program_code,
                        "institution": institution,
                        "course": course,
                        "source_page": page_number,
                        "cutoffs": {
                            str(year): _parse_cutoff(row[index])
                            for index, year in enumerate(cutoff_years, start=4)
                        },
                    })
    return entries, source_name, cutoff_years


def extract_cutoff_rows(pdf_path=SOURCE_PDF):
    entries, _, _ = _extract_cutoff_document(pdf_path)
    return entries


def _fetch_online_cutoff_database():
    response = requests.get(
        ONLINE_SOURCE_URL,
        headers={
            "User-Agent": "CareerPath AI cutoff-data updater",
            "Accept": "application/pdf",
        },
        timeout=(5, 25),
    )
    response.raise_for_status()
    if urlparse(response.url).hostname != "statics.kuccps.net":
        raise ValueError("The cutoff PDF redirected away from the official KUCCPS static host.")
    if len(response.content) > MAX_ONLINE_PDF_BYTES:
        raise ValueError("The KUCCPS cutoff PDF exceeds the configured size limit.")
    if not response.content.startswith(b"%PDF-"):
        raise ValueError("The official KUCCPS cutoff source did not return a PDF.")

    entries, source_name, cutoff_years = _extract_cutoff_document(response.content)
    if len(entries) < 1000:
        raise ValueError(
            f"The online KUCCPS cutoff PDF contained only {len(entries)} programme rows."
        )
    if "DEGREE PROGRAMME CUT OFFS" not in source_name:
        raise ValueError("The online KUCCPS document does not identify itself as cutoff data.")
    return {
        "schema_version": 1,
        "source": source_name,
        "source_file": Path(urlparse(response.url).path).name,
        "source_url": response.url,
        "retrieval_status": "online",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "cutoff_years": list(cutoff_years),
        "latest_published_cutoff_year": max(cutoff_years),
        "entries": entries,
    }


def build_cutoff_database(pdf_path=SOURCE_PDF, output_path=CUTOFFS_FILE):
    entries, source_name, cutoff_years = _extract_cutoff_document(pdf_path)
    if not entries:
        raise ValueError(f"No KUCCPS cutoff rows were extracted from {pdf_path}.")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    database = {
        "schema_version": 1,
        "source": source_name,
        "source_file": Path(pdf_path).name,
        "cutoff_years": list(cutoff_years),
        "entries": entries,
    }
    handle, temporary_path = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(database, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        Path(temporary_path).replace(output_path)
    finally:
        if Path(temporary_path).exists():
            Path(temporary_path).unlink()
    _load_cutoff_database.cache_clear()
    return len(entries)


@lru_cache(maxsize=1)
def _load_cutoff_database():
    try:
        with CUTOFFS_FILE.open("r", encoding="utf-8") as file:
            database = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logging.error("Could not load KUCCPS cutoff data from %s: %s", CUTOFFS_FILE, error)
        return {
            "source": SOURCE_NAME,
            "source_file": SOURCE_PDF.name,
            "retrieval_status": "offline_snapshot",
            "cutoff_years": list(CUTOFF_YEARS),
            "entries": [],
        }
    entries = database.get("entries") if isinstance(database, dict) else None
    if not isinstance(entries, list):
        logging.error("KUCCPS cutoff data at %s has an invalid format.", CUTOFFS_FILE)
        return {
            "source": SOURCE_NAME,
            "source_file": SOURCE_PDF.name,
            "retrieval_status": "offline_snapshot",
            "cutoff_years": list(CUTOFF_YEARS),
            "entries": [],
        }
    return database


def _get_latest_cutoff_database():
    global _online_checked_at, _online_cutoff_database
    now = time.monotonic()
    if (
        _online_checked_at is not None
        and now - _online_checked_at < ONLINE_REFRESH_TTL_SECONDS
    ):
        return _online_cutoff_database or _load_cutoff_database()

    with _online_refresh_lock:
        now = time.monotonic()
        if (
            _online_checked_at is None
            or now - _online_checked_at >= ONLINE_REFRESH_TTL_SECONDS
        ):
            try:
                _online_cutoff_database = _fetch_online_cutoff_database()
            except (OSError, PDFSyntaxError, ValueError, requests.RequestException) as error:
                logging.warning(
                    "Could not refresh KUCCPS cutoffs from the official online source; "
                    "using the bundled snapshot when available: %s",
                    error,
                )
                if _online_cutoff_database:
                    _online_cutoff_database["retrieval_status"] = "stale_online_cache"
            _online_checked_at = time.monotonic()

    if _online_cutoff_database:
        return _online_cutoff_database
    fallback = dict(_load_cutoff_database())
    fallback.setdefault("retrieval_status", "offline_snapshot")
    return fallback


def find_course_cutoff(course, institution):
    normalized_course = normalize_course_title(course)
    normalized_institution = _normalize_institution_name(institution)
    if not normalized_course or not normalized_institution:
        return None

    database = _get_latest_cutoff_database()
    matches = [
        entry
        for entry in database["entries"]
        if isinstance(entry, dict)
        and normalize_course_title(entry.get("course")) == normalized_course
        and _normalize_institution_name(entry.get("institution")) == normalized_institution
    ]
    if not matches:
        return None

    years = database.get("cutoff_years", CUTOFF_YEARS)
    records = []
    for entry in matches:
        cutoffs = entry.get("cutoffs", {})
        latest = next(
            (
                {"year": year, "points": cutoffs.get(str(year))}
                for year in reversed(years)
                if cutoffs.get(str(year)) is not None
            ),
            None,
        )
        records.append({
            "program_code": entry.get("program_code", ""),
            "institution": entry.get("institution", ""),
            "course": entry.get("course", ""),
            "source_page": entry.get("source_page"),
            "cutoffs": cutoffs,
            "latest": latest,
        })

    records.sort(
        key=lambda entry: (
            entry["latest"]["year"] if entry["latest"] else 0,
            entry["program_code"],
        ),
        reverse=True,
    )
    cutoff_years = database.get("cutoff_years", CUTOFF_YEARS)
    latest_published_cutoff_year = max(
        (int(year) for year in cutoff_years if str(year).isdigit()),
        default=max(CUTOFF_YEARS),
    )
    return {
        "status": "available",
        "source": database.get("source", SOURCE_NAME),
        "source_file": database.get("source_file", SOURCE_PDF.name),
        "source_url": database.get("source_url"),
        "retrieval_status": database.get("retrieval_status", "offline_snapshot"),
        "retrieved_at": database.get("retrieved_at"),
        "latest_published_cutoff_year": latest_published_cutoff_year,
        "records": records,
        "notice": (
            f"The published {database.get('source', SOURCE_NAME)} cutoff data extends "
            f"through {latest_published_cutoff_year}; this is not a current-cycle cutoff. "
            "Historical values are reference points, not a guarantee of admission."
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Extract historical programme cutoffs from the KUCCPS PDF."
    )
    parser.add_argument("--pdf", type=Path, default=SOURCE_PDF)
    parser.add_argument("--output", type=Path, default=CUTOFFS_FILE)
    args = parser.parse_args()
    try:
        count = build_cutoff_database(args.pdf, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Could not extract KUCCPS cutoffs: {error}\n")
    print(f"Saved {count} programme rows to {args.output}")


if __name__ == "__main__":
    main()
