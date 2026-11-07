import argparse
import json
import logging
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path

import pdfplumber


BACKEND_DIR = Path(__file__).resolve().parents[1]
SOURCE_PDF = BACKEND_DIR / "utils" / "DEGREE_PROGRAMMES_2025 (1).pdf"
REQUIREMENTS_FILE = BACKEND_DIR / "data" / "course_requirements.json"
SOURCE_NAME = "KUCCPS 2025/2026 DEGREE PROGRAMMES"
SUBJECT_CELL_PATTERN = re.compile(
    r"(?P<subjects>[A-Z][A-Z ]{0,12}\(\s*[\d\s]{2,6}\s*\)"
    r"(?:\s*/\s*[A-Z][A-Z ]{0,12}\(\s*[\d\s]{2,6}\s*\))*)"
    r"\s*:\s*(?P<grade>[A-E][+-]?)\s*"
)
SUBJECT_TOKEN_PATTERN = re.compile(
    r"(?P<name>[A-Z][A-Z ]{0,12})\(\s*(?P<code>[\d\s]{2,6})\s*\)"
)
GRADE_POINTS = {
    "A": 12, "A-": 11, "B+": 10, "B": 9, "B-": 8, "C+": 7,
    "C": 6, "C-": 5, "D+": 4, "D": 3, "D-": 2, "E": 1,
}
SUBJECT_LABELS = {
    "101": "English",
    "102": "Kiswahili",
    "121": "Mathematics",
    "122": "Mathematics",
    "231": "Biology",
    "232": "Physics",
    "233": "Chemistry",
    "234": "Physical Science",
    "235": "Biological Science",
    "237": "General Science",
    "311": "History and Government",
    "312": "Geography",
    "313": "Christian Religious Education",
    "314": "Islamic Religious Education",
    "315": "Hindu Religious Education",
    "316": "Social Education and Ethics",
    "441": "Home Science",
    "442": "Art and Design",
    "443": "Agriculture",
    "451": "Computer Studies",
    "501": "French",
    "502": "German",
    "503": "Arabic",
    "504": "Kenya Sign Language",
    "511": "Music",
    "561": "Accounting",
    "562": "Commerce",
    "565": "Business Studies",
}
SUBJECT_ALIASES = {
    "english": {"english", "eng"},
    "kiswahili": {"kiswahili", "kis", "swahili"},
    "mathematics": {
        "mathematics", "math", "maths", "mat a", "mat b",
        "mathematics a", "mathematics b", "math a", "math b", "121", "122",
    },
    "biology": {"biology", "bio", "biological science", "biological sciences"},
    "physics": {"physics", "phy"},
    "chemistry": {"chemistry", "chem"},
    "physical science": {"physical science", "psc"},
    "biological science": {"biological science", "biological sciences", "biology", "bio", "bsc"},
    "general science": {"general science", "gsc"},
    "history and government": {"history", "history and government", "hag"},
    "geography": {"geography", "geo"},
    "christian religious education": {"cre", "christian religious education"},
    "islamic religious education": {"ire", "islamic religious education"},
    "hindu religious education": {"hre", "hindu religious education"},
    "social education and ethics": {"see", "social education and ethics"},
    "home science": {"home science", "hsc"},
    "art and design": {"art", "art and design", "ard"},
    "agriculture": {"agriculture", "agr"},
    "computer studies": {"computer studies", "computer", "cmp"},
    "french": {"french", "fre"},
    "german": {"german", "ger"},
    "arabic": {"arabic", "arb"},
    "kenya sign language": {"kenya sign language", "ksl"},
    "music": {"music", "muc"},
    "accounting": {"accounting", "acc"},
    "commerce": {"commerce", "com"},
    "business studies": {"business studies", "bst"},
}


