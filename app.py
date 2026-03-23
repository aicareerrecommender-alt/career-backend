import os
import time
import secrets
import logging
import concurrent.futures
from collections import Counter
from datetime import datetime
import io                   
from xhtml2pdf import pisa  

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
from utils.database import db, init_db, save_json, load_json, USER_FILE, LOGS_FILE
from utils.ai_engines import ask_hybrid_career_advice, calculate_total_points, grade_to_int
from utils.web_scraper import healer 

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

import base64
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s]: %(message)s', datefmt='%H:%M:%S')

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

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
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False) 
    
    name = db.Column(db.String(100), nullable=True) 
    is_verified = db.Column(db.Boolean, default=False)
    verification_code = db.Column(db.String(10), nullable=True) 
    _has_taken_test = db.Column(db.Boolean, default=False)
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
    db.create_all()

student_logs = load_json(LOGS_FILE)

# ==========================================
# ✨ GOOGLE SIGN-IN ENDPOINT ✨
# ==========================================
GOOGLE_CLIENT_ID = "108086559679-et4vvki3fehs0beefbv8bn9psonh5ubp.apps.googleusercontent.com"
@app.route('/google-login', methods=['POST'])
def google_login():
    data = request.json
    token = data.get('token')

    if not token:
        return jsonify({"message": "No token provided"}), 400

    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        email = idinfo['email']
        name = idinfo.get('name', 'Student') 

        user = User.query.filter_by(email=email).first()

        if user:
            return jsonify({
                "message": "Login successful", 
                "name": user.name, 
                "email": user.email,
                "has_taken_test": user._has_taken_test,
                "history": user.history
            }), 200
        else:
            new_user = User(
                name=name,
                email=email,
                password_hash="GOOGLE_AUTH_USER", 
                is_verified=True,
                verification_code=None
            )
            db.session.add(new_user)
            db.session.commit()
            
            try:
                msg = Message('🎉 Welcome to CareerPath AI - Account Verified!', 
                              sender=app.config['MAIL_USERNAME'], 
                              recipients=[email])
                
                msg.html = f"""
                <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden;">
                    <div style="background-color: #198754; color: white; padding: 25px; text-align: center;">
                        <h2 style="margin: 0;">🎉 Welcome, {name}! 🎉</h2>
                    </div>
                    <div style="padding: 25px; color: #333; line-height: 1.6;">
                        <p style="font-size: 16px;">Your account has been successfully created via Google.</p>
                        <p style="font-size: 16px;">You are now ready to use our AI engine to find the best university courses for your career.</p>
                        <div style="text-align: center; margin-top: 20px;">
                            <a href="https://career-frontend-livid.vercel.app" style="display: inline-block; background-color: #0d6efd; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; color: white;">Go to Dashboard</a>
                        </div>
                    </div>
                </div>
                """
                mail.send(msg)
                logging.info(f"✅ Google Welcome email sent to {email}")
            except Exception as e:
                logging.error(f"Failed to send Google congrats email: {e}")

            return jsonify({
                "message": "Account created and logged in!", 
                "name": new_user.name, 
                "email": new_user.email,
                "has_taken_test": new_user._has_taken_test,
                "history": new_user.history
            }), 200
           
    except ValueError:
        return jsonify({"message": "Invalid Google token"}), 401

# ==========================================
# 🚀 AUTHENTICATION & HISTORY ROUTES
# ==========================================

