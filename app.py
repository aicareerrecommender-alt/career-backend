import os
import time
import logging
import concurrent.futures
from collections import Counter
from datetime import datetime
import io                   # For creating the PDF in memory
from xhtml2pdf import pisa  # For converting HTML to PDF

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

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

import base64
import logging
import asyncio # <-- ADD THIS HERE
import concurrent.futures
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s]: %(message)s', datefmt='%H:%M:%S')
import urllib.parse

app = Flask(__name__)
# Enable CORS for all routes so your frontend can communicate without being blocked
CORS(app)

# --- FLASK-MAIL CONFIGURATION ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
mail = Mail(app)

# --- POSTGRESQL DATABASE CONFIGURATION ---
db_url = os.environ.get("DATABASE_URL", "sqlite:///local_test.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- DATABASE MODELS ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    _has_taken_test = db.Column(db.Boolean, default=False)
    is_verified = db.Column(db.Boolean, default=False)
    verification_code = db.Column(db.String(10), nullable=True)
    history = db.Column(db.JSON, default=list)

class StudentLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String(50))
    student_name = db.Column(db.String(100))
    interest = db.Column(db.String(100))
    points = db.Column(db.Integer)
    grades = db.Column(db.JSON)
    ai_response = db.Column(db.JSON)
    history = db.Column(db.JSON, default=list)

with app.app_context():
    # db.drop_all() # Uncomment this temporarily if you ever need to completely wipe the database
    db.create_all()

users_db = load_json(USER_FILE)
student_logs = load_json(LOGS_FILE)

# ==========================================
# ✨ GOOGLE SIGN-IN ENDPOINT ✨
# ==========================================
GOOGLE_CLIENT_ID = "55276360637-aijk41qg09i78s3inr24bsnai1k1huqu.apps.googleusercontent.com"

@app.route('/google-login', methods=['POST'])
def google_login():
    data = request.json
    token = data.get('token')

    if not token:
        return jsonify({"message": "No token provided"}), 400

    try:
        # 1. Verify the token with Google
        idinfo = id_token.verify_oauth2_token(
            token, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID
        )

        # 2. Extract user info from the verified token
        email = idinfo['email']
        name = idinfo.get('name', 'Student') # Defaults to 'Student' if no name

        # 3. Check our database
        users = load_json(USER_FILE)

        if email in users:
            # User already exists -> Log them in!
            return jsonify({
                "message": "Login successful", 
                "name": users[email]['name'], 
                "email": email
            }), 200
        else:
            # New user -> Auto-register them! 
            # (No password needed, and they are already verified by Google)
            users[email] = {
                "name": name,
                "password": "GOOGLE_AUTH_USER", # Placeholder so they can't login with a blank normal password
                "verified": True
            }
            save_json(USER_FILE, users)
            
            return jsonify({
                "message": "Account created and logged in!", 
                "name": name, 
                "email": email
            }), 200

    except ValueError:
        # If a hacker tries to send a fake token, Google's library catches it here
        return jsonify({"message": "Invalid Google token"}), 401
# ==========================================
# 🚀 AUTHENTICATION & HISTORY ROUTES
# ==========================================

