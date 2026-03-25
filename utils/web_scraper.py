import os
import re
import json
import logging
import requests
import urllib3
import threading
import concurrent.futures
import warnings
import time
import random
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

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

# Import Groq client from your AI engines file
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
    1. INSTITUTION MATCH: Does the text/URL belong to {uni_name}? Reject if it is clearly a competitor university's site or a third-party course directory. Mentions of partner institutions, KUCCPS, or regulatory bodies (like CUE) are acceptable.
    2. COURSE MATCH: Is '{course_name}' (or a closely related valid variant) offered by this institution according to the text? Note that PDF syllabus conversions might have messy formatting.
    
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


class LinkResolver:
    def __init__(self, groq_client):
        self.groq = groq_client
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        })

    def resolve_deep_link(self, start_url, target_course, max_hops=4):
        """
        Navigates from a root URL to a specific course page.
        Increased max_hops to allow for: Homepage -> Academics -> Faculty -> Dept -> Course
        """
        current_url = start_url
        visited_urls = set()

        for depth in range(max_hops):
            logging.info(f"🚀 Hop {depth + 1}: AI Analyzing {current_url}")
            visited_urls.add(current_url)
            
            try:
                # 1. Fetch clean markdown via Jina AI
                res = self.session.get(f"https://r.jina.ai/{current_url}", timeout=20, verify=False)
                markdown = res.text
                
                # 2. Ask AI to pick the next best link from the Markdown
                next_link = self._ask_ai_for_next_step(markdown, target_course, current_url)
                
                # Exit conditions
                if not next_link or next_link.upper() == "STAY":
                    logging.info(f"🎯 AI decided to STAY. Target reached at: {current_url}")
                    return current_url
                    
                if next_link in visited_urls:
                    logging.warning(f"🔄 AI returned a visited URL ({next_link}). Stopping loop.")
                    break
                
                current_url = next_link
                
            except Exception as e:
                logging.error(f"Navigation failed at depth {depth}: {e}")
                break
                
        return current_url

    def _ask_ai_for_next_step(self, content, course, current_url):
        prompt = f"""
        Current Page: {current_url}
        Goal: Find the official page for '{course}'.
        
        You are an expert web navigator hunting for a specific university course.
        University websites follow this general funnel:
        1. Homepage -> Academics / Admissions / Programmes
        2. Academics -> Faculty / School / College (Pick the one related to '{course}')
        3. Faculty -> Department
        4. Department -> Specific Course Page
        
        Analyze the links in this Markdown content from the current page:
        {content[:6000]} 
        
        Pick the ONE absolute URL most likely to lead to the NEXT step in the funnel, or the course itself.
        If you are already on the specific page detailing '{course}', return the exact word 'STAY'.
        
        RULES:
        - RETURN ONLY THE URL OR 'STAY'. No explanations.
        - Ensure it is a valid, absolute http/https URL.
        """
        try:
            res = self.groq.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="llama-3.1-8b-instant",
                temperature=0.1
            )
            return res.choices[0].message.content.strip()
        except Exception as e:
            logging.debug(f"LinkResolver AI Error: {e}")
            return None


