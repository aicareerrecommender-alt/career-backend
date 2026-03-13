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
from .ai_engines import client_groq

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

def ai_kuccps_auditor(uni_name, course_name, scraped_text, source_url, verified=False):
    if not client_groq: return {"ai_approved": False, "reason": "Groq client offline"}
    status = "VERIFIED ON KUCCPS PORTAL." if verified else "NOT FOUND ON KUCCPS PORTAL. STRICT SCRUTINY REQUIRED."
    prompt = f"Institution: {uni_name}\nTarget Course: {course_name}\nSource URL: {source_url}\nKUCCPS Status: {status}\n\nRules:\n1. Course must be clearly listed.\n2. Must be a dedicated course/department page. Reject general homepages/news.\n3. Verify Institution matches Domain.\n\nScraped Text:\n{scraped_text[:8000]}"
    try:
        res = client_groq.chat.completions.create(
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": "Output strictly JSON format: {'ai_approved': boolean, 'reason': 'string'}"}, {"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant", temperature=0.1
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e: return {"ai_approved": False, "reason": "AI offline"}

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
        with open(self.db_path, 'w', encoding='utf-8') as f: json.dump(self.url_cache, f, indent=4)

    def _is_alive(self, url):
        if url == "PLACEHOLDER_FOR_HEALER": return False
        try:
            return self.session.get(url, timeout=5, verify=False).status_code in [200, 401, 403]
        except: return False

    def _hunt_for_url(self, uni_name, course_name):
        core = re.sub(r'^(Bachelor of Science in|Bachelor of Arts in|Bachelor of|Diploma in|Certificate in|Artisan in)\s+', '', course_name, flags=re.IGNORECASE).strip()
        verified = False
        
        try:
            with DDGS() as ddgs:
                if list(ddgs.text(f'site:students.kuccps.net "{uni_name}" "{core}"', max_results=3)): verified = True
        except: pass

        urls_to_check = []
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(f'site:ac.ke {uni_name} "{core}" (course details OR admission requirements)', max_results=8))
                for r in results:
                    url = r.get('href', '')
                    if url and not any(bad in url.lower() for bad in ['/about', '/downloads', '/staff', '/news']):
                        if self.validator.is_legitimate(url, uni_name)[0]: urls_to_check.append(url)
        except: pass

        if not urls_to_check: return "PLACEHOLDER_FOR_HEALER", False

        def verify_single(target_url):
            try:
                text = self.session.get(f"https://r.jina.ai/{target_url}", timeout=8, verify=False).text
                if ai_kuccps_auditor(uni_name, course_name, text, target_url, verified).get("ai_approved"): return target_url
            except: pass
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            future_to_url = {executor.submit(verify_single, u): u for u in urls_to_check}
            for future in concurrent.futures.as_completed(future_to_url):
                if res := future.result(): return res, True
        
        return "PLACEHOLDER_FOR_HEALER", False

    def get_verified_url(self, uni_name, course_name):
        cache_key = f"{uni_name}_{course_name}"
        if cache_key in self.url_cache and "academic_portal" in self.url_cache[cache_key]:
            cached_url = self.url_cache[cache_key]["academic_portal"]
            is_verified = self.url_cache[cache_key].get("is_verified", False)
            if self._is_alive(cached_url): return cached_url, is_verified
        
        new_url, is_verified = self._hunt_for_url(uni_name, course_name)
        if cache_key not in self.url_cache: self.url_cache[cache_key] = {}
        self.url_cache[cache_key] = {"academic_portal": new_url, "is_verified": is_verified}
        self._save_db()
        return new_url, is_verified

# Initialize AutoHealer target directory at the project root level
TARGET_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "university_portals")
healer = AutoHealer(TARGET_DIR)