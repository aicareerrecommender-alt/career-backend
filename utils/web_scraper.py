import os
import re
import json
import logging
import requests
import urllib3
import concurrent.futures
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS
from .ai_engines import client_groq  # Adjust import based on your actual structure

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

def ai_kuccps_auditor(uni_name, course_name, scraped_text, source_url, verified=False):
    if not client_groq: return {"ai_approved": False, "reason": "Groq client offline"}
    status = "VERIFIED ON KUCCPS PORTAL." if verified else "NOT FOUND ON KUCCPS PORTAL. STRICT SCRUTINY REQUIRED."
    prompt = f"Institution: {uni_name}\nTarget Course: {course_name}\nSource URL: {source_url}\nKUCCPS Status: {status}\n\nRules:\n1. Course must be clearly listed.\n2. Must be a dedicated course/department page. Reject general homepages/news.\n3. Verify Institution matches Domain.\n\nScraped Text:\n{scraped_text[:8000]}"
    try:
        res = client_groq.chat.completions.create(
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Output strictly JSON format: {'ai_approved': boolean, 'reason': 'string'}"}, 
                {"role": "user", "content": prompt}
            ],
            model="llama-3.1-8b-instant", temperature=0.1
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e: 
        return {"ai_approved": False, "reason": "AI offline"}

class InstitutionValidator:
    def __init__(self):
        self.official_tlds = ['.ac.ke', '.sc.ke', '.edu.ke', '.go.ke']
        self.whitelist = ["strathmore.edu", "usiu.ac.ke", "kmtc.ac.ke", "kuccps.net"]

    def is_legitimate(self, url, uni_name):
        try:
            domain = urlparse(url).netloc.lower().replace('www.', '')
            if not any(domain.endswith(t) for t in self.official_tlds) and domain not in self.whitelist: 
                return False, f"Not official TLD: {domain}"

            res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5, verify=False)
            if res.status_code not in [200, 401, 403]: return False, "Dead Link"

            page_text = BeautifulSoup(res.text, 'html.parser').get_text().lower()
            clean_name = re.sub(r'\b(university|college|national|polytechnic|institute|of|the|and|for)\b', '', uni_name, flags=re.IGNORECASE).strip()
            all_words = [w.lower() for w in clean_name.split() if len(w) > 2] + [w.lower() for w in re.findall(r'\b[A-Z]{3,}\b', uni_name)]
            
            if not any(w in domain or w in page_text for w in all_words): return False, "Identity Mismatch"
            return True, "Verified Official Site"
        except Exception as e: return False, str(e)

class AutoHealer:
    def __init__(self, target_folder):
        self.db_path = os.path.join(target_folder, "academic_urls.json")
        os.makedirs(target_folder, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        self.validator = InstitutionValidator()
        self.url_cache = self._load_db()

    def _load_db(self):
        try:
            if os.path.exists(self.db_path):
                with open(self.db_path, 'r', encoding='utf-8') as f: return json.load(f)
        except: pass
        return {}

    def _save_db(self):
        with open(self.db_path, 'w', encoding='utf-8') as f: 
            json.dump(self.url_cache, f, indent=4)

    def get_verified_url(self, uni_name, course_name):
        """Swarm strategy: 3 Workers seek the course, then AI strictly audits the best result."""
        core = re.sub(r'^(Bachelor of Science in|Bachelor of Arts in|Bachelor of|Diploma in|Certificate in|Artisan in)\s+', '', course_name, flags=re.IGNORECASE).strip()
        
        # 1. SWARM MISSIONS (Focus on depth: syllabus, department, overview)
        search_queries = [
            f'site:ac.ke "{uni_name}" "{core}" syllabus units',
            f'site:ac.ke "{uni_name}" "{core}" department faculty',
            f'site:ac.ke "{uni_name}" "{core}" program overview'
        ]

        def search_worker(query):
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=2))
                    for r in results:
                        url = r.get('href', '')
                        # Secure Filter: Must be .ac.ke and NOT a login portal
                        if url and ".ac.ke" in url and not any(x in url.lower() for x in ['login', 'portal', 'index']):
                            return url
            except: pass
            return None

        # 2. DEPLOY THE 3-WORKER SWARM
        best_candidates = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(search_worker, q): q for q in search_queries}
            for future in concurrent.futures.as_completed(futures):
                if res := future.result(): 
                    best_candidates.append(res)

        # 3. STRICT AI AUDIT (The Judge)
        if best_candidates:
            target_url = best_candidates[0] # Take the fastest/best result from the swarm
            try:
                # Read page via Jina AI
                text = self.session.get(f"https://r.jina.ai/{target_url}", timeout=8, verify=True).text
                ai_check = ai_kuccps_auditor(uni_name, course_name, text, target_url)
                
                if ai_check.get("ai_approved"):
                    logging.info(f"✅ STRICT SUCCESS: AI verified {target_url}")
                    return target_url, True # Fully Verified!
            except Exception as e:
                logging.warning(f"⚠️ AI Audit failed/timeout for {uni_name}: {e}")

            # 4. BACKUP STRATEGY (The Ducky Acts Alone)
            # If the AI was too strict or failed, we STILL keep the URL to avoid dropping the uni.
            logging.info(f"🔗 SWARM BACKUP: Returning unverified secure link for {uni_name}")
            return target_url, False

        # Total failure to find any link
        return None, False

# Initialize at the bottom (Left Margin)
TARGET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "university_portals")
healer = AutoHealer(TARGET_DIR)