import os
import re
import json
import logging
import time
import requests
import urllib3
import threading
import concurrent.futures
import warnings
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import heapq
from collections import deque

# Tenacity for handling API Rate Limits
from tenacity import retry, stop_after_attempt, wait_exponential

# Suppress the DDGS renaming warning cluttering the logs
warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Handle the DDGS import
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        logging.error("❌ Critical: duckduckgo_search/ddgs library not found.")

from .ai_engines import client_groq 

# ==========================================
# 🧠 AI VALIDATION & RETRY LOGIC
# ==========================================

@retry(wait=wait_exponential(multiplier=1, min=5, max=60), stop=stop_after_attempt(5))
def safe_ai_call(prompt):
    """Wraps the Groq API call with exponential backoff for 429 Rate Limits."""
    return client_groq.chat.completions.create(
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
        model="llama-3.1-8b-instant",
        temperature=0.1
    )

def ai_course_validator(uni_name, course_name, scraped_text, source_url):
    """
    AI Auditor: Prevents 'Zetech Hijacking' by ensuring the scraped text 
    actually belongs to the requested university, while assessing page depth.
    """
    # A small buffer sleep is fine, but let Tenacity handle the heavy lifting
    time.sleep(2) 
    
    # Aggressively trim text to save tokens and stay within context limits
    optimized_text = scraped_text[:2500] 
    
    if not client_groq: 
        return {}

    prompt = f"""
    Requested Institution: {uni_name}
    Requested Course: {course_name}
    Found URL: {source_url}

    AUDIT RULES:
    1. INSTITUTION MATCH: Does the text/URL belong to {uni_name}? 
       - Accept if it is the official site, a student portal, or an official social media page.
       - Reject ONLY if it is clearly a competitor university's site (e.g., page says 'Zetech' but we want 'JKUAT').
    
    2. FLEXIBLE COURSE MATCH: Is '{course_name}' offered here?
       - BE HIGHLY FLEXIBLE. 
       - Treat 'BSc', 'B.Sc', 'BS', and 'Bachelor of Science' as identical.
       - Treat 'B.A', 'BA', and 'Bachelor of Arts' as identical.
       - If the page lists many courses and '{course_name}' is among them, it is a MATCH.
       - If the course name is slightly different (e.g., 'Computer Science' vs 'Informatics & Computer Science'), it is a MATCH.

    3. HIGH FIDELITY CHECK: Does this page contain deep course details (admission requirements, units, fees)?
    
    4. PAGE TYPE: Is this a specific "course_detail" page, or a "hub_or_list" of many courses?

    Return a JSON object with strictly these keys based on the rules above:
    {{
        "is_correct_uni": bool, 
        "specific_course_found": bool, 
        "has_high_fidelity_details": bool,
        "page_type": "course_detail" | "hub_or_list" | "other"
    }}
    
    Webpage Content:
    {optimized_text}
    """
    
    try:
        res = safe_ai_call(prompt)
        data = json.loads(res.choices[0].message.content)
        return data
    except Exception as e: 
        logging.error(f"AI Validator Error: {e}")
        return {}


# ==========================================
# 🕷️ AUTO-HEALER SCRAPER CLASS
# ==========================================

