import os
import json
import time
import threading
import tempfile
import logging
import requests
import urllib3
import urllib.parse
import concurrent.futures
import re  
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse
from bs4 import BeautifulSoup 
from ddgs import DDGS 
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from groq import Groq
from google import genai
from google.genai import types

# 🚀 NEW: Google OAuth Imports
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm.attributes import flag_modified

# --- LOGGING CONFIGURATION ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s]: %(message)s', datefmt='%H:%M:%S')
# Suppress the massive InsecureRequestWarning wall of text since we are intentionally ignoring SSL errors
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- FLASK SETUP & API KEYS ---
app = Flask(__name__)
CORS(app)

# 🚀 POSTGRESQL CONFIG
db_url = os.environ.get("DATABASE_URL", "sqlite:///local_test.db")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app, engine_options={
    "pool_pre_ping": True, 
    "pool_recycle": 300
})

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    is_verified = db.Column(db.Boolean, default=False)
    verification_code = db.Column(db.String(6))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class StudentLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    interest = db.Column(db.String(100))
    data = db.Column(db.JSON) # This replaces the ai_insight dictionary storage
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'your-email@gmail.com') 
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your-app-password')    
mail = Mail(app)

# 🚨 SECURITY NOTICE: Keys are pulled from environment variables
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "YOUR_GROQ_API_KEY_HERE") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "YOUR_GOOGLE_CLIENT_ID_HERE") # Make sure to set this in Render!

client_groq = None
client_gemini = None

try:
    if not GROQ_API_KEY or GROQ_API_KEY == "YOUR_GROQ_API_KEY_HERE":
        raise ValueError("Groq API Key is missing.")
    client_groq = Groq(api_key=GROQ_API_KEY, timeout=90.0, max_retries=2)
    print("✅ System connected to Groq AI.")
except Exception as e:
    print(f"❌ Groq Connection Failed: {e}")

try:
    if not GEMINI_API_KEY or GEMINI_API_KEY == "YOUR_GEMINI_API_KEY_HERE":
        raise ValueError("Gemini API Key is missing.")
    client_gemini = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ System connected to Gemini AI.")
except Exception as e:
    print(f"❌ Gemini Connection Failed: {e}")


# ==========================================
# 🛡️ YOUR SPEED-OPTIMIZED SCRAPING LOGIC BELOW
# ==========================================

# --- 🤝 THE AI KUCCPS AUDITOR ---
def ai_kuccps_auditor(university_name, course_name, scraped_text, source_url, kuccps_verified=False):
    if not client_groq:
        return {"ai_approved": False, "reason": "Groq client offline"}

    kuccps_status = "VERIFIED ON KUCCPS PORTAL." if kuccps_verified else "NOT FOUND ON KUCCPS PUBLIC PORTAL. STRICT SCRUTINY REQUIRED."

    prompt = f"""
    You are an AI auditor strict on KUCCPS (Kenya Universities and Colleges Central Placement Service) standards.
    Your task is to review text scraped from an official university website and confirm if the URL is the EXACT course page.
    
    Institution: {university_name}
    Target Course: {course_name}
    Source URL: {source_url}
    KUCCPS Status: {kuccps_status}
    
    Rules for Validation:
    1. The course must be clearly listed as an offered academic program.
    2. Accept valid abbreviations (e.g., 'BSc. IT' = 'Bachelor of Science in Information Technology').
    3. If KUCCPS Status is "NOT FOUND", you must be 100% certain the text proves the course exists.
    4. 🎯 THE LAST CLICK RULE: The scraped text MUST indicate this is a dedicated course page, a specific departmental page, or a detailed syllabus/requirements page. REJECT the page if it is just a general homepage, a news article, or a generic list where the course is only mentioned in passing.
    5. 🏛️ INSTITUTION MATCH: Ensure the website actually belongs to the specified Institution. If the URL or text clearly belongs to a DIFFERENT institution (e.g., University of Nairobi instead of University of Eldoret), you MUST REJECT it immediately and note the domain mismatch in your reason.
    
    Scraped Text: 
    {scraped_text[:8000]} 
    """

    try:
        chat_completion = client_groq.chat.completions.create(
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Output strictly JSON format: {'ai_approved': boolean, 'reason': 'string'}"},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.1-8b-instant",
            temperature=0.1
        )
        return json.loads(chat_completion.choices[0].message.content)
    except Exception as e:
        logging.warning(f"⚠️ AI Auditor Error: {e}")
        return {"ai_approved": False, "reason": "AI offline or failed to parse."}


