import os
import json
import threading
import tempfile

# Go up one level from 'utils' to the main directory
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_FILE = os.path.join(BASE_DIR, 'users.json')
LOGS_FILE = os.path.join(BASE_DIR, 'student_logs.json')
db_lock = threading.Lock()

def load_json(filename):
    with db_lock:
        if not os.path.exists(filename): 
            return {} if filename == USER_FILE else []
        try:
            with open(filename, 'r', encoding='utf-8') as f: 
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): 
            return {} if filename == USER_FILE else []

def save_json(filename, data):
    directory = os.path.dirname(filename)
    with db_lock:
        try:
            with tempfile.NamedTemporaryFile('w', dir=directory, delete=False, encoding='utf-8') as tf:
                json.dump(data, tf, indent=4)
                temp_name = tf.name
            os.replace(temp_name, filename)
            return True
        except Exception as e: 
            return False