def normalize_course_title(course):
    """Normalize degree-title variants while retaining the actual course subject."""
    value = str(course or "").casefold().strip()
    value = re.sub(r"\bb\s*\.?\s*sc\.?\b", " bachelor of science ", value)
    value = re.sub(r"\bb\s*\.?\s*ed\.?\b", " bachelor of education ", value)
    value = re.sub(r"\bb\s*\.?\s*a\.?\b", " bachelor of arts ", value)
    value = re.sub(r"^bachelor\s+(?:in|of)\b", " ", value)
    value = re.sub(r"\bin\b", " ", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _normalize_subject_cell(value):
    value = re.sub(r"\s+", " ", str(value or "")).strip().upper()
    return re.sub(
        r"\(\s*(\d)\s+(\d{2})\s*\)",
        r"(\1\2)",
        value,
    )


def parse_subject_requirement_cell(value):
    """Parse one KUCCPS subject column; subjects within it are alternatives."""
    normalized = _normalize_subject_cell(value)
    match = SUBJECT_CELL_PATTERN.fullmatch(normalized)
    if not match:
        return None

    subjects = []
    for token in SUBJECT_TOKEN_PATTERN.finditer(match.group("subjects")):
        code = re.sub(r"\s+", "", token.group("code"))
        raw_name = " ".join(token.group("name").split()).strip()
        name = SUBJECT_LABELS.get(code, raw_name.title())
        subject = {"code": code, "name": name}
        if subject not in subjects:
            subjects.append(subject)
    if not subjects:
        return None
    return {
        "subjects": subjects,
        "minimum_grade": match.group("grade"),
    }


def extract_requirement_rows(pdf_path=SOURCE_PDF):
    entries = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            for table in page.extract_tables() or []:
                for row in table:
                    if len(row) < 10:
                        continue
                    program_code = str(row[1] or "").strip()
                    if not re.fullmatch(r"\d{7}", program_code):
                        continue
                    institution = re.sub(r"\s+", " ", str(row[2] or "")).strip()
                    course = re.sub(r"\s+", " ", str(row[3] or "")).strip()
                    if not institution or not course:
                        continue

                    groups = []
                    unparsed_cells = []
                    for column, cell in enumerate(row[6:10], start=1):
                        if not cell or not str(cell).strip():
                            continue
                        group = parse_subject_requirement_cell(cell)
                        if group:
                            groups.append(group)
                        else:
                            unparsed_cells.append({
                                "subject_column": column,
                                "text": re.sub(r"\s+", " ", str(cell)).strip(),
                            })
                    entries.append({
                        "program_code": program_code,
                        "institution": institution,
                        "course": course,
                        "source_page": page_number,
                        "subject_requirements": groups,
                        "unparsed_subject_cells": unparsed_cells,
                    })
    return entries


def build_requirements_database(pdf_path=SOURCE_PDF, output_path=REQUIREMENTS_FILE):
    entries = extract_requirement_rows(pdf_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    database = {
        "schema_version": 1,
        "source": SOURCE_NAME,
        "source_file": Path(pdf_path).name,
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
        os.replace(temporary_path, output_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    _load_requirements_database.cache_clear()
    return len(entries)


@lru_cache(maxsize=1)
def _load_requirements_database():
    try:
        with REQUIREMENTS_FILE.open("r", encoding="utf-8") as file:
            database = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logging.error("Could not load KUCCPS course requirements from %s: %s", REQUIREMENTS_FILE, error)
        return {"source": SOURCE_NAME, "entries": []}
    if database.get("schema_version") != 1 or not isinstance(database.get("entries"), list):
        logging.error("Unsupported KUCCPS requirements format in %s.", REQUIREMENTS_FILE)
        return {"source": SOURCE_NAME, "entries": []}
    return database


def _normalized_subject(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _grade_value(value):
    if isinstance(value, dict):
        value = value.get("grade") or value.get("value")
    grade = str(value or "").strip().upper()
    return grade, GRADE_POINTS.get(grade, 0)


def _grade_for_subject(subject, grades):
    label = subject["name"].casefold()
    aliases = SUBJECT_ALIASES.get(label, {label})
    aliases = {_normalized_subject(alias) for alias in aliases}
    code = subject["code"]
    aliases.add(_normalized_subject(code))
    for submitted_subject, submitted_grade in grades.items():
        if not isinstance(submitted_subject, str):
            continue
        key = _normalized_subject(submitted_subject)
        if key in aliases:
            grade, points = _grade_value(submitted_grade)
            if points:
                return grade
    return None


def _assess_group(group, grades):
    attained = []
    alternatives = group["subjects"]
    for subject in alternatives:
        grade = _grade_for_subject(subject, grades)
        if grade:
            attained.append({"subject": subject["name"], "grade": grade})

    required_points = GRADE_POINTS[group["minimum_grade"]]
    met = any(GRADE_POINTS[item["grade"]] >= required_points for item in attained)
    all_tested = len(attained) >= len(alternatives)
    if met:
        status = "Met"
    elif all_tested:
        status = "Not met"
    elif attained:
        status = "Not fully assessed"
    else:
        status = "Not provided"
    return {
        "subjects": [subject["name"] for subject in alternatives],
        "minimum_grade": group["minimum_grade"],
        "attained": attained,
        "status": status,
    }


def assess_course_requirements(course, grades=None, preferred_institutions=None, limit=5):
    database = _load_requirements_database()
    course_key = normalize_course_title(course)
    grades = grades if isinstance(grades, dict) else {}
    preferred_keys = {
        _normalized_subject(name)
        for name in (preferred_institutions or [])
        if isinstance(name, str) and name.strip()
    }
    matches = [
        entry for entry in database["entries"]
        if normalize_course_title(entry.get("course")) == course_key
    ] if course_key else []
    matches.sort(key=lambda entry: (
        _normalized_subject(entry.get("institution")) not in preferred_keys,
        entry.get("institution", "").casefold(),
        entry.get("program_code", ""),
    ))
    selected = matches[:max(0, limit)]

    records = []
    for entry in selected:
        groups = [
            _assess_group(group, grades)
            for group in entry.get("subject_requirements", [])
        ]
        statuses = [group["status"] for group in groups]
        if "Not met" in statuses:
            status = "Subject requirement not met"
        elif statuses and all(value == "Met" for value in statuses):
            status = "Listed subject requirements met"
        elif any(value == "Not fully assessed" for value in statuses):
            status = "Partially assessed"
        elif any(value == "Met" for value in statuses):
            status = "More subjects need assessment"
        elif statuses:
            status = "Not assessed"
        else:
            status = "Subject requirements unavailable"
        records.append({
            "institution": entry["institution"],
            "program_code": entry["program_code"],
            "course": entry["course"],
            "source_page": entry["source_page"],
            "requirements": groups,
            "unparsed_subject_cells": entry.get("unparsed_subject_cells", []),
            "status": status,
            "course_page_verified": False,
            "course_page_url": "",
        })

    return {
        "source": database.get("source", SOURCE_NAME),
        "status": "available" if records else "unavailable",
        "records": records,
        "omitted_record_count": max(0, len(matches) - len(records)),
        "notice": (
            "These are subject minima transcribed from the cited KUCCPS document. "
            "They do not confirm admission; mean-grade, cutoff, cluster-point, "
            "institution-specific and current-year rules may also apply."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Extract subject requirements from the KUCCPS 2025/2026 PDF.")
    parser.add_argument("--pdf", type=Path, default=SOURCE_PDF)
    parser.add_argument("--output", type=Path, default=REQUIREMENTS_FILE)
    args = parser.parse_args()
    try:
        count = build_requirements_database(args.pdf, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Could not extract KUCCPS requirements: {error}\n")
    print(f"Saved {count} programme rows to {args.output}")


if __name__ == "__main__":
    main()
