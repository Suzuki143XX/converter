from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, session
from flask_login import LoginManager, login_required, login_user, logout_user, current_user, UserMixin
from flask_cors import CORS
from authlib.integrations.flask_client import OAuth
import sqlite3
import os
import tempfile
import shutil
import re
from pathlib import Path
from datetime import datetime, timedelta
import yt_dlp

app = Flask(__name__)
# Use environment variable for secret key (Render sets this)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-this-in-production')
app.config['SESSION_TYPE'] = 'filesystem'
CORS(app)

# Setup Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'index'

# Google OAuth Config
GOOGLE_CLIENT_ID = '420462376171-hpsgp580an2douisas893bqiki92ccsv.apps.googleusercontent.com'
GOOGLE_CLIENT_SECRET = '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'

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

PLANS = {
    'free': {
        'name': 'Free', 
        'mp3': 5, 
        'mp4': 5, 
        'price': 0,
        'max_quality': 480,
        'qualities': ['360', '480']
    },
    'basic': {
        'name': 'Basic', 
        'mp3': 100, 
        'mp4': 100, 
        'price': 10,
        'max_quality': 720,
        'qualities': ['480', '720']
    },
    'gold': {
        'name': 'Gold', 
        'mp3': 300, 
        'mp4': 300, 
        'price': 25,
        'max_quality': 720,
        'qualities': ['480', '720']
    },
    'premium': {
        'name': 'Premium', 
        'mp3': 1000, 
        'mp4': 1000, 
        'price': 50,
        'max_quality': 2160,
        'qualities': ['480', '720', '1080', '1440', '2160']
    }
}

DOWNLOAD_DIR = Path("C:/Users/gmelc/OneDrive/Desktop")

def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_id TEXT UNIQUE,
            email TEXT UNIQUE,
            name TEXT,
            picture TEXT,
            plan TEXT DEFAULT 'free',
            mp3_count INTEGER DEFAULT 0,
            mp4_count INTEGER DEFAULT 0,
            last_reset TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

class User(UserMixin):
    def __init__(self, id, google_id, email, name, picture, plan, mp3_count, mp4_count, last_reset):
        self.id = id
        self.google_id = google_id
        self.email = email
        self.name = name
        self.picture = picture
        self.plan = plan
        self.mp3_count = mp3_count
        self.mp4_count = mp4_count
        self.last_reset = last_reset

def get_db():
    conn = sqlite3.connect('users.db')
    conn.row_factory = sqlite3.Row
    return conn

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    if user:
        return User(user['id'], user['google_id'], user['email'], user['name'], 
                   user['picture'], user['plan'], user['mp3_count'], user['mp4_count'], user['last_reset'])
    return None