class AutoHealer:
    def __init__(self, target_folder="data"):
        # TWO SEPARATE FILES: One for domains, one for specific course links
        self.domain_db_path = os.path.join(target_folder, "school_domains.json")
        self.course_db_path = os.path.join(target_folder, "course_urls.json")
        
        os.makedirs(target_folder, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        })
        self.db_lock = threading.Lock() 

        # Load both databases into memory
        self.domain_db = self._load_json(self.domain_db_path)
        self.course_db = self._load_json(self.course_db_path)
        
        # Ensure consistency with your _hunt_for_url method
        self.db = self.course_db  
        self.db_path = self.course_db_path 

        # Baseline "Verified Anchor" List
        self.known_domains = {
            "university of nairobi": "uonbi.ac.ke", "kenyatta university": "ku.ac.ke",
            "jomo kenyatta": "jkuat.ac.ke", "egerton": "egerton.ac.ke", "moi university": "mu.ac.ke",
            "maseno": "maseno.ac.ke", "strathmore": "strathmore.edu", "kisii": "kisiiuniversity.ac.ke",
            "masinde muliro": "mmust.ac.ke", "technical university of kenya": "tukenya.ac.ke",
            "kca university": "kca.ac.ke", "mount kenya": "mku.ac.ke", "zetech": "zetech.ac.ke"
        }
        
        # Merge baseline anchors into domain_db if they aren't already there
        for name, domain in self.known_domains.items():
            if name not in self.domain_db:
                self.domain_db[name] = domain

    def _load_json(self, path):
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception: pass
        return {}

    def _save_all(self):
        """Saves both domains and course URLs to their respective files."""
        with self.db_lock:
            try:
                with open(self.domain_db_path, 'w', encoding='utf-8') as f:
                    json.dump(self.domain_db, f, indent=4)
                with open(self.course_db_path, 'w', encoding='utf-8') as f:
                    json.dump(self.course_db, f, indent=4)
            except Exception as e:
                logging.error(f"Failed to save databases: {e}")

    def _load_db(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception: pass
        return {}

    def _save_db(self):
        with self.db_lock:
            try:
                with open(self.db_path, 'w', encoding='utf-8') as f:
                    json.dump(self.db, f, indent=4)
            except Exception: pass

    def _get_domain(self, university_name):
        """Resolves official domains and UPDATES the local domain cache."""
        uni_lower = university_name.lower().strip()
        
        if uni_lower in self.domain_db:
            return self.domain_db[uni_lower]
                
        try:
            with DDGS() as ddgs:
                kuccps_query = f'site:students.kuccps.net "{university_name}" website'
                for r in list(ddgs.text(kuccps_query, max_results=3)):
                    body = r.get('body', '').lower()
                    match = re.search(r'([a-zA-Z0-9\-]+\.(?:ac\.ke|edu\.ke|sc\.ke|edu))', body)
                    if match:
                        found_domain = match.group(1)
                        self.domain_db[uni_lower] = found_domain
                        self._save_all() 
                        return found_domain
                
                direct_query = f'official website "{university_name}" Kenya'
                for r in list(ddgs.text(direct_query, max_results=3)):
                    href = r.get('href', '').lower()
                    match = re.search(r'([a-zA-Z0-9\-]+\.(?:ac\.ke|edu\.ke|sc\.ke|edu))', href)
                    if match:
                        found_domain = match.group(1)
                        self.domain_db[uni_lower] = found_domain
                        self._save_all() 
                        return found_domain
        except Exception as e: 
            logging.debug(f"Domain lookup exception: {e}")
            
        return None

    def _internal_navigation_crawl(self, root_domain, university_name, course_name, max_pages=20):
        """
        AI-Driven Best-First Search (Priority Queue). 
        Searches until the specific course name AND requirements are found.
        """
        homepage_url = f"https://{root_domain}"
        
        pq = [(0, 0, homepage_url)] 
        visited = {homepage_url}
        pages_scanned = 0

        clean_course_name = re.sub(r'[^\w\s]', '', course_name.lower())
        course_keywords = set(clean_course_name.split())
        if not course_keywords:
            course_keywords = {course_name.lower()}
            
        academic_keywords = {'requirement', 'unit', 'curriculum', 'module', 'syllabus', 'admission', 'program', 'course'}

        while pq and pages_scanned < max_pages:
            current_neg_score, depth, current_url = heapq.heappop(pq)
            
            if depth > 3: 
                continue
                
            pages_scanned += 1
            
            try:
                current_score = -current_neg_score
                logging.info(f"🕵️ Scanning [{pages_scanned}/{max_pages}] (Score: {current_score}): {current_url}")
                
                res = self.session.get(f"https://r.jina.ai/{current_url}", timeout=15)
                if res.status_code != 200: 
                    continue

                text_lower = res.text.lower()
                is_hub = False

                if pages_scanned > 1 and not any(word in text_lower for word in course_keywords):
                    logging.info(f"⏩ Skipping AI Validation: Course keywords not found in {current_url}")
                else:
                    analysis = ai_course_validator(university_name, course_name, res.text, current_url)

                    if analysis:
                        if (analysis.get("is_correct_uni") and 
                            analysis.get("specific_course_found") and 
                            analysis.get("has_high_fidelity_details")):
                            logging.info(f"✅ FINAL MATCH FOUND: {current_url}")
                            return current_url

                        is_hub = analysis.get("page_type") == "hub_or_list"

                markdown_links = re.findall(r'\[([^\]]+)\]\(([^\)]+)\)', res.text)

                for link_text, extracted_url in markdown_links:
                    full_url = urljoin(current_url, extracted_url)
                    full_url = full_url.split('#')[0].rstrip('/')
                    link_text_lower = link_text.lower()

                    if root_domain in full_url and full_url not in visited:
                        visited.add(full_url)
                        
                        score = 0
                        if any(kw in link_text_lower for kw in course_keywords): score += 30
                        if any(ak in link_text_lower for ak in academic_keywords): score += 10
                        if is_hub: score += 5 
                            
                        heapq.heappush(pq, (-score, depth + 1, full_url))

            except Exception as e:
                logging.debug(f"⚠️ Navigation skipped {current_url}: {e}")

        logging.warning(f"❌ Could not find a specific match for '{course_name}' after {pages_scanned} pages.")
        return None

    def _hunt_for_url(self, university_name, course_name):
        # 1. Check Cache First
        if university_name in self.db and course_name in self.db[university_name]:
            return self.db[university_name][course_name], True

        # 2. Identify the Anchor Domain
        root_domain = self._get_domain(university_name)
        if not root_domain:
            logging.warning(f"⚠️ Could not isolate official domain for {university_name}.")
            return None, False

        # 3. Precision Deep-Link Search 
        precision_query = f'site:{root_domain} "{course_name}"'
        urls_to_check = []
        
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(precision_query, max_results=5))
                for r in results:
                    url = r.get('href', '')
                    if url and root_domain in url:
                        urls_to_check.append(url)
        except Exception as e:
            logging.error(f"❌ Search Error: {e}")

        # 4. Concurrent Scraping & Validation
        if urls_to_check:
            def verify_single_url(target_url):
                try:
                    res = self.session.get(f"https://r.jina.ai/{target_url}", timeout=20, verify=False)
                    analysis = ai_course_validator(university_name, course_name, res.text, target_url)
                    if analysis and analysis.get("is_correct_uni") and analysis.get("specific_course_found"):
                        return target_url
                except Exception as e:
                    logging.debug(f"Failed to scrape {target_url}: {e}")
                return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                for approved_url in executor.map(verify_single_url, urls_to_check):
                    if approved_url:
                        if university_name not in self.db: self.db[university_name] = {}
                        self.db[university_name][course_name] = approved_url
                        self._save_db()
                        return approved_url, True

        # 4.5 Internal Navigation Fallback
        logging.warning(f"🕵️ External search failed. Starting internal navigation...")
        internal_url = self._internal_navigation_crawl(root_domain, university_name, course_name)
        
        if internal_url:
            if university_name not in self.db: self.db[university_name] = {}
            self.db[university_name][course_name] = internal_url
            self._save_db()
            return internal_url, True
             
        # 5. SOFT-FAIL: Fallback to the main homepage
        logging.warning(f"⚠️ Specific page for {course_name} not found.")
        homepage = f"https://{root_domain}"
        return homepage, False


