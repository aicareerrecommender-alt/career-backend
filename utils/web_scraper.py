import base64
import binascii
import json
import io
import logging
import os
import re
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import truststore
from bs4 import BeautifulSoup

from utils.ai_engines import is_course_in_catalog


truststore.inject_into_ssl()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FOLDER = os.path.join(PROJECT_ROOT, "data")
COURSE_URLS_FILE = os.path.join(DATA_FOLDER, "course_urls.json")
SCHOOL_DOMAINS_FILE = os.path.join(DATA_FOLDER, "school_domains.json")
SEARCH_URL = "https://www.bing.com/search"


def _load_json_mapping(path, description):
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        logging.error("Could not load %s from %s: %s", description, path, error)
        return {}

    if not isinstance(data, dict):
        logging.error("%s at %s must contain a JSON object.", description, path)
        return {}
    return data


def load_course_url_database():
    return _load_json_mapping(COURSE_URLS_FILE, "course URL database")


def load_school_domains():
    return _load_json_mapping(SCHOOL_DOMAINS_FILE, "institution domain database")


OFFICIAL_DOMAINS = load_school_domains()


def _normalized_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _course_title_words(value):
    text = str(value).casefold()
    text = re.sub(r"\bb\s*\.?\s*sc\.?\b", " bachelor of science ", text)
    text = re.sub(r"\bb\s*\.?\s*ed\.?\b", " bachelor of education ", text)
    text = re.sub(r"\bb\s*\.?\s*a\.?\b", " bachelor of arts ", text)
    text = re.sub(
        r"\b(?:bachelor|master)\s+(?:of|in)\s+(?:science|arts|education)\b",
        " ",
        text,
    )
    stop_words = {
        "and", "bachelor", "certificate", "degree", "diploma", "doctor",
        "for", "in", "master", "of", "phd", "the", "with",
    }
    return [
        word for word in re.findall(r"[a-z0-9]+", text)
        if word not in stop_words
    ]


def _course_title_is_present(course_name, text):
    course_words = _course_title_words(course_name)
    if not course_words:
        return False
    page_words = _course_title_words(text)
    window_size = len(course_words)
    return any(
        page_words[index:index + window_size] == course_words
        for index in range(len(page_words) - window_size + 1)
    )


def _official_domain(university_name):
    key = _normalized_key(university_name)
    for name, domain in OFFICIAL_DOMAINS.items():
        if _normalized_key(name) == key and isinstance(domain, str) and domain.strip():
            domain = domain.strip().lower().removeprefix("www.")
            if re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", domain):
                return domain
            logging.warning("Ignoring invalid official domain for institution %s.", university_name)
            return None
    return None


def _url_is_on_domain(url, domain):
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and (host == domain or host.endswith(f".{domain}"))
    )


def _search_result_url(url):
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return None
    if parsed.hostname not in {"bing.com", "www.bing.com"}:
        return url

    encoded_target = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded_target.startswith("a1"):
        return None
    encoded_target = encoded_target[2:]
    encoded_target += "=" * (-len(encoded_target) % 4)
    try:
        return base64.urlsafe_b64decode(encoded_target).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        logging.warning("Ignoring a malformed course-search result URL.")
        return None


def _local_course_urls(university_name, course_name, domain=None):
    database = load_course_url_database()
    university_key = _normalized_key(university_name)
    entries = database.get("entries")
    if isinstance(entries, list):
        course_key = _normalized_key(course_name)
        urls = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("verified") is not True
                or entry.get("status") != "verified"
                or _normalized_key(entry.get("university", "")) != university_key
                or _normalized_key(entry.get("course", "")) != course_key
            ):
                continue
            saved_domain = entry.get("domain")
            url = entry.get("url")
            if (
                isinstance(saved_domain, str)
                and _url_is_on_domain(url, saved_domain.strip().lower().removeprefix("www."))
                and (domain is None or domain == saved_domain.strip().lower().removeprefix("www."))
                and url not in urls
            ):
                urls.append(url)
        return urls

    if not domain:
        return []
    urls = []
    for name, courses in database.items():
        if _normalized_key(name) != university_key or not isinstance(courses, dict):
            continue
        course_key = _normalized_key(course_name)
        for title, url in courses.items():
            if _normalized_key(title) == course_key and isinstance(url, str):
                if _url_is_on_domain(url, domain) and url not in urls:
                    urls.append(url)
                elif not _url_is_on_domain(url, domain):
                    logging.warning(
                        "Ignoring non-official course URL for %s at %s.",
                        course_name,
                        university_name,
                    )
    return urls


