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
from duckduckgo_search import DDGS

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
# ==========================================
# 🧠 AI VALIDATION & RETRY LOGIC
# ==========================================

# Increased multiplier and minimum wait time to respect Groq limits
@retry(wait=wait_exponential(multiplier=2, min=10, max=120), stop=stop_after_attempt(5))
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
    time.sleep(4) # Increased delay to prevent 429s
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
    time.sleep(4) # Increased delay to prevent 429s
    
    optimized_text = page_text[:1500]
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
    2. If not, pick the BEST link from the 'Available Links' that will get us closer. Set status to "KEEP_SEARCHING".
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
        buffer = "" # Added a buffer to handle lines split across multiple lines
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                # Add the current line to our buffer
                buffer += " " + line.strip()
                
                # Only process if we have BOTH the arrow and an http link in our buffer
                if '->' in buffer and 'http' in buffer:
                    parts = buffer.split('->')
                    
                    if len(parts) >= 2:
                        uni_name = parts[0].lower().strip()
                        uni_name = re.sub(r'\[.*?\]', '', uni_name).strip()
                        uni_name = re.sub(r'\\', '', uni_name).strip()
                        uni_name = re.sub(r'\s+', ' ', uni_name)
                        
                        # Extract the URL part from the second half
                        url_part = parts[1].strip()
                        
                        # Find where the actual http starts (in case of weird formatting)
                        http_index = url_part.find('http')
                        if http_index != -1:
                            url = url_part[http_index:].split()[0] # Grab just the URL string
                            
                            domain = urlparse(url).netloc.replace('www.', '')
                            if uni_name and uni_name not in self.domain_db:
                                self.domain_db[uni_name] = domain
                                added_count += 1
                                
                    # Clear the buffer for the next university
                    buffer = "" 
        
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
    def clean_url(self, base, link):
        """Fixes broken links and skips non-web links like mailto:"""
        if not link or any(link.startswith(x) for x in ['mailto:', 'tel:', 'javascript:', '#', 'whatsapp:']):
            return None
        
        # Proper way to join URLs to avoid 'ac.kenode' errors
        return urljoin(base, link).split('#')[0]
    def _internal_navigation_crawl(self, root_domain, university_name, course_name):
        """
        Heuristic Crawler: Uses Best-First Search via a priority queue to rank 
        and click the most promising links to find the course page.
        """
        start_url = f"https://{root_domain}"
        visited = set()
        
        # Priority Queue: stores tuples of (score, url). Lower score = higher priority.
        # We start at 0 for the root domain.
        queue = []
        heapq.heappush(queue, (0, start_url))
        
        # Expanded junk filter to skip non-navigational or heavy files
        JUNK_KEYWORDS = [
            'login', 'portal', 'webmail', 'gallery', '.pdf', 'personnel', 
            'bio', 'graduation', 'brochure', 'team', 'staff', 'download', 
            'alumni', 'calendar', 'news', 'events', 'contact', '.jpg', '.png'
        ]

        # Target identifiers
        course_tokens = [token.lower() for token in course_name.split() if len(token) > 3]
        nav_keywords = ['academics', 'programmes', 'courses', 'undergraduate', 'postgraduate', 'faculties', 'schools', 'departments']
        success_keywords = ['curriculum', 'course units', 'syllabus', 'fee structure', 'duration', 'entry requirements']

        max_pages_to_visit = 25 # Hard cap to prevent infinite crawling
        pages_visited = 0

        while queue and pages_visited < max_pages_to_visit:
            current_score, current_url = heapq.heappop(queue)

            if current_url in visited:
                continue
            
            visited.add(current_url)
            pages_visited += 1
            
            logging.info(f"🖱️ Crawling ({pages_visited}/{max_pages_to_visit}) [Priority: {current_score}]: {current_url}")
            
            try:
                # Use self.session to leverage connection pooling (much faster than requests.get)
                response = self.session.get(current_url, timeout=15, verify=False)
                if response.status_code != 200:
                    continue
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # NOISE REDUCTION
                for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    element.decompose()
                
                clean_text = soup.get_text(separator=' ', strip=True).lower()
                page_title = soup.title.string.lower() if soup.title else ""
                
                # --- 1. EVALUATE IF CURRENT PAGE IS THE TARGET ---
                has_course_mentions = any(token in clean_text for token in course_tokens)
                has_title_match = any(token in page_title for token in course_tokens)
                has_details = any(keyword in clean_text for keyword in success_keywords)
                
                if (has_course_mentions and has_details) or (has_title_match and has_details):
                    logging.info(f"🎯 HEURISTIC MATCH FOUND at: {current_url}")
                    return current_url

              # --- 2. EXTRACT, SCORE, AND QUEUE NEW LINKS ---
                for a in soup.find_all('a', href=True):
                    raw_href = a['href']
                    clean_href = self.clean_url(current_url, raw_href) 
                    
                    # SAFETY FILTER: Must stay on the university domain and not be visited
                    if not clean_href or clean_href in visited or root_domain not in clean_href:
                        continue 

                    # 1. EXTRACT LINK TEXT
                    link_text = a.get_text().strip().lower()
                    url_lower = clean_href.lower()

                    # 2. SKIP JUNK (PDFs, Social Media, etc.)
                    if any(junk in url_lower for junk in JUNK_KEYWORDS):
                        continue

                    # 3. CHECK IF THIS IS THE TARGET (The "Gold" Match / Early Exit)
                    # If all core course tokens are in the link text or URL, we found it!
                    if all(token in link_text or token in url_lower for token in course_tokens):
                        logging.info(f"🎯 EARLY TARGET FOUND via link text: {clean_href}")
                        return clean_href 

                    # 4. CALCULATE PRIORITY SCORE
                    link_score = 100 
                    if any(nav in link_text or nav in url_lower for nav in nav_keywords):
                        link_score = 10  # High priority for academic/course links
                    elif 'faculty' in link_text or 'department' in link_text:
                        link_score = 50  

                    # 5. PUSH TO QUEUE
                    heapq.heappush(queue, (link_score, clean_href))

            except Exception as e:
                logging.warning(f"⚠️ Crawl Error at {current_url}: {e}")

                   # -- SCORING LOGIC --
                link_score = 100 # Default score (low priority)
                    
                   # --- 2. EXTRACT, SCORE, AND QUEUE NEW LINKS ---
                for a in soup.find_all('a', href=True):
                    raw_href = a['href']
                    
                    # Resolve relative URLs and strip anchors
                    clean_href = self.clean_url(current_url, raw_href) 
                    
                    # Safety Filter: Stay within the domain and avoid loops
                    if not clean_href or clean_href in visited or root_domain not in clean_href:
                        continue 

                    link_text = a.get_text().strip().lower()
                    url_lower = clean_href.lower()

                    # --- SCORING LOGIC ---
                    link_score = 100 

                    # 1. Exact course tokens IN THE URL (Super High Priority)
                    if any(token in url_lower for token in course_tokens):
                        link_score = 5 
                    # 2. Exact course tokens IN THE LINK TEXT (High Priority)
                    elif any(token in link_text for token in course_tokens):
                        link_score = 10
                    # 3. Academic navigation keywords (Medium Priority)
                    elif any(nav in link_text or nav in url_lower for nav in nav_keywords):
                        link_score = 30
                    # 4. General department/faculty links (Lower Priority)
                    elif 'faculty' in link_text or 'department' in link_text:
                        link_score = 50

                    # 5. PUSH TO QUEUE
                    # Fixed: Use 'clean_href' instead of 'full_url'
                    heapq.heappush(queue, (link_score, clean_href))

            except Exception as e:
                logging.warning(f"⚠️ Crawl Error at {current_url}: {e}")

        logging.info(f"🛑 Reached depth limit ({max_pages_to_visit} pages) without conclusive match.")
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
        
        # Inside _hunt_for_url (Precision Deep-Link Search)
        JUNK_KEYWORDS = ['.pdf', 'personnel', 'bio', 'graduation', 'brochure', 'team', 'staff', 'download']
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(precision_query, max_results=5))
                for r in results:
                    url = r.get('href', '')
                    if url and root_domain in url:
                        # Added junk filter here
                        if not any(bad in url.lower() for bad in JUNK_KEYWORDS):
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

