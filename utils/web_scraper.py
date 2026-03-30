import os
import re
import json
import logging
import requests
import threading
import time
import random
import urllib3
from tenacity import retry, stop_after_attempt, wait_exponential
from groq import Groq

# Suppress annoying SSL warnings in console
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- INITIALIZE GROQ CLIENT ---
client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# --- CONFIGURATION ---
DATA_FOLDER = "data"
CACHE_FILE = os.path.join(DATA_FOLDER, "verified_urls.json")
KENET_FILE = "kenet_all_200_institutions.txt"

# Thread-safety for writing to the JSON file
cache_lock = threading.Lock()

# Lock to ensure only ONE thread talks to Groq at a time to prevent concurrent 429 errors
groq_api_lock = threading.Lock()

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

# Tweaked backoff boundaries slightly to survive token bucket refills
@retry(wait=wait_exponential(multiplier=2, min=4, max=15), stop=stop_after_attempt(3))
def get_course_url(university_name, course_name):
    uni_key = university_name.lower().strip()
    course_key = course_name.lower().strip()

    # 1. LAYER ONE: INSTANT CACHE CHECK
    cached = get_cached_url(uni_key, course_key)
    if cached:
        logging.info(f"⚡ [CACHE HIT] {university_name}: {course_name}")
        return cached

    # 2. LAYER TWO: KENET + GROQ COMPOUND (With Redo Logic)
    domain_hint = next((dom for name, dom in KENET_DOMAINS.items() if name in uni_key), None)
    domain_instruction = f"Search specifically on '{domain_hint}'. " if domain_hint else ""
    
    bad_urls = []
    
    # We acquire the lock here so other threads wait their turn!
    with groq_api_lock:
        for attempt in range(2): 
            prompt = (
                f"{domain_instruction}Find the official undergraduate course page for "
                f"'{course_name}' at '{university_name}' in Kenya. "
                "You MUST use your 'Visit Website' or Search tool to verify that the URL actually loads and is active. "
                "Return ONLY the direct raw URL string. No conversational text."
            )
            
            # If the first link was a 404, we add this penalty to the prompt
            if bad_urls:
                prompt += f" Crucially, DO NOT return any of these broken URLs: {', '.join(bad_urls)}."

            # --- RATE LIMIT JITTER ---
            jitter = random.uniform(1.0, 2.5)
            logging.info(f"⏳ Jitter added: Sleeping for {jitter:.2f}s to respect rate limits.")
            time.sleep(jitter)

            try:
                response = client_groq.chat.completions.create(
                    model="groq/compound", 
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0
                )
                
                raw_ai_text = response.choices[0].message.content.strip()
                
                # Robust regex extraction to eliminate conversational filler
                url_match = re.search(r'(https?://[^\s"\'`]+)', raw_ai_text)

                if url_match:
                    found_url = url_match.group(1).rstrip('.,;)]}')

                    # 3. LAYER THREE: PYTHON PING (VERIFICATION)
                    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
                    
                    try:
                        resp = requests.get(found_url, headers=headers, timeout=7, verify=False)
                        
                        if resp.status_code == 200:
                            save_to_cache(uni_key, course_key, found_url)
                            logging.info(f"✅ [NEW VERIFIED] Saved to cache: {found_url}")
                            return found_url
                        else:
                            logging.warning(f"⚠️ URL {found_url} returned status {resp.status_code}. Asking Groq for a redo...")
                            bad_urls.append(found_url)
                            
                    except Exception as e:
                        logging.warning(f"💥 Ping failed for {found_url}: {e}. Asking Groq for a redo...")
                        bad_urls.append(found_url)
                else:
                    logging.warning(f"❌ No valid URL found in Groq response: {raw_ai_text}")

            except Exception as e:
                # Catch 429 specifically and pause to give bucket time to refill
                if "429" in str(e):
                    logging.warning("🚨 Hit Groq 429 Rate Limit! Backing off for 15 seconds...")
                    time.sleep(15.0)
                
                logging.error(f"🚨 Groq search failed: {e}")
                raise e  # Let Tenacity handle the retry exponential backoff
                
    return None

# --- Healer class ---
class Healer:
    def _internal_navigation_crawl(self, url, name, course):
        # Kept this string to match your expected imports/fallbacks,
        # but you can change this to return None if your front end handles it better!
        return "PLACEHOLDER_FOR_HEALER"

healer = Healer()