def _course_page_soup(response):
    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "header", "nav", "footer", "aside"]):
        element.decompose()
    return soup


def _page_heading(response):
    return _page_heading_from_soup(_course_page_soup(response))


def _page_heading_from_soup(soup):
    headings = [soup.title.get_text(" ", strip=True)] if soup.title else []
    headings.extend(heading.get_text(" ", strip=True) for heading in soup.find_all(["h1", "h2", "h3"]))
    return " ".join(headings)


def _verify_official_course_page(url, course_name, domain):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "CareerPath course-page verifier"},
            timeout=8,
            allow_redirects=True,
        )
    except requests.RequestException as error:
        logging.warning("Could not re-verify saved course page %s: %s", url, error)
        return None

    final_url = response.url
    if (
        response.status_code != 200
        or not _url_is_on_domain(final_url, domain)
        or "application/pdf" in response.headers.get("Content-Type", "").lower()
    ):
        return None

    soup = _course_page_soup(response)
    titled_programmes = " ".join(
        element.get_text(" ", strip=True)
        for element in soup.select(
            ".course-title, .programme-title, .program-title, .prog-title"
        )
    )
    if _course_title_is_present(
        course_name,
        f"{_page_heading_from_soup(soup)} {titled_programmes}",
    ):
        return final_url
    return None


def _search_official_course_page(university_name, course_name, domain):
    query = f'site:{domain} "{course_name}"'
    try:
        search_response = requests.get(
            SEARCH_URL,
            params={"q": query},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=8,
        )
    except requests.RequestException as error:
        logging.warning("Official course search failed for %s at %s: %s", course_name, university_name, error)
        return None

    if search_response.status_code != 200:
        logging.warning(
            "Official course search returned HTTP %s for %s at %s.",
            search_response.status_code,
            course_name,
            university_name,
        )
        return None

    soup = BeautifulSoup(search_response.text, "html.parser")
    for result in soup.select("li.b_algo"):
        link = result.select_one("h2 a[href]")
        if not link:
            continue
        url = _search_result_url(link.get("href"))
        title = link.get_text(" ", strip=True)
        snippet = result.get_text(" ", strip=True)
        if not isinstance(url, str) or not _url_is_on_domain(url, domain):
            continue
        if not _course_title_is_present(course_name, f"{title} {snippet}"):
            continue

        try:
            response = requests.get(
                url,
                headers={"User-Agent": "CareerPath course-page verifier"},
                timeout=8,
                allow_redirects=True,
            )
        except requests.RequestException as error:
            logging.warning("Could not open official course page %s: %s", url, error)
            continue

        final_url = response.url
        if response.status_code != 200 or not _url_is_on_domain(final_url, domain):
            continue
        if _course_title_is_present(course_name, _page_heading(response)):
            return final_url

    logging.info("No official course page verified for %s at %s.", course_name, university_name)
    return None


def _safe_application_url(url):
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def _application_link_from_official_page(
    url,
    domain,
    university_name,
    is_verified_course_page=False,
):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "CareerPath official application-link verifier"},
            timeout=8,
            allow_redirects=True,
        )
    except requests.RequestException as error:
        logging.warning("Could not inspect official admissions page %s: %s", url, error)
        return None

    final_url = response.url
    if (
        response.status_code != 200
        or not _url_is_on_domain(final_url, domain)
        or "application/pdf" in response.headers.get("Content-Type", "").lower()
    ):
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    page_context = " ".join((
        final_url,
        soup.title.get_text(" ", strip=True) if soup.title else "",
        " ".join(
            heading.get_text(" ", strip=True)
            for heading in soup.find_all(["h1", "h2", "h3"])
        ),
    )).casefold()
    has_admissions_context = re.search(
        r"\b(apply|application|admission|admissions|enrol|enroll)\b",
        page_context,
    )
    if not has_admissions_context and not is_verified_course_page:
        return None

    application_actions = re.compile(
        r"\b(apply\s*(?:online|now|courses?|programmes?|programs?)|"
        r"online application|application portal|"
        r"apply for admission|start (?:your )?application|submit application|"
        r"student application portal)\b",
        re.IGNORECASE,
    )
    application_links = []
    for anchor in soup.select("a[href]"):
        label = " ".join((
            anchor.get_text(" ", strip=True),
            str(anchor.get("aria-label") or ""),
            str(anchor.get("title") or ""),
        ))
        href = anchor.get("href", "")
        candidate_url = urljoin(final_url, href)
        if not _safe_application_url(candidate_url):
            continue
        if not application_actions.search(f"{label} {href}"):
            continue
        if (
            not _url_is_on_domain(candidate_url, domain)
            and not application_actions.search(label)
        ):
            continue
        application_links.append((candidate_url, label.casefold()))

    if application_links:
        application_links.sort(
            key=lambda item: (
                "application portal" in item[1],
                "apply online" in item[1],
                "apply now" in item[1],
            ),
            reverse=True,
        )
        return application_links[0][0]

    logging.info(
        "No direct application link was found on the official admissions page for %s.",
        university_name,
    )
    return None