def check_reset(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_reset, plan FROM users WHERE id = ?", (user_id,))
    result = c.fetchone()
    if result:
        last_reset = datetime.fromisoformat(result['last_reset'])
        if datetime.now() - last_reset > timedelta(days=30):
            c.execute("UPDATE users SET mp3_count = 0, mp4_count = 0, last_reset = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
            conn.commit()
    conn.close()

def sanitize_filename(filename):
    invalid_chars = r'[\\/*?:"<>|]'
    sanitized = re.sub(invalid_chars, "", filename)
    sanitized = sanitized.strip(" .")
    if len(sanitized) > 100:
        sanitized = sanitized[:100]
    return sanitized or "download"

def setup_ffmpeg():
    try:
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        if ffmpeg_exe and os.path.exists(ffmpeg_exe):
            ffmpeg_dir = os.path.dirname(ffmpeg_exe)
            current_path = os.environ.get('PATH', '')
            if ffmpeg_dir not in current_path:
                os.environ['PATH'] = ffmpeg_dir + os.pathsep + current_path
            return True
    except Exception as e:
        print(f"FFmpeg setup error: {e}")
    return False

ffmpeg_available = setup_ffmpeg()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login')
def login():
    try:
        redirect_uri = request.url_root.rstrip('/') + '/authorize'
        return google.authorize_redirect(redirect_uri)
    except Exception as e:
        return f"OAuth Error: {str(e)}", 400

@app.route('/authorize')
def authorize():
    try:
        token = google.authorize_access_token()
        resp = google.get('https://www.googleapis.com/oauth2/v1/userinfo')
        user_info = resp.json()
        
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE google_id = ?", (user_info['id'],))
        user = c.fetchone()
        
        if not user:
            c.execute("INSERT INTO users (google_id, email, name, picture) VALUES (?, ?, ?, ?)",
                     (user_info['id'], user_info['email'], user_info['name'], user_info.get('picture', '')))
            conn.commit()
            c.execute("SELECT * FROM users WHERE google_id = ?", (user_info['id'],))
            user = c.fetchone()
        
        conn.close()
        
        user_obj = User(user['id'], user['google_id'], user['email'], user['name'],
                       user['picture'], user['plan'], user['mp3_count'], user['mp4_count'], user['last_reset'])
        login_user(user_obj)
        return redirect('/')
    except Exception as e:
        return f"Login failed: {str(e)}", 400

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect('/')

@app.route('/api/user')
def api_user():
    # FIX: Check session for anonymous users
    mp3_used = session.get('downloads_audio', 0)
    mp4_used = session.get('downloads_video', 0)
    
    if current_user.is_authenticated:
        check_reset(current_user.id)
        plan_data = PLANS.get(current_user.plan, PLANS['free'])
        remaining_mp3 = max(0, plan_data['mp3'] - current_user.mp3_count)
        remaining_mp4 = max(0, plan_data['mp4'] - current_user.mp4_count)
        
        return jsonify({
            'logged_in': True,
            'name': current_user.name,
            'picture': current_user.picture,
            'plan': current_user.plan,
            'plan_name': plan_data['name'],
            'limits': plan_data,
            'usage': {
                'mp3': {'used': current_user.mp3_count, 'limit': plan_data['mp3'], 'remaining': remaining_mp3},
                'mp4': {'used': current_user.mp4_count, 'limit': plan_data['mp4'], 'remaining': remaining_mp4}
            }
        })
    
    # FIX: Return actual session data for anonymous users
    return jsonify({
        'logged_in': False,
        'plan': 'free',
        'plan_name': 'Free',
        'limits': PLANS['free'],
        'usage': {
            'mp3': {'used': mp3_used, 'limit': 5, 'remaining': max(0, 5 - mp3_used)},
            'mp4': {'used': mp4_used, 'limit': 5, 'remaining': max(0, 5 - mp4_used)}
        }
    })

def update_usage(user_id, media_type):
    conn = get_db()
    c = conn.cursor()
    if media_type == 'audio':
        c.execute("UPDATE users SET mp3_count = mp3_count + 1 WHERE id = ?", (user_id,))
    else:
        c.execute("UPDATE users SET mp4_count = mp4_count + 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

@app.route('/download', methods=['POST'])
def download():
    data = request.json
    url = data.get('url')
    media_type = data.get('type', 'audio')
    format_type = data.get('format', 'mp3')
    quality = data.get('quality', '192')
    
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    
    # Determine user's plan and check limits
    if current_user.is_authenticated:
        check_reset(current_user.id)
        user_plan = current_user.plan
        limits = PLANS.get(user_plan, PLANS['free'])
        
        # Check download limits
        if media_type == 'audio' and current_user.mp3_count >= limits['mp3']:
            return jsonify({'error': 'MP3 download limit reached. Please upgrade your plan.'}), 403
        if media_type == 'video' and current_user.mp4_count >= limits['mp4']:
            return jsonify({'error': 'MP4 download limit reached. Please upgrade your plan.'}), 403
            
        # Check quality limit
        if media_type == 'video':
            requested_height = int(quality)
            max_allowed = limits['max_quality']
            if requested_height > max_allowed:
                return jsonify({
                    'error': f'Quality limit exceeded. Your {limits["name"]} plan allows maximum {max_allowed}p.'
                }), 403
    else:
        # Anonymous users - check session limits
        limits = PLANS['free']
        session_key = f'downloads_{media_type}'
        current_downloads = session.get(session_key, 0)
        
        if current_downloads >= 5:
            return jsonify({'error': 'Free limit reached (5 downloads). Please sign in to continue.'}), 403
            
        # Check quality for guests
        if media_type == 'video':
            requested_height = int(quality)
            if requested_height > limits['max_quality']:
                return jsonify({
                    'error': f'Free plan limited to {limits["max_quality"]}p. Please upgrade to access higher quality.'
                }), 403
    
    temp_dir = tempfile.mkdtemp()
    
    try:
        if media_type == 'video':
            height = int(quality)
            
            ydl_opts = {
                'format': f'best[height<={height}][ext=mp4]/best[height<={height}]',
                'outtmpl': os.path.join(temp_dir, 'temp_video.%(ext)s'),
                'quiet': True,
            }
            
            if ffmpeg_available:
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4'
                }]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = sanitize_filename(info.get('title', 'video'))
                
                files = [f for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
                if not files:
                    raise Exception("Download failed")
                
                downloaded_file = os.path.join(temp_dir, files[0])
                final_name = f"{title}_{height}p.mp4"
                final_path = DOWNLOAD_DIR / final_name
                
                counter = 1
                while final_path.exists():
                    final_name = f"{title}_{height}p_{counter}.mp4"
                    final_path = DOWNLOAD_DIR / final_name
                    counter += 1
                
                shutil.move(downloaded_file, final_path)
                
                # FIX: Update usage after successful download
                if current_user.is_authenticated:
                    update_usage(current_user.id, 'video')
                else:
                    session[session_key] = current_downloads + 1
                    session.modified = True  # Ensure session saves
                
                return jsonify({
                    'success': True,
                    'filename': final_name,
                    'type': 'video',
                    'quality': f"{height}p",
                    'size': os.path.getsize(final_path)
                })
        
        else:  # Audio
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio',
                'outtmpl': os.path.join(temp_dir, 'temp_audio.%(ext)s'),
                'quiet': True,
            }
            
            if ffmpeg_available and format_type != 'm4a':
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': format_type,
                    'preferredquality': quality
                }]
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = sanitize_filename(info.get('title', 'audio'))
                
                files = [f for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
                if not files:
                    raise Exception("Download failed")
                
                output_file = os.path.join(temp_dir, files[0])
                ext = format_type if (ffmpeg_available or format_type == 'm4a') else 'm4a'
                final_name = f"{title}.{ext}"
                final_path = DOWNLOAD_DIR / final_name
                
                counter = 1
                while final_path.exists():
                    final_name = f"{title}_{counter}.{ext}"
                    final_path = DOWNLOAD_DIR / final_name
                    counter += 1
                
                shutil.move(output_file, final_path)
                
                # FIX: Update usage after successful download
                if current_user.is_authenticated:
                    update_usage(current_user.id, 'audio')
                else:
                    session[session_key] = current_downloads + 1
                    session.modified = True  # Ensure session saves
                
                return jsonify({
                    'success': True,
                    'filename': final_name,
                    'type': 'audio',
                    'size': os.path.getsize(final_path)
                })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

@app.route('/file/<filename>')
def serve_file(filename):
    safe_filename = os.path.basename(filename)
    return send_file(DOWNLOAD_DIR / safe_filename, as_attachment=True)

if __name__ == '__main__':
    print("=" * 60)
    print("🌐 YouTube Converter Pro")
    print("=" * 60)
    print(f"✅ FFmpeg ready: {ffmpeg_available}")
    print("=" * 60)
    print("📍 Open: http://localhost:5000")
    print("=" * 60)
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)