# --- 🛑 THE INSTITUTION VALIDATOR ---
class InstitutionValidator:
    def __init__(self):
        self.official_tlds = ['.ac.ke', '.sc.ke', '.edu.ke', '.go.ke']
        self.whitelist = ["strathmore.edu", "usiu.ac.ke", "kmtc.ac.ke", "kuccps.net"]

    def is_legitimate(self, url, university_name):
        try:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            if domain.startswith('www.'): domain = domain[4:]

            is_official_tld = any(domain.endswith(tld) for tld in self.official_tlds)
            is_whitelisted = domain in self.whitelist
            
            if not (is_official_tld or is_whitelisted):
                return False, f"🛡️ Blocked Aggregator: {domain} is not an official .ac.ke domain."

            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            # ensure verify=False is here so manual ping tests also ignore bad SSL
            response = requests.get(url, headers=headers, timeout=5, verify=False)
            
            if response.status_code not in [200, 401, 403]:
                return False, f"🚨 Dead Link: {domain} returned status {response.status_code}."

            soup = BeautifulSoup(response.text, 'html.parser')
            page_text = soup.get_text().lower()
            
            clean_name = re.sub(r'\b(university|college|national|polytechnic|institute|of|the|and|for)\b', '', university_name, flags=re.IGNORECASE).strip()
            core_words = [w.lower() for w in clean_name.split() if len(w) > 2]
            acronyms = [w.lower() for w in re.findall(r'\b[A-Z]{3,}\b', university_name)]
            all_identifying_words = core_words + acronyms
            
            if not any(word in domain or word in page_text for word in all_identifying_words):
                return False, f"🚨 Identity Mismatch: {domain} doesn't seem to belong to {university_name}."

            return True, "✅ Verified Official Site"
        except Exception as e:
            return False, f"🚨 Validation Error: {str(e)}"

