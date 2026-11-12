import argparse
import json
import logging
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path

import pdfplumber

from utils.kuccps_requirements import normalize_course_title


BACKEND_DIR = Path(__file__).resolve().parents[1]
SOURCE_PDF = BACKEND_DIR / "utils" / "DEGREE_CUTOFFS_14-07-2025.pdf"
CUTOFFS_FILE = BACKEND_DIR / "data" / "course_cutoffs.json"
SOURCE_NAME = "KUCCPS 2025/2026 DEGREE PROGRAMME CUT OFFS"
CUTOFF_YEARS = tuple(range(2018, 2025))


def _normalize_institution_name(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").casefold())


def _parse_cutoff(value):
    value = str(value or "").strip().replace(",", ".")
    if not value or value in {"-", "—"}:
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None
    return float(value)


def extract_cutoff_rows(pdf_path=SOURCE_PDF):
    entries = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables() or []:
                for row in table:
                    if len(row) < 11:
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
                            for index, year in enumerate(CUTOFF_YEARS, start=4)
                        },
                    })
    return entries


def build_cutoff_database(pdf_path=SOURCE_PDF, output_path=CUTOFFS_FILE):
    entries = extract_cutoff_rows(pdf_path)
    if not entries:
        raise ValueError(f"No KUCCPS cutoff rows were extracted from {pdf_path}.")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    database = {
        "schema_version": 1,
        "source": SOURCE_NAME,
        "source_file": Path(pdf_path).name,
        "cutoff_years": list(CUTOFF_YEARS),
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
            "cutoff_years": list(CUTOFF_YEARS),
            "entries": [],
        }
    entries = database.get("entries") if isinstance(database, dict) else None
    if not isinstance(entries, list):
        logging.error("KUCCPS cutoff data at %s has an invalid format.", CUTOFFS_FILE)
        return {
            "source": SOURCE_NAME,
            "source_file": SOURCE_PDF.name,
            "cutoff_years": list(CUTOFF_YEARS),
            "entries": [],
        }
    return database


def find_course_cutoff(course, institution):
    normalized_course = normalize_course_title(course)
    normalized_institution = _normalize_institution_name(institution)
    if not normalized_course or not normalized_institution:
        return None

    database = _load_cutoff_database()
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
    return {
        "status": "available",
        "source": database.get("source", SOURCE_NAME),
        "source_file": database.get("source_file", SOURCE_PDF.name),
        "records": records,
        "notice": (
            "Historical KUCCPS programme cutoffs are reference points, not a "
            "guarantee of admission. Current cutoffs and institution rules may differ."
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