class AutoHealer:
    def __init__(self, target_folder="data", groq_client=None):
        self.db_path = os.path.join(target_folder, "academic_urls.json")
        os.makedirs(target_folder, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        })
        self.db_lock = threading.Lock() 
        self.db = self._load_db()
        self.groq_client = groq_client

        # The 75+ Verified Domain Anchor List (KUCCPS Aligned)
        self.known_domains = {
            # MAJOR PUBLIC
            "university of nairobi": "uonbi.ac.ke", "kenyatta university": "ku.ac.ke",
            "jomo kenyatta": "jkuat.ac.ke", "moi university": "mu.ac.ke", "egerton": "egerton.ac.ke",
            "maseno": "maseno.ac.ke", "masinde muliro": "mmust.ac.ke", "technical university of kenya": "tukenya.ac.ke",
            "technical university of mombasa": "tum.ac.ke", "chuka university": "chuka.ac.ke",
            "dedan kimathi": "dkut.ac.ke", "kisii": "kisiiuniversity.ac.ke", "pwani": "pu.ac.ke",
            "university of eldoret": "uoeld.ac.ke", "south eastern kenya": "seku.ac.ke",
            "multimedia university": "mmu.ac.ke", "machakos university": "mksu.ac.ke",
            "murang'a university": "mut.ac.ke", "university of embu": "embuni.ac.ke",
            "meru university": "must.ac.ke", "karatina university": "karu.ac.ke",
            "laikipia university": "laikipia.ac.ke", "rongo university": "rongovarsity.ac.ke",
            "kibabii university": "kibu.ac.ke", "garissa university": "gau.ac.ke",
            "taita taveta": "ttu.ac.ke", "kirinyaga university": "kyu.ac.ke",
            "co-operative university": "cuk.ac.ke", "alupe university": "au.ac.ke",
            "bomet university": "buc.ac.ke", "kaimosi friends": "kafu.ac.ke",
            "tharaka university": "tharaka.ac.ke", "tom mboya": "tmu.ac.ke",
            "turkana university": "tuc.ac.ke", "koitalel samoei": "ksuc.ac.ke",
            "maasai mara": "mmarau.ac.ke",
            
            # PRIVATE
            "strathmore": "strathmore.edu", "united states international": "usiu.ac.ke",
            "usiu africa": "usiu.ac.ke", "daystar university": "daystar.ac.ke",
            "mount kenya": "mku.ac.ke", "catholic university": "cuea.edu",
            "cuea": "cuea.edu", "pan africa christian": "pacuniversity.ac.ke",
            "st. paul's": "spu.ac.ke", "africa nazarene": "anu.ac.ke",
            "kabarak university": "kabarak.ac.ke", "kca university": "kcau.ac.ke",
            "zetech": "zetech.ac.ke", "umma university": "umma.ac.ke",
            "great lakes university": "gluk.ac.ke", "gretsa university": "gretsauniversity.ac.ke",
            "kag east": "kageast.ac.ke", "adventist university": "aua.ac.ke",
            "amref international": "amiu.ac.ke", "management university of africa": "mua.ac.ke",
            "riara university": "riarauniversity.ac.ke", "presbyterian university": "puea.ac.ke",
            "pioneer international": "pioneer.ac.ke", "scott christian": "scott.ac.ke",
            "tangaza university": "tangaza.ac.ke", "uzima university": "uzimauniversity.ac.ke",
            "kenya methodist": "kemu.ac.ke", "lukenya university": "lukenyauniversity.ac.ke",
            "raf international": "raf.ac.ke", "kiriri women's": "kwust.ac.ke",
            "the east african university": "teau.ac.ke", "kenya highlands": "kheu.ac.ke",

            # TVETS & POLYTECHNICS
            "kenya medical training": "kmtc.ac.ke", "kmtc": "kmtc.ac.ke",
            "kabete national": "kabetepoly.ac.ke", "nairobi technical": "nairobitti.ac.ke",
            "kisumu national": "kisumupoly.ac.ke", "eldoret national": "tenp.ac.ke",
            "nyeri national": "thenyeripoly.ac.ke", "meru national": "merunationalpolytechnic.ac.ke",
            "kenya coast": "kenyacoastpoly.ac.ke", "sigalagala": "sigalagalapoly.ac.ke",
            "kasneb": "kasneb.or.ke", "kenya school of government": "ksg.ac.ke",
            "kenya utalii": "utalii.ac.ke", "kenya institute of mass communication": "kimc.ac.ke",
            "railway training": "rti.ac.ke"
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
        uni_lower = university_name.lower().strip()
        
        # 1. Fuzzy match against the known domains dictionary
        for name, domain in self.known_domains.items():
            if name in uni_lower or uni_lower in name: 
                return domain
                
        # Add a tiny sleep before external searches to prevent rate limits
        time.sleep(random.uniform(1.0, 2.0))
                
        try:
            with DDGS() as ddgs:
                # 2. KUCCPS Deep-Search for the Domain
                kuccps_query = f'site:students.kuccps.net "{university_name}" website'
                for r in list(ddgs.text(kuccps_query, max_results=3)):
                    body = r.get('body', '').lower()
                    match = re.search(r'([a-zA-Z0-9\-]+\.(?:ac\.ke|edu\.ke|sc\.ke|edu))', body)
                    if match: return match.group(1)
                
                # 3. FALLBACK: Direct search if KUCCPS fails
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

        # Add a tiny sleep to avoid search engine 429 Too Many Requests errors
        time.sleep(random.uniform(1.0, 2.5))

        # 3. Precision Deep-Link Search
        precision_query = f'site:{root_domain} "{course_name}"'
        urls_to_check = []
        
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(precision_query, max_results=5))
                for r in results:
                    url = r.get('href', '')
                    if url and root_domain in url:
                        # Prioritize URLs containing academic keywords
                        if any(sub in url.lower() for sub in ['academic', 'programme', 'course', 'school']):
                            urls_to_check.insert(0, url) 
                        else:
                            urls_to_check.append(url)
        except Exception as e:
            logging.error(f"❌ Search Error: {e}")

        # 4. Concurrent Scraping & Validation
        if urls_to_check:
            def verify_single_url(target_url):
                try:
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

        # 4.5: Internal Navigation Fallback (AI-Driven Hierarchical Crawl)
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
                
    def _internal_navigation_crawl(self, root_domain, university_name, course_name):
        """
        Replaces rigid HTML parsing with an AI-driven multi-hop hunt.
        Follows the path: Homepage -> Academics -> Faculty -> Department -> Course
        """
        homepage_url = f"https://{root_domain}"
        logging.info(f"🏠 Starting AI hierarchical crawl from: {homepage_url}")
        
        if not self.groq_client:
            logging.error("No Groq client provided for internal crawling.")
            return None

        # Instantiate our upgraded LinkResolver
        crawler = LinkResolver(self.groq_client)
        
        # Give the crawler 4 hops to dig through the university hierarchy
        found_url = crawler.resolve_deep_link(start_url=homepage_url, target_course=course_name, max_hops=4)
        
        # If the AI navigated somewhere other than the homepage, validate the final destination
        if found_url and found_url != homepage_url:
            logging.info(f"🕵️ Internal crawl landed on: {found_url}. Validating...")
            try:
                scrape_res = self.session.get(f"https://r.jina.ai/{found_url}", timeout=20, verify=False)
                if ai_course_validator(university_name, course_name, scrape_res.text, found_url):
                    logging.info(f"✅ Final page validated successfully!")
                    return found_url
            except Exception as e:
                logging.debug(f"Validation of crawled URL failed: {e}")
                
        return None


# Initialize Target Directory
TARGET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
if not os.path.exists(TARGET_DIR):
    os.makedirs(TARGET_DIR)

# Initialize the instances and PASS GROQ CLIENT to both
healer = AutoHealer(target_folder=TARGET_DIR, groq_client=client_groq)
resolver = LinkResolver(groq_client=client_groq)