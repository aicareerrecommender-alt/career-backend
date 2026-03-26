import os
import re
import json
import logging
import bs4
import requests
import urllib3
import threading
import concurrent.futures
import warnings
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin # Update your existing urlparse import

from collections import deque
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

def ai_course_validator(uni_name, course_name, scraped_text, source_url):
    """
    AI Auditor: Prevents 'Zetech Hijacking' by ensuring the scraped text 
    actually belongs to the requested university.
    """
    if not client_groq: 
        return False
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

    Return JSON: {{"is_official_site": bool, "is_valid_course": bool, "reason": "string"}}
    
    Webpage Content:
    {scraped_text[:5000]}
    """
    try:
        res = client_groq.chat.completions.create(
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", 
            temperature=0.1
        )
        data = json.loads(res.choices[0].message.content)
        return data.get("is_official_site") and data.get("is_valid_course")
    except Exception as e: 
        logging.error(f"AI Validator Error: {e}")
        return False

class AutoHealer:
    def __init__(self, target_folder="data"):
        self.db_path = os.path.join(target_folder, "academic_urls.json")
        os.makedirs(target_folder, exist_ok=True)
        self.session = requests.Session()
        # Updated User-Agent to a newer version to reduce blocks
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        })
        self.db_lock = threading.Lock() 
        self.db = self._load_db()

        # The "Verified Anchor" List (KUCCPS Aligned)
        self.known_domains = {
            "university of nairobi": "uonbi.ac.ke", "kenyatta university": "ku.ac.ke",
            "jomo kenyatta": "jkuat.ac.ke", "egerton": "egerton.ac.ke", "moi university": "mu.ac.ke",
            "maseno": "maseno.ac.ke", "strathmore": "strathmore.edu", "kisii": "kisiiuniversity.ac.ke",
            "masinde muliro": "mmust.ac.ke", "technical university of kenya": "tukenya.ac.ke",
            "kca university": "kca.ac.ke", "mount kenya": "mku.ac.ke", "zetech": "zetech.ac.ke"
        }

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
        """Resolves official domains by checking local map, then KUCCPS, then direct search."""
        uni_lower = university_name.lower()
        for name, domain in self.known_domains.items():
            if name in uni_lower: return domain
                
        try:
            with DDGS() as ddgs:
                # 1. KUCCPS Deep-Search for the Domain
                kuccps_query = f'site:students.kuccps.net "{university_name}" website'
                for r in list(ddgs.text(kuccps_query, max_results=3)):
                    body = r.get('body', '').lower()
                    match = re.search(r'([a-zA-Z0-9\-]+\.(?:ac\.ke|edu\.ke|sc\.ke|edu))', body)
                    if match: return match.group(1)
                
                # 2. FALLBACK: Direct search if KUCCPS fails
                direct_query = f'official website "{university_name}" Kenya'
                for r in list(ddgs.text(direct_query, max_results=3)):
                    href = r.get('href', '').lower()
                    match = re.search(r'([a-zA-Z0-9\-]+\.(?:ac\.ke|edu\.ke|sc\.ke|edu))', href)
                    if match: return match.group(1)
        except Exception as e: 
            logging.debug(f"Domain lookup exception: {e}")
            
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

        # 3. Precision Deep-Link Search (Removed -filetype:pdf constraint)
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
                    # Bumped timeout to 20s for slow university websites & Jina AI overhead
                    res = self.session.get(f"https://r.jina.ai/{target_url}", timeout=20, verify=False)
                    if ai_course_validator(university_name, course_name, res.text, target_url):
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
    # --- STEP 4.5: Internal Navigation Fallback ---
        # RUNS ONLY IF STEP 4 FINISHED WITHOUT RETURNING
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
    
                
    def _internal_navigation_crawl(self, root_domain, university_name, course_name, max_depth=3):
        """
        Fallback: Navigates the university homepage to find the course 
        using a Breadth-First Search (BFS) Deep Crawler.
        """
        homepage_url = f"https://{root_domain}"
        
        # 1. Clean the target course name (removes filler words)
        ignore_words = {'in', 'of', 'and', 'bachelor', 'degree', 'diploma', 'certificate', 'bsc', 'ba'}
        course_keywords = [w.lower() for w in course_name.split() if w.lower() not in ignore_words]
        
        # 2. Define our "Tabs to Click" (Allowlist) and "Tabs to Ignore" (Blocklist)
        hub_keywords = ['academic', 'faculty', 'school', 'department', 'programme', 'course', 'undergraduate', 'college']
        deny_keywords = ['admission', 'portal', 'login', 'news', 'tender', 'contact', 'about', 'library', 'staff']

        visited_urls = set()
        # The Queue holds tuples of (url_to_visit, current_depth)
        queue = deque([(homepage_url, 0)]) 
        
        while queue:
            current_url, depth = queue.popleft()
            
            # Don't visit the same page twice to prevent infinite loops
            if current_url in visited_urls:
                continue
            visited_urls.add(current_url)
            
            try:
                logging.info(f"🕵️ [Depth {depth}]: Scanning tabs on {current_url}")
                res = self.session.get(current_url, timeout=10, verify=False)
                soup = BeautifulSoup(res.text, 'html.parser')
                
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    text = a.get_text().strip().lower()
                    full_url = urljoin(current_url, href)
                    
                    # Skip massive unhelpful links, external links, or dead links
                    if not full_url.startswith('http') or any(bad in text or bad in href.lower() for bad in deny_keywords):
                        continue
                        
                    # 🎯 PHASE 1: Did we find the exact course?
                    if all(kw in text for kw in course_keywords):
                        logging.info(f"🎉 BINGO! Found target course page: {full_url}")
                        
                        # Validate with AI before returning to ensure it's not a false positive
                        scrape_res = self.session.get(f"https://r.jina.ai/{full_url}", timeout=20, verify=False)
                        if ai_course_validator(university_name, course_name, scrape_res.text, full_url):
                            return full_url
                        else:
                            logging.warning(f"⚠️ AI Rejected URL: {full_url}")
                        
                    # 🚪 PHASE 2: Is this a Navigation Tab we should click into?
                    if depth < max_depth:
                        if any(hub in text for hub in hub_keywords):
                            if full_url not in visited_urls:
                                queue.append((full_url, depth + 1))
                                
            except Exception as e:
                logging.debug(f"⚠️ Failed to navigate {current_url}: {e}")
                
        logging.warning(f"❌ Could not find {course_name} within {max_depth} clicks from {homepage_url}")
        return None
# Initialize
# Replace your current TARGET_DIR and healer lines with this:
TARGET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')

# Create the directory if it doesn't exist to stop the WARNING
if not os.path.exists(TARGET_DIR):
    os.makedirs(TARGET_DIR)

healer = AutoHealer(target_folder=TARGET_DIR)