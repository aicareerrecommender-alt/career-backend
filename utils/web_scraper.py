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

# Make sure this matches your actual import path
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

# ==========================================
# 🧠 AI VALIDATION & RETRY LOGIC
# ==========================================

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
    time.sleep(4) 
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
        self.domain_db_path = os.path.join(target_folder, "school_domains.json")
        self.course_db_path = os.path.join(target_folder, "course_urls.json")
        
        os.makedirs(target_folder, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        })
        self.db_lock = threading.Lock() 

        self.domain_db = self._load_json(self.domain_db_path)
        self.course_db = self._load_json(self.course_db_path)
        
        self.db = self.course_db  
        self.db_path = self.course_db_path 

        self.known_domains = {
            "university of nairobi": "uonbi.ac.ke", "kenyatta university": "ku.ac.ke",
            "jomo kenyatta": "jkuat.ac.ke", "egerton": "egerton.ac.ke", "moi university": "mu.ac.ke",
            "maseno": "maseno.ac.ke", "strathmore": "strathmore.edu", "kisii": "kisiiuniversity.ac.ke",
            "masinde muliro": "mmust.ac.ke", "technical university of kenya": "tukenya.ac.ke",
            "kca university": "kca.ac.ke", "mount kenya": "mku.ac.ke", "zetech": "zetech.ac.ke"
        }
        
        for name, domain in self.known_domains.items():
            if name not in self.domain_db:
                self.domain_db[name] = domain
                
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
        with self.db_lock:
            try:
                with open(self.domain_db_path, 'w', encoding='utf-8') as f:
                    json.dump(self.domain_db, f, indent=4)
                with open(self.course_db_path, 'w', encoding='utf-8') as f:
                    json.dump(self.course_db, f, indent=4)
            except Exception as e:
                logging.error(f"Failed to save databases: {e}")
    
    def _load_kenet_file(self, file_path):
        if not os.path.exists(file_path):
            logging.warning(f"KENET file not found at {file_path}")
            return

        added_count = 0
        buffer = "" 
        
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                buffer += " " + line.strip()
                if '->' in buffer and 'http' in buffer:
                    parts = buffer.split('->')
                    if len(parts) >= 2:
                        uni_name = parts[0].lower().strip()
                        uni_name = re.sub(r'\[.*?\]', '', uni_name).strip()
                        uni_name = re.sub(r'\\', '', uni_name).strip()
                        uni_name = re.sub(r'\s+', ' ', uni_name)
                        
                        url_part = parts[1].strip()
                        http_index = url_part.find('http')
                        if http_index != -1:
                            url = url_part[http_index:].split()[0] 
                            domain = urlparse(url).netloc.replace('www.', '')
                            if uni_name and uni_name not in self.domain_db:
                                self.domain_db[uni_name] = domain
                                added_count += 1
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
        if not link or any(link.startswith(x) for x in ['mailto:', 'tel:', 'javascript:', '#', 'whatsapp:']):
            return None
        return urljoin(base, link).split('#')[0]

    def _internal_navigation_crawl(self, root_domain, university_name, course_name):
        """Heuristic Crawler Fallback"""
        start_url = f"https://{root_domain}"
        visited = set()
        queue = []
        heapq.heappush(queue, (0, start_url))
        
        JUNK_KEYWORDS = [
            'login', 'portal', 'webmail', 'gallery', '.pdf', 'personnel', 
            'bio', 'graduation', 'brochure', 'team', 'staff', 'download', 
            'alumni', 'calendar', 'news', 'events', 'contact', '.jpg', '.png'
        ]

        course_tokens = [token.lower() for token in course_name.split() if len(token) > 3]
        nav_keywords = ['academics', 'programmes', 'courses', 'undergraduate', 'postgraduate', 'faculties', 'schools', 'departments']
        success_keywords = ['curriculum', 'course units', 'syllabus', 'fee structure', 'duration', 'entry requirements']

        max_pages_to_visit = 25 
        pages_visited = 0

        while queue and pages_visited < max_pages_to_visit:
            current_score, current_url = heapq.heappop(queue)

            if current_url in visited:
                continue
            
            visited.add(current_url)
            pages_visited += 1
            
            logging.info(f"🖱️ Crawling ({pages_visited}/{max_pages_to_visit}) [Priority: {current_score}]: {current_url}")
            
            try:
                response = self.session.get(current_url, timeout=15, verify=False)
                if response.status_code != 200:
                    continue
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
                for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
                    element.decompose()
                
                clean_text = soup.get_text(separator=' ', strip=True).lower()
                page_title = soup.title.string.lower() if soup.title else ""
                
                has_course_mentions = any(token in clean_text for token in course_tokens)
                has_title_match = any(token in page_title for token in course_tokens)
                has_details = any(keyword in clean_text for keyword in success_keywords)
                
                if (has_course_mentions and has_details) or (has_title_match and has_details):
                    logging.info(f"🎯 HEURISTIC MATCH FOUND at: {current_url}")
                    return current_url

                for a in soup.find_all('a', href=True):
                    raw_href = a['href']
                    clean_href = self.clean_url(current_url, raw_href) 
                    
                    if not clean_href or clean_href in visited or root_domain not in clean_href:
                        continue 

                    link_text = a.get_text().strip().lower()
                    url_lower = clean_href.lower()

                    if any(junk in url_lower for junk in JUNK_KEYWORDS):
                        continue

                    link_score = 100 
                    if any(token in url_lower for token in course_tokens):
                        link_score = 5 
                    elif any(token in link_text for token in course_tokens):
                        link_score = 10
                    elif any(nav in link_text or nav in url_lower for nav in nav_keywords):
                        link_score = 30
                    elif 'faculty' in link_text or 'department' in link_text:
                        link_score = 50

                    heapq.heappush(queue, (link_score, clean_href))

            except Exception as e:
                logging.warning(f"⚠️ Crawl Error at {current_url}: {e}")

        logging.info(f"🛑 Reached depth limit ({max_pages_to_visit} pages) without conclusive match.")
        return None

    def _hunt_for_url(self, university_name, course_name):
        if university_name in self.db and course_name in self.db[university_name]:
            return self.db[university_name][course_name], True

        root_domain = self._get_domain(university_name)
        if not root_domain:
            return None, False

        precision_query = f'site:{root_domain} "{course_name}"'
        urls_to_check = []
        JUNK_KEYWORDS = ['.pdf', 'personnel', 'bio', 'graduation', 'brochure', 'team', 'staff', 'download']
        
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(precision_query, max_results=5))
                for r in results:
                    url = r.get('href', '')
                    if url and root_domain in url:
                        if not any(bad in url.lower() for bad in JUNK_KEYWORDS):
                            urls_to_check.append(url)
        except Exception as e:
            logging.error(f"❌ Search Error: {e}")

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

        logging.warning(f"🕵️ External search failed. Starting internal navigation...")
        internal_url = self._internal_navigation_crawl(root_domain, university_name, course_name)
        
        if internal_url:
            if university_name not in self.db: self.db[university_name] = {}
            self.db[university_name][course_name] = internal_url
            self._save_db()
            return internal_url, True
             
        homepage = f"https://{root_domain}"
        return homepage, False