@app.route('/')
def home():
    return jsonify({"message": "KUCCPS AI Backend is running beautifully!"}), 200

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')

    if not name or not email or not password:
        return jsonify({"message": "Name, email, and password are required"}), 400

    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({"message": "User already exists"}), 400

    hashed_pw = generate_password_hash(password)
    verification_code = str(int(time.time()))[-6:]

    new_user = User(
        name=name, email=email, password_hash=hashed_pw,
        is_verified=False, verification_code=verification_code
    )
    db.session.add(new_user)
    db.session.commit()

    users_db[email] = {
        "name": name, "password": hashed_pw, 
        "has_taken_test": False, "is_verified": False, 
        "verification_code": verification_code, "history": []
    }
    save_json(USER_FILE, users_db)

    # --- ACTION REQUIRED EMAIL (WITH MAGIC LINK) ---
    try:
        msg = Message('Action Required: Verify your CareerPath AI account ✉️', 
                      sender=app.config['MAIL_USERNAME'], 
                      recipients=[email])
        
        msg.body = f"Hello {name},\n\nWelcome to CareerPath AI! Your verification code is: {verification_code}\n\nPlease enter this code on the website to activate your account."
        
        msg.html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
            <div style="background-color: #0d6efd; color: white; padding: 25px; text-align: center;">
                <h2 style="margin: 0; font-size: 24px;">Verify Your Email Address</h2>
            </div>
            
            <div style="padding: 30px; color: #333; line-height: 1.6;">
                <p style="font-size: 16px; margin-top: 0;">Hello <b>{name}</b>,</p>
                <p style="font-size: 16px;">Welcome to <b style="color: #0d6efd;">CareerPath AI</b>! You are just one step away from unlocking your personalized university and career recommendations.</p>
                
                <p style="font-size: 16px;">To securely activate your account, please enter the following verification code on your screen:</p>
                
                <div style="text-align: center; margin: 35px 0;">
                    <span style="display: inline-block; font-size: 32px; font-weight: bold; background-color: #f8f9fa; padding: 15px 40px; border-radius: 8px; border: 2px dashed #0d6efd; letter-spacing: 8px; color: #0d6efd;">
                        {verification_code}
                    </span>
                </div>
                <div style="text-align: center; margin-top: 30px;">
                    <a href="https://career-frontend-livid.vercel.app/login.html?code={verification_code}&email={email}" style="display: inline-block; background-color: #198754; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">
                        Verify Automatically
                    </a>
                </div>
                
                <p style="text-align: center; font-size: 14px; color: #6c757d; margin-top: 15px;">
                    (Or manually enter the code on the website)
                </p>
                
                <div style="background-color: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 15px; margin-bottom: 25px; margin-top: 25px;">
                    <p style="margin: 0; font-size: 14px; color: #664d03;">
                        <b>Note:</b> This code will expire soon. Do not share this code with anyone.
                    </p>
                </div>
                
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                
                <p style="font-size: 13px; color: #6c757d; margin-bottom: 0;">
                    If you did not attempt to create a CareerPath AI account, you can safely ignore this email.
                </p>
            </div>
            
            <div style="background-color: #f8f9fa; padding: 15px; text-align: center; font-size: 12px; color: #adb5bd; border-top: 1px solid #e0e0e0;">
                <p style="margin: 0;">CareerPath AI - Guiding your future, today.</p>
            </div>
        </div>
        """

        mail.send(msg)
        logging.info(f"✅ Verification email sent successfully to {email}")
        
    except Exception as e:
        logging.error(f"Failed to send verification email: {e}")

    return jsonify({"message": "User created. Please check your email for the verification code."}), 201

@app.route('/verify', methods=['POST'])
def verify():
    data = request.json
    email = data.get('email')
    code = data.get('code')

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"message": "User not found"}), 404

    if user.verification_code == code:
        user.is_verified = True
        user.verification_code = None
        db.session.commit()

        if email in users_db:
            users_db[email]["is_verified"] = True
            save_json(USER_FILE, users_db)

        # --- SEND CONGRATULATIONS EMAIL UPON SUCCESSFUL VERIFICATION ---
        try:
            msg = Message('🎉 Account Verified - Welcome to CareerPath AI!', 
                          sender=app.config['MAIL_USERNAME'], 
                          recipients=[email])
            
            msg.html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden;">
                <div style="background-color: #198754; color: white; padding: 25px; text-align: center;">
                    <h2 style="margin: 0;">🎉 You're Verified! 🎉</h2>
                </div>
                <div style="padding: 25px; color: #333; line-height: 1.6;">
                    <p style="font-size: 16px;">Hello <b>{user.name}</b>,</p>
                    <p style="font-size: 16px;">Congratulations! Your email has been successfully verified and your account is now fully active.</p>
                    <p style="font-size: 16px;">You can now use our AI engine to match your academic strengths with the best university courses.</p>
                    <br>
                    <div style="text-align: center;">
<a href="https://career-frontend-livid.vercel.app/login.html" style="display: inline-block; background-color: #0d6efd; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">                            Go to Your Dashboard
                        </a>
                    </div>
                </div>
            </div>
            """
            mail.send(msg)
            logging.info(f"✅ Congrats email sent to {email}")
        except Exception as e:
            logging.error(f"Failed to send congrats email: {e}")

        # --- RETURN USER DATA FOR AUTO-LOGIN ---
        return jsonify({
            "message": "Email verified successfully!",
            "user": {
                "name": user.name, 
                "email": user.email,
                "has_taken_test": user._has_taken_test,
                "history": user.history
            }
        }), 200
    else:
        return jsonify({"message": "Invalid verification code"}), 400

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"message": "Invalid email or password"}), 401

    if not user.is_verified:
        return jsonify({"message": "Please verify your email first."}), 403

    return jsonify({
        "message": "Login successful",
        "user": {
            "name": user.name, "email": user.email,
            "has_taken_test": user._has_taken_test,
            "history": user.history
        }
    }), 200

