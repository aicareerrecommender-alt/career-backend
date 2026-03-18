import os
from flask_sqlalchemy import SQLAlchemy

# 1. Create the SQLAlchemy instance. 
# This object 'db' contains all the database methods (session, Model, Column, etc.)
db = SQLAlchemy()

def init_db(app):
    """
    Helper function to bind the database to the Flask app.
    This will be called inside app.py.
    """
    # Force the app to use the PostgreSQL URL from Render's environment variables
    # If not found, it defaults to a local sqlite file for safety.
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///local.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    db.init_app(app)
    
    # This automatically creates your 'users' and 'student_logs' tables 
    # based on the classes you have sitting in app.py.
    with app.app_context():
        db.create_all()