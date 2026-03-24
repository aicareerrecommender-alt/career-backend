import os
import re
import json
import logging
import requests
import urllib3
import threading
import concurrent.futures
from urllib.parse import urlparse

# Handle the new DDGS library name changes gracefully
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from duckduckgo_search.exceptions import DuckDuckGoSearchException

# Import your Groq client
from .ai_engines import client_groq 

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

def ai_course_validator(uni_name, course_name, scraped_text, source_url):
    """
    The Upgraded AI Agent: Features strict Anti-Crossover logic
    to prevent competitor universities (like Zetech) from hijacking searches.
    """
    if not client_groq: 
        return False
    
    prompt = f"""
    Requested Institution: {uni_name}
    Requested Course: {course_name}
    Found URL: {source_url}

    You are a strict Academic Auditor preventing cross-contamination.
    CRITICAL RULE: Search engines sometimes return the WRONG university's website. 
    If the Requested Institution is '{uni_name}', but the URL or Webpage Text belongs to a DIFFERENT university (e.g., you found Zetech or KCA instead of University of Nairobi), THIS IS A FATAL MISMATCH. 

    Evaluate these two conditions:
    1. INSTITUTION MATCH: Does this URL and webpage text STRICTLY and ONLY belong to the OFFICIAL '{uni_name}'? (Return false immediately if it belongs to a competitor, news site, or directory).
    2. COURSE MATCH: Is the specific course '{course_name}' explicitly offered there?
    
    Answer STRICTLY with JSON: {{"is_official_site": true/false, "is_valid_course": true/false, "reason": "short explanation"}}.
    
    Webpage Text:
    {scraped_text[:6000]}
    """
    
    try:
        res = client_groq.chat.completions.create(
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", 
            temperature=0.1
        )
        data = json.loads(res.choices[0].message.content)
        
        if data.get("is_official_site") and data.get("is_valid_course"):
            return True
        else:
            logging.warning(f"🤖 AI REJECTED HIJACK ATTEMPT -> Reason: {data.get('reason')}")
            return False
            
    except Exception as e: 
        logging.error(f"AI Validator Error: {e}")
        return False

class AutoHealer:
    def __init__(self, target_folder="data"):
        self.db_path = os.path.join(target_folder, "academic_urls.json")
        os.makedirs(target_folder, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        
        self.db_lock = threading.Lock() 
        self.db = self._load_db()

        # 🔥 LAYER 1: Hardcoded lookup for top universities to guarantee 100% accuracy
        self.known_domains = {
            "university of nairobi": "uonbi.ac.ke", "kenyatta university": "ku.ac.ke",
            "jomo kenyatta": "jkuat.ac.ke", "egerton": "egerton.ac.ke", "moi university": "mu.ac.ke",
            "maseno": "maseno.ac.ke", "strathmore": "strathmore.edu", "kisii": "kisiiuniversity.ac.ke",
            "masinde muliro": "mmust.ac.ke", "laikipia": "laikipia.ac.ke", "chuka": "chuka.ac.ke",
            "dedan kimathi": "dkut.ac.ke", "technical university of kenya": "tukenya.ac.ke",
            "technical university of mombasa": "tum.ac.ke", "kabianga": "kabianga.ac.ke",
            "karatina": "karatina.ac.ke", "kca university": "kca.ac.ke", "mount kenya": "mku.ac.ke",
            "kabarak": "kabarak.ac.ke", "zetech": "zetech.ac.ke", "daystar": "daystar.ac.ke",
            "catholic university": "cuea.edu", "machakos": "mksu.ac.ke", "meru": "must.ac.ke",
            "pwani": "pu.ac.ke", "kibabii": "kibu.ac.ke", "garissa": "gau.ac.ke", "seku": "seku.ac.ke",
            "moringa": "moringaschool.com", "alx": "alxafrica.com", "kmtc": "kmtc.ac.ke",
            "kenya medical training": "kmtc.ac.ke"
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
        """Resolves the official domain using the Cache -> KUCCPS -> DDGS pipeline."""
        uni_lower = university_name.lower()
        for name, domain in self.known_domains.items():
            if name in uni_lower: return domain
                
        try:
            with DDGS() as ddgs:
                kuccps_query = f'site:students.kuccps.net "{university_name}" website'
                for r in list(ddgs.text(kuccps_query, max_results=3)):
                    match = re.search(r'([a-zA-Z0-9\-]+\.(?:ac\.ke|edu\.ke|sc\.ke|edu))', r.get('body', '').lower())
                    if match: return match.group(1)
        except Exception: pass
            
        try:
            with DDGS() as ddgs:
                for r in list(ddgs.text(f'"{university_name}" official website kenya', max_results=3)):
                    found_url = r.get('href', '')
                    if "bing.com" not in found_url and any(ext in found_url for ext in [".ac.ke", ".edu", ".sc.ke", ".org"]):
                        return urlparse(found_url).netloc
        except Exception: pass
            
        return None

    def _hunt_for_url(self, university_name, course_name):
        # --- CACHE CHECK ---
        if university_name in self.db and course_name in self.db[university_name]:
            cached_url = self.db[university_name][course_name]
            logging.info(f"⚡ CACHE HIT! Loaded {university_name} -> {course_name} from DB.")
            return cached_url, True

        logging.info(f"🎯 Strict Deep-Link Hunting: {university_name} -> {course_name}")
        root_domain = self._get_domain(university_name)

        if not root_domain:
            logging.warning(f"⚠️ Could not isolate official domain for {university_name}.")
            return None, False

        # --- GATHER URLS (DEEP LINK SNIPER) ---
        precision_query = f'site:{root_domain} "{course_name}"'
        urls_to_check = []
        
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(precision_query, max_results=5))
                for r in results:
                    url = r.get('href', '')
                    if url and "bing.com" not in url and root_domain in url and not any(bad in url.lower() for bad in ['/login', '.pdf', '/download']):
                        urls_to_check.append(url)
        except Exception as e:
            logging.error(f"❌ DDGS Course Search Error: {e}")

        if not urls_to_check:
            logging.warning(f"❌ Failed to find deep-link for {course_name} strictly inside {root_domain}")
            return None, False

        # --- THE SPEED UPGRADE: JINA + CONCURRENT SCRAPING ---
        def verify_single_url(target_url):
            logging.info(f"📄 Testing promising academic URL via Jina: {target_url}...")
            try:
                jina_url = f"https://r.jina.ai/{target_url}"
                res_page = self.session.get(jina_url, timeout=8, verify=False)
                clean_markdown_text = res_page.text 
                
                is_valid = ai_course_validator(university_name, course_name, clean_markdown_text, target_url)
                
                if is_valid:
                    logging.info(f"🤝 SUCCESS! AI verified exact course page: {target_url}")
                    return target_url
                return None
            except Exception as e:
                logging.warning(f"⚠️ Scraping failed for {target_url}: {e}")
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_url = {executor.submit(verify_single_url, url): url for url in urls_to_check}
            
            # as_completed yields results the exact millisecond they finish downloading
            for future in concurrent.futures.as_completed(future_to_url):
                approved_url = future.result()
                if approved_url:
                    # The FIRST url to get approved wins! We stop waiting for the rest.
                    if university_name not in self.db:
                        self.db[university_name] = {}
                    self.db[university_name][course_name] = approved_url
                    self._save_db()
                    return approved_url, True

        return None, False

# Initialize the healer so app.py can import it easily
TARGET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
healer = AutoHealer(target_folder=TARGET_DIR)