@app.route('/')
def home():
    return jsonify({"message": "CareerPath AI Backend is running beautifully!"}), 200

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
    # SECURE: Cryptographically secure verification code
    verification_code = f"{secrets.randbelow(1000000):06d}"

    new_user = User(
        name=name, email=email, password_hash=hashed_pw,
        is_verified=False, verification_code=verification_code
    )
    db.session.add(new_user)
    db.session.commit()

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
                    <a href="https://career-frontend-livid.vercel.app/login?code={verification_code}&email={email}" style="display: inline-block; background-color: #198754; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">
                        Verify Automatically
                    </a>
                </div>
                
                <p style="text-align: center; font-size: 14px; color: #6c757d; margin-top: 15px;">
                    (Or manually enter the code on the website)
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
        return '', 200  
        
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
@app.route('/send-report', methods=['POST', 'OPTIONS'])
def send_report():
    if request.method == 'OPTIONS':
        return '', 200

    try:
        data = request.json
        user_email = data.get('email')
        pdf_base64 = data.get('pdf_data')
        name = data.get('name', 'Student')

        client_id = os.environ.get("GMAIL_CLIENT_ID")
        client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
        refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")

        if not all([client_id, client_secret, refresh_token]):
            return jsonify({"error": "Server email credentials are missing!"}), 500

        creds = Credentials(
            None, 
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret
        )

        service = build('gmail', 'v1', credentials=creds)

        message = MIMEMultipart()
        message['To'] = user_email
        message['Subject'] = "🎓 Your CareerPath AI Report"
        
        html_body = f"""
        <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e0e0e0; border-radius: 10px; overflow: hidden;">
            <div style="background-color: #0d6efd; color: white; padding: 25px; text-align: center;">
                <h2 style="margin: 0;">🎓 Your AI Career Report is Ready!</h2>
            </div>
            <div style="padding: 25px; color: #333; line-height: 1.6;">
                <p style="font-size: 16px;">Hello <strong>{name}</strong>,</p>
                <p style="font-size: 16px;">Attached is your highly personalized AI-generated university placement report.</p>
                <p style="font-size: 16px;">Best of luck with your career journey!</p>
            </div>
        </div>
        """
        message.attach(MIMEText(html_body, 'html'))

        clean_base64 = pdf_base64.split(',')[1] if ',' in pdf_base64 else pdf_base64
        pdf_bytes = base64.b64decode(clean_base64)
        
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(pdf_bytes)
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{name.replace(" ", "_")}_Career_Report.pdf"')
        message.attach(part)

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        send_request = service.users().messages().send(userId="me", body={"raw": raw_message}).execute()

        print(f"✅ Email sent successfully! Message ID: {send_request['id']}")
        return jsonify({"message": "Report sent successfully!"}), 200

    except Exception as e:
        print(f"❌ Server Error: {str(e)}")
        return jsonify({"error": str(e)}), 500

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
            expected_level = "Diploma" 

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
        
        # PERFECTED INTEGRATION: Syncs perfectly with the new web_scraper.py format
        def heal_university(uni):
            uni_name = uni.get("name")
            course_name = ai_insight.get("specific_course", user_interest)
            
            try:
                # Expecting a dictionary: {"url": str, "verified": bool, "status": str}
                result = healer.get_verified_url(uni_name, course_name)
                
                if result and isinstance(result, dict) and result.get("url"):
                    uni["website_url"] = result.get("url")
                    uni["verified_offering"] = result.get("verified", False)
                    # You could optionally append the status to the dict here if your frontend uses it
                    # uni["kuccps_status"] = result.get("status", "UNKNOWN")
                    return uni
                    
            except Exception as e:
                logging.warning(f"🚨 Error verifying {uni_name}: {e}")

            # FALLBACK
            logging.info(f"ℹ️ Could not find verified link for {uni_name}. Applying Fallback.")
            query = f"site:kuccps.net {uni_name} {course_name}".replace(" ", "+")
            uni["website_url"] = f"https://duckduckgo.com/?q={query}"
            uni["verified_offering"] = False
            
            return uni

        # THREADING IS ACTIVE: Runs extremely fast with your new scraper
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            raw_results = list(executor.map(heal_university, ai_insight.get("universities", [])))
            ai_insight["universities"] = [u for u in raw_results if u is not None]

        ai_insight["validated_points"] = calculated_points

        if user:
            user._has_taken_test = True
            current_history = user.history if user.history is not None else []
            current_history.append(ai_insight)
            user.history = current_history
            flag_modified(user, "history") 
            db.session.commit()

        # ==========================================
        # 💾 Postgres DB Save 
        # ==========================================
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
            
            new_db_record = StudentLog(**new_entry)
            db.session.add(new_db_record)
            db.session.commit()
            
        except Exception as e: 
            logging.error(f"Failed to save student log: {e}")
            db.session.rollback()

        # ==========================================
        # ☁️ WORD CLOUD 
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
            for log in student_logs:
                resp = log.get("ai_response", {}) if isinstance(log.get("ai_response"), dict) else {}
                main_role = resp.get("ai_role")
                if main_role:
                    career_counts[main_role.strip().title()] += 1
                alt_careers = resp.get("alternative_careers", [])
                if isinstance(alt_careers, list):
                    for alt in alt_careers:
                        if isinstance(alt, dict) and alt.get("name"):
                            career_counts[alt.get("name").strip().title()] += 1
            
        if not career_counts:
            main_role = ai_insight.get("ai_role")
            if main_role: career_counts[main_role.strip().title()] += 1
            for alt in ai_insight.get("alternative_careers", []):
                if alt_name := (alt.get("name") if isinstance(alt, dict) else None):
                    career_counts[alt_name.strip().title()] += 1

        ai_insight["trending_careers"] = [{"career": c, "count": count} for c, count in career_counts.items()]
        
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

    # SECURE: Cryptographically secure verification code
    new_code = f"{secrets.randbelow(1000000):06d}"
    user.verification_code = new_code
    db.session.commit()

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
<a href="https://career-frontend-livid.vercel.app/login?code={new_code}&email={email}" style="display: inline-block; background-color: #198754; color: white; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: bold; font-size: 16px;">                        Verify Automatically
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
    with app.app_context():
        db.create_all()
        print("✅ Database tables synchronized successfully!")

    port = int(os.environ.get("PORT", 5001))
    app.run(host='0.0.0.0', port=port)