# ==import time
import re
from duckduckgo_search import DDGS
import logging

def get_course_url(university_name, course_name):
    """
    The Funnel:
    1. Look up exact KENET domain.
    2. Agentic Crawl inside the official website.
    3. Fallback to AI Web Search ONLY if the crawler fails.
    """
    instance = get_healer()
    logging.info(f"🔍 Initializing search for: {university_name} - {course_name}")

    root_domain = instance._get_domain(university_name)

    # STEP 1 & 2: KENET Domain & Agentic Crawl
    if root_domain:
        logging.info(f"✅ KENET Domain found: {root_domain}. Launching Agentic Crawler...")
        found_deep_link = instance._internal_navigation_crawl(root_domain, university_name, course_name)
        
        if found_deep_link:
            return found_deep_link
        else:
            logging.warning(f"⚠️ Direct crawl missed the specific page. Falling back to external AI Search...")
    else:
        logging.warning(f"⚠️ Domain unknown. Falling back to broad AI Search...")

    # --- PREPARE FOR FALLBACK AI SEARCH ---
    # 1. Normalize the course name (ignore generic words, focus on the unique subject)
    raw_words = re.sub(r'[^a-z0-9\s]', ' ', course_name.lower()).split()
    generic_words = {'bachelor', 'science', 'arts', 'degree', 'diploma', 'certificate', 'education', 'with', 'and', 'the', 'of', 'programme', 'undergraduate'}
    core_course_words = [w for w in raw_words if len(w) > 3 and w not in generic_words]
    
    if not core_course_words: 
        core_course_words = [w for w in raw_words if len(w) > 3]

    JUNK_KEYWORDS = ['.pdf', 'personnel', 'bio', 'graduation', 'brochure', 'team', 'staff', 'download', 'news', 'events', 'blog', 'portal', 'login', 'elearning', 'library', 'mail', 'contacts', 'account', 'studentportal']
    GOOD_KEYWORDS = ['course', 'program', 'undergraduate', 'bachelor', 'bsc', 'ba', 'degree', 'academics', 'admission', 'faculty', 'school']

    # 2. Setup Search Variations (Retry Loop Queries)
    search_queries = [
        f"{course_name} at {university_name}",
        f"{course_name} undergraduate course {university_name}"
    ]
    if root_domain:
        clean_domain = root_domain.replace("https://", "").replace("http://", "").split('/')[0]
        search_queries.append(f"{course_name} site:{clean_domain}")

    # STEP 3: Fallback AI Search (DuckDuckGo + Validator Loop)
    try:
        with DDGS() as ddgs:
            for attempt, query in enumerate(search_queries):
                logging.info(f"🔍 AI Search Attempt {attempt + 1}: '{query}'")
                
                results = list(ddgs.text(query, max_results=5))
                if not results:
                    time.sleep(1) # Prevent rate limiting
                    continue

                # Pass 1: Strict Validation (Perfect Match)
                for result in results:
                    candidate_url = result.get('href', '').lower()
                    
                    # Block junk pages instantly
                    if any(bad in candidate_url for bad in ['facebook', 'twitter', 'kenyayote', 'advance-africa', 'kuccps'] + JUNK_KEYWORDS):
                        continue
                        
                    # Normalize URL to connect broken words (agri-business -> agribusiness)
                    normalized_url = re.sub(r'[-_]', '', candidate_url)
                    
                    has_core_word = any(cw in normalized_url for cw in core_course_words)
                    has_good_word = any(gw in normalized_url for gw in GOOD_KEYWORDS)
                    
                    if has_core_word and has_good_word:
                        logging.info(f"🎯 Perfect Fallback Match found: {result.get('href')}")
                        return result.get('href') 

                # Pass 2: Lenient Validation (Just the core course words)
                for result in results:
                    candidate_url = result.get('href', '').lower()
                    
                    if any(bad in candidate_url for bad in ['facebook', 'twitter', 'kenyayote', 'advance-africa', 'kuccps'] + JUNK_KEYWORDS):
                        continue
                        
                    normalized_url = re.sub(r'[-_]', '', candidate_url)
                    
                    if any(cw in normalized_url for cw in core_course_words):
                        logging.info(f"✅ Good Fallback Match found: {result.get('href')}")
                        return result.get('href')

                # Wait before trying the next search variation
                time.sleep(1)

        logging.warning(f"⚠️ All {len(search_queries)} AI Search attempts failed. Defaulting to homepage.")

    except Exception as e:
        logging.error(f"❌ Dux Search Error: {e}")
    
    # Final safety net: Return the university homepage
    if root_domain:
        if not root_domain.startswith("http"):
            return f"https://{root_domain}"
        return root_domain
        
    return None
#========================================
# 🛠️ FINAL INITIALIZATION (Bottom of file)
# ==========================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DIR = os.path.join(BASE_DIR, '..', 'data')

if not os.path.exists(TARGET_DIR):
    os.makedirs(TARGET_DIR)

healer = AutoHealer(target_folder=TARGET_DIR)