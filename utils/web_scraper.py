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
import urllib.parse
import re
import requests
import logging

def get_course_url(university_name, course_name, target_type="kuccps"):
    uni_key = university_name.lower().strip()
    course_key = f"{course_name.lower().strip()}_{target_type}"

    # 1. CACHE CHECK
    cached = get_cached_url(uni_key, course_key)
    if cached: return cached

    domain_hint = next((dom for name, dom in KENET_DOMAINS.items() if name in uni_key), None)
    
    # Base Google Fallback (If all 3 Groq attempts fail)
    safe_query = urllib.parse.quote_plus(f"{course_name} {university_name} Kenya")
    
    if target_type == "kuccps":
        fallback_url = f"https://www.google.com/search?q=site:students.kuccps.net+{safe_query}"
        base_prompt = (
            f"Find the EXACT KUCCPS portal URL (starts with https://students.kuccps.net/programmes/) "
            f"for '{course_name}' at '{university_name}'. Return ONLY the raw URL string."
        )
    else:
        domain_instruction = f"Search specifically on '{domain_hint}'. " if domain_hint else ""
        fallback_url = f"https://www.google.com/search?q={safe_query}+course"
        base_prompt = (
            f"{domain_instruction}Find the official institution course information page for "
            f"'{course_name}' at '{university_name}' in Kenya. Return ONLY the direct raw URL string."
        )

    # ---------------------------------------------------------
    # 2. THE AI RETRY LOOP (Max 3 attempts)
    # ---------------------------------------------------------
    max_attempts = 3
    bad_urls = []  # Keep track of hallucinated/dead links
    
    for attempt in range(max_attempts):
        # Dynamically update the prompt if we have failed before
        current_prompt = base_prompt
        if bad_urls:
            current_prompt += f"\n\nIMPORTANT: Do NOT return these dead URLs: {', '.join(bad_urls)}. They resulted in a 404. Find an ALTERNATIVE working link."

        try:
            # ASK GROQ
            response = call_groq_api(current_prompt)
            match = re.search(r'(https?://[^\s"\'\`]+)', response.choices[0].message.content)
            
            if match:
                found_url = match.group(1)
                
                # 3. ADVANCED PYTHON PING (Catches 404s and Soft 404s)
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
                resp = requests.get(found_url, headers=headers, timeout=5, verify=False)
                
                if resp.status_code == 200:
                    html_lower = resp.text.lower()
                    
                    # Detect Soft 404s
                    is_soft_404 = any(phrase in html_lower for phrase in [
                        "page not found", "404 error", "cannot be found", "could not be found"
                    ])
                    
                    if not is_soft_404:
                        # SUCCESS! Save and return
                        save_to_cache(uni_key, course_key, found_url)
                        logging.info(f"✅ [VERIFIED] Attempt {attempt+1}: {found_url}")
                        return found_url
                    else:
                        logging.warning(f"⚠️ [SOFT 404] Attempt {attempt+1}: {found_url} is dead text. Retrying...")
                        bad_urls.append(found_url)
                else:
                    logging.warning(f"⚠️ [HTTP {resp.status_code}] Attempt {attempt+1}: {found_url} is dead. Retrying...")
                    bad_urls.append(found_url)
            else:
                logging.warning(f"⚠️ Attempt {attempt+1}: Groq didn't return a URL. Retrying...")

        except RateLimitError:
            logging.error(f"⏳ Rate limit hit on attempt {attempt+1}. Stopping loop.")
            break # Exit the loop immediately to avoid hitting the API further
        except Exception as e:
            logging.error(f"🚨 Ping/Search failed on attempt {attempt+1}: {e}")

    # 4. IF ALL ATTEMPTS FAIL: Use the Unbreakable Google Fallback
    logging.warning(f"❌ All {max_attempts} attempts failed for {university_name}. Using Fallback.")
    return fallback_url
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