# --- 🚀 SELF-HEALING & CONCURRENT VALIDATION UTILITY ---
class AutoHealer:
    def __init__(self, target_folder="university_portals"):
        self.folder = target_folder
        self.db_path = os.path.join(self.folder, "academic_urls.json")
        os.makedirs(self.folder, exist_ok=True)
        
        # SPEED UPGRADE: Persistent requests session
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        # 🟢 THE SSL FIX: Force the session to ignore broken University certificates
        self.session.verify = False
        
        self.validator = InstitutionValidator() 
        self.url_cache = self._load_db()

    def _load_db(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_db(self):
        with open(self.db_path, 'w', encoding='utf-8') as f:
            json.dump(self.url_cache, f, indent=4)

    def _is_alive(self, url):
        if url == "PLACEHOLDER_FOR_HEALER": 
            return False 
            
        try:
            # Using the persistent session (which already has verify=False)
            response = self.session.get(url, timeout=5)
            return response.status_code in [200, 401, 403]
        except requests.exceptions.RequestException as e:
            logging.warning(f"Link {url} appears dead: {e}")
            return False

    def _hunt_for_url(self, university_name, course_name):
        logging.info(f"🔎 Initiating KUCCPS-First Search for: {university_name} - {course_name}...")
        
        core_course = re.sub(r'^(Bachelor of Science in|Bachelor of Arts in|Bachelor of|Diploma in|Certificate in|Artisan in)\s+', '', course_name, flags=re.IGNORECASE).strip()
        
        # 🟢 STEP 1: THE KUCCPS REGISTRY CHECK
        kuccps_query = f'site:students.kuccps.net "{university_name}" "{core_course}"'
        kuccps_verified = False
        
        try:
            with DDGS() as ddgs:
                kuccps_results = list(ddgs.text(kuccps_query, max_results=3))
                
            if kuccps_results:
                logging.info(f"🏛️ KUCCPS REGISTRY MATCH: Found official listing for {core_course} at {university_name}.")
                kuccps_verified = True
            else:
                logging.warning(f"⚠️ KUCCPS WARNING: Could not find {core_course} at {university_name} on the public portal.")
        except Exception as e:
            logging.error(f"❌ KUCCPS Search Error: {e}")

        # 🟢 RATE LIMIT PROTECT: Small pause so DuckDuckGo doesn't block us on the second search!
        time.sleep(1)

        # 🟢 STEP 2: FINDING THE EXACT COURSE URL
        uni_query = f'site:ac.ke {university_name} "{core_course}" (course details OR programme structure OR admission requirements OR curriculum)'
        url_blacklist = ['/about', '/profile', '/council', '/history', '/staff', '/dean', '/leadership', '/management', '/news', '/blog', '/author', '/category', '/tender', '/downloads']
        
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(uni_query, max_results=8)) 
                
            # Gather all URLs first
            urls_to_check = []
            for res in results:
                url = res.get('href', '')
                if not url: continue
                
                url_lower = url.lower()
                if any(bad_word in url_lower for bad_word in url_blacklist):
                    continue

                # Run Python strict domain validation
                is_valid_site, msg = self.validator.is_legitimate(url, university_name)
                if is_valid_site:
                    urls_to_check.append(url)

            if not urls_to_check: return "PLACEHOLDER_FOR_HEALER", False

            # Inner function to handle a single URL asynchronously
            def verify_single_url(target_url):
                logging.info(f"📄 Testing promising academic URL: {target_url}...")
                try:
                    # Switch to Jina Reader + session for blistering speed
                    jina_url = f"https://r.jina.ai/{target_url}"
                    res_page = self.session.get(jina_url, timeout=8)
                    clean_markdown_text = res_page.text 
                    
                    ai_verdict = ai_kuccps_auditor(university_name, course_name, clean_markdown_text, target_url, kuccps_verified)
                    if ai_verdict.get("ai_approved", False):
                        logging.info(f"🤝 SUCCESS! AI verified exact course page: {target_url}")
                        return target_url
                    return None
                except Exception as e:
                    logging.warning(f"⚠️ Scraping failed for {target_url}")
                    return None

            # 🚀 STEP 3: CONCURRENT SCRAPING (The Rate Limited Speed Upgrade)
            # We lowered max_workers to 2 to prevent triggering 429 DDoS protections!
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_to_url = {}
                for url in urls_to_check:
                    # 🟢 THE RATE LIMIT FIX: Add a 1.5 second pause between queueing requests
                    time.sleep(1.5)
                    future_to_url[executor.submit(verify_single_url, url)] = url
                
                # as_completed yields results the exact millisecond they finish downloading
                for future in concurrent.futures.as_completed(future_to_url):
                    approved_url = future.result()
                    if approved_url: 
                        # The FIRST url to get approved wins! We stop waiting for the rest.
                        return approved_url, True

        except Exception as e: 
            logging.error(f"❌ DuckDuckGo Search CRASHED: {e}")
            
        return "PLACEHOLDER_FOR_HEALER", False

    def get_verified_url(self, university_name, course_name):
        cache_key = f"{university_name}_{course_name}"
        uni_data = self.url_cache.get(cache_key)
        
        if uni_data and "academic_portal" in uni_data:
            cached_url = uni_data["academic_portal"]
            is_verified = uni_data.get("is_verified", False)
            if self._is_alive(cached_url) and "duckduckgo.com" not in cached_url and "google.com" not in cached_url:
                logging.info(f"⚡ Cache Hit: Quick load for {university_name} - {course_name}")
                return cached_url, is_verified

        new_url, is_verified = self._hunt_for_url(university_name, course_name)
        if cache_key not in self.url_cache: self.url_cache[cache_key] = {}
        self.url_cache[cache_key]["academic_portal"] = new_url
        self.url_cache[cache_key]["is_verified"] = is_verified
        self._save_db()
        return new_url, is_verified

