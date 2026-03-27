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
from .ai_engines import client_groq 

# --- NEW LAZY LOADING LOGIC ---
healer = None

def get_healer():
    """Lazily initializes the AutoHealer only when needed."""
    global healer
    if healer is None:
        logging.info("🚀 First request received. Initializing AutoHealer...")
        # Get the path to the data folder relative to this file
        current_file_dir = os.path.dirname(os.path.abspath(__file__))
        target_dir = os.path.normpath(os.path.join(current_file_dir, '..', 'data'))
        
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)
            
        healer = AutoHealer(target_folder=target_dir)
    return healer
# ------------------------------
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

def ai_course_validator(university_name, course_name, web_text, source_url):
    """Evaluates if a page is the final, dedicated course page."""
    time.sleep(2)
    optimized_text = web_text[:2500] 
    
    prompt = f"""
    Analyze if this webpage is the SPECIFIC official landing page for '{course_name}' at {university_name}.
    URL: {source_url}
    
    CRITERIA:
    - HUB/LIST PAGE: Contains many courses (e.g., 'Our Programs', 'Faculty of Science').
    - DEDICATED PAGE: Focuses exclusively on '{course_name}'. Contains detailed units, duration, or specific career paths.

    Return JSON strictly in this format:
    {{
        "is_correct_uni": true,
        "specific_course_found": true,
        "page_type": "dedicated" | "hub" | "irrelevant",
        "confidence_score": 85,
        "is_conclusive": true 
    }}
    (is_conclusive should ONLY be true if page_type is 'dedicated')
    
    Webpage Content:
    {optimized_text}
    """
    try:
        res = safe_ai_call(prompt)
        return json.loads(res.choices[0].message.content)
    except Exception as e: 
        logging.error(f"AI Validator Error: {e}")
        return {}