@app.route('/history', methods=['GET', 'POST'])
def history():
    if request.method == 'GET':
        identifier = request.args.get('username') or request.args.get('email')
        if not identifier:
            return jsonify({"message": "Username or email required"}), 400
            
        user = User.query.filter((User.name == identifier) | (User.email == identifier)).first()
        
    else:
        data = request.json
        email = data.get('email')
        user = User.query.filter_by(email=email).first()

    if user:
        return jsonify(user.history[::-1] if user.history else []), 200
    return jsonify([]), 200

@app.route('/save-history', methods=['POST', 'OPTIONS'])
def save_history():
    if request.method == 'OPTIONS':
        return '', 200  # Satisfies the CORS preflight check
        
    data = request.json
    user_name = data.get('username')
    user_email = data.get('email')
    report_data = data.get('report')

    if not user_name or not report_data:
        return jsonify({"error": "Missing data"}), 400

    user = None
    if user_email:
        user = User.query.filter_by(email=user_email).first()
    if not user:
        # Case-insensitive search using .ilike()
        user = User.query.filter(User.name.ilike(user_name)).first()

    if user:
        current_history = list(user.history) if user.history else []
        current_history.append(report_data)
        user.history = current_history
        flag_modified(user, "history")
        db.session.commit()
        return jsonify({"message": "Saved successfully"}), 200
        
    return jsonify({"error": "User not found"}), 404