def _search_official_application_page(university_name, course_name, domain):
    query = (
        f'site:{domain} "{course_name}" '
        '(admissions OR admission OR "apply online" OR "application portal")'
    )
    try:
        search_response = requests.get(
            SEARCH_URL,
            params={"q": query},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=8,
        )
    except requests.RequestException as error:
        logging.warning(
            "Official application-page search failed for %s: %s",
            university_name,
            error,
        )
        return None

    if search_response.status_code != 200:
        logging.warning(
            "Official application-page search returned HTTP %s for %s.",
            search_response.status_code,
            university_name,
        )
        return None

    soup = BeautifulSoup(search_response.text, "html.parser")
    candidates = []
    for result in soup.select("li.b_algo"):
        link = result.select_one("h2 a[href]")
        if not link:
            continue
        url = _search_result_url(link.get("href"))
        if not isinstance(url, str) or not _url_is_on_domain(url, domain):
            continue
        evidence = " ".join((
            link.get_text(" ", strip=True),
            result.get_text(" ", strip=True),
            url,
        ))
        if re.search(r"\b(apply|application|admission|admissions|enrol|enroll)\b", evidence, re.I):
            candidates.append(url)

    for candidate_url in dict.fromkeys(candidates):
        application_url = _application_link_from_official_page(
            candidate_url,
            domain,
            university_name,
        )
        if application_url:
            return application_url
    logging.info("No official application portal was verified for %s.", university_name)
    return None


def get_official_application_url(university_name, course_name, course_url=None):
    """Find an application link published on an institution's verified HTTPS site."""
    if not isinstance(university_name, str) or not university_name.strip():
        return None
    if not isinstance(course_name, str) or not course_name.strip():
        return None

    domain = _official_domain(university_name)
    if not domain:
        logging.info("No official domain is registered for institution %s.", university_name)
        return None

    if isinstance(course_url, str) and _url_is_on_domain(course_url, domain):
        application_url = _application_link_from_official_page(
            course_url,
            domain,
            university_name,
            is_verified_course_page=True,
        )
        if application_url:
            return application_url

    homepage_url = f"https://{domain}/"
    application_url = _application_link_from_official_page(
        homepage_url,
        domain,
        university_name,
        is_verified_course_page=True,
    )
    if application_url:
        return application_url

    return _search_official_application_page(university_name, course_name, domain)


def get_course_url(university_name, course_name, target_type="institution"):
    """Return a direct official course page only when catalog and page evidence agree."""
    if not isinstance(university_name, str) or not university_name.strip():
        return None
    if not isinstance(course_name, str) or not is_course_in_catalog(course_name):
        logging.info("Skipping course-page search for a course absent from the KUCCPS catalog.")
        return None
    if not _course_title_words(course_name):
        logging.info("Skipping course-page search for a course without a specific subject.")
        return None

    domain = _official_domain(university_name)
    if not domain:
        logging.info("No official domain is registered for institution %s.", university_name)
        return None

    for local_url in _local_course_urls(university_name, course_name, domain):
        verified_url = _verify_official_course_page(local_url, course_name, domain)
        if verified_url:
            return verified_url

    return _search_official_course_page(university_name, course_name, domain)