healer = AutoHealer()


# ==========================================
# 🚀 UPGRADED FLASK ROUTES (Using PostgreSQL)
# ==========================================

def grade_to_int(grade_str):
    grade_mapping = {'A': 12, 'A-': 11, 'B+': 10, 'B': 9, 'B-': 8, 'C+': 7, 'C': 6, 'C-': 5, 'D+': 4, 'D': 3, 'D-': 2, 'E': 1}
    return grade_mapping.get(str(grade_str).upper(), 0)

def calculate_total_points(student_grades):
    return sum(grade_to_int(grade) for grade in student_grades.values()) if isinstance(student_grades, dict) else 0

def validate_ai_response(ai_data, user_grades, expected_level):
    errors = []
    course_name = ai_data.get("specific_course", "").lower()
    recommended_level = ai_data.get("level", "").lower()

    if "degree" in recommended_level:
        if "engineer" in course_name or "mechatronic" in course_name:
            if grade_to_int(user_grades.get("Mathematics", user_grades.get("Math", "E"))) < 7 or grade_to_int(user_grades.get("Physics", "E")) < 7:
                return ["CRITICAL ERROR: Engineering Degree requires C+ in Math and Physics. Downgrade to Diploma/Cert."]
        if "med" in course_name or "nurs" in course_name or "clinic" in course_name or "surg" in course_name:
            if grade_to_int(user_grades.get("Biology", "E")) < 7 or grade_to_int(user_grades.get("Chemistry", "E")) < 7:
                return ["CRITICAL ERROR: Medical Degree requires C+ in Bio/Chem. Downgrade tier."]
        if "comput" in course_name or "software" in course_name or "it" in course_name:
            if grade_to_int(user_grades.get("Mathematics", user_grades.get("Math", "E"))) < 7:
                return ["CRITICAL ERROR: IT Degree requires C+ in Math. Downgrade tier."]
    elif "diploma" in recommended_level:
        if "engineer" in course_name or "comput" in course_name or "software" in course_name or "it" in course_name:
            if grade_to_int(user_grades.get("Mathematics", user_grades.get("Math", "E"))) < 5: 
                return ["CRITICAL ERROR: STEM Diplomas require C- in Math. Downgrade to Certificate."]

    valid_unis = []
    
    for uni in ai_data.get("universities", []):
        reqs = uni.get("requirements_met", [])
        if not reqs: continue 
            
        uni_is_valid = True
        for req in reqs:
            subject = req.get("subject", "")
            required_grade = req.get("required", "E")
            actual_grade = "E"
            
            for user_subj, user_grade in user_grades.items():
                if user_subj.lower().startswith(subject.lower()[:4]):
                    actual_grade = user_grade
                    break
            
            if grade_to_int(actual_grade) < grade_to_int(required_grade):
                logging.info(f"🧹 Pruning {uni.get('name')}: Required {required_grade} in {subject}, but student has {actual_grade}.")
                uni_is_valid = False
                break 
                
        if uni_is_valid: 
            uni["requirements_met"] = [{"subject": "Statutory Requirements", "required": "Met", "attained": "Verified"}]
            valid_unis.append(uni)

    ai_data["universities"] = valid_unis
    if len(valid_unis) == 0:
        errors.append("CRITICAL ERROR: ALL universities generated had requirements higher than the student's actual grades. Lower the requirements or suggest lower-tier institutions.")

    return errors

