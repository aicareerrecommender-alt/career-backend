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
from typing import Dict, List, Any


# ==========================================
# 1. CONFIGURATION & GLOBALS
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COURSES_DB_PATH = os.path.join(BASE_DIR, 'kuccps_courses.json')

GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 


# ==========================================
# 2. HELPER FUNCTIONS (Must be defined first)
# ==========================================
def normalize_course_name(name: str) -> str:
    if not name: 
        return ""
    
    # Lowercase and strip whitespace
    name = str(name).lower().strip()
    
    # Standardize common variations
    replacements = {
        "bachelor of science": "bsc",
        "bachelor of arts": "ba",
        "bachelor of education": "bed",
        "diploma in": "diploma",
        "certificate in": "certificate",
        "information technology": "it",
        "information communication technology": "ict",
        "computing": "computer",
    }
    
    for old_val, new_val in replacements.items():
        name = name.replace(old_val, new_val)
        
    # Remove non-alphanumeric but KEEP spaces temporarily
    name = re.sub(r'[^a-z0-9\s]', '', name)
    # Remove all whitespace for the final comparison string
    return "".join(name.split())

def grade_to_int(grade_str) -> int:
    """Maps Kenyan grades to integers."""
    if not isinstance(grade_str, str): 
        return 0
    mapping = {
        'A': 12, 'A-': 11, 'B+': 10, 'B': 9, 'B-': 8, 'C+': 7, 
        'C': 6, 'C-': 5, 'D+': 4, 'D': 3, 'D-': 2, 'E': 1
    }
    clean_grade = grade_str.strip().upper()
    return mapping.get(clean_grade, 0)

# ==========================================
# 3. DATABASE & AI INITIALIZATION
# ==========================================
def load_master_courses():
    """Loads the real KUCCPS courses and creates a fast lookup list."""
    try:
        if os.path.exists(COURSES_DB_PATH):
            with open(COURSES_DB_PATH, 'r', encoding='utf-8') as f:
                courses = json.load(f)
                clean_courses = []
                for c in courses:
                    # 🚨 FIX: Split the string just like the funnel does!
                    clean_name = re.split(r'\d+\.\d+|-', str(c))[0].strip()
                    clean_courses.append(clean_name)
                return clean_courses
        else:
            logging.warning(f"⚠️ Course DB not found at {COURSES_DB_PATH}")
    except Exception as e:
        logging.error(f"Error loading course database: {e}")
    return []

# Load into memory ONCE
MASTER_COURSE_LIST = load_master_courses()
NORMALIZED_MASTER_LIST = {normalize_course_name(c) for c in MASTER_COURSE_LIST}

# Initialize Groq Client
client_groq = None
try:
    if GROQ_API_KEY:
        client_groq = Groq(api_key=GROQ_API_KEY, timeout=90.0, max_retries=2)
    else:
        logging.warning("⚠️ GROQ_API_KEY missing. AI fetching disabled.")
except Exception as e: 
    logging.error(f"Groq Init Error: {e}")

# ==========================================
# 4. CORE LOGIC & FUNNELS
# ==========================================
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

def get_eligible_context(interest: str, student_grades: Dict[str, str], db_path: str = COURSES_DB_PATH) -> List[str]:
    eligible_matches = []
    keywords = [k.lower() for k in interest.split() if len(k) > 2]
    
    try:
        with open(db_path, 'r', encoding='utf-8') as f:
            course_database = json.load(f)

        for entry in course_database:
            if not any(kw in entry.lower() for kw in keywords):
                continue
            
            req_matches = re.findall(r'([A-Za-z/]+)(?:\(\d+\))?:([A-Z][+-]?)', entry)
            is_eligible = True
            
            for req_subject, req_grade in req_matches:
                actual_grade = "E"
                for u_subj, u_grade in student_grades.items():
                    if req_subject.lower()[:3] in u_subj.lower():
                        actual_grade = u_grade
                        break
                
                if grade_to_int(actual_grade) < grade_to_int(req_grade):
                    is_eligible = False
                    break
            
            if is_eligible:
                clean_name = re.split(r'\d+\.\d+|-', entry)[0].strip()
                eligible_matches.append(clean_name)
                
    except Exception as e:
        logging.error(f"Funnel execution error: {e}")
    
    return list(set(eligible_matches))[:20]

# ==========================================
# 5. VALIDATORS
# ==========================================
def validate_course_names(ai_response_data):
    if not ai_response_data or "universities" not in ai_response_data:
        return ai_response_data

    valid_unis = []
    for uni in ai_response_data.get("universities", []):
        course_name = uni.get("specific_course", "")
        ai_norm = normalize_course_name(course_name)
        
        # Pylance is happy now because NORMALIZED_MASTER_LIST is globally defined at the top
        if NORMALIZED_MASTER_LIST and ai_norm in NORMALIZED_MASTER_LIST:
            uni["db_verified_name"] = True
            valid_unis.append(uni)
        else:
            logging.warning(f"🚨 STRICT BLOCK: Removed hallucinated course '{course_name}'")
            
    ai_response_data["universities"] = valid_unis
    return ai_response_data

def validate_ai_response(ai_data, user_grades, expected_level):
    errors = []
    course_name = ai_data.get("specific_course", "").lower()
    recommended_level = ai_data.get("level", "").lower()

    if "degree" in recommended_level:
        if "engineer" in course_name or "mechatronic" in course_name:
            if grade_to_int(str(user_grades.get("Mathematics", user_grades.get("Math", "E")))) < 7 or grade_to_int(str(user_grades.get("Physics", "E"))) < 7:
                return ["CRITICAL ERROR: Engineering Degree requires C+ in Math and Physics."]
        if "med" in course_name or "nurs" in course_name or "clinic" in course_name or "surg" in course_name:
            if grade_to_int(str(user_grades.get("Biology", "E"))) < 7 or grade_to_int(str(user_grades.get("Chemistry", "E"))) < 7:
                return ["CRITICAL ERROR: Medical Degree requires C+ in Bio/Chem."]
        if "comput" in course_name or "software" in course_name or "it" in course_name:
            if grade_to_int(str(user_grades.get("Mathematics", user_grades.get("Math", "E")))) < 7:
                return ["CRITICAL ERROR: IT Degree requires C+ in Math."]
    elif "diploma" in recommended_level:
        if "engineer" in course_name or "comput" in course_name or "software" in course_name or "it" in course_name:
            if grade_to_int(str(user_grades.get("Mathematics", user_grades.get("Math", "E")))) < 5: 
                return ["CRITICAL ERROR: STEM Diplomas require C- in Math."]

    valid_unis = []
    for uni in ai_data.get("universities", []):
        reqs = uni.get("requirements_met", [])
        if not reqs: continue 
            
        uni_is_valid = True
        for req in reqs:
            subject = req.get("subject", "")
            required_grade = req.get("required", "E")
            actual_grade = "E"
            
            for user_subj, user_grade in (user_grades.items() if isinstance(user_grades, dict) else []):
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
        errors.append("CRITICAL ERROR: ALL universities generated had requirements higher than grades.")
    return errors

# ==========================================
# 6. ORCHESTRATORS / AI FETCHERS
# ==========================================
def fetch_from_groq(system_instruction, base_prompt, grades, expected_level):
    if not client_groq: 
        logging.error("Groq client not initialized.")
        return None
        
    max_retries = 3 
    retry_count = 0
    current_prompt = base_prompt 
    
    while retry_count < max_retries:
        try:
            res = client_groq.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_instruction}, 
                    {"role": "user", "content": current_prompt}
                ],
                model="llama-3.1-8b-instant", 
                response_format={"type": "json_object"}, 
                temperature=0.3 
            )
            data = json.loads(res.choices[0].message.content)
            
            validate_ai_response(data, grades, expected_level)
            data = validate_course_names(data)
            
            valid_count = len(data.get("universities", []))
            
            if valid_count >= 5:
                logging.info(f"✅ FINAL SUCCESS: Delivering {valid_count} valid courses.")
                return data
            
            logging.warning(f"⚠️ Threshold failed: Found {valid_count} courses. Forcing AI top-up...")
            current_prompt = base_prompt + f"\n\n🚨 SYSTEM FEEDBACK: You only provided {valid_count} valid courses. Provide AT LEAST 8 valid, distinct universities/courses from the list."
            
            retry_count += 1
            time.sleep(2)

        except Exception as e:
            logging.error(f"🚨 Groq Execution Error: {e}")
            retry_count += 1
            time.sleep(2)
            
    return None

# ==========================================
# 🧠 CORE HYBRID ENGINE
# ==========================================
def ask_hybrid_career_advice(student_name, interest, grades, calculated_points, expected_level, pop_count=0, exclude_unis=None, successful_unis=None):
    # --- 1. FILTER DATABASE FIRST ---
    valid_courses = get_eligible_context(interest, grades)
    
    # --- 2. DYNAMIC TIER GUIDANCE ---
    # Instead of hardcoding "D+ range", we determine guidance by points
    if calculated_points >= 46:
        tier_status = "Degree/Diploma"
        tier_guidance = "The student has strong grades. Prioritize University Degrees."
    elif calculated_points >= 30:
        tier_status = "Diploma/Certificate"
        tier_guidance = "Recommend Diplomas at National Polytechnics or Universities."
    else:
        tier_status = "Certificate/Artisan"
        tier_guidance = "Recommend Certificates or Artisan courses at TVET institutions."

    # --- 3. DYNAMIC SYSTEM INSTRUCTION ---
    system_instruction = f"""
    You are a strict Kenyan KUCCPS advisor.
    
    1. DATABASE CONSTRAINTS: You MUST pick the 'specific_course' ONLY from this list: {", ".join(valid_courses) if valid_courses else "Suggest relevant TVET courses."}
    2. ABBREVIATION RULE: Always use 'BSc.', 'B.Ed.', or 'Diploma' correctly.
    3. INSTITUTION RADIUS: Provide AT LEAST 8 DIFFERENT real Kenyan institutions.
    4. URL POLICY: Output EXACTLY "PLACEHOLDER_FOR_HEALER" for website_url.
    5. STRICT NAMING: The `specific_course` field MUST be an exact, formal degree or diploma name (e.g., 'Bachelor of Science in Mathematics'). NEVER output conversational text, phrases, or student interests in this field.
    
    🚨 GRADE-BASED STRATEGY:
    - Student Total Points: {calculated_points}/84.
    - Guidance: {tier_guidance}
    - If the student's grades in core subjects (Math/Science) are low, pivot level down (e.g., Degree to Diploma) but keep the interest.
    """
    
    exclusion_rule = ""
    if exclude_unis:
        exclusion_rule += f"\n🚨 DO NOT suggest: {', '.join(exclude_unis)}."

    # --- 4. PREPARE THE PROMPT ---
    base_prompt = f"Student: {student_name} | Points: {calculated_points}/84 | Requested Tier: {expected_level} | Passion: {interest}\nSubject Grades: {json.dumps(grades)}{exclusion_rule}"

    json_structure = """
    Respond ONLY with valid JSON:
    {
        "specific_course": "Name", "level": "Level", "ai_role": "Job", 
        "interest_match_reason": "Reason", "ai_roadmap": "HTML steps", 
        "career_exploration_url": "Search URL", 
        "universities": [{"name": "Uni Name", "students": 100, "specific_course": "Course", "reason": "Why", "website_url": "PLACEHOLDER_FOR_HEALER", "verified_offering": true, "requirements_met": [{"subject": "Math", "required": "C", "attained": "B"}]}],
        "alternative_careers": [{"name": "Job", "title": "Job", "description": "Desc", "fit": "Why"}]
    }
    """
    full_prompt = base_prompt + "\n" + json_structure

   # --- 5. EXECUTE GROQ CALL ---
    final_data = fetch_from_groq(system_instruction, full_prompt, grades, expected_level)
    
    # If Groq failed OR if the validator stripped all universities due to grade mismatches
    if not final_data or not final_data.get("universities"): 
        logging.error(f"❌ Engine Failure: No valid data or universities for {student_name}")
        return None

    # --- 6. METADATA & RETURN ---
    # Ensure these keys exist in the dictionary
    final_data["popularity"] = f"👥 {pop_count} other students asked about this!" if pop_count > 0 else "✨ You are the first to pioneer this path!"
    final_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    logging.info(f"✅ SUCCESS: Returning validated data for {student_name}")
    return final_data