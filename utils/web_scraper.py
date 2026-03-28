import os
import re
import json
import logging
import time
import requests
import urllib3
import threading
import warnings
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import heapq
from duckduckgo_search import DDGS
from tenacity import retry, stop_after_attempt, wait_exponential

# Import your Groq client (ensure this matches your project structure)
from .ai_engines import client_groq 

# Suppress warnings and background logging clutter
warnings.filterwarnings("ignore", category=RuntimeWarning, module="duckduckgo_search")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

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
            return

        added_count = 0
        buffer = ""
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                buffer += " " + line.strip()
                if '->' in buffer and 'http' in buffer:
                    parts = buffer.split('->')
                    if len(parts) >= 2:
                        uni_name = re.sub(r'\[.*?\]|\\|\s+', ' ', parts[0].lower()).strip()
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
            self._save_all()

    def _get_domain(self, university_name):
        uni_lower = university_name.lower().strip()
        if uni_lower in self.domain_db:
            return self.domain_db[uni_lower]
                
        try:
            with DDGS() as ddgs:
                query = f'official website "{university_name}" Kenya .ac.ke'
                for r in list(ddgs.text(query, max_results=3)):
                    href = r.get('href', '').lower()
                    match = re.search(r'([a-zA-Z0-9\-]+\.(?:ac\.ke|edu\.ke|sc\.ke|edu))', href)
                    if match:
                        found_domain = match.group(1)
                        self.domain_db[uni_lower] = found_domain
                        self._save_all() 
                        return found_domain
        except Exception: pass
        return None

    def clean_url(self, base, link):
        if not link or any(link.startswith(x) for x in ['mailto:', 'tel:', 'javascript:', '#', 'whatsapp:']):
            return None
        return urljoin(base, link).split('#')[0]

    def _internal_navigation_crawl(self, root_domain, university_name, course_name):
        start_url = f"https://{root_domain}"
        visited = set()
        queue = []
        heapq.heappush(queue, (0, start_url))
        
        JUNK_KEYWORDS = ['.pdf', 'login', 'portal', 'webmail', 'gallery', 'personnel', 'bio', 'brochure', 'staff', 'download', 'calendar', 'events', 'contact', '.jpg', '.png']
        nav_keywords = ['academics', 'programmes', 'courses', 'undergraduate', 'faculties', 'schools', 'departments']
        success_keywords = ['curriculum', 'course units', 'syllabus', 'fee structure', 'duration', 'entry requirements']

        # --- SMART TOKENIZER ---
        generic_words = {'bachelor', 'science', 'arts', 'degree', 'program', 'programme', 'diploma', 'certificate', 'course', 'of', 'in'}
        raw_tokens = [token.lower() for token in course_name.split() if len(token) > 3]
        core_tokens = [t for t in raw_tokens if t not in generic_words]
        if not core_tokens:
            core_tokens = raw_tokens 

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
                
                # --- 1. EVALUATE (USING CORE TOKENS ONLY) ---
                has_course_mentions = all(token in clean_text for token in core_tokens)
                has_details = any(keyword in clean_text for keyword in success_keywords)
                
                if has_course_mentions and has_details:
                    logging.info(f"🎯 SPECIFIC TARGET FOUND: {current_url}")
                    return current_url

                # --- 2. EXTRACT & QUEUE LINKS ---
                for a in soup.find_all('a', href=True):
                    raw_href = a['href']
                    clean_href = self.clean_url(current_url, raw_href) 
                    
                    if not clean_href or clean_href in visited or root_domain not in clean_href:
                        continue 

                    link_text = a.get_text().strip().lower()
                    url_lower = clean_href.lower()

                    if any(junk in url_lower for junk in JUNK_KEYWORDS):
                        continue

                    # Early Exit: If the link text literally IS the core course
                    if all(token in link_text or token in url_lower for token in core_tokens):
                        logging.info(f"🎯 EARLY TARGET FOUND via link: {clean_href}")
                        return clean_href 

                    link_score = 100 
                    if any(nav in link_text or nav in url_lower for nav in nav_keywords):
                        link_score = 10  
                    elif 'faculty' in link_text or 'department' in link_text:
                        link_score = 50  

                    heapq.heappush(queue, (link_score, clean_href))

            except Exception as e:
                logging.warning(f"⚠️ Crawl Error at {current_url}: {e}")

        return None

# ==========================================
# 🌐 LAZY LOADING & MAIN FUNNEL
# ==========================================

healer = None
def get_healer():
    global healer
    if healer is None:
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        TARGET_DIR = os.path.join(BASE_DIR, '..', 'data')
        healer = AutoHealer(target_folder=TARGET_DIR)
    return healer

@retry(wait=wait_exponential(multiplier=2, min=4, max=30), stop=stop_after_attempt(3))
def get_course_url(university_name, course_name):
    """The Main Funnel: Domain -> Primary Groq AI Search -> Crawler Fallback"""
    instance = get_healer()
    logging.info(f"🔍 Initializing search for: {university_name} - {course_name}")

    root_domain = instance._get_domain(university_name)

    # ==========================================
    # 1. PRIMARY SEEKER: GROQ AI
    # ==========================================
    logging.info("🧠 Asking Groq AI to find the link directly...")
    try:
        prompt = (
            f"Find the direct official undergraduate course URL for '{course_name}' at '{university_name}'. "
            f"Official Domain Reference: {root_domain if root_domain else 'Find via search'}. "
            "Return ONLY the raw URL. Filter out news articles, PDFs, or third-party blogs. "
            "Output format: Just the URL string starting with http."
        )

        response = client_groq.chat.completions.create(
            # Change this to "groq/compound" if your Groq key has search tools enabled
            model="llama-3.1-8b-instant", 
            messages=[
                {"role": "system", "content": "You are an expert academic researcher. Output only the final URL."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )

        # Clean the response of spaces, quotes, and markdown
        found_url = re.sub(r'[`"\'\s]', '', response.choices[0].message.content.strip())

        # Hallucination Check: Ensure it's a real link on the correct university domain
        if found_url.startswith("http"):
            if root_domain and root_domain not in found_url:
                logging.warning(f"⚠️ Groq hallucinated an off-domain link: {found_url}. Rejecting.")
            else:
                logging.info(f"🎯 Groq Successfully Found: {found_url}")
                return found_url
        else:
            logging.warning(f"⚠️ Groq returned invalid text: {found_url}")

    except Exception as e:
        logging.error(f"❌ Groq Search Failed: {e}")

    # ==========================================
    # 2. FALLBACK: HEURISTIC CRAWLER
    # ==========================================
    if root_domain:
        logging.warning(f"🕸️ Groq failed. Falling back to Heuristic Crawler...")
        found_link = instance._internal_navigation_crawl(root_domain, university_name, course_name)
        if found_link:
            return found_link
        logging.warning("⚠️ Crawler also missed the specific page.")
    
    # ==========================================
    # 3. FINAL SAFETY NET
    # ==========================================
    logging.info("🔙 Falling back to root domain.")
    return f"https://{root_domain}" if root_domain else None