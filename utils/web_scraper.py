import os
import re
import json
import logging
import requests
import threading
import time
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from groq import Groq, RateLimitError  # <-- Added RateLimitError

client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))

DATA_FOLDER = "data"
CACHE_FILE = os.path.join(DATA_FOLDER, "verified_urls.json")
KENET_FILE = "kenet_all_200_institutions.txt"

cache_lock = threading.Lock()
# --- NEW: Lock to pace Groq requests ---
groq_lock = threading.Lock()

def load_kenet_domains():
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

KENET_DOMAINS = load_kenet_domains()

def get_cached_url(uni_name, course_name):
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
            return cache.get(uni_name, {}).get(course_name)
    except:
        return None

def save_to_cache(uni_name, course_name, url):
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

# --- NEW: Enhanced retry specifically for Rate Limits ---
@retry(
    wait=wait_exponential(multiplier=2, min=3, max=20), 
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type(RateLimitError),
    reraise=True
)
def call_groq_api(prompt):
    """Wraps the actual Groq call with a lock and a small delay to respect 30 RPM."""
    with groq_lock:
        response = client_groq.chat.completions.create(
            model="groq/compound", 
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        # 30 RPM means max 1 request every 2 seconds. 
        # Sleeping for 2.1 seconds ensures we stay under that ceiling.
        time.sleep(2.1) 
        return response

# Update the signature to include target_type with a default value
def get_course_url(university_name, course_name, target_type="kuccps"):
    uni_key = university_name.lower().strip()
    # Unique cache key for each link type
    course_key = f"{course_name.lower().strip()}_{target_type}"

    # 1. LAYER ONE: INSTANT CACHE CHECK
    cached = get_cached_url(uni_key, course_key)
    if cached:
        logging.info(f"⚡ [CACHE HIT] {university_name}: {course_name} ({target_type})")
        return cached

    # 2. LAYER TWO: DYNAMIC PROMPT GENERATION
    domain_hint = next((dom for name, dom in KENET_DOMAINS.items() if name in uni_key), None)
    
    if target_type == "kuccps":
        # Specific search for the government portal
        prompt = (
            f"Find the EXACT KUCCPS students portal link for '{course_name}' at '{university_name}'. "
            "The URL MUST start with 'https://students.kuccps.net/programmes/'. "
            "Return ONLY the raw URL string."
        )
    else:
        # Specific search for the University's own website
        domain_instruction = f"Search specifically on '{domain_hint}'. " if domain_hint else ""
        prompt = (
            f"{domain_instruction}Find the official institution course information page for "
            f"'{course_name}' at '{university_name}' in Kenya. "
            "Return ONLY the direct raw URL string."
        )

    try:
        # Calls the rate-limited wrapper
        response = call_groq_api(prompt)
        found_url = re.sub(r'[`"\'\s]', '', response.choices[0].message.content.strip())

        # 3. LAYER THREE: PYTHON PING (VERIFICATION)
        if found_url.startswith("http"):
            resp = requests.get(found_url, timeout=7, verify=False)
            if resp.status_code == 200:
                save_to_cache(uni_key, course_key, found_url)
                logging.info(f"✅ [NEW VERIFIED] Saved {target_type} to cache: {found_url}")
                return found_url

    except RateLimitError:
        logging.warning(f"⏳ Rate limit hit for {university_name}. Using safe fallback.")
    except Exception as e:
        logging.error(f"🚨 Groq search failed: {e}")
    
    # 4. LAYER FOUR: SAFE FALLBACK (Prevents 'None' crashes)
    return "https://students.kuccps.net/programmes/" if target_type == "kuccps" else "https://google.com"

def healer(ai_response_json):
    """
    Heals the AI response by fetching both KUCCPS and Institution URLs.
    """
    if not ai_response_json or "universities" not in ai_response_json:
        return ai_response_json
        
    for uni in ai_response_json["universities"]:
        uni_name = uni.get("name", "")
        course_name = uni.get("specific_course", "")
        
        # Heal KUCCPS link (checks if field exists or is placeholder)
        if not uni.get("kuccps_url") or uni.get("kuccps_url") == "PLACEHOLDER_FOR_HEALER":
            uni["kuccps_url"] = get_course_url(uni_name, course_name, target_type="kuccps")
            
        # Heal Institution link
        if not uni.get("institution_url") or uni.get("institution_url") == "PLACEHOLDER_FOR_HEALER":
            uni["institution_url"] = get_course_url(uni_name, course_name, target_type="institution")
            
    return ai_response_json
def healer(ai_response_json):
    """
    Heals the AI response by fetching both KUCCPS and Institution URLs.
    """
    if not ai_response_json or "universities" not in ai_response_json:
        return ai_response_json
        
    for uni in ai_response_json["universities"]:
        uni_name = uni.get("name", "")
        course_name = uni.get("specific_course", "")
        
        # Heal KUCCPS link (checks if field exists or is placeholder)
        if not uni.get("kuccps_url") or uni.get("kuccps_url") == "PLACEHOLDER_FOR_HEALER":
            uni["kuccps_url"] = get_course_url(uni_name, course_name, target_type="kuccps")
            
        # Heal Institution link
        if not uni.get("institution_url") or uni.get("institution_url") == "PLACEHOLDER_FOR_HEALER":
            uni["institution_url"] = get_course_url(uni_name, course_name, target_type="institution")
            
    return ai_response_json