# ==========================================
# 📧 EMAIL REPORT ROUTE WITH PDF ATTACHMENT
# ==========================================
@app.route('/send-report', methods=['POST'])
def send_report():
    data = request.json
    username = data.get('username')
    report = data.get('report')       # Used for the HTML email body
    pdf_base64 = data.get('pdf_file') # Used for the actual PDF attachment
    filename = data.get('filename', f"{username.replace(' ', '_')}_Placement_Report.pdf")

    # 1. Validation
    if not username or not report or not pdf_base64:
        return jsonify({"error": "Username, report data, and PDF file are required"}), 400

    # 2. Find User Details
    user = User.query.filter(User.name.ilike(username)).first()
    if not user or not user.email:
        return jsonify({"error": "Could not find a registered email for this user."}), 404

    try:
        # 3. Setup the Email Message
        msg = Message('Your CareerPath AI Intelligence Report 🚀', 
                      sender=app.config['MAIL_USERNAME'], 
                      recipients=[user.email])
        
        # 4. Extract Specific Data for the Email Body
        role = report.get('ai_role', 'Recommended Career')
        course = report.get('specific_course', 'Recommended Course')
        reason = report.get('interest_match_reason', 'Based on your academic strengths and selected interests, this path offers the best probability for success.')
        
        # 5. Build the Beautiful HTML Email Body
        email_html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 650px; margin: 0 auto; border: 1px solid #e0e0e0; background-color: #ffffff;">
            <div style="background-color: #0d6efd; color: white; padding: 20px; text-align: center;">
                <h2 style="margin: 0;">CareerPath AI Report</h2>
            </div>
            
            <div style="padding: 25px; color: #333;">
                <p>Hello <b>{user.name}</b>,</p>
                <p>Based on your academic profile, here is your AI-generated intelligence report. <b>A detailed, official PDF is attached to this email.</b></p>
                
                <h3 style="color: #0d6efd; margin-bottom: 5px;">🏆 Top Career: {role}</h3>
                <h4 style="color: #198754; margin-top: 0;">📚 Course to Study: {course}</h4>
                
                <h4 style="margin-bottom: 5px; color: #333;">💡 Why this path fits you:</h4>
                <div style="background-color: #f8f9fa; padding: 15px; border-left: 5px solid #0d6efd; margin-bottom: 25px; margin-top: 0;">
                    <p style="margin: 0; line-height: 1.5;">{reason}</p>
                </div>
                
                <h4>🎓 Recommended Universities:</h4>
                <ul style="line-height: 1.6;">
        """
        
        # Add the universities to the list
        for uni in report.get('universities', []):
            email_html += f"<li><b>{uni.get('name')}</b></li>"

        # Close the HTML tags and add a dashboard button
        email_html += """
                </ul>
                <div style="text-align: center; margin-top: 30px;">
                    <a href="https://career-frontend-livid.vercel.app/login.html" style="display: inline-block; background-color: #198754; color: white; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold; font-size: 16px;">
                        Log In to Dashboard
                    </a>
                </div>
            </div>
        </div>
        """
        
        # Apply the HTML to the email
        msg.html = email_html

        # 6. ATTACH THE FRONTEND-GENERATED PDF
        # Decode the Base64 string from JavaScript back into a binary PDF
        pdf_binary = base64.b64decode(pdf_base64)

        msg.attach(
            filename=filename,
            content_type="application/pdf",
            data=pdf_binary
        )

        # 7. Send the Email
        mail.send(msg)
        logging.info(f"✅ HTML Email with PDF attached successfully sent to {user.email}!")
        return jsonify({"message": "Report sent successfully to your registered email!"}), 200

    except Exception as e:
        logging.error(f"🚨 Email Error: {e}")
        return jsonify({"error": "Failed to send email"}), 500

# ==========================================
# 🧠 MAIN AI & SCRAPING ROUTE
# ==========================================

@app.route('/recommend', methods=['POST'])
def recommend():
    try:
        data = request.json
        user_name = data.get("name", "Student")
        user_interest = data.get("interest", "General")
        user_grades = data.get("grades", {})
        user_email = data.get("email")

        calculated_points = calculate_total_points(user_grades)
        expected_level = "Degree"
        if calculated_points < 46: expected_level = "Diploma"
        if calculated_points < 33: expected_level = "Certificate"
        if calculated_points < 25: expected_level = "Artisan"

        math_grade = "E"
        if isinstance(user_grades, dict):
            for subject, grade_data in user_grades.items():
                if subject.lower() in ['math', 'mathematics', 'maths']:
                    math_grade = grade_data.get("grade") if isinstance(grade_data, dict) else str(grade_data)
                    break
                    
        if grade_to_int(math_grade) < 5 and expected_level == "Degree": 
            expected_level = "Diploma" # KUCCPS rule: Need C- in Math for most degrees

        logging.info(f"🧠 [AI ENGINE] Starting Hybrid Generation for {user_name}...")
        
        ai_insight = ask_hybrid_career_advice(
            user_name, user_interest, user_grades, calculated_points, expected_level, 0, []
        )
        
        if not ai_insight:
            return jsonify({"error": "Failed to generate AI response. Please try again."}), 500

        user = User.query.filter_by(email=user_email).first() if user_email else None
        if not user:
            user = User.query.filter_by(name=user_name).first()
            
        if user:
            user._has_taken_test = True
            current_history = user.history if user.history is not None else []
            current_history.append(ai_insight)
            user.history = current_history
            flag_modified(user, "history")
            db.session.commit()

        logging.info("🏥 [AUTO-HEALER] Commencing Web Scraping to verify URLs...")
        
        course_name = ai_insight.get("specific_course", user_interest)
        # ... inside your /recommend route ...
        
        logging.info("🏥 [AUTO-HEALER] Commencing Web Scraping to verify URLs...")
        
        course_name = ai_insight.get("specific_course", user_interest)
        def heal_university(uni):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            uni_name = uni.get("name")
            # We are explicitly passing the specific course to the AI scraper here
            course_name = ai_insight.get("specific_course", user_interest)
            
            # Reduced to 2: Since the web_scraper now uses a 3-worker swarm, 
            # doing 3 retries means 9 heavy requests, which might trigger rate limits.
            max_retries = 2 
            
            try:
                for attempt in range(max_retries):
                    try:
                        # The Swarm and AI Judge operate here
                        real_url, is_verified = healer.get_verified_url(uni_name, course_name)
                        
                        # STRICT CHECK: If the swarm found a URL (verified or fallback)
                        if real_url:
                            uni["website_url"] = real_url
                            uni["verified_offering"] = is_verified
                            return uni # Success! Keep the university.
                            
                        logging.warning(f"⚠️ Swarm attempt {attempt + 1} found no URL for {uni_name}. Retrying...")
                        time.sleep(1.5) 
                        
                    except Exception as e:
                        logging.warning(f"🚨 Attempt {attempt + 1} error for {uni_name}: {e}")
                        time.sleep(1.5)
                
                # THE BACKUP STRATEGY (Replaces the Strict Drop)
                # If the swarm fails completely, we DO NOT return None.
                # We provide a highly targeted Google Search link so it still comes along.
                logging.info(f"ℹ️ Exhausted attempts for {uni_name}. Applying Search Fallback.")
                query = f"site:ac.ke {uni_name} {course_name} requirements".replace(" ", "+")
                uni["website_url"] = f"https://www.google.com/search?q={query}"
                uni["verified_offering"] = False
                return uni 
                
            finally:
                # Graceful cleanup of the event loop
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.close()
                except Exception as cleanup_error:
                    logging.debug(f"Cleanup non-fatal error: {cleanup_error}")

# ... scraper runs and filters universities ...
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            raw_results = list(executor.map(heal_university, ai_insight.get("universities", [])))
            # This line stays the same, but because heal_university never returns None anymore,
            # no universities will be filtered out!
            ai_insight["universities"] = [u for u in raw_results if u is not None]

        ai_insight["validated_points"] = calculated_points

        # 3. Save the CLEANED results to PostgreSQL history
        if user:
            user._has_taken_test = True
            current_history = user.history if user.history is not None else []
            current_history.append(ai_insight)
            user.history = current_history
            
            # Use the global import from the top of the file
            flag_modified(user, "history") 
            db.session.commit()
        # ... rest of your save logic ...

        # ==========================================
        # 💾 DUAL SAVE (JSON + PostgreSQL)
        # ==========================================
        global student_logs
        try:
            timestamp_str = time.strftime("%Y-%m-%d %H:%M:%S")
            new_entry = {
                "timestamp": timestamp_str,
                "student_name": user_name, 
                "interest": user_interest,
                "grades": user_grades, 
                "points": calculated_points, 
                "ai_response": ai_insight,
                "history": [ai_insight]
            }
            
            if not isinstance(student_logs, list): 
                student_logs = []
            student_logs.append(new_entry)
            save_json(LOGS_FILE, student_logs)
            
            new_db_record = StudentLog(**new_entry)
            db.session.add(new_db_record)
            db.session.commit()
            
        except Exception as e: 
            logging.error(f"Failed to save student log: {e}")
            db.session.rollback()

        # ==========================================
        # ☁️ WORD CLOUD LOGIC (Linked to PostgreSQL)
        # ==========================================
        career_counts = Counter()
        try:
            matching_logs = StudentLog.query.filter(StudentLog.interest.ilike(user_interest)).all()
            
            for log in matching_logs:
                resp = log.ai_response if isinstance(log.ai_response, dict) else {}
                
                main_role = resp.get("ai_role")
                if main_role: 
                    career_counts[main_role.strip().title()] += 1
                
                alt_careers = resp.get("alternative_careers", [])
                if isinstance(alt_careers, list):
                    for alt in alt_careers:
                        if isinstance(alt, dict) and alt.get("name"): 
                            career_counts[alt.get("name").strip().title()] += 1

        except Exception as db_err:
            logging.error(f"⚠️ Database query for Word Cloud failed: {db_err}")
            
        if not career_counts:
            main_role = ai_insight.get("ai_role")
            if main_role: career_counts[main_role.strip().title()] += 1
            for alt in ai_insight.get("alternative_careers", []):
                if alt_name := (alt.get("name") if isinstance(alt, dict) else None):
                    career_counts[alt_name.strip().title()] += 1

        ai_insight["trending_careers"] = [{"career": c, "count": count} for c, count in career_counts.items()]
        # ==========================================
          # 1. THE FINAL SAFETY CHECK (Make sure it is indented here!)
        for uni in ai_insight.get("universities", []):
            if "requirements_met" not in uni:
                uni["requirements_met"] = [
                    {"subject": "General Requirement", "required": "Check University Website", "status": "Pending"}
                ]

          
        logging.info(f"✅ [SUCCESS] Request successfully completed and dispatched to frontend for {user_name}!")
        return jsonify(ai_insight), 200

    except Exception as e:
        logging.error(f"🚨 Critical Error in /recommend: {str(e)}")
        return jsonify({"error": "An internal server error occurred.", "details": str(e)}), 500

@app.route('/resend-code', methods=['POST'])
def resend_code():
    data = request.json
    email = data.get('email')

    if not email:
        return jsonify({"message": "Email is required"}), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify({"message": "User not found"}), 404
        
    if user.is_verified:
        return jsonify({"message": "User is already verified."}), 400

    # 1. Generate a brand new 6-digit code
    new_code = str(int(time.time()))[-6:]
    user.verification_code = new_code
    db.session.commit()

    # (Dual Save for your JSON backup)
    if email in users_db:
        users_db[email]["verification_code"] = new_code
        save_json(USER_FILE, users_db)

    # 2. Email the new code
    try:
        msg = Message('🔄 Your New Verification Code', sender=app.config['MAIL_USERNAME'], recipients=[email])
        msg.html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden;">
            <div style="background-color: #0d6efd; color: white; padding: 25px; text-align: center;">
                <h2 style="margin: 0;">New Verification Code</h2>
            </div>
            <div style="padding: 25px; color: #333; line-height: 1.6;">
                <p style="font-size: 16px;">Hello <b>{user.name}</b>,</p>
                <p style="font-size: 16px;">You requested a new code to activate your CareerPath AI account. Here is your fresh 6-digit code:</p>
                <div style="text-align: center; margin: 35px 0;">
                    <span style="font-size: 28px; font-weight: bold; background-color: #f8f9fa; padding: 15px 30px; border-radius: 8px; border: 2px dashed #0d6efd; letter-spacing: 4px; color: #0d6efd;">
                        {new_code}
                    </span>
                </div>
                
                <div style="text-align: center; margin-top: 30px;">
<a href="https://career-frontend-livid.vercel.app/login.html?code={new_code}&email={email}" style="display: inline-block; background-color: #198754; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">                        Verify Automatically
                    </a>
                </div>
            </div>
        </div>
        """
        mail.send(msg)
        return jsonify({"message": "New code sent successfully!"}), 200
        
    except Exception as e:
        logging.error(f"Failed to resend verification email: {e}")
        return jsonify({"message": "Failed to send email"}), 500


if __name__ == "__main__":
    import os
    # Render provides a PORT environment variable. 
    # If it doesn't exist (like on your laptop), it defaults to 5001.
    port = int(os.environ.get("PORT", 5001))
    
    # host='0.0.0.0' is REQUIRED for Render to expose the server to the internet
    app.run(host='0.0.0.0', port=port)