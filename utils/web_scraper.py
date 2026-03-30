import os
import re
import json
import logging
import requests
import threading
import urllib3
import warnings
from tenacity import retry, stop_after_attempt, wait_exponential
from groq import Groq

# Suppress SSL warnings for inconsistent university sites
warnings.filterwarnings("ignore", category=RuntimeWarning)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- INITIALIZE GROQ CLIENT ---
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

# ==========================================
# 🚀 BATCHED GROQ SEARCH
# ==========================================
def get_course_urls_batched(university_list, course_name):
    """Queries Groq in ONE API call for a list of universities."""
    results = {}
    missing_unis = []
    course_key = course_name.lower().strip()

    # LAYER 1: Check cache for each university first
    for uni in university_list:
        uni_key = uni.lower().strip()
        cached_url = get_cached_url(uni_key, course_key)
        if cached_url:
            logging.info(f"⚡ [CACHE HIT] {uni}")
            results[uni] = cached_url
        else:
            missing_unis.append(uni)

    if not missing_unis:
        return results

    # LAYER 2: Dynamic Batched Prompt
    logging.info(f"🧠 [GROQ BATCH] Searching for {len(missing_unis)} universities simultaneously...")
    prompt_lines = [f"{i+1}. {uni}" for i, uni in enumerate(missing_unis)]
    formatted_list = "\n".join(prompt_lines)

    prompt = (
        f"Find the direct official undergraduate course page for '{course_name}' "
        f"at these Kenyan institutions:\n{formatted_list}\n\n"
        "Return STRICTLY a raw JSON object. Keys must be the EXACT university names, "
        "and values must be the direct raw URLs. No markdown."
    )

    try:
        response = client_groq.chat.completions.create(
            model="llama3-8b-8192", # Reliable for structured tasks
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        
        raw_content = response.choices[0].message.content.strip()
        clean_json = re.sub(r'```json|```', '', raw_content).strip()
        groq_results = json.loads(clean_json)

        # LAYER 3: Verification Ping
        for uni, url in groq_results.items():
            if url and url.startswith("http"):
                try:
                    resp = requests.get(url, timeout=5, verify=False)
                    if resp.status_code == 200:
                        results[uni] = url
                        save_to_cache(uni.lower().strip(), course_key, url)
                        logging.info(f"✅ [VERIFIED] Saved {uni} to cache.")
                except:
                    logging.warning(f"❌ [DEAD LINK] Bad link for {uni}: {url}")

    except Exception as e:
        logging.error(f"🚨 Batch search failed: {e}")

    return results

# Required to maintain compatibility with other parts of your code
@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(2))
def get_course_url(university_name, course_name):
    result = get_course_urls_batched([university_name], course_name)
    return result.get(university_name)

class Healer:
    def _internal_navigation_crawl(self, url, name, course):
        return "HEALER_PLACEHOLDER"

healer = Healer()