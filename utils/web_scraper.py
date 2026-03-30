import os
import re
import json
import logging
import requests
import threading
import time
import random
from tenacity import retry, stop_after_attempt, wait_exponential
from groq import Groq

# --- INITIALIZE GROQ CLIENT ---
# This assumes you have GROQ_API_KEY in your .env file
client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# --- CONFIGURATION ---
DATA_FOLDER = "data"
CACHE_FILE = os.path.join(DATA_FOLDER, "verified_urls.json")
KENET_FILE = "kenet_all_200_institutions.txt"

# Thread-safety for writing to the JSON file
cache_lock = threading.Lock()

def load_kenet_domains():
    """Parses the KENET text file into a searchable dictionary."""
    domains = {}
    if not os.path.exists(KENET_FILE):
        logging.warning(f"⚠️ KENET file not found at {KENET_FILE}. Proceeding without domain hints.")
        return domains
    
    with open(KENET_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            if '->' in line:
                parts = line.split('->')
                name = parts[0].lower().strip()
                url_match = re.search(r'https?://([a-zA-Z0-9.-]+)', parts[1])
                if url_match:
                    domains[name] = url_match.group(1).replace('www.', '')
    return domains

# Global KENET dictionary for the session
KENET_DOMAINS = load_kenet_domains()

def get_cached_url(uni_name, course_name):
    """Retrieves a URL from the local JSON file if it exists."""
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
            return cache.get(uni_name, {}).get(course_name)
    except:
        return None

def save_to_cache(uni_name, course_name, url):
    """Saves a verified URL to the local JSON file."""
    with cache_lock:
        cache = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r') as f:
                    cache = json.load(f)
            except: pass
        
        if uni_name not in cache:
            cache[uni_name] = {}
        
        cache[uni_name][course_name] = url
        
        os.makedirs(DATA_FOLDER, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=4)

@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(2))
def get_course_url(university_name, course_name):
    uni_key = university_name.lower().strip()
    course_key = course_name.lower().strip()

    # 1. LAYER ONE: INSTANT CACHE CHECK
    cached = get_cached_url(uni_key, course_key)
    if cached:
        logging.info(f"⚡ [CACHE HIT] {university_name}: {course_name}")
        return cached

    # 2. LAYER TWO: KENET + GROQ COMPOUND
    domain_hint = next((dom for name, dom in KENET_DOMAINS.items() if name in uni_key), None)
    domain_instruction = f"Search specifically on '{domain_hint}'. " if domain_hint else ""
    
    prompt = (
        f"{domain_instruction}Find the official undergraduate course page for "
        f"'{course_name}' at '{university_name}' in Kenya. "
        "Return ONLY the direct raw URL string."
    )

    # --- RATE LIMIT JITTER FIX ---
    # Sleeps for a random duration between 1 and 2.5 seconds to space out concurrent requests
    jitter = random.uniform(1.0, 2.5)
    logging.info(f"⏳ Jitter added: Sleeping for {jitter:.2f}s to respect rate limits.")
    time.sleep(jitter)
    # ----------------------------

    try:
        response = client_groq.chat.completions.create(
            # --- MODEL FIX: Changed from groq/compound to a valid, fast model ---
            model="llama-3.1-8b-instant", 
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        found_url = re.sub(r'[`"\'\s]', '', response.choices[0].message.content.strip())

        # 3. LAYER THREE: PYTHON PING (VERIFICATION)
        if found_url.startswith("http"):
            # Quick status check
            resp = requests.get(found_url, timeout=7, verify=False)
            if resp.status_code == 200:
                # SUCCESS: Save to file for future speed
                save_to_cache(uni_key, course_key, found_url)
                logging.info(f"✅ [NEW VERIFIED] Saved to cache: {found_url}")
                return found_url

    except Exception as e:
        logging.error(f"🚨 Groq search failed: {e}")
    
    return None

# --- Dummy healer class to satisfy your app.py imports ---
class Healer:
    def _internal_navigation_crawl(self, url, name, course):
        # Your existing healer logic here or pass-through
        return "PLACEHOLDER_FOR_HEALER"

healer = Healer()