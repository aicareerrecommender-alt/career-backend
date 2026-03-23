import os
import re
import json
import logging
import requests
import urllib3
import concurrent.futures
from duckduckgo_search import DDGS
from duckduckgo_search.exceptions import DuckDuckGoSearchException
from .ai_engines import client_groq 

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

# 1. OPTIMIZATION: Precompile regex so it doesn't re-compile on every function call
COURSE_PREFIX_REGEX = re.compile(
    r'^(Bachelor of Science in|Bachelor of Arts in|Bachelor of|Diploma in|Certificate in|Artisan in)\s+', 
    re.IGNORECASE
)

def fetch_kuccps_proof(uni_name, course_name):
    """
    Specifically targets the KUCCPS portal via DuckDuckGo to verify if a course 
    is officially recognized for a specific university.
    """
    query = f'site:students.kuccps.net "{uni_name}" "{course_name}"'
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
            return " ".join([r.get('body', '').lower() for r in results])
    except DuckDuckGoSearchException:
        logging.error("KUCCPS Fetch: Rate limited by DuckDuckGo.")
        return ""
    except Exception as e:
        logging.error(f"KUCCPS Registry Fetch Error: {e}")
        return ""

def ai_kuccps_auditor(uni_name, course_name, scraped_text, source_url, verified=False):
    """
    Passes the scraped text to Groq AI to act as a judge and verify 
    if the page genuinely offers the requested course.
    """
    if not client_groq: 
        return {"ai_approved": False, "reason": "Groq client offline"}
    
    # Inject the KUCCPS verification status directly into the AI's prompt
    status = "VERIFIED ON KUCCPS PORTAL." if verified else "NOT FOUND ON KUCCPS PORTAL. STRICT SCRUTINY REQUIRED."
    
    prompt = f"Institution: {uni_name}\nTarget Course: {course_name}\nSource URL: {source_url}\nKUCCPS Status: {status}\n\nRules:\n1. Course must be clearly listed.\n2. Must be a dedicated course/department page. Reject general homepages/news.\n3. Verify Institution matches Domain.\n\nScraped Text:\n{scraped_text[:8000]}"
    
    try:
        res = client_groq.chat.completions.create(
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Output strictly JSON format: {'ai_approved': boolean, 'reason': 'string'}"}, 
                {"role": "user", "content": prompt}
            ],
            model="llama-3.1-8b-instant", 
            temperature=0.1
        )
        return json.loads(res.choices[0].message.content)
    except json.JSONDecodeError:
        # Prevent crashes if the AI hallucinates bad JSON
        logging.error(f"AI returned invalid JSON for {uni_name}")
        return {"ai_approved": False, "reason": "AI returned invalid format"}
    except Exception as e: 
        logging.error(f"AI Auditor API Error: {e}")
        return {"ai_approved": False, "reason": "AI offline"}

class AutoHealer:
    def __init__(self, target_folder="data"):
        self.db_path = os.path.join(target_folder, "academic_urls.json")
        os.makedirs(target_folder, exist_ok=True)
        self.session = requests.Session()
        # Added a realistic User-Agent to prevent Jina AI or university sites (.ac.ke) from blocking the request
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })

    def get_verified_url(self, uni_name, course_name):
        core = COURSE_PREFIX_REGEX.sub('', course_name).strip()
        
        # WE BROUGHT BACK YOUR GOLDEN QUERY FROM THE OLD FILE
        search_query = f'site:ac.ke {uni_name} "{core}" (course details OR programme structure OR admission requirements OR curriculum)'

        def search_worker(query):
            try:
                with DDGS() as ddgs:
                    # Look at the top 5 results to ensure we find the deep link
                    results = list(ddgs.text(query, max_results=5))
                    for r in results:
                        url = r.get('href', '')
                        # Filter out generic junk pages like your old file did
                        blacklist = ['/about', '/profile', '/news', '/blog', 'login', 'portal']
                        if url and ".ac.ke" in url and not any(x in url.lower() for x in blacklist):
                            return url
            except Exception as e: 
                logging.error(f"DDGS Search Error: {e}")
            return None

        # Execute the search
        target_url = search_worker(search_query)

        kuccps_context = fetch_kuccps_proof(uni_name, course_name)
        is_on_kuccps = (uni_name.lower() in kuccps_context) and (core.lower() in kuccps_context)

        if target_url:
            try:
                # Use Jina AI to read the page text just like your old file did
                text = self.session.get(f"https://r.jina.ai/{target_url}", timeout=8).text
                
                # Pass it to your strict Groq AI Auditor
                ai_check = ai_kuccps_auditor(uni_name, course_name, text, target_url, verified=is_on_kuccps)
                
                # If your AI auditor approves it, return the exact course link!
                if ai_check.get("ai_approved"):
                    return {
                        "url": target_url,
                        "verified": True,
                        "status": "AI_VERIFIED_COURSE_PAGE"
                    }
            except Exception as e:
                logging.error(f"Error fetching Jina URL {target_url}: {e}")

        # If it fails to find the exact page, return None (so app.py deletes it)
        return None
# Initialize the healer so app.py can import it easily
TARGET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data')
healer = AutoHealer(target_folder=TARGET_DIR)