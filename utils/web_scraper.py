import os
import re
import json
import logging
import requests
import threading
import time
import urllib.parse
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from groq import Groq, RateLimitError

client_groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))

DATA_FOLDER = "data"
CACHE_FILE = os.path.join(DATA_FOLDER, "verified_urls.json")
KENET_FILE = "kenet_all_200_institutions.txt"

cache_lock = threading.Lock()
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

def get_cached_url(uni_name, course_key):
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
            return cache.get(uni_name, {}).get(course_key)
    except:
        return None

def save_to_cache(uni_name, course_key, url):
    with cache_lock:
        cache = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, 'r') as f:
                    cache = json.load(f)
            except: pass
        
        if uni_name not in cache:
            cache[uni_name] = {}
        
        cache[uni_name][course_key] = url
        os.makedirs(DATA_FOLDER, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f, indent=4)

# --- 1. UPDATED: Call Groq with Compound-Level Reasoning (70B) ---
@retry(
    wait=wait_exponential(multiplier=2, min=3, max=20), 
    stop=stop_after_attempt(4),
    retry=retry_if_exception_type(RateLimitError),
    reraise=True
)
def call_groq_api(prompt):
    """Uses Llama 3.1 70B (Compound Reasoning) with strict RPM management."""
    with groq_lock:
        response = client_groq.chat.completions.create(
            model="llama-3.1-70b-versatile", # High-capacity reasoning model
            messages=[
                {
                    "role": "system", 
                    "content": "You are a precise web navigation assistant. You find official Kenyan university URLs. Return ONLY the raw URL string. No text, no markdown."
                },
                {"role": "user", "content": prompt}
            ],
            temperature=0 # Absolute precision
        )
        # Pace requests to respect the 30 RPM limit
        time.sleep(2.1) 
        return response

# --- 2. UPDATED: Optimized URL Retrieval Logic ---
def get_course_url(university_name, course_name, target_type="kuccps"):
    uni_key = university_name.lower().strip()
    course_key = f"{course_name.lower().strip()}_{target_type}"

    # Cache Check
    cached = get_cached_url(uni_key, course_key)
    if cached: return cached

    domain_hint = next((dom for name, dom in KENET_DOMAINS.items() if name in uni_key), None)
    safe_query = urllib.parse.quote_plus(f"{course_name} {university_name} Kenya")
    
    if target_type == "kuccps":
        fallback_url = f"https://www.google.com/search?q=site:students.kuccps.net+{safe_query}"
        base_prompt = (
            f"Find the EXACT KUCCPS portal URL (starts with https://students.kuccps.net/programmes/) "
            f"for '{course_name}' at '{university_name}'. Return ONLY the raw URL string."
        )
    else:
        domain_instruction = f"Target domain: {domain_hint}. " if domain_hint else ""
        fallback_url = f"https://www.google.com/search?q={safe_query}+official+course+page"
        base_prompt = (
            f"{domain_instruction}Find the official institution course info page for "
            f"'{course_name}' at '{university_name}' in Kenya. Return ONLY the direct raw URL."
        )

    max_attempts = 3
    bad_urls = [] 
    
    for attempt in range(max_attempts):
        current_prompt = base_prompt
        if bad_urls:
            current_prompt += f"\n\nDO NOT use these dead links: {', '.join(bad_urls)}."

        try:
            response = call_groq_api(current_prompt)
            # Use regex to extract the URL cleanly from the AI response
            match = re.search(r'(https?://[^\s"\'\`]+)', response.choices[0].message.content)
            
            if match:
                found_url = match.group(1).strip().rstrip('.,')
                
                # Validation Ping
                headers = {'User-Agent': 'Mozilla/5.0'}
                try:
                    resp = requests.get(found_url, headers=headers, timeout=5, verify=False)
                    if resp.status_code == 200:
                        html_lower = resp.text.lower()
                        is_soft_404 = any(p in html_lower for p in ["page not found", "404 error", "cannot be found"])
                        
                        if not is_soft_404:
                            save_to_cache(uni_key, course_key, found_url)
                            logging.info(f"✅ [VERIFIED] {university_name}: {found_url}")
                            return found_url
                        
                    logging.warning(f"⚠️ [INVALID/404] Attempt {attempt+1}: {found_url}")
                    bad_urls.append(found_url)
                except Exception as ping_e:
                    logging.warning(f"⚠️ [PING FAILED] {found_url}: {ping_e}")
                    bad_urls.append(found_url)
            else:
                logging.warning(f"⚠️ Attempt {attempt+1}: No URL found in Groq response.")

        except RateLimitError:
            logging.error(f"⏳ Rate limit hit. Using Fallback.")
            break 
        except Exception as e:
            logging.error(f"🚨 Groq error: {e}")

    logging.warning(f"❌ Fallback used for {university_name}")
    return fallback_url

def healer(ai_response_json):
    """Heals the AI response by fetching both KUCCPS and Institution URLs."""
    if not ai_response_json or "universities" not in ai_response_json:
        return ai_response_json
        
    for uni in ai_response_json["universities"]:
        uni_name = uni.get("name", "")
        course_name = uni.get("specific_course", "")
        
        # Heal KUCCPS link
        if not uni.get("kuccps_url") or uni.get("kuccps_url") == "PLACEHOLDER_FOR_HEALER":
            uni["kuccps_url"] = get_course_url(uni_name, course_name, target_type="kuccps")
            
        # Heal Institution link
        if not uni.get("institution_url") or uni.get("institution_url") == "PLACEHOLDER_FOR_HEALER":
            uni["institution_url"] = get_course_url(uni_name, course_name, target_type="institution")
            
    return ai_response_json