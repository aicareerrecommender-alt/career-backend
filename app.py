import os
import time
import logging
from collections import Counter

# Load the .env file FIRST so API keys are available locally
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash

# --- IMPORT OUR MODULAR UTILS ---
from utils.database import load_json, save_json, USER_FILE, LOGS_FILE
from utils.ai_engines import ask_hybrid_career_advice, calculate_total_points, grade_to_int
from utils.web_scraper import healer

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s]: %(message)s', datefmt='%H:%M:%S')

app = Flask(__name__)
CORS(app)

app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'your-email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your-app-password')
mail = Mail(app)

@app.route('/', methods=['GET'])
@app.route('/healthz', methods=['GET'])
def health_check():
    return jsonify({"status": "online", "message": "KUCCPS AI Auditor is active."}), 200

# --- AUTH ROUTES ---
@app.route('/register', methods=['POST'])
def register():
    data = request.json
    username, password, email = data.get('username', '').strip(), data.get('password', '').strip(), data.get('email', '').strip() 
    if not username or not password or not email: return jsonify({"message": "Required fields missing"}), 400
    
    users = load_json(USER_FILE)
    if username in users: return jsonify({"message": "User already exists!"}), 409
    
    users[username] = {"hash": generate_password_hash(password), "email": email}
    save_json(USER_FILE, users)
    
    # Example Mail usage (Uncomment to activate)
    # try:
    #     msg = Message("Welcome to KUCCPS AI Auditor", sender=app.config['MAIL_USERNAME'], recipients=[email])
    #     msg.body = f"Hello {username},\n\nYour account has been successfully registered!"
    #     mail.send(msg)
    # except Exception as e:
    #     logging.warning(f"Email failed to send: {e}")

    return jsonify({"message": "Registration successful!", "user": username}), 201

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username, password = data.get('username', '').strip(), data.get('password', '').strip()
    users = load_json(USER_FILE)
    user_data = users.get(username)
    if user_data and check_password_hash(user_data['hash'], password):
        return jsonify({"message": "Login successful", "user": username, "email": user_data['email']}), 200
    return jsonify({"message": "Invalid credentials"}), 401 

@app.route('/history', methods=['GET'])
def get_history():
    username = request.args.get('username')
    logs = load_json(LOGS_FILE)
    
    student_record = next((log for log in logs if isinstance(log, dict) and log.get("student_name") == username), None)
    
    if student_record and "history" in student_record:
        return jsonify(student_record["history"][::-1]), 200
    elif student_record and "ai_response" in student_record:
        return jsonify([student_record["ai_response"]]), 200
    return jsonify([]), 200

# --- MAIN RECOMMENDATION ROUTE ---
@app.route('/recommend', methods=['POST', 'OPTIONS'])
def recommend():
    if request.method == 'OPTIONS': return '', 200

    data = request.json
    user_name = data.get('studentName', 'Anonymous')
    user_interest = data.get('interestArea', 'General')
    user_grades = data.get('subjects', {})

    logging.info(f"🚀 New Request Started for {user_name} - Interest: {user_interest}")

    calculated_points = calculate_total_points(user_grades)
    if calculated_points == 0: calculated_points = data.get('aggregatePoints', 0)

    math_grade = user_grades.get("Mathematics", user_grades.get("Math", "E"))
    
    if grade_to_int(math_grade) < 5: 
        expected_level = "Artisan/Craft Certificate"
        user_interest += f". STRICT RULES: Student has a '{math_grade}' in Math. DO NOT suggest Degrees or STEM Diplomas. ONLY suggest Artisan or Craft Certificates."
    elif calculated_points >= 49: expected_level = "Degree"
    elif calculated_points >= 35: expected_level = "Diploma"
    elif calculated_points >= 21: expected_level = "Certificate"
    elif calculated_points > 0: expected_level = "Artisan"
    else: expected_level = "Unknown"

    student_logs = load_json(LOGS_FILE)
    popularity = sum(1 for log in student_logs if isinstance(log, dict) and log.get('interest', '').lower() == user_interest.lower())
    
    previous_unis_names, previous_unis_data = [], []
    is_continuation = False 
    
    student_record = next((log for log in student_logs if isinstance(log, dict) and log.get("student_name") == user_name), None)
    
    if student_record and "history" in student_record and len(student_record["history"]) > 0:
        last_search = student_record["history"][-1]
        if last_search.get('level', '') == expected_level and student_record.get('interest', '').lower() == user_interest.lower():
            is_continuation = True
            for uni in last_search.get('universities', []):
                if uni.get('name') and uni.get('name') not in previous_unis_names:
                    previous_unis_names.append(uni.get('name'))
                    previous_unis_data.append(uni)

    # 🔄 Outer Hallucination Retry Loop
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
            return jsonify({"error": True, "message": "Server encountered an error."}), 503
        
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
        return jsonify({"error": True, "message": "Could not verify authentic institutions offering this. Try a related path."}), 404

    ai_insight["universities"] = verified_combined_unis
    ai_insight["validated_points"] = calculated_points

    # 🟢 Update the SAME student's history array
    try:
        if not isinstance(student_logs, list): student_logs = []
        
        if student_record:
            logging.info(f"🔄 Updating existing record for student: {user_name}")
            student_record["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            student_record["interest"] = user_interest
            student_record["grades"] = user_grades
            student_record["points"] = calculated_points
            student_record["ai_response"] = ai_insight
            
            if "history" not in student_record: student_record["history"] = []
            student_record["history"].append(ai_insight)
        else:
            logging.info(f"🆕 Creating new record for student: {user_name}")
            student_logs.append({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "student_name": user_name, 
                "interest": user_interest,
                "grades": user_grades, 
                "points": calculated_points, 
                "ai_response": ai_insight,
                "history": [ai_insight]
            })
            
        save_json(LOGS_FILE, student_logs)
    except Exception as e: 
        logging.error(f"Failed to save student log: {e}")

    # 📈 Calculate Trending Careers
    career_counts = Counter()
    for log in student_logs:
        if isinstance(log, dict) and log.get("interest", "").lower() == user_interest.lower():
            main_role = log.get("ai_response", {}).get("ai_role")
            if main_role: career_counts[main_role.strip().title()] += 1
            alt_careers = log.get("ai_response", {}).get("alternative_careers", [])
            if isinstance(alt_careers, list):
                for alt in alt_careers:
                    if alt_name := (alt.get("name") if isinstance(alt, dict) else None): 
                        career_counts[alt_name.strip().title()] += 1

    ai_insight["trending_careers"] = [{"career": c, "count": count} for c, count in career_counts.items()]
    return jsonify(ai_insight), 200 

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port, debug=False)