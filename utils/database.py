import os
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

# Initialize SQLAlchemy without an app first to avoid circular imports
db = SQLAlchemy()

# ==========================================
# 👤 USER MODEL (Replaces users.json)
# ==========================================
class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    name = db.Column(db.String(100), nullable=True)
    
    # Verification & State
    is_verified = db.Column(db.Boolean, default=False)
    verification_code = db.Column(db.String(10), nullable=True)
    _has_taken_test = db.Column(db.Boolean, default=False)
    
    # JSON field to store the history of AI reports
    history = db.Column(db.JSON, default=list)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ==========================================
# 📝 STUDENT LOG MODEL (Replaces student_logs.json)
# ==========================================
class StudentLog(db.Model):
    __tablename__ = 'student_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.String(50)) # Kept as string to match your previous logging format
    student_name = db.Column(db.String(100))
    interest = db.Column(db.String(100))
    points = db.Column(db.Integer)
    
    # Complex data stored as JSON
    grades = db.Column(db.JSON)
    ai_response = db.Column(db.JSON)
    history = db.Column(db.JSON, default=list)
    
    # Database-specific timestamp for sorting
    db_created_at = db.Column(db.DateTime, default=datetime.utcnow)

# ==========================================
# 🛠️ HELPER FUNCTIONS (Optional)
# ==========================================
# You no longer need load_json or save_json here, 
# as app.py will now use db.session.add() and db.session.commit()