def ai_navigator_audit(university_name, course_name, page_text, available_links):
    """The 'Brain' of the Agentic Crawler. Decides which link to click next."""
    time.sleep(2)
    
    # Aggressively trim text to save tokens. The AI mostly needs the links.
    optimized_text = page_text[:1500]
    # Limit links to top 60 to prevent blowing up the Groq context window
    links_json = json.dumps(available_links[:60])
    
    prompt = f"""
    You are an AI Web Crawler Agent.
    Goal: Find the dedicated official course page for '{course_name}' at '{university_name}'.
    
    Current Page Content Summary:
    {optimized_text}
    
    Available Links on this page:
    {links_json}
    
    Determine your next action:
    1. If the Current Page Content proves we are already on the conclusive, dedicated page for {course_name} (shows units, fees, duration), set status to "FINAL_MATCH".
    2. If not, pick the BEST link from the 'Available Links' that will get us closer (e.g., look for 'Academics', 'Undergraduate', 'School of Science', or the course name itself). Set status to "KEEP_SEARCHING".
    3. If none of the links are helpful, set status to "DEAD_END".
    
    Return JSON strictly in this format:
    {{
        "status": "FINAL_MATCH" | "KEEP_SEARCHING" | "DEAD_END",
        "next_best_link": "exact url string from the list or null",
        "current_page_score": 0-100
    }}
    """
    try:
        res = safe_ai_call(prompt)
        return json.loads(res.choices[0].message.content)
    except Exception as e: 
        logging.error(f"AI Navigator Error: {e}")
        return {"status": "DEAD_END", "next_best_link": None, "current_page_score": 0}

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
        # Load the KENET text file database
        current_dir = os.path.dirname(os.path.abspath(__file__))
        kenet_file_path = os.path.join(current_dir, "kenet_all_200_institutions.txt")
        self._load_kenet_file(kenet_file_path)

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
    
    def _load_kenet_file(self, file_path):
        """Parses the KENET text file, cleaning formatting artifacts, and injects domains."""
        if not os.path.exists(file_path):
            logging.warning(f"KENET file not found at {file_path}")
            return

        added_count = 0
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if '->' in line:
                    parts = line.split('->')
                    if len(parts) == 2:
                        uni_name = parts[0].lower().strip()
                        
                        # 1. Removes citations like [1] or [2] if they exist from AI outputs
                        uni_name = re.sub(r'\[.*?\]', '', uni_name).strip()
                        
                        # Note: If you were actually trying to remove single quotes ('), 
                        # you can uncomment the line below instead:
                        # uni_name = uni_name.replace("'", "").strip()
                        
                        # 2. Removes literal backslashes
                        uni_name = re.sub(r'\\', '', uni_name).strip()
                        
                        # 3. Cleans up extra whitespace
                        uni_name = re.sub(r'\s+', ' ', uni_name)
                        
                        url = parts[1].strip()
                        
                        if url.startswith('http'):
                            domain = urlparse(url).netloc.replace('www.', '')
                            if uni_name and uni_name not in self.domain_db:
                                self.domain_db[uni_name] = domain
                                added_count += 1
        
        if added_count > 0:
            logging.info(f"✅ Loaded {added_count} new verified domains from KENET file.")
            self._save_all()

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
    def _internal_navigation_crawl(self, root_domain, university_name, course_name):
        """
        Agentic Crawler: Groq decides which links to click until 
        the specific course page is found.
        """
        current_url = f"https://{root_domain}"
        visited = set()
        best_match_so_far = {"url": None, "score": 0}
        
        # We allow the AI to 'click' up to 5 times to find the deep page
        for click_depth in range(5):
            if current_url in visited:
                break
            visited.add(current_url)
            
            logging.info(f"🖱️ Click Depth {click_depth}: Navigating to {current_url}")
            
            try:
                # 1. Use Jina to get the content of the current 'click'
                jina_url = f"https://r.jina.ai/{current_url}"
                response = self.session.get(jina_url, timeout=15, verify=False)
                page_text = response.text
                
                # 2. Extract potential links (Jina returns Markdown, so we use Regex, NOT BeautifulSoup)
                markdown_links = re.findall(r'\[([^\]]+)\]\(([^\)]+)\)', page_text)
                
                available_links = []
                junk_paths = ['.pdf', '.jpg', '.png', 'facebook', 'twitter', 'login', 'webmail', 'portal']
                
                for link_text, extracted_url in markdown_links:
                    full_url = urljoin(current_url, extracted_url).split('#')[0].rstrip('/')
                    
                    # Clean the link text and ensure it stays on the target university's domain
                    clean_text = link_text.strip()
                    if root_domain in full_url and len(clean_text) > 2:
                        if not any(junk in full_url.lower() for junk in junk_paths):
                            available_links.append({"text": clean_text, "url": full_url})

                # Deduplicate links while preserving order
                unique_links = list({v['url']: v for v in available_links}.values())

                # 3. ASK THE AI: "Are we there yet? If not, where to next?"
                analysis = ai_navigator_audit(university_name, course_name, page_text, unique_links)
                
                # LOGIC: If AI found the 'Conclusive' page, we are DONE
                if analysis.get("status") == "FINAL_MATCH":
                    logging.info(f"🎯 AI SATISFIED! Final Page: {current_url}")
                    return current_url
                
                # LOGIC: If AI sees a better link to click, update current_url and loop again
                if analysis.get("status") == "KEEP_SEARCHING":
                    next_url = analysis.get("next_best_link")
                    score = analysis.get("current_page_score", 0)
                    
                    # Keep track of the best page seen in case we never find the 'perfect' one
                    if score > best_match_so_far["score"]:
                        best_match_so_far = {"url": current_url, "score": score}
                    
                    if next_url and next_url not in visited:
                        current_url = next_url
                        time.sleep(2) # Respectful delay between clicks
                        continue
                
                # Stop if AI is confused (DEAD_END) or no valid links returned
                break 

            except Exception as e:
                logging.error(f"⚠️ Navigation error at {current_url}: {e}")
                break

        return best_match_so_far["url"]
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
    The Funnel:
    1. Look up exact KENET domain.
    2. Agentic Crawl inside the official website.
    3. Fallback to AI Web Search ONLY if the crawler fails.
    """
    instance = get_healer()
    logging.info(f"🔍 Initializing search for: {university_name} - {course_name}")

    # STEP 1: Get the domain from the KENET list / JSON DB
    root_domain = instance._get_domain(university_name)

    # STEP 2: Use the exact domain to crawl the site directly
    if root_domain:
        logging.info(f"✅ KENET Domain found: {root_domain}. Launching Agentic Crawler...")
        found_deep_link = instance._internal_navigation_crawl(root_domain, university_name, course_name)
        
        if found_deep_link:
            return found_deep_link
        else:
            logging.warning(f"⚠️ Direct crawl missed the specific page. Falling back to external AI Search...")
            search_query = f'site:{root_domain} "{course_name}" requirements'
    else:
        logging.warning(f"⚠️ Domain unknown. Falling back to broad AI Search...")
        search_query = f'site:.ac.ke OR site:.edu "{university_name}" "{course_name}" requirements'

    # STEP 3: Fallback AI Search (DuckDuckGo + Jina AI Validator)
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(search_query, max_results=5)) 

            for result in results:
                candidate_url = result.get('href', '').lower()
                
                if any(bad in candidate_url for bad in ['facebook', 'twitter', 'kenyayote', 'advance-africa']):
                    continue
                    
                time.sleep(1.5)
                logging.info(f"🧐 AI Auditor checking fallback candidate: {candidate_url}")

                try:
                    jina_url = f"https://r.jina.ai/{candidate_url}"
                    response = requests.get(jina_url, timeout=30, verify=False)
                    
                    analysis = ai_course_validator(university_name, course_name, response.text, candidate_url)
                    
                    # Use your updated validator logic keys here
                    if analysis and analysis.get("is_correct_uni") and analysis.get("is_conclusive"):
                        logging.info(f"✅ AI VALIDATED FALLBACK SITE: {candidate_url}")
                        return candidate_url
                    else:
                        logging.warning(f"❌ AI Rejected (Content mismatch): {candidate_url}")

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