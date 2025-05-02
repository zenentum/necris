from flask import Flask, render_template, request, send_file, redirect, url_for, flash, session
import os
import time
from werkzeug.utils import secure_filename
from functools import wraps
from password_manager import PasswordManager
from disk_monitor import DiskMonitor

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Generate a random secret key for sessions

# Configuration
UPLOAD_FOLDER = '/media/necris-user'
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'mp4', 'zip'}
CREDENTIALS_FILE = '/etc/necris/credentials.json'

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Initialize password manager
password_manager = PasswordManager()
disk_monitor = DiskMonitor(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_human_readable_size(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        credentials = password_manager.get_credentials()
        username = request.form['username']
        password = request.form['password']
        
        if (username == credentials['username'] and 
            password_manager.verify_password(password)):
            session['logged_in'] = True
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        else:
            flash('Invalid username or password')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form['current_password']
        new_password = request.form['new_password']
        confirm_password = request.form['confirm_password']
        
        if not password_manager.verify_password(current_password):
            flash('Current password is incorrect')
        elif new_password != confirm_password:
            flash('New passwords do not match')
        elif password_manager.update_password(new_password):
            flash('Password successfully updated')
            return redirect(url_for('index'))
        else:
            flash('Failed to update password. Please try again.')
    
    return render_template('change_password.html')

@app.route('/')
@login_required
def index():
    current_path = request.args.get('path', '')
    full_path = os.path.join(UPLOAD_FOLDER, current_path)
    
    # Ensure we don't allow directory traversal
    if not os.path.realpath(full_path).startswith(os.path.realpath(UPLOAD_FOLDER)):
        return redirect(url_for('index'))
    
    files = []
    for item in os.scandir(full_path):
        item_info = {
            'name': item.name,
            'is_dir': item.is_dir(),
            'size': get_human_readable_size(item.stat().st_size) if not item.is_dir() else '',
            'modified': os.path.getmtime(item.path)
        }
        files.append(item_info)
    
    # Sort directories first, then files
    files.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
    
    return render_template('index.html', 
                         files=files, 
                         current_path=current_path,
                         parent_path=os.path.dirname(current_path))

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return redirect(request.referrer)
    
    file = request.files['file']
    current_path = request.form.get('current_path', '')
    
    if file.filename == '':
        return redirect(request.referrer)
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        upload_path = os.path.join(UPLOAD_FOLDER, current_path)
        file.save(os.path.join(upload_path, filename))
    
    return redirect(url_for('index', path=current_path))

@app.route('/download/<path:filepath>')
@login_required
def download_file(filepath):
    full_path = os.path.join(UPLOAD_FOLDER, filepath)
    return send_file(full_path, as_attachment=True)

@app.route('/delete/<path:filepath>')
@login_required
def delete_file(filepath):
    full_path = os.path.join(UPLOAD_FOLDER, filepath)
    if os.path.exists(full_path):
        os.remove(full_path)
    return redirect(request.referrer)

# Disk usage monitoring. These routes are used by the frontend to get disk usage info.
@app.route('/api/disk-usage')
@login_required
def get_disk_usage():
    return disk_monitor.get_all_disk_usage()

@app.route('/api/disk-thresholds', methods=['POST'])
@login_required
def update_thresholds():
    data = request.json
    warning = data.get('warning')
    critical = data.get('critical')
    
    if not all(isinstance(x, int) and 0 <= x <= 100 for x in [warning, critical]):
        return {'error': 'Invalid threshold values'}, 400
    if warning >= critical:
        return {'error': 'Warning threshold must be less than critical threshold'}, 400
        
    if disk_monitor.save_thresholds(warning, critical):
        return {'status': 'success'}
    return {'error': 'Failed to save thresholds'}, 500

@app.route('/api/refresh-services')
@login_required
def refresh_services():
    try:
        # Create a refresh signal file
        signal_file = '/tmp/necris_refresh_services'
        with open(signal_file, 'w') as f:
            f.write(str(time.time()))  # Write current timestamp
        return {'status': 'success', 'message': 'Services refresh initiated'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

if __name__ == '__main__':
    # Create upload folder if it doesn't exist
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    # Run the app on all network interfaces
    app.run(host='0.0.0.0', port=80, debug=True)