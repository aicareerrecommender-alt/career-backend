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
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm.attributes import flag_modified

# --- IMPORT OUR MODULAR UTILS ---
from utils.database import load_json, save_json, USER_FILE, LOGS_FILE
from utils.ai_engines import ask_hybrid_career_advice, calculate_total_points, grade_to_int
from utils.web_scraper import healer

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s]: %(message)s', datefmt='%H:%M:%S')

app = Flask(__name__)
CORS(app)

# --- NEW: POSTGRESQL DATABASE CONFIGURATION ---
db_url = os.environ.get("DATABASE_URL", "sqlite:///local_test.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(120), nullable=False)

class StudentLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_name = db.Column(db.String(80), nullable=False)
    interest = db.Column(db.String(120))
    grades = db.Column(db.JSON)
    points = db.Column(db.Integer)
    ai_response = db.Column(db.JSON)
    history = db.Column(db.JSON)
    timestamp = db.Column(db.String(50))

with app.app_context():
    db.create_all()

# --- DATABASE BRIDGE FUNCTIONS (Preserves your exact dictionary logic!) ---
def get_all_users_as_dicts():
    users = User.query.all()
    return {u.username: {"hash": u.password_hash, "email": u.email} for u in users}

def get_all_logs_as_dicts():
    logs = StudentLog.query.all()
    return [{
        "student_name": l.student_name, "interest": l.interest,
        "grades": l.grades, "points": l.points,
        "ai_response": l.ai_response, "history": l.history, "timestamp": l.timestamp
    } for l in logs]
# -------------------------------------------------------------------------

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
    
    # REPLACED load_json: Now loads from DB but keeps your dictionary format
    users = get_all_users_as_dicts()
    if username in users: return jsonify({"message": "User already exists!"}), 409
    
    users[username] = {"hash": generate_password_hash(password), "email": email}
    
    # REPLACED save_json: Saves the new user directly to DB
    new_user = User(username=username, password_hash=users[username]["hash"], email=email)
    db.session.add(new_user)
    db.session.commit()
    
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
    
    # REPLACED load_json: Now loads from DB
    users = get_all_users_as_dicts()
    
    user_data = users.get(username)
    if user_data and check_password_hash(user_data['hash'], password):
        return jsonify({"message": "Login successful", "user": username, "email": user_data['email']}), 200
    return jsonify({"message": "Invalid credentials"}), 401 

@app.route('/history', methods=['GET'])
def get_history():
    username = request.args.get('username')
    
    # REPLACED load_json: Now loads from DB
    logs = get_all_logs_as_dicts()
    
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

    # REPLACED load_json: Now loads from DB
    student_logs = get_all_logs_as_dicts()
    
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
            
            # REPLACED save_json: Update existing DB record
            db_record = StudentLog.query.filter_by(student_name=user_name).first()
            if db_record:
                db_record.timestamp = student_record["timestamp"]
                db_record.interest = student_record["interest"]
                db_record.grades = student_record["grades"]
                db_record.points = student_record["points"]
                db_record.ai_response = student_record["ai_response"]
                db_record.history = student_record["history"]
                flag_modified(db_record, "history") # Required for JSON arrays in Postgres
                db.session.commit()
                
        else:
            logging.info(f"🆕 Creating new record for student: {user_name}")
            new_entry = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "student_name": user_name, 
                "interest": user_interest,
                "grades": user_grades, 
                "points": calculated_points, 
                "ai_response": ai_insight,
                "history": [ai_insight]
            }
            student_logs.append(new_entry)
            
            # REPLACED save_json: Insert new DB record
            new_db_record = StudentLog(**new_entry)
            db.session.add(new_db_record)
            db.session.commit()
            
    except Exception as e: 
        logging.error(f"Failed to save student log: {e}")
        db.session.rollback()

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