# ==========================================
# 🌐 STANDALONE HELPER FUNCTION
# ==========================================

def get_course_url(university_name, course_name):
    """
    AI-Gated 'Dux' Search: Forces results to come ONLY from official .ac.ke 
    or .edu domains to prevent 'Zetech Hijacking'.
    """
    search_query = f'site:.ac.ke OR site:.edu "{university_name}" "{course_name}" requirements'
    logging.info(f"🦆 Dux is scanning official school domains for: {university_name}")

    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(search_query, max_results=10))

            for result in results:
                candidate_url = result.get('href', '').lower()
                
                # 🛑 SAFETY CHECK
                if any(bad in candidate_url for bad in ['facebook', 'twitter', 'kenyayote', 'advance-africa']):
                    continue

                logging.info(f"🧐 AI Auditor checking official candidate: {candidate_url}")

                try:
                    jina_url = f"https://r.jina.ai/{candidate_url}"
                    response = requests.get(jina_url, timeout=30, verify=False)
                    
                    analysis = ai_course_validator(university_name, course_name, response.text, candidate_url)
                    
                    if analysis and analysis.get("is_correct_uni") and analysis.get("specific_course_found"):
                        logging.info(f"✅ AI VALIDATED OFFICIAL SITE: {candidate_url}")
                        
                        # Fix: Extract root domain from candidate_url before passing to crawler
                        root_domain = urlparse(candidate_url).netloc
                        found_deep_link = healer._internal_navigation_crawl(root_domain, university_name, course_name)
                        
                        return found_deep_link if found_deep_link else candidate_url
                    
                    else:
                        logging.warning(f"❌ AI Rejected (Content mismatch): {candidate_url}")
                        continue 

                except Exception as e:
                    logging.error(f"⚠️ Validation error on {candidate_url}: {e}")
                    continue

    except Exception as e:
        logging.error(f"❌ Dux Search Error: {e}")
    
    return None


# ==========================================
# 🛠️ FINAL INITIALIZATION (Bottom of file)
# ==========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIR = os.path.join(BASE_DIR, '..', 'data')

if not os.path.exists(TARGET_DIR):
    os.makedirs(TARGET_DIR)

healer = AutoHealer(target_folder=TARGET_DIR)