# --- 🔐 AUTH ROUTES (PostgreSQL + Google OAuth) ---
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username, password, email = data.get('username', '').strip(), data.get('password', '').strip(), data.get('email', '').strip() 
    if not username or not password or not email: return jsonify({"message": "Required fields missing"}), 400
    
    if User.query.filter_by(username=username).first() or User.query.filter_by(email=email).first():
        return jsonify({"message": "User already exists!"}), 409
        
    new_user = User(username=username, email=email, password=generate_password_hash(password))
    db.session.add(new_user)
    db.session.commit()
    return jsonify({"message": "Registration successful!", "user": username}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username, password = data.get('username', '').strip(), data.get('password', '').strip()
    user = User.query.filter_by(username=username).first()
    
    if user and check_password_hash(user.password, password):
        return jsonify({"message": "Login successful", "user": username, "email": user.email}), 200
    return jsonify({"message": "Invalid credentials"}), 401 

@app.route('/google-login', methods=['POST'])
def google_login():
    """Handles Google OAuth Token verification and seamless login/registration"""
    data = request.json
    token = data.get('token')
    
    if not token:
        return jsonify({"message": "Google token missing"}), 400

    try:
        # Verify the token with Google
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        
        email = idinfo.get('email')
        name = idinfo.get('name', email.split('@')[0]) # Use their Google name, or default to email prefix

        # Check if the user already exists in PostgreSQL
        user = User.query.filter_by(email=email).first()
        
        if not user:
            logging.info(f"🆕 Creating new account for Google user: {email}")
            # Generate a random secure password for Google users since they don't need to type it
            random_password = generate_password_hash(os.urandom(24).hex())
            user = User(
                username=name, 
                email=email, 
                password=random_password, 
                is_verified=True # Google emails are already verified!
            )
            db.session.add(user)
            db.session.commit()
        else:
            logging.info(f"✅ Existing Google user logged in: {email}")

        return jsonify({
            "message": "Login successful", 
            "user": user.username, 
            "email": user.email
        }), 200

    except ValueError as e:
        logging.warning(f"🚨 Invalid Google token submitted: {e}")
        return jsonify({"message": "Invalid Google token. Authentication failed."}), 401


# --- 📜 HISTORY & AI LOGIC ---
@app.route('/history', methods=['GET'])
def get_history():
    username = request.args.get('username')
    logs = StudentLog.query.filter_by(username=username).order_by(StudentLog.timestamp.desc()).all()
    return jsonify([log.data for log in logs if log.data]), 200

def analyze_history(interest):
    return StudentLog.query.filter(StudentLog.interest.ilike(f"%{interest}%")).count()

def fetch_from_groq(system_instruction, base_prompt, grades, expected_level):
    if not client_groq: return None
    max_retries = 3 
    retry_count = 0
    error_feedback = ""
    
    while retry_count < max_retries:
        current_prompt = base_prompt
        if error_feedback: current_prompt += f"\n\n🚨 YOUR LAST RESPONSE FAILED:\n{error_feedback}\nFIX THIS."
            
        try:
            chat_completion = client_groq.chat.completions.create(
                messages=[{"role": "system", "content": system_instruction}, {"role": "user", "content": current_prompt}],
                model="llama-3.1-8b-instant", response_format={"type": "json_object"}, temperature=0.3 
            )
            data = json.loads(chat_completion.choices[0].message.content)
            validation_errors = validate_ai_response(data, grades, expected_level)
            
            if not validation_errors: return data
            error_feedback = "\n- ".join(validation_errors)
            logging.warning(f"⚠️ Groq Validation Failed (Attempt {retry_count + 1}/{max_retries}). Retrying... Errors: {error_feedback}")
            retry_count += 1
        except Exception as e:
            logging.warning(f"⚠️ Groq API/JSON Error: {e}")
            error_feedback = "Ensure you are returning ONLY valid, properly escaped JSON."
            retry_count += 1
            
    return None

def fetch_from_gemini(system_instruction, base_prompt, grades, expected_level):
    if not client_gemini: return None
    max_retries = 3 
    retry_count = 0
    error_feedback = ""
    
    while retry_count < max_retries:
        current_prompt = base_prompt
        if error_feedback: current_prompt += f"\n\n🚨 YOUR LAST RESPONSE FAILED:\n{error_feedback}\nFIX THIS."
            
        try:
            response = client_gemini.models.generate_content(
                model='gemini-2.5-flash',
                contents=current_prompt,
                config=types.GenerateContentConfig(system_instruction=system_instruction, response_mime_type="application/json", temperature=0.3)
            )
            data = json.loads(response.text)
            validation_errors = validate_ai_response(data, grades, expected_level)
            
            if not validation_errors: return data
            error_feedback = "\n- ".join(validation_errors)
            logging.warning(f"⚠️ Gemini Validation Failed (Attempt {retry_count + 1}/{max_retries}). Retrying... Errors: {error_feedback}")
            retry_count += 1
        except Exception as e:
            if "429" in str(e):
                time.sleep((retry_count + 1) * 4)
            error_feedback = "Ensure you are returning ONLY valid JSON."
            retry_count += 1
            
    return None

def ask_hybrid_career_advice(student_name, interest, grades, calculated_points, expected_level, popularity_count, previous_unis, failed_hallucinations=None):
    system_instruction = """
    You are a strict, factual Kenyan KUCCPS career advisor API. 
    CRITICAL RULES:
    1. Recommend courses that actually exist at REAL KENYAN institutions.
    2. OVER-GENERATE: You MUST provide an array of AT LEAST 8 DIFFERENT institutions.
    3. Output EXACTLY "PLACEHOLDER_FOR_HEALER" for website_url.
    4. NO GENERIC COURSES: You MUST independently select a specific, real branch (e.g., "Certificate in Electrical and Electronics Engineering").
    """
    
    base_prompt = f"Student: {student_name} | Points: {calculated_points}/84 | Tier: {expected_level} | Passion: {interest}\nSubject Grades: {json.dumps(grades)}"
    
    if previous_unis: base_prompt += f"\n🚨 DO NOT recommend these universities again: {', '.join(previous_unis)}.\n"
    if failed_hallucinations:
        base_prompt += f"\n🚨 CRITICAL VALIDATION ERROR: DO NOT suggest these specific campus-course combinations again:\n"
        for h in failed_hallucinations: base_prompt += f"- {h}\n"

    json_structure = """
    Respond ONLY with valid JSON matching this exact structure:
    {
        "specific_course": "Specific Name of the Degree/Diploma/Certificate",
        "level": "Expected Level",
        "ai_role": "Specific Job Title",
        "interest_match_reason": "Personalized 2-3 sentences about why this fits.",
        "ai_roadmap": "A brief 3-step HTML roadmap",
        "career_exploration_url": "Search URL",
        "universities": [
            {
                "name": "Kenyan University Name",
                "students": 120, 
                "specific_course": "Exact Name",
                "reason": "Why this is a good fit",
                "website_url": "PLACEHOLDER_FOR_HEALER",
                "verified_offering": true,
                "requirements_met": [{"subject": "Math", "required": "C-", "attained": "REAL_GRADE"}]
            }
        ],
        "alternative_careers": [{"name": "Backup Career", "exploration_url": "URL", "reason": "Reason"}]
    }
    """
    
    full_prompt = base_prompt + "\n" + json_structure

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_groq = executor.submit(fetch_from_groq, system_instruction, full_prompt, grades, expected_level)
        future_gemini = executor.submit(fetch_from_gemini, system_instruction, full_prompt, grades, expected_level)
        groq_data = future_groq.result()
        gemini_data = future_gemini.result()

    if not groq_data and not gemini_data: return None

    final_data = groq_data or gemini_data
    if groq_data and gemini_data:
        seen_unis = {u.get("name", "").lower() for u in final_data.get("universities", [])}
        for uni in gemini_data.get("universities", []):
            if uni.get("name", "").lower() not in seen_unis:
                final_data["universities"].append(uni)
                seen_unis.add(uni.get("name", "").lower())
                
        seen_alts = {a.get("name", "").lower() for a in final_data.get("alternative_careers", [])}
        for alt in gemini_data.get("alternative_careers", []):
            if alt.get("name", "").lower() not in seen_alts:
                final_data["alternative_careers"].append(alt)
                seen_alts.add(alt.get("name", "").lower())

    final_data["popularity"] = f"👥 {popularity_count} other {'student' if popularity_count == 1 else 'students'} asked about this!" if popularity_count > 0 else "✨ You are the first to pioneer this unique career path!"
    final_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return final_data

@app.route('/recommend', methods=['POST', 'OPTIONS'])
def recommend():
    if request.method == 'OPTIONS': return '', 200

    data = request.json
    user_name = data.get('studentName', 'Anonymous')
    user_interest = data.get('interestArea', 'General')
    user_grades = data.get('subjects', {})

    calculated_points = calculate_total_points(user_grades)
    if calculated_points == 0: calculated_points = data.get('aggregatePoints', 0)

    math_grade = user_grades.get("Mathematics", user_grades.get("Math", "E"))
    
    if grade_to_int(math_grade) < 5: 
        expected_level = "Artisan/Craft Certificate"
        user_interest = f"{user_interest}. STRICT RULES: Student has a '{math_grade}' in Math. DO NOT suggest Degrees or STEM Diplomas. ONLY suggest Artisan or Craft Certificates."
    elif calculated_points >= 49: expected_level = "Degree"
    elif calculated_points >= 35: expected_level = "Diploma"
    elif calculated_points >= 21: expected_level = "Certificate"
    elif calculated_points > 0: expected_level = "Artisan"
    else: expected_level = "Unknown"

    popularity = analyze_history(user_interest)
    
    # DB Update: Load previous logs from Postgres
    previous_unis_names, previous_unis_data = [], []
    is_continuation = False 
    user_logs = StudentLog.query.filter_by(username=user_name).order_by(StudentLog.timestamp.desc()).all()
    
    for log in user_logs:
        if log.interest and log.interest.lower() == user_interest.lower() and log.data and log.data.get('level', '') == expected_level:
            is_continuation = True
            for uni in log.data.get('universities', []):
                if uni.get('name') and uni.get('name') not in previous_unis_names:
                    previous_unis_names.append(uni.get('name'))
                    previous_unis_data.append(uni)
        else:
            break

    max_pipeline_attempts = 3
    pipeline_attempt = 0
    hallucination_blacklist = []
    verified_combined_unis = []
    ai_insight = None

    while pipeline_attempt < max_pipeline_attempts:
        pipeline_attempt += 1
        try:
            ai_insight = ask_hybrid_career_advice(
                user_name, user_interest, user_grades, calculated_points, 
                expected_level, popularity, previous_unis_names, hallucination_blacklist
            )
        except Exception as e:
            return jsonify({"error": True, "message": "The AI encountered an internal error. Please try again!"}), 503
        
        if not ai_insight: break

        if "universities" in ai_insight:
            new_unis = ai_insight["universities"]
            verified_combined_unis = []
            seen_names = set()
            main_course_name = ai_insight.get('specific_course', '')

            for uni in new_unis:
                uni_name = uni.get("name", "University")
                uni_course = uni.get("specific_course", main_course_name)
                verified_link, is_verified = healer.get_verified_url(uni_name, uni_course)
                
                if is_verified and verified_link != "PLACEHOLDER_FOR_HEALER":
                    uni["website_url"] = verified_link
                    uni["safe_url"] = verified_link 
                    uni["verified_offering"] = True
                    
                    if uni_name not in seen_names:
                        verified_combined_unis.append(uni)
                        seen_names.add(uni_name)
                else:
                    hallucination_blacklist.append(f"{uni_course} at {uni_name}")

                if len(verified_combined_unis) >= 3: break

            if is_continuation:
                for old_uni in previous_unis_data:
                    if old_uni.get("name") not in seen_names and len(verified_combined_unis) < 3:
                        verified_combined_unis.append(old_uni)
                        seen_names.add(old_uni.get("name"))

            if len(verified_combined_unis) > 0: break

    if len(verified_combined_unis) == 0:
        return jsonify({
            "error": True,
            "specific_course": "General Advice",
            "level": "Career Counseling",
            "message": "We struggled to verify institutions genuinely offering this specific program for your grade profile. Try adjusting your interest slightly."
        }), 404

    ai_insight["universities"] = verified_combined_unis
    ai_insight["validated_points"] = calculated_points

    # DB Update: Save the new log safely to Postgres
    try:
        new_log = StudentLog(username=user_name, interest=user_interest, data=ai_insight)
        db.session.add(new_log)
        db.session.commit()
    except Exception as e: 
        logging.error(f"Failed to save student log to DB: {e}")

    # DB Update: Calculate trending careers from Postgres
    career_counts = Counter()
    similar_logs = StudentLog.query.filter(StudentLog.interest.ilike(f"%{user_interest}%")).all()
    for log in similar_logs:
        if log.data:
            main_role = log.data.get("ai_role")
            if main_role: career_counts[main_role.strip().title()] += 1
            alt_careers = log.data.get("alternative_careers", [])
            if isinstance(alt_careers, list):
                for alt in alt_careers:
                    if alt_name := (alt.get("name") if isinstance(alt, dict) else None): 
                        career_counts[alt_name.strip().title()] += 1

    ai_insight["trending_careers"] = [{"career": c, "count": count} for c, count in career_counts.items()]
    return jsonify(ai_insight), 200 


# ==========================================
# 🔌 THE NEW FRONTEND SCRAPE ENDPOINT
# ==========================================
@app.route('/scrape', methods=['POST'])
def scrape_data():
    """Allows the frontend React/Vanilla JS 'Find Course Link' button to use the Healer natively."""
    data = request.json
    target_url = data.get('url')
    course_name = data.get('course', 'Computer Science') 
    
    if not target_url:
        return jsonify({"error": "URL is required"}), 400
    
    try:
        logging.info(f"🕷️ Triggering frontend AutoHealer request for {course_name} at {target_url}")
        
        # We pass the domain in as the "university name" so your Healer logic works perfectly
        domain = urlparse(target_url).netloc or target_url
        verified_link, is_verified = healer.get_verified_url(domain, course_name)
        
        if is_verified and verified_link != "PLACEHOLDER_FOR_HEALER":
            return jsonify({"status": "success", "result": verified_link}), 200
        else:
             return jsonify({"error": "Could not locate the exact course page."}), 404
             
    except Exception as e:
        logging.error(f"Scraper route error: {e}")
        return jsonify({"error": "An unexpected error occurred", "details": str(e)}), 500

from sqlalchemy import text
@app.route('/fix-my-db-schema')
def fix_db():
    try:
        from sqlalchemy import text
        # Adding 'data' column which is currently missing
        db.session.execute(text("ALTER TABLE student_log ADD COLUMN IF NOT EXISTS data TEXT;"))
        # Just in case, ensuring 'username' is there too
        db.session.execute(text("ALTER TABLE student_log ADD COLUMN IF NOT EXISTS username VARCHAR(255);"))
        db.session.commit()
        return "✅ Columns 'data' and 'username' are now ready!"
    except Exception as e:
        db.session.rollback()
        return f"❌ Error: {e}"

if __name__ == "__main__":
    with app.app_context():
        # Creates PostgreSQL tables automatically if they don't exist yet
        db.create_all() 
        logging.info("✅ Database tables synchronized successfully!")
        
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)