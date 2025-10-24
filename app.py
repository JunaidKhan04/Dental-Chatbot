import os
import shutil
import pandas as pd
import sqlite3
import threading
import re
import io
import base64
import json
import matplotlib.pyplot as plt
from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
from chatbot_model import get_chat_response  # Make sure chatbot_model.py exists
from bs4 import BeautifulSoup
import traceback
import time

# === Paths ===
stop_execution_flag = False
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ALLOWED_EXTENSIONS = {'csv', 'db'}
STATIC_CSV = os.path.join(BASE_DIR, 'patient_details2.csv')  # Default CSV
DB_FILE = os.path.join(BASE_DIR, 'chatbot_data.db')

# === Flask App ===
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = 'AIzaSyDhSrwZaIdEM2WVIELNAu7qIa-WRfbsqn4'

# === DB Initialization ===
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS chat_history 
                        (id INTEGER PRIMARY KEY, message TEXT, response TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS current_file 
                        (id INTEGER PRIMARY KEY, filename TEXT)''')
        conn.commit()

init_db()

# === Cache & Lock ===
data_cache = None
data_lock = threading.Lock()

# === File Utils ===
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_current_file():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT filename FROM current_file ORDER BY id DESC LIMIT 1")
        result = cursor.fetchone()
    return result[0] if result else None

def set_current_file(filename):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM current_file")
        cursor.execute("INSERT INTO current_file (filename) VALUES (?)", (filename,))
        conn.commit()

def load_data():
    global data_cache
    current_file = get_current_file()
    if current_file:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], current_file)
        if os.path.exists(file_path):
            try:
                df = pd.read_csv(file_path)
                with data_lock:
                    data_cache = df
                print(f"[DATA] Loaded {current_file} into cache")
            except Exception as e:
                print(f"[DATA] Failed to read CSV {file_path}: {e}")
                with data_lock:
                    data_cache = None
        else:
            with data_lock:
                data_cache = None
    else:
        with data_lock:
            data_cache = None

# Change STATIC_CSV path to match where you actually store it in repo
STATIC_CSV = os.path.join(BASE_DIR, 'uploads', 'patient_details2.csv')  

def bootstrap_dataset():
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    current = get_current_file()
    current_path = os.path.join(UPLOAD_FOLDER, current) if current else None
    needs_seed = (not current) or (current and not os.path.exists(current_path))
    if needs_seed:
        if os.path.exists(STATIC_CSV):
            dest = os.path.join(UPLOAD_FOLDER, os.path.basename(STATIC_CSV))
            shutil.copy(STATIC_CSV, dest)  # Always overwrite to be safe
            set_current_file(os.path.basename(STATIC_CSV))
            print(f"[INIT] Seed dataset loaded: {dest}")
        else:
            print(f"[INIT] No static CSV found at {STATIC_CSV}")

try:
    bootstrap_dataset()
    load_data()
except Exception as e:
    print(f"[INIT] Bootstrap error: {e}")

# === Helper function to clean HTML ===
def clean_html(html_content):
    """Remove extra whitespace and empty lines from HTML content"""
    # Remove leading/trailing whitespace from each line
    lines = [line.strip() for line in html_content.split('\n')]
    # Remove empty lines
    lines = [line for line in lines if line]
    # Join lines with single newlines
    return '\n'.join(lines)

# === Routes ===
@app.route('/')
def index():
    current_file = get_current_file()
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT message, response FROM chat_history")
        history = cursor.fetchall()
    return render_template('index.html', history=history, filename=current_file)

@app.route('/ask', methods=['POST'])
def ask():
    global stop_execution_flag
    stop_execution_flag = False  # reset at the start of request
    start_time = time.time()
    
    try:
        user_input = request.json.get('message')
        print(f"[DEBUG] Received request: {user_input}")
        
        with data_lock:
            df = data_cache
        
        if df is None:
            print("[DEBUG] No data loaded")
            return jsonify({'response': '⚠ No file uploaded or data loaded. Please upload a CSV first.'})
        
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT message, response FROM chat_history ORDER BY id ASC")
            session_history = cursor.fetchall()
        
        if stop_execution_flag:
            print("[DEBUG] Execution stopped by flag")
            return jsonify({'status': 'stopped', 'response': None})
        
        print(f"[DEBUG] Getting chat response... (Time: {time.time() - start_time:.2f}s)")
        try:
            response = get_chat_response(user_input, df, session_history=session_history)
            print(f"[DEBUG] Got response in {time.time() - start_time:.2f}s")
            print(f"[DEBUG] Response length: {len(response)} characters")
            print(f"[DEBUG] Response preview: {response[:200]}...")
        except Exception as e:
            print(f"[ERROR] Error in get_chat_response: {str(e)}")
            print(traceback.format_exc())
            return jsonify({'response': f'Error getting response: {str(e)}'})
        
        if stop_execution_flag:
            print("[DEBUG] Execution stopped by flag after getting response")
            return jsonify({'status': 'stopped', 'response': None})
        
        try:
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO chat_history (message, response) VALUES (?, ?)", (user_input, response))
                conn.commit()
            print("[DEBUG] Saved to chat history")
        except Exception as e:
            print(f"[ERROR] Error saving to chat history: {str(e)}")
        
        # Check if the response contains a table
        if "<table" in response:
            print("[DEBUG] Response contains table, creating HTML template")
            try:
                # Create HTML template for the table without extra indentation
                table_template = """<!DOCTYPE html>
<html>
<head>
<title>Patient Data Table</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {
font-family: Arial, sans-serif;
margin: 0;
padding: 20px;
background-color: #f5f5f5;
}
.container {
max-width: 1200px;
margin: 0 auto;
background-color: white;
padding: 20px;
border-radius: 8px;
box-shadow: 0 2px 10px rgba(0,0,0,0.1);
}
.table-container {
overflow-x: auto;
}
table {
width: 100%;
border-collapse: collapse;
margin-top: 20px;
}
th, td {
border: 1px solid #ddd;
padding: 12px;
text-align: left;
}
th {
background-color: #f2f2f2;
font-weight: bold;
}
tr:nth-child(even) {
background-color: #f9f9f9;
}
tr:hover {
background-color: #f1f1f1;
}
</style>
</head>
<body>
<div class="container">
<div class="table-container">
{response}
</div>
</div>
</body>
</html>"""
                
                # Format the template with the response and clean up extra spaces
                formatted_template = table_template.format(response=response)
                clean_template = clean_html(formatted_template)
                print(f"[DEBUG] Created table template (length: {len(clean_template)})")
                
                # Return only the cleaned HTML template in the response field
                return jsonify({'response': clean_template})
            except Exception as e:
                print(f"[ERROR] Error creating table template: {str(e)}")
                print(traceback.format_exc())
                # Fall back to the original response
                return jsonify({'response': response})
        
        # Check if the response contains chart data
        elif "CHART_DATA:" in response:
            print("[DEBUG] Response contains chart data, creating HTML template")
            try:
                # Extract the JSON part after "CHART_DATA:"
                chart_str = response.split("CHART_DATA:")[1].strip()
                print(f"[DEBUG] Extracted chart string: {chart_str[:100]}...")
                
                # Parse the JSON
                chart_json = json.loads(chart_str)
                print(f"[DEBUG] Parsed chart JSON: {chart_json}")
                
                labels = chart_json.get("labels", [])
                values = chart_json.get("values", [])
                title = chart_json.get("title", "Chart")
                
                print(f"[DEBUG] Chart data - Labels: {labels}, Values: {values}, Title: {title}")
                
                # Generate chart
                fig, ax = plt.subplots(figsize=(10, 6))
                ax.bar(labels, values)
                ax.set_title(title)
                ax.set_xlabel("Category")
                ax.set_ylabel("Value")
                plt.xticks(rotation=45, ha='right')
                plt.tight_layout()
                
                # Convert to base64
                img = io.BytesIO()
                plt.savefig(img, format='png')
                img.seek(0)
                plot_url = base64.b64encode(img.getvalue()).decode()
                plt.close(fig)
                print("[DEBUG] Generated chart image")
                
                # Create HTML template for the chart without extra indentation
                chart_template = """<!DOCTYPE html>
<html>
<head>
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {
font-family: Arial, sans-serif;
margin: 0;
padding: 20px;
background-color: #f5f5f5;
}
.container {
max-width: 1200px;
margin: 0 auto;
background-color: white;
padding: 20px;
border-radius: 8px;
box-shadow: 0 2px 10px rgba(0,0,0,0.1);
}
h1 {
color: #333;
text-align: center;
margin-bottom: 20px;
}
.chart-container {
text-align: center;
margin-top: 20px;
}
.chart-container img {
max-width: 100%;
height: auto;
border: 1px solid #ddd;
border-radius: 4px;
}
</style>
</head>
<body>
<div class="container">
<h1>{title}</h1>
<div class="chart-container">
<img src="data:image/png;base64,{plot_url}" alt="{title}">
</div>
</div>
</body>
</html>"""
                
                # Format the template with the title and plot URL and clean up extra spaces
                formatted_template = chart_template.format(title=title, plot_url=plot_url)
                clean_template = clean_html(formatted_template)
                print(f"[DEBUG] Created chart template (length: {len(clean_template)})")
                
                # Return only the cleaned HTML template in the response field
                return jsonify({'response': clean_template})
            except Exception as e:
                print(f"[ERROR] Error creating chart template: {str(e)}")
                print(traceback.format_exc())
                # Fall back to the original response
                return jsonify({'response': response})
        
        # Regular text response
        else:
            print("[DEBUG] Regular text response")
            # Return the text response as-is
            return jsonify({'response': response})
    
    except Exception as e:
        print(f"[ERROR] Unhandled exception in /ask: {str(e)}")
        print(traceback.format_exc())
        return jsonify({'response': f'Error: {str(e)}'})
    
    finally:
        print(f"[DEBUG] Request completed in {time.time() - start_time:.2f}s")

@app.route('/stop_execution', methods=['POST'])
def stop_execution():
    global stop_execution_flag
    stop_execution_flag = True
    return jsonify({'status': 'stopped'})

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return redirect(url_for('index'))
    file = request.files['file']
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        file.save(save_path)
        set_current_file(filename)
        load_data()
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("DELETE FROM chat_history")
            conn.commit()
    return redirect(url_for('index'))

@app.route('/delete_file', methods=['POST'])
def delete_file():
    current_file = get_current_file()
    if current_file:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], current_file)
        if os.path.exists(file_path):
            os.remove(file_path)
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM current_file")
            cursor.execute("DELETE FROM chat_history")
            conn.commit()
        global data_cache
        with data_lock:
            data_cache = None
    return redirect(url_for('index'))

@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM chat_history")
        conn.commit()
    return jsonify({'status': 'cleared'})

# === Entry Point ===
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5005))
    app.run(host='0.0.0.0', port=port, debug=True)