# ==========================================
# 🌐 THE MAIN FUNNEL: GROQ PRIMARY -> PEEK -> CRAWLER
# ==========================================
def verify_course_page(url, course_name):
    """
    The 'Peek' Validator: 
    Checks if a URL actually contains course details (modules, fees, units)
    and matches the course keywords.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        # verify=False is often necessary for university sites with outdated SSL certs
        response = requests.get(url, headers=headers, timeout=10, verify=False, allow_redirects=True)
        
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            page_text = soup.get_text().lower()
            
            # 1. Academic Markers: Does the page look like a course description?
            academic_markers = ["module", "unit", "curriculum", "syllabus", "requirement", "fees", "duration", "admission"]
            has_academic_context = any(marker in page_text for marker in academic_markers)
            
            # 2. Keyword Match: Does it actually mention the subject?
            ignore_words = {"bsc", "bachelor", "of", "in", "degree", "program", "course", ".", ",", "-"}
            course_keywords = [word.lower() for word in re.findall(r'\w+', course_name) if word.lower() not in ignore_words]
            match_count = sum(1 for kw in course_keywords if kw in page_text)
            
            # STRICT LOGIC: Must have academic context AND at least 50% of the keywords
            if has_academic_context and (match_count >= len(course_keywords) * 0.5):
                return True
    except Exception as e:
        logging.warning(f"⚠️ Verification failed for {url}: {e}")
    
    return False

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(3))
def get_course_url(university_name, course_name):
    """
    STRICT MODE FUNNEL:
    Returns the URL only if it is verified. Otherwise returns None.
    """
    instance = get_healer()
    logging.info(f"🔍 Strict search initialized: {university_name} - {course_name}")

    root_domain = instance._get_domain(university_name)

    # ==========================================
    # STEP 1: GROQ COMPOUND (PRIMARY)
    # ==========================================
    logging.info("🧠 Attempting Groq Compound Search...")
    try:
        prompt = (
            f"Find the direct official undergraduate course URL for '{course_name}' at '{university_name}'. "
            f"Target domain: {root_domain if root_domain else 'official university site'}. "
            "Return ONLY the raw URL string."
        )

        response = client_groq.chat.completions.create(
            model="groq/compound", 
            messages=[{"role": "user", "content": prompt}],
            temperature=0 
        )

        found_url = re.sub(r'[`"\'\s]', '', response.choices[0].message.content.strip())

        if found_url.startswith("http"):
            if verify_course_page(found_url, course_name):
                logging.info(f"🎯 SUCCESS: Groq link verified: {found_url}")
                return found_url
            else:
                logging.warning(f"❌ Groq found a link, but it failed verification: {found_url}")

    except Exception as e:
        logging.error(f"❌ Groq Compound Search Failed: {e}")

    # ==========================================
    # STEP 2: CRAWLER FALLBACK (STRICT)
    # ==========================================
    if root_domain:
        logging.warning("🕸️ Groq failed. Starting Agentic Crawler fallback...")
        crawler_url = instance._internal_navigation_crawl(root_domain, university_name, course_name)
        
        if crawler_url:
            if verify_course_page(crawler_url, course_name):
                logging.info(f"🎯 SUCCESS: Crawler link verified: {crawler_url}")
                return crawler_url
            else:
                logging.warning(f"❌ Crawler found a link, but it failed verification: {crawler_url}")

    # ==========================================
    # STEP 3: TERMINATE (NO FALLBACK TO HOMEPAGE)
    # ==========================================
    logging.error(f"🛑 No verified match found for {course_name} at {university_name}.")
    return None
  
# ==========================================
# 🚀 DYNAMIC 5-LINK RECOMMENDER LOOP
# ==========================================

def generate_dynamic_universities(course_name, amount_needed, exclude_list=None):
    """Asks Groq to dynamically generate a list of Kenyan universities."""
    if exclude_list is None:
        exclude_list = []
    try:
        exclude_str = ", ".join(exclude_list)
        exclude_instruction = f" DO NOT include any of these: {exclude_str}." if exclude_str else ""
        
        prompt = (
            f"List {amount_needed} universities in Kenya that offer a bachelor's degree in '{course_name}'."
            f"{exclude_instruction}"
            " Return ONLY a comma-separated list of the exact university names. No extra text."
        )
        
        response = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant", 
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        
        raw_list = response.choices[0].message.content.split(',')
        return [u.strip() for u in raw_list if u.strip()]
    except Exception as e:
        logging.error(f"Failed to generate universities: {e}")
        return []

def get_guaranteed_five(course_name):
    """100% Dynamic Loop: Generates universities on the fly and verifies exactly 5."""
    verified_results = []
    tried_universities = set()
    
    logging.info(f"🤖 Asking Groq to find universities in Kenya for: {course_name}...")
    current_queue = generate_dynamic_universities(course_name, amount_needed=8)
    
    while len(verified_results) < 5:
        if not current_queue:
            needed = 5 - len(verified_results)
            logging.warning(f"⚠️ Need {needed} more! Asking Groq for a fresh batch...")
            
            new_batch = generate_dynamic_universities(course_name, amount_needed=needed + 3, exclude_list=list(tried_universities))
            
            if not new_batch:
                logging.error("🛑 Groq couldn't find any more universities. Stopping.")
                break
            current_queue.extend(new_batch)

        uni_to_try = current_queue.pop(0)
        
        if uni_to_try in tried_universities:
            continue
            
        tried_universities.add(uni_to_try)
        
        logging.info(f"🔍 Testing Groq's suggestion: {uni_to_try}...")
        url = get_course_url(uni_to_try, course_name) # Calls the scraper function right above it!
        
        if url:
            # Format strictly for what the frontend expects
            verified_results.append({
                "name": uni_to_try, 
                "website_url": url,
                "verified_offering": True,
                "requirements_met": [{"subject": "General", "required": "Check Website", "status": "Pending"}]
            })
            logging.info(f"✅ VERIFIED ({len(verified_results)}/5): {uni_to_try}")

    return verified_results