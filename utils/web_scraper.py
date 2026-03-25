import os
import re
import json
import time
import random
import logging
import threading
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

# Optional AI validator (kept from your system)
try:
    from .ai_engines import client_groq
except:
    client_groq = None

# Optional search fallback
try:
    from ddgs import DDGS
except:
    DDGS = None

logging.basicConfig(level=logging.INFO)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

KEYWORDS = [
    "course", "program", "programme", "degree",
    "faculty", "school", "department", "study", "academics"
]

BAD_PATHS = [
    "login", "register", "media", "news", "event",
    "download", "pdf", "image", "video"
]

ERROR_PATTERNS = [
    "page not found", "404", "not found",
    "error", "does not exist"
]


# ==========================================
# 🧠 OPTIONAL AI VALIDATOR
# ==========================================
def ai_validate(uni_name, course_name, text, url):
    if not client_groq:
        return True

    prompt = f"""
    University: {uni_name}
    Course: {course_name}
    URL: {url}

    Does this page belong to the university AND contain the course?

    Return JSON:
    {{"valid": true/false}}

    Content:
    {text[:3000]}
    """

    try:
        res = client_groq.chat.completions.create(
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0
        )
        return json.loads(res.choices[0].message.content).get("valid", False)
    except:
        return False


# ==========================================
# 🌐 SMART SCRAPER
# ==========================================
class SmartScraper:

    def __init__(self):
        self.cache = {}
        self.lock = threading.Lock()

    # --------------------------------------
    # Extract links
    # --------------------------------------
    def extract_links(self, url):
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)

            if res.status_code != 200:
                return []

            soup = BeautifulSoup(res.text, "html.parser")

            links = []
            for a in soup.find_all("a", href=True):
                href = urljoin(url, a["href"])
                text = a.get_text(strip=True).lower()
                links.append((text, href))

            return links

        except Exception as e:
            logging.warning(f"⚠️ Failed to extract links: {url} → {e}")
            return []

    # --------------------------------------
    # Score links
    # --------------------------------------
    def score_link(self, text, url, course):
        score = 0
        course = course.lower()

        if course in text:
            score += 5
        if course in url:
            score += 4

        for k in KEYWORDS:
            if k in url:
                score += 2
            if k in text:
                score += 1

        if any(bad in url.lower() for bad in BAD_PATHS):
            score -= 5

        return score

    # --------------------------------------
    # Find candidate links
    # --------------------------------------
    def find_candidates(self, base_url, course):
        links = self.extract_links(base_url)

        scored = []
        for text, href in links:
            score = self.score_link(text, href, course)
            if score > 2:
                scored.append((score, href))

        scored.sort(reverse=True)
        return [link for _, link in scored[:5]]

    # --------------------------------------
    # Verify page validity
    # --------------------------------------
    def verify_page(self, url, course, base_url=""):
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)

            # ❌ HTTP error
            if res.status_code != 200:
                return False

            text = res.text.lower()

            # ❌ Detect fake/404 pages
            if any(err in text for err in ERROR_PATTERNS):
                logging.warning(f"❌ Dead page detected: {url}")
                return False

            # ❌ Junk links
            if any(bad in url.lower() for bad in BAD_PATHS):
                return False

            # ✅ Course must exist in content
            if course.lower() not in text:
                return False

            # 🤖 Optional AI validation
            return ai_validate(base_url, course, text, url)

        except Exception as e:
            logging.warning(f"⚠️ Verification failed: {url} → {e}")
            return False

    # --------------------------------------
    # Crawl intelligently
    # --------------------------------------
    def crawl(self, start_url, course, depth=3):
        visited = set()
        queue = [(start_url, 0)]

        while queue:
            url, level = queue.pop(0)

            if url in visited or level > depth:
                continue

            visited.add(url)

            logging.info(f"🔍 Crawling: {url}")

            # Step 1: Find candidates
            candidates = self.find_candidates(url, course)

            # Step 2: Validate candidates
            for link in candidates:
                if self.verify_page(link, course):
                    logging.info(f"✅ Found course page: {link}")
                    return link

            # Step 3: Expand crawl
            links = self.extract_links(url)

            for text, href in links:
                if any(k in href.lower() for k in KEYWORDS):
                    queue.append((href, level + 1))

        return None

    # --------------------------------------
    # Search fallback (DuckDuckGo)
    # --------------------------------------
    def search_fallback(self, domain, course):
        if not DDGS:
            return None

        query = f"{course} site:{domain}"

        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))

                for r in results:
                    url = r.get("href")
                    if url and self.verify_page(url, course):
                        logging.info(f"🌐 Found via search: {url}")
                        return url
        except Exception as e:
            logging.warning(f"Search fallback failed: {e}")

        return None

    # --------------------------------------
    # Main entry
    # --------------------------------------
    def find_course_url(self, university_url, course_name):
        cache_key = f"{university_url}_{course_name}"

        with self.lock:
            if cache_key in self.cache:
                return self.cache[cache_key]

        # Step 1: Crawl
        result = self.crawl(university_url, course_name)

        # Step 2: Fallback search
        if not result:
            domain = urlparse(university_url).netloc
            result = self.search_fallback(domain, course_name)

        # Step 3: Final fallback
        if not result:
            result = university_url

        with self.lock:
            self.cache[cache_key] = result

        return result


# ==========================================
# 🌟 GLOBAL INSTANCE
# ==========================================
scraper = SmartScraper()


# ==========================================
# 🚀 PUBLIC FUNCTION (USE IN APP)
# ==========================================
def get_course_url(university_url, course_name):
    return scraper.find_course_url(university_url, course_name)