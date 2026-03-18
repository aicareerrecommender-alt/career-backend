import os
import json
import time
import logging
import concurrent.futures
from datetime import datetime
from groq import Groq
from google import genai
from google.genai import types

GROQ_API_KEY = os.environ.get("GROQ_API_KEY") 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

client_groq = None
client_gemini = None

try:
    if GROQ_API_KEY:
        client_groq = Groq(api_key=GROQ_API_KEY, timeout=90.0, max_retries=2)
except Exception as e: logging.error(f"Groq Init Error: {e}")

try:
    if GEMINI_API_KEY:
        client_gemini = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e: logging.error(f"Gemini Init Error: {e}")

# ==========================================
# 🧮 SMART GRADE CALCULATOR
# ==========================================
def grade_to_int(grade_str):
    if not isinstance(grade_str, str): 
        return 0
    mapping = {'A': 12, 'A-': 11, 'B+': 10, 'B': 9, 'B-': 8, 'C+': 7, 'C': 6, 'C-': 5, 'D+': 4, 'D': 3, 'D-': 2, 'E': 1}
    # Clean the string just in case there are invisible spaces
    clean_grade = grade_str.strip().upper()
    return mapping.get(clean_grade, 0)

def calculate_total_points(student_grades):
    total = 0
    
    # 1. If frontend sends a normal dictionary: {"Math": "A", "English": "B"}
    if isinstance(student_grades, dict):
        for subject, grade_data in student_grades.items():
            # Sometimes frontend sends nested dicts: {"Math": {"grade": "A"}}
            if isinstance(grade_data, dict) and "grade" in grade_data:
                total += grade_to_int(str(grade_data["grade"]))
            else:
                total += grade_to_int(str(grade_data))
                
    # 2. If frontend sends a list/array: [{"subject": "Math", "grade": "A"}]
    elif isinstance(student_grades, list):
        for item in student_grades:
            if isinstance(item, dict):
                # Check for common keys frontends use
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
def validate_ai_response(ai_data, user_grades, expected_level):
    errors = []
    course_name = ai_data.get("specific_course", "").lower()
    recommended_level = ai_data.get("level", "").lower()

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
            
            # Extract actual grade intelligently based on our new math engine logic
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
                model="llama-3.1-8b-instant", response_format={"type": "json_object"}, temperature=0.3 
            )
            data = json.loads(res.choices[0].message.content)
            errors = validate_ai_response(data, grades, expected_level)
            if not errors: return data
            error_feedback = "\n- ".join(errors)
            retry_count += 1
        except Exception:
            error_feedback = "Ensure ONLY valid JSON."
            retry_count += 1
    return None

def fetch_from_gemini(system_instruction, base_prompt, grades, expected_level):
    if not client_gemini: return None
    max_retries = 3 
    retry_count = 0
    error_feedback = ""
    
    while retry_count < max_retries:
        current_prompt = base_prompt + (f"\n\n🚨 LAST RESPONSE FAILED:\n{error_feedback}\nFIX THIS." if error_feedback else "")
        try:
            res = client_gemini.models.generate_content(
                model='gemini-2.5-flash', contents=current_prompt,
                config=types.GenerateContentConfig(system_instruction=system_instruction, response_mime_type="application/json", temperature=0.3)
            )
            data = json.loads(res.text)
            errors = validate_ai_response(data, grades, expected_level)
            if not errors: return data
            error_feedback = "\n- ".join(errors)
            retry_count += 1
        except Exception as e:
            if "429" in str(e): time.sleep((retry_count + 1) * 4)
            error_feedback = "Ensure ONLY valid JSON."
            retry_count += 1
    return None

# ==========================================
# 🧠 CORE HYBRID ENGINE
# ==========================================
def ask_hybrid_career_advice(student_name, interest, grades, calculated_points, expected_level, pop_count, prev_unis, failed_hallucinations=None):
    system_instruction = """
    You are a strict, factual Kenyan KUCCPS career advisor API. 
    1. Recommend courses that actually exist at REAL KENYAN institutions. Major Universities, Colleges, and TVETs/Polytechnics ALL offer Cert/Diploma programs.
    2. OVER-GENERATE: Provide AT LEAST 8 DIFFERENT institutions.
    3. Output EXACTLY "PLACEHOLDER_FOR_HEALER" for website_url.
    4. NO GENERIC COURSES: Select specific branches (e.g., "Certificate in Electrical Engineering").
    5. CRITICAL TECH OVERRIDE: If the student is at the Artisan or Certificate level, but their passion is Technology/Coding/IT, recommend tech-adjacent practical courses like 'Artisan in ICT', 'Computer Repair', or 'Certificate in IT' instead of standard manual trades like Plumbing.
    """
    
    base_prompt = f"Student: {student_name} | Points: {calculated_points}/84 | Tier: {expected_level} | Passion: {interest}\nSubject Grades: {json.dumps(grades)}"
    
    if prev_unis: base_prompt += f"\n🚨 DO NOT recommend these universities again: {', '.join(prev_unis)}.\n"
    if failed_hallucinations:
        base_prompt += "\n🚨 CRITICAL VALIDATION ERROR: DO NOT suggest these failing combos again:\n" + "\n".join([f"- {h}" for h in failed_hallucinations]) + "\n"

    json_structure = """
    Respond ONLY with valid JSON matching this exact structure:
    {"specific_course": "Specific Name", "level": "Expected Level", "ai_role": "Specific Job Title", "interest_match_reason": "2-3 sentences.", "ai_roadmap": "A brief 3-step HTML roadmap", "career_exploration_url": "Search URL", "universities": [{"name": "Kenyan University Name", "students": 120, "specific_course": "Exact Name", "reason": "Why this fits", "website_url": "PLACEHOLDER_FOR_HEALER", "verified_offering": true, "requirements_met": [{"subject": "Math", "required": "C-", "attained": "REAL_GRADE"}]}], "alternative_careers": [{"name": "Backup Career", "exploration_url": "URL", "reason": "Reason"}]}
    """
    full_prompt = base_prompt + "\n" + json_structure

    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_groq = executor.submit(fetch_from_groq, system_instruction, full_prompt, grades, expected_level)
        future_gemini = executor.submit(fetch_from_gemini, system_instruction, full_prompt, grades, expected_level)
        groq_data, gemini_data = future_groq.result(), future_gemini.result()

    final_data = groq_data or gemini_data
    if not final_data: return None

    if groq_data and gemini_data:
        seen_unis = {u.get("name", "").lower() for u in final_data.get("universities", [])}
        for uni in gemini_data.get("universities", []):
            uni_name = uni.get("name", "").lower()
            if uni_name not in seen_unis:
                final_data["universities"].append(uni)
                seen_unis.add(uni_name)
                
        seen_alts = {a.get("name", "").lower() for a in final_data.get("alternative_careers", [])}
        for alt in gemini_data.get("alternative_careers", []):
            alt_name = alt.get("name", "").lower()
            if alt_name not in seen_alts:
                final_data["alternative_careers"].append(alt)
                seen_alts.add(alt_name)

    final_data["popularity"] = f"👥 {pop_count} other {'student' if pop_count == 1 else 'students'} asked about this!" if pop_count > 0 else "✨ You are the first to pioneer this unique career path!"
    final_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    return final_data