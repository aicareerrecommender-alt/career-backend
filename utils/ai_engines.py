import os
import re  # Added for course normalization
import json
import time
import logging
import concurrent.futures
from datetime import datetime
from groq import Groq
from google import genai
from google.genai import types

# --- API KEYS ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# --- DATABASE LOADING ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Adjust the path below if your json file is in a different folder (like /data)
COURSES_DB_PATH = os.path.join(BASE_DIR,'kuccps_courses.json')

def normalize_course_name(name):
    """
    Lowercase, replace common KUCCPS synonyms/abbreviations, 
    and remove non-alphanumerics for lenient fuzzy matching.
    """
    if not name: 
        return ""
        
    name = str(name).lower()
    
    # Replace common synonyms/abbreviations before stripping characters
    # Using a dictionary makes it easy to add new rules later
    replacements = {
        "bachelor of science": "bsc",
        "bachelor of arts": "ba",
        "bachelor of education": "bed",
        "diploma in": "dip",
        "certificate in": "cert",
        " in ": "",
        " of ": "",
        " and ": ""
    }
    
    for old_val, new_val in replacements.items():
        name = name.replace(old_val, new_val)
        
    # Strip non-alphanumeric characters (removes spaces, hyphens, ampersands, etc.)
    return re.sub(r'[^a-z0-9]', '', name)

def load_master_courses():
    """Loads the real KUCCPS courses and creates a fast, lookup list."""
    try:
        if os.path.exists(COURSES_DB_PATH):
            with open(COURSES_DB_PATH, 'r', encoding='utf-8') as f:
                courses = json.load(f)
                return [str(c).strip() for c in courses]
        else:
            logging.warning(f"⚠️ Course DB not found at {COURSES_DB_PATH}")
    except Exception as e:
        logging.error(f"Error loading course database: {e}")
    return []

# Load into memory once for speed
MASTER_COURSE_LIST = load_master_courses()
# O(1) lookup set for blazing fast normalized validation
NORMALIZED_MASTER_LIST = {normalize_course_name(c) for c in MASTER_COURSE_LIST}

# --- AI CLIENT INITIALIZATION ---
client_groq = None
client_gemini = None

try:
    if GROQ_API_KEY:
        client_groq = Groq(api_key=GROQ_API_KEY, timeout=90.0, max_retries=2)
except Exception as e: 
    logging.error(f"Groq Init Error: {e}")

try:
    if GEMINI_API_KEY:
        client_gemini = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e: 
    logging.error(f"Gemini Init Error: {e}")

# ==========================================
# 🧮 SMART GRADE CALCULATOR
# ==========================================
def grade_to_int(grade_str):
    if not isinstance(grade_str, str): 
        return 0
    mapping = {'A': 12, 'A-': 11, 'B+': 10, 'B': 9, 'B-': 8, 'C+': 7, 'C': 6, 'C-': 5, 'D+': 4, 'D': 3, 'D-': 2, 'E': 1}
    clean_grade = grade_str.strip().upper()
    return mapping.get(clean_grade, 0)

def calculate_total_points(student_grades):
    total = 0
    
    if isinstance(student_grades, dict):
        for subject, grade_data in student_grades.items():
            if isinstance(grade_data, dict) and "grade" in grade_data:
                total += grade_to_int(str(grade_data["grade"]))
            else:
                total += grade_to_int(str(grade_data))
                
    elif isinstance(student_grades, list):
        for item in student_grades:
            if isinstance(item, dict):
                grade_val = item.get("grade") or item.get("value") or "E"
                total += grade_to_int(str(grade_val))
            elif isinstance(item, str):
                total += grade_to_int(item)

    print(f"🧮 [MATH ENGINE] Frontend sent: {student_grades}")
    print(f"🧮 [MATH ENGINE] Calculated Total Points: {total}/84")
    
    return total

# ==========================================
# 🛡️ VALIDATORS & AI FETCHERS
# ==========================================
def validate_course_names(ai_response_data):
    """
    Cross-references AI suggestions against the local KUCCPS database to flag hallucinations.
    Uses fuzzy normalization to handle minor punctuation/casing differences.
    """
    if not ai_response_data or "universities" not in ai_response_data:
        return ai_response_data

    for uni in ai_response_data.get("universities", []):
        course_name = uni.get("specific_course", "")
        
        # Safe, normalized matching
        ai_norm = normalize_course_name(course_name)
        if NORMALIZED_MASTER_LIST and ai_norm not in NORMALIZED_MASTER_LIST:
            logging.warning(f"⚠️ AI Hallucinated Course Name: {course_name}")
            uni["db_verified_name"] = False
        else:
            uni["db_verified_name"] = True
            
    return ai_response_data

def validate_ai_response(ai_data, user_grades, expected_level):
    errors = []
    course_name = ai_data.get("specific_course", "").lower()
    recommended_level = ai_data.get("level", "").lower()

    # Hardcoded statutory requirements for STEM and Medicine
    if "degree" in recommended_level:
        if "engineer" in course_name or "mechatronic" in course_name:
            if grade_to_int(str(user_grades.get("Mathematics", user_grades.get("Math", "E")))) < 7 or grade_to_int(str(user_grades.get("Physics", "E"))) < 7:
                return ["CRITICAL ERROR: Engineering Degree requires C+ in Math and Physics. Downgrade to Diploma/Cert."]
        if "med" in course_name or "nurs" in course_name or "clinic" in course_name or "surg" in course_name:
            if grade_to_int(str(user_grades.get("Biology", "E"))) < 7 or grade_to_int(str(user_grades.get("Chemistry", "E"))) < 7:
                return ["CRITICAL ERROR: Medical Degree requires C+ in Bio/Chem. Downgrade tier."]
        if "comput" in course_name or "software" in course_name or "it" in course_name:
            if grade_to_int(str(user_grades.get("Mathematics", user_grades.get("Math", "E")))) < 7:
                return ["CRITICAL ERROR: IT Degree requires C+ in Math. Downgrade tier."]
    elif "diploma" in recommended_level:
        if "engineer" in course_name or "comput" in course_name or "software" in course_name or "it" in course_name:
            if grade_to_int(str(user_grades.get("Mathematics", user_grades.get("Math", "E")))) < 5: 
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
            
            for user_subj, user_grade in user_grades.items() if isinstance(user_grades, dict) else []:
                if user_subj.lower().startswith(subject.lower()[:4]):
                    actual_grade = user_grade.get("grade", "E") if isinstance(user_grade, dict) else str(user_grade)
                    break
            
            if grade_to_int(actual_grade) < grade_to_int(required_grade):
                uni_is_valid = False
                break 
                
        if uni_is_valid: 
            uni["requirements_met"] = [{"subject": "Statutory Requirements", "required": "Met", "attained": "Verified"}]
            valid_unis.append(uni)

    ai_data["universities"] = valid_unis
    if len(valid_unis) == 0:
        errors.append("CRITICAL ERROR: ALL universities generated had requirements higher than the student's actual grades.")
    return errors
def fetch_from_groq(system_instruction, base_prompt, grades, expected_level):
    if not client_groq: return None
    max_retries = 3 
    retry_count = 0
    error_feedback = ""
    
    while retry_count < max_retries:
        current_prompt = base_prompt + (f"\n\n🚨 LAST RESPONSE FAILED:\n{error_feedback}\nFIX THIS." if error_feedback else "")
        try:
            res = client_groq.chat.completions.create(
                messages=[{"role": "system", "content": system_instruction}, {"role": "user", "content": current_prompt}],
                model="llama-3.3-70b-specdec", 
                response_format={"type": "json_object"}, 
                temperature=0.3 
            )
            data = json.loads(res.choices[0].message.content)
            errors = validate_ai_response(data, grades, expected_level)
            if not errors: 
                return validate_course_names(data)
            
            # 🚨 ADJUSTMENT: If validation fails, sleep before retrying
            error_feedback = "\n- ".join(errors)
            logging.warning(f"⏳ Groq validation failed. Attempt {retry_count+1}. Sleeping 10s...")
            time.sleep(10) 
            retry_count += 1

        except Exception as e:
            # 🚨 ADJUSTMENT: If API hits a rate limit (429), sleep longer
            logging.error(f"🚨 Groq API Error: {e}. Sleeping 15s...")
            time.sleep(15) 
            error_feedback = "Ensure ONLY valid JSON."
            retry_count += 1
            
    return None


# ==========================================
# 🧠 CORE HYBRID ENGINE
# ==========================================
# ==========================================
# 🧠 CORE HYBRID ENGINE
# ==========================================
def ask_hybrid_career_advice(student_name, interest, grades, calculated_points, expected_level, pop_count=0, exclude_unis=None, successful_unis=None):
    style_sample = ""
    if MASTER_COURSE_LIST:
        sample_list = MASTER_COURSE_LIST[100:120] if len(MASTER_COURSE_LIST) > 120 else MASTER_COURSE_LIST[:20]
        style_sample = f"\n7. Formatting Examples of VALID KUCCPS courses: {', '.join(sample_list)}..."

    # 1. System Instruction (Unchanged)
    system_instruction = f"""
    You are a strict, factual Kenyan KUCCPS career advisor API. 
    
    1. DATABASE CONSTRAINTS: You MUST only recommend course titles that match this specific naming convention: {', '.join(MASTER_COURSE_LIST[:10])}.
    2. ABBREVIATION RULE: Always use 'BSc.' instead of 'Bachelor of Science'. Use 'B.Ed.' instead of 'Bachelor of Education'.
    3. NO HALLUCINATIONS: Do not invent courses. If 'Computer Engineering' is not in the list, you MUST suggest 'BSc. Computer Science' or 'BSc. Software Engineering' instead.
    4. INSTITUTION RADIUS: Provide AT LEAST 8 DIFFERENT real Kenyan institutions offering the exact same course.
    5. URL POLICY: Output EXACTLY "PLACEHOLDER_FOR_HEALER" for website_url.
    6. TECH OVERRIDE: For Artisan/Certificate levels with IT passion, use 'Artisan in ICT' or 'Certificate in IT'.
    
    🚨 THE PIVOT STRATEGY (CRITICAL): 
    If a student has a low grade (e.g., an 'E' in Math or a 'D' overall) but wants a highly technical field like "Engineering" or "Medicine", DO NOT recommend a University Degree. 
    Instead, maintain their exact interest but pivot the institution and course level. Recommend Artisan Certificates, Craft Certificates, or Diplomas at recognized Kenyan TVETs (e.g., Kabete National Polytechnic, Sigalagala National Polytechnic, Kenya Coast National Polytechnic).
    
    {style_sample}
    """
    
    exclusion_rule = ""
    if exclude_unis:
        bad_unis_str = ", ".join(exclude_unis)
        exclusion_rule += f"\n🚨 FAILED HALLUCINATIONS: The following institutions DO NOT offer this course or have broken links. YOU MUST NOT suggest any of these: {bad_unis_str}."

    if successful_unis:
        good_unis_str = ", ".join(successful_unis)
        exclusion_rule += f"\n✅ ALREADY VERIFIED: You have already successfully recommended these institutions: {good_unis_str}. DO NOT output them again. Generate DIFFERENT institutions to complete the list."

    base_prompt = f"Student: {student_name} | Points: {calculated_points}/84 | Tier: {expected_level} | Passion: {interest}\nSubject Grades: {json.dumps(grades)}{exclusion_rule}"

    json_structure = """
    Respond ONLY with valid JSON matching this exact structure:
    {
        "specific_course": "Specific Name", 
        "level": "Expected Level", 
        "ai_role": "Specific Job Title", 
        "interest_match_reason": "2-3 sentences explaining how this fits their passion.", 
        "ai_roadmap": "A brief 3-step HTML roadmap", 
        "career_exploration_url": "Search URL", 
        "universities": [
            {"name": "Kenyan University or Polytechnic Name", "students": 120, "specific_course": "Exact Name", "reason": "Why this fits", "website_url": "PLACEHOLDER_FOR_HEALER", "verified_offering": true, "requirements_met": [{"subject": "Math", "required": "E", "attained": "REAL_GRADE"}]}
        ],
        "alternative_careers": [
            {"name": "Job Title", "title": "Job Title", "description": "1-2 sentence description of the career.", "fit": "Why this is a great alternative fit for the student."}
        ]
    }
    """
    full_prompt = base_prompt + "\n" + json_structure

    # 🚨 THE FIX: REMOVED THREADPOOL AND GEMINI. CALLING GROQ DIRECTLY.
    # This uses the fetch_from_groq function you defined earlier in the file.
    final_data = fetch_from_groq(system_instruction, full_prompt, grades, expected_level)

    # If Groq fails or hits rate limits after its internal retries, we return None
    # so that app.py can catch it and show a 503 error.
    if not final_data: 
        return None

    # Final metadata additions
    final_data["popularity"] = f"👥 {pop_count} other {'student' if pop_count == 1 else 'students'} asked about this!" if pop_count > 0 else "✨ You are the first to pioneer this unique career path!"
    final_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    return final_data