import os
import logging
import asyncio
import sqlite3
import time
from datetime import datetime
import subprocess
import re
import uuid
import threading
import json
from typing import Dict, List, Optional, Tuple, Union

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.constants import ChatAction

# Konfigurasi logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Konfigurasi bot
TOKEN = "7839177497:AAFS7PtzQFXmaMkucUUgbdT5SjmEiWAJVRQ"  # Ganti dengan token bot Anda
ADMIN_IDS = [5988451717]  # Ganti dengan ID admin Anda
DOWNLOAD_PATH = "downloads/"  # Folder untuk menyimpan hasil rekaman

# Konfigurasi kualitas dan kompresi
TIKTOK_QUALITY = "best"  # Kualitas terbaik untuk TikTok
BIGO_QUALITY = "best"  # Kualitas terbaik untuk Bigo
COMPRESSION_ENABLED = True  # Aktifkan kompresi otomatis untuk file besar
COMPRESSION_THRESHOLD = 45 * 1024 * 1024  # Batas ukuran file untuk kompresi (45MB)
COMPRESSION_CRF = 23  # Constant Rate Factor (18-28, semakin rendah semakin berkualitas)
CHECK_INTERVAL = 300  # Interval pemeriksaan livestream (dalam detik)

# Pastikan folder download ada
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# Database untuk menyimpan data pengguna dan job
DB_PATH = "recorder_bot.db"

# Status record
active_recordings: Dict[str, Dict] = {}
recording_processes: Dict[str, subprocess.Popen] = {}
monitored_accounts: Dict[str, Dict] = {}  # Akun yang dipantau

# ========== DATABASE FUNCTIONS ==========

def init_db():
    """Initialize the database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Tabel pengguna
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        join_date TEXT,
        is_admin INTEGER DEFAULT 0
    )
    ''')
    
    # Tabel rekaman
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS recordings (
        id TEXT PRIMARY KEY,
        user_id INTEGER,
        platform TEXT,
        target TEXT,
        status TEXT,
        start_time TEXT,
        end_time TEXT,
        file_path TEXT,
        file_size INTEGER,
        compressed_path TEXT,
        compressed_size INTEGER,
        quality TEXT DEFAULT 'HD',
        FOREIGN KEY (user_id) REFERENCES users(user_id)
    )
    ''')
    
    # Tabel akun yang dipantau
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS monitored_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        platform TEXT,
        username TEXT,
        last_check TEXT,
        is_live INTEGER DEFAULT 0,
        auto_record INTEGER DEFAULT 0,
        notify_only INTEGER DEFAULT 0,
        added_time TEXT,
        FOREIGN KEY (user_id) REFERENCES users(user_id),
        UNIQUE(user_id, platform, username)
    )
    ''')
    
    # Tabel riwayat livestream
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS livestream_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER,
        start_time TEXT,
        end_time TEXT,
        duration INTEGER,
        was_recorded INTEGER DEFAULT 0,
        recording_id TEXT,
        FOREIGN KEY (account_id) REFERENCES monitored_accounts(id),
        FOREIGN KEY (recording_id) REFERENCES recordings(id)
    )
    ''')
    
    conn.commit()
    conn.close()

def register_user(user_id: int, username: str, first_name: str, last_name: str):
    """Register new user to database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username, first_name, last_name, join_date) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, first_name, last_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    
    conn.commit()
    conn.close()

def save_recording(recording_id: str, user_id: int, platform: str, target: str, 
                  status: str, start_time: str, end_time: str = None, 
                  file_path: str = None, file_size: int = 0, quality: str = "HD"):
    """Save recording data to database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        """INSERT INTO recordings 
        (id, user_id, platform, target, status, start_time, end_time, file_path, file_size, quality) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (recording_id, user_id, platform, target, status, start_time, end_time, file_path, file_size, quality)
    )
    
    conn.commit()
    conn.close()

def update_recording_status(recording_id: str, status: str, end_time: str = None, 
                           file_path: str = None, file_size: int = None,
                           compressed_path: str = None, compressed_size: int = None):
    """Update recording status in database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    update_fields = ["status = ?"]
    update_values = [status]
    
    if end_time:
        update_fields.append("end_time = ?")
        update_values.append(end_time)
        
    if file_path:
        update_fields.append("file_path = ?")
        update_values.append(file_path)
        
    if file_size is not None:
        update_fields.append("file_size = ?")
        update_values.append(file_size)
        
    if compressed_path:
        update_fields.append("compressed_path = ?")
        update_values.append(compressed_path)
        
    if compressed_size is not None:
        update_fields.append("compressed_size = ?")
        update_values.append(compressed_size)
    
    update_query = f"UPDATE recordings SET {', '.join(update_fields)} WHERE id = ?"
    update_values.append(recording_id)
    
    cursor.execute(update_query, update_values)
    
    conn.commit()
    conn.close()

def add_monitored_account(user_id: int, platform: str, username: str, auto_record: bool = False, notify_only: bool = False):
    """Add account to monitoring list"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            """INSERT INTO monitored_accounts 
            (user_id, platform, username, last_check, added_time, auto_record, notify_only) 
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, platform, username, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(auto_record), int(notify_only))
        )
        
        account_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return True, account_id
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Akun ini sudah dipantau"
    except Exception as e:
        conn.close()
        return False, str(e)

def remove_monitored_account(user_id: int, platform: str, username: str):
    """Remove account from monitoring list"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "DELETE FROM monitored_accounts WHERE user_id = ? AND platform = ? AND username = ?",
        (user_id, platform, username)
    )
    
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_monitored_accounts(user_id: int = None, platform: str = None):
    """Get monitored accounts from database"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM monitored_accounts"
    params = []
    
    if user_id:
        query += " WHERE user_id = ?"
        params.append(user_id)
        
        if platform:
            query += " AND platform = ?"
            params.append(platform)
    
    cursor.execute(query, params)
    result = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return result

def update_account_live_status(account_id: int, is_live: bool):
    """Update account live status"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute(
        "UPDATE monitored_accounts SET is_live = ?, last_check = ? WHERE id = ?",
        (int(is_live), current_time, account_id)
    )
    
    conn.commit()
    conn.close()
    
def add_livestream_history(account_id: int, start_time: str):
    """Add new livestream to history"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        """INSERT INTO livestream_history 
        (account_id, start_time) 
        VALUES (?, ?)""",
        (account_id, start_time)
    )
    
    history_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return history_id

def update_livestream_history(history_id: int, end_time: str, duration: int, was_recorded: bool = False, recording_id: str = None):
    """Update livestream history when stream ends"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        """UPDATE livestream_history 
        SET end_time = ?, duration = ?, was_recorded = ?, recording_id = ?
        WHERE id = ?""",
        (end_time, duration, int(was_recorded), recording_id, history_id)
    )
    
    conn.commit()
    conn.close()

def get_user_recordings(user_id: int, status: str = None) -> List[Dict]:
    """Get recordings for a specific user"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM recordings WHERE user_id = ?"
    params = [user_id]
    
    if status:
        query += " AND status = ?"
        params.append(status)
    
    query += " ORDER BY start_time DESC"
    
    cursor.execute(query, params)
    result = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return result

def get_recording_by_id(recording_id: str) -> Optional[Dict]:
    """Get recording info by ID"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,))
    result = cursor.fetchone()
    
    conn.close()
    return dict(result) if result else None

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_IDS

# ========== HELPER FUNCTIONS ==========

def get_platform_from_url(url: str) -> Optional[str]:
    """Detect platform from URL"""
    if not url:
        return None
        
    if "tiktok.com" in url:
        return "tiktok"
    elif "bigo.tv" in url or "bigo.live" in url:
        return "bigo"
    else:
        return None

def validate_tiktok_username(username: str) -> bool:
    """Validate TikTok username format"""
    # TikTok usernames typically allow letters, numbers, underscores, and periods
    pattern = r'^[a-zA-Z0-9_.]+$'
    return bool(re.match(pattern, username))

def validate_bigo_username(username: str) -> bool:
    """Validate Bigo username format"""
    # Bigo usernames typically allow letters, numbers and underscores
    pattern = r'^[a-zA-Z0-9_]+$'
    return bool(re.match(pattern, username))

def get_file_size(file_path: str) -> int:
    """Get file size in bytes"""
    try:
        return os.path.getsize(file_path)
    except (FileNotFoundError, OSError):
        return 0

def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes/(1024*1024):.2f} MB"
    else:
        return f"{size_bytes/(1024*1024*1024):.2f} GB"

def get_progress_bar(percentage: float, length: int = 10) -> str:
    """Create a text-based progress bar"""
    filled_length = int(length * percentage / 100)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {percentage:.1f}%"

def generate_thumbnail(file_path: str, timestamp: float = 5.0) -> Optional[str]:
    """Generate thumbnail from video file using ffmpeg"""
    try:
        thumbnail_path = f"{file_path}.jpg"
        cmd = [
            "ffmpeg",
            "-ss", str(timestamp),
            "-i", file_path,
            "-vframes", "1",
            "-q:v", "2",
            thumbnail_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return thumbnail_path if os.path.exists(thumbnail_path) else None
    except subprocess.CalledProcessError:
        logger.error(f"Failed to generate thumbnail for {file_path}")
        return None

# ========== RECORDING FUNCTIONS ==========

async def start_tiktok_recording(username_or_url: str, user_id: int, auto_record: bool = False) -> Tuple[bool, str, str]:
    """Start recording TikTok livestream"""
    recording_id = str(uuid.uuid4())
    current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    # Determine if input is username or URL
    if "tiktok.com" in username_or_url:
        # Input is URL
        target = username_or_url
        # Extract username from URL for filename
        try:
            match = re.search(r'@([^/?]+)', username_or_url)
            username = match.group(1) if match else "unknown"
        except:
            username = "unknown"
    else:
        # Input is username
        username = username_or_url.strip('@')
        target = f"https://www.tiktok.com/@{username}/live"
    
    if not validate_tiktok_username(username):
        return False, recording_id, "Format username tidak valid"
    
    output_file = os.path.join(DOWNLOAD_PATH, f"tiktok_{username}_{current_time}.mp4")
    
    # Prepare command to record using yt-dlp with HD quality
    cmd = [
        "yt-dlp",
        "--no-part",
        "--no-mtime",
        "--no-playlist",
        "-f", TIKTOK_QUALITY, # Pilih kualitas terbaik
        "--hls-use-mpegts",   # Gunakan format MPEG-TS untuk streaming yang lebih stabil
        "--live-from-start",  # Rekam dari awal livestream
        "--wait-for-video", "10", # Tunggu hingga 10 detik jika livestream belum dimulai
        "-o", output_file,
        target
    ]
    
    try:
        # Start recording process
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Save to database
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_recording(
            recording_id=recording_id,
            user_id=user_id,
            platform="tiktok",
            target=target,
            status="recording",
            start_time=start_time,
            file_path=output_file,
            quality="HD"
        )
        
        # Add to active recordings
        active_recordings[recording_id] = {
            "user_id": user_id,
            "platform": "tiktok",
            "target": target,
            "start_time": start_time,
            "output_file": output_file,
            "username": username,
            "auto_record": auto_record
        }
        
        # Store process reference
        recording_processes[recording_id] = process
        
        # Start monitoring thread
        threading.Thread(
            target=monitor_recording_process,
            args=(recording_id, process),
            daemon=True
        ).start()
        
        return True, recording_id, output_file
    
    except Exception as e:
        logger.error(f"Error starting TikTok recording: {str(e)}")
        return False, recording_id, f"Error: {str(e)}"

async def start_bigo_recording(username_or_url: str, user_id: int, auto_record: bool = False) -> Tuple[bool, str, str]:
    """Start recording Bigo livestream"""
    recording_id = str(uuid.uuid4())
    current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    # Determine if input is username or URL
    if "bigo.tv" in username_or_url or "bigo.live" in username_or_url:
        # Input is URL
        target = username_or_url
        # Extract username from URL for filename
        try:
            if "bigo.tv" in username_or_url:
                match = re.search(r'bigo.tv/([^/?]+)', username_or_url)
            else:
                match = re.search(r'bigo.live/([^/?]+)', username_or_url)
            username = match.group(1) if match else "unknown"
        except:
            username = "unknown"
    else:
        # Input is username
        username = username_or_url
        target = f"https://www.bigo.tv/{username}"
    
    if not validate_bigo_username(username):
        return False, recording_id, "Format username tidak valid"
    
    output_file = os.path.join(DOWNLOAD_PATH, f"bigo_{username}_{current_time}.mp4")
    
    # Prepare command to record using streamlink dengan kualitas HD
    cmd = [
        "streamlink",
        "--force",
        "--hls-live-restart",
        "--hls-segment-threads", "3",  # Gunakan beberapa thread untuk download
        "--hls-timeout", "180",        # Timeout lebih lama
        "--retry-streams", "5",
        "--retry-max", "10",
        "--retry-open", "5",
        "--stream-timeout", "120",
        "--ringbuffer-size", "64M",     # Buffer besar untuk kualitas tinggi
        "-o", output_file,
        target, BIGO_QUALITY            # Kualitas terbaik
    ]
    
    try:
        # Start recording process
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Save to database
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_recording(
            recording_id=recording_id,
            user_id=user_id,
            platform="bigo",
            target=target,
            status="recording",
            start_time=start_time,
            file_path=output_file,
            quality="HD"
        )
        
        # Add to active recordings
        active_recordings[recording_id] = {
            "user_id": user_id,
            "platform": "bigo",
            "target": target,
            "start_time": start_time,
            "output_file": output_file,
            "username": username,
            "auto_record": auto_record
        }
        
        # Store process reference
        recording_processes[recording_id] = process
        
        # Start monitoring thread
        threading.Thread(
            target=monitor_recording_process,
            args=(recording_id, process),
            daemon=True
        ).start()
        
        return True, recording_id, output_file
    
    except Exception as e:
        logger.error(f"Error starting Bigo recording: {str(e)}")
        return False, recording_id, f"Error: {str(e)}"

def monitor_recording_process(recording_id: str, process: subprocess.Popen):
    """Monitor recording process and update status when done"""
    # Wait for process to complete
    stdout, stderr = process.communicate()
    
    # Get current time
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Check if recording_id still exists in active_recordings
    if recording_id in active_recordings:
        recording_info = active_recordings[recording_id]
        output_file = recording_info["output_file"]
        
        # Check if process was terminated normally or forcefully
        if process.returncode == 0:
            status = "completed"
        else:
            # Check if file exists and has size
            if os.path.exists(output_file) and get_file_size(output_file) > 0:
                status = "completed"  # It has some content
            else:
                status = "failed"
                logger.error(f"Recording {recording_id} failed: {stderr}")
        
        # Update database
        file_size = get_file_size(output_file) if os.path.exists(output_file) else 0
        
        # Check if we need to compress the file
        compressed_path = None
        compressed_size = None
        
        if status == "completed" and os.path.exists(output_file) and file_size > 0:
            # If file is large, compress it
            if COMPRESSION_ENABLED and file_size > COMPRESSION_THRESHOLD:
                compressed_path = compress_video(output_file)
                if compressed_path:
                    compressed_size = get_file_size(compressed_path)
                    logger.info(f"Compressed {output_file} from {format_size(file_size)} to {format_size(compressed_size)}")
        
        # Update database
        update_recording_status(
            recording_id=recording_id,
            status=status,
            end_time=end_time,
            file_path=output_file,
            file_size=file_size,
            compressed_path=compressed_path,
            compressed_size=compressed_size
        )
        
        # Clean up
        del active_recordings[recording_id]
        if recording_id in recording_processes:
            del recording_processes[recording_id]
        
        # Try to notify user that recording is complete
        # Note: This is an async operation but we're in a non-async thread
        # For a full implementation, you would need a proper async task management
        asyncio.run_coroutine_threadsafe(
            notify_recording_completed(recording_id, status), 
            asyncio.get_event_loop()
        )

def compress_video(input_file: str) -> Optional[str]:
    """Compress video using FFmpeg with high quality"""
    try:
        # Get file info
        file_name, file_ext = os.path.splitext(input_file)
        output_file = f"{file_name}_compressed{file_ext}"
        
        # FFmpeg command for high quality compression
        cmd = [
            "ffmpeg",
            "-i", input_file,
            "-c:v", "libx264",         # Use H.264 codec
            "-crf", str(COMPRESSION_CRF),  # Constant Rate Factor (18-28, lower is better quality)
            "-preset", "slow",         # Slow preset for better compression
            "-c:a", "aac",             # AAC audio codec
            "-b:a", "128k",            # Audio bitrate
            "-movflags", "+faststart", # Optimize for web
            output_file
        ]
        
        # Run compression
        process = subprocess.run(
            cmd, 
            check=True, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE
        )
        
        # Check if output file exists and has content
        if os.path.exists(output_file) and get_file_size(output_file) > 0:
            return output_file
        else:
            return None
            
    except Exception as e:
        logger.error(f"Error compressing video: {str(e)}")
        return None

async def check_livestream_status(account_id: int, platform: str, username: str, user_id: int):
    """Check if an account is currently livestreaming"""
    try:
        is_live = False
        
        if platform == "tiktok":
            # Check TikTok livestream
            url = f"https://www.tiktok.com/@{username}/live"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                if "LIVE" in response.text and username.lower() in response.text.lower():
                    is_live = True
        
        elif platform == "bigo":
            # Check Bigo livestream
            url = f"https://www.bigo.tv/{username}"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                if "isLive" in response.text and "liveRoom" in response.text:
                    is_live = True
        
        # Get current account status
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT is_live, auto_record, notify_only FROM monitored_accounts WHERE id = ?", (account_id,))
        account_data = cursor.fetchone()
        conn.close()
        
        if not account_data:
            return
        
        was_live = bool(account_data["is_live"])
        auto_record = bool(account_data["auto_record"])
        notify_only = bool(account_data["notify_only"])
        
        # Update status in database
        update_account_live_status(account_id, is_live)
        
        # If status changed from not live to live
        if is_live and not was_live:
            # Add to livestream history
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            history_id = add_livestream_history(account_id, start_time)
            
            # Send notification to user
            bot = Application.get_instance().bot
            if platform == "tiktok":
                message = f"🔴 <b>@{username} sedang LIVE di TikTok!</b>"
                link = f"https://www.tiktok.com/@{username}/live"
            else:
                message = f"🔴 <b>{username} sedang LIVE di Bigo!</b>"
                link = f"https://www.bigo.tv/{username}"
                
            keyboard = []
            
            # Add record button if not auto-recording
            if not auto_record or notify_only:
                if platform == "tiktok":
                    record_data = f"record_notif_tiktok_{username}"
                else:
                    record_data = f"record_notif_bigo_{username}"
                    
                keyboard.append([InlineKeyboardButton("🎥 Rekam Sekarang", callback_data=record_data)])
            
            # Add view button
            keyboard.append([InlineKeyboardButton("👁️ Tonton Livestream", url=link)])
            
            reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
            
            await bot.send_message(
                chat_id=user_id,
                text=f"{message}\n\nLink: {link}",
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML
            )
            
            # Auto-record if enabled and not notify-only
            if auto_record and not notify_only:
                if platform == "tiktok":
                    success, recording_id, result = await start_tiktok_recording(username, user_id, auto_record=True)
                else:
                    success, recording_id, result = await start_bigo_recording(username, user_id, auto_record=True)
                
                if success:
                    # Update livestream history
                    update_livestream_history(history_id, None, 0, was_recorded=True, recording_id=recording_id)
                    
                    # Send additional notification
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"✅ <b>Auto-Record dimulai untuk {platform.upper()}: {username}</b>\n\nID: <code>{recording_id}</code>",
                        parse_mode=ParseMode.HTML
                    )
                    
        # If status changed from live to not live
        elif not is_live and was_live:
            # Update livestream history
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            
            cursor.execute(
                """SELECT id, start_time FROM livestream_history 
                WHERE account_id = ? AND end_time IS NULL
                ORDER BY start_time DESC LIMIT 1""", 
                (account_id,)
            )
            
            history = cursor.fetchone()
            conn.close()
            
            if history:
                history_id, start_time = history
                end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Calculate duration
                start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
                end_dt = datetime.now()
                duration = int((end_dt - start_dt).total_seconds())
                
                # Update history
                update_livestream_history(history_id, end_time, duration)
                
                # Send notification
                bot = Application.get_instance().bot
                await bot.send_message(
                    chat_id=user_id,
                    text=f"⚫ <b>{username} telah selesai LIVE di {platform.upper()}</b>\n\nDurasi: {format_duration(duration)}",
                    parse_mode=ParseMode.HTML
                )
                
    except Exception as e:
        logger.error(f"Error checking livestream status: {str(e)}")

def format_duration(seconds: int) -> str:
    """Format seconds to human-readable duration"""
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

async def stop_recording(recording_id: str) -> bool:
    """Stop an active recording"""
    if recording_id not in active_recordings or recording_id not in recording_processes:
        return False
    
    try:
        # Get process
        process = recording_processes[recording_id]
        
        # Terminate process
        process.terminate()
        
        # Wait a bit to ensure it's terminated
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()  # Force kill if it doesn't terminate
        
        # Update database
        end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        output_file = active_recordings[recording_id]["output_file"]
        file_size = get_file_size(output_file) if os.path.exists(output_file) else 0
        
        update_recording_status(
            recording_id=recording_id,
            status="stopped",
            end_time=end_time,
            file_path=output_file,
            file_size=file_size
        )
        
        # Clean up
        del active_recordings[recording_id]
        del recording_processes[recording_id]
        
        return True
    
    except Exception as e:
        logger.error(f"Error stopping recording {recording_id}: {str(e)}")
        return False

async def notify_recording_completed(recording_id: str, status: str):
    """Notify user about completed recording"""
    try:
        # Get recording info
        recording_info = get_recording_by_id(recording_id)
        if not recording_info:
            return
        
        user_id = recording_info["user_id"]
        platform = recording_info["platform"]
        target = recording_info["target"]
        file_path = recording_info["file_path"]
        file_size = recording_info["file_size"]
        compressed_path = recording_info.get("compressed_path")
        compressed_size = recording_info.get("compressed_size", 0)
        
        # Determine which file to use (compressed or original)
        use_compressed = compressed_path and os.path.exists(compressed_path) and compressed_size > 0
        actual_file = compressed_path if use_compressed else file_path
        actual_size = compressed_size if use_compressed else file_size
        
        # Prepare message
        if status == "completed":
            message = (
                f"✅ <b>Recording Selesai!</b>\n\n"
                f"<b>Platform:</b> {platform.upper()}\n"
                f"<b>Target:</b> {target}\n"
                f"<b>Ukuran:</b> {format_size(actual_size)}"
            )
            
            if use_compressed:
                message += f"\n<b>Kompresi:</b> {format_size(file_size)} → {format_size(compressed_size)}"
                
            message += f"\n<b>Status:</b> {status.upper()}"
        else:
            message = (
                f"❌ <b>Recording Gagal!</b>\n\n"
                f"<b>Platform:</b> {platform.upper()}\n"
                f"<b>Target:</b> {target}\n"
                f"<b>Status:</b> {status.upper()}"
            )
        
        # Add buttons
        keyboard = []
        
        if status == "completed" and os.path.exists(actual_file) and actual_size > 0:
            # Generate thumbnail
            thumbnail = generate_thumbnail(actual_file)
            
            # Add download button
            download_button = InlineKeyboardButton(
                "⬇️ Download", 
                callback_data=f"download_{recording_id}"
            )
            keyboard.append([download_button])
            
            # Add delete button
            delete_button = InlineKeyboardButton(
                "🗑️ Hapus", 
                callback_data=f"delete_{recording_id}"
            )
            keyboard.append([delete_button])
        
        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        # Send message to user
        bot = Application.get_instance().bot
        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
        
        # Send thumbnail if available
        if status == "completed" and thumbnail and os.path.exists(thumbnail):
            try:
                with open(thumbnail, "rb") as photo:
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=photo,
                        caption=f"Preview dari {os.path.basename(actual_file)}"
                    )
            except Exception as e:
                logger.error(f"Error sending thumbnail: {str(e)}")
        
    except Exception as e:
        logger.error(f"Error notifying user about completed recording: {str(e)}")

async def run_account_monitor():
    """Check monitored accounts periodically"""
    while True:
        try:
            # Get all monitored accounts
            accounts = get_monitored_accounts()
            
            for account in accounts:
                # Check if it's time to check this account
                last_check_str = account["last_check"]
                if not last_check_str:
                    last_check_time = datetime.min
                else:
                    last_check_time = datetime.strptime(last_check_str, "%Y-%m-%d %H:%M:%S")
                
                current_time = datetime.now()
                time_diff = (current_time - last_check_time).total_seconds()
                
                # Only check if enough time has passed
                if time_diff >= CHECK_INTERVAL:
                    await check_livestream_status(
                        account_id=account["id"],
                        platform=account["platform"],
                        username=account["username"],
                        user_id=account["user_id"]
                    )
            
            # Sleep before next check
            await asyncio.sleep(60)  # Check every minute
            
        except Exception as e:
            logger.error(f"Error in account monitor: {str(e)}")
            await asyncio.sleep(60)  # Sleep and try again

# ========== UI COMPONENTS ==========

def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Create main menu keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("🎥 Record TikTok", callback_data="record_tiktok"),
            InlineKeyboardButton("🎥 Record Bigo", callback_data="record_bigo")
        ],
        [
            InlineKeyboardButton("📋 Recordings Aktif", callback_data="list_active"),
            InlineKeyboardButton("📂 Recordings Selesai", callback_data="list_completed")
        ],
        [
            InlineKeyboardButton("🔔 Monitor Akun", callback_data="monitor_accounts"),
            InlineKeyboardButton("⚙️ Pengaturan", callback_data="settings")
        ],
        [
            InlineKeyboardButton("ℹ️ Info", callback_data="info"),
            InlineKeyboardButton("❓ Bantuan", callback_data="help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_monitor_menu_keyboard() -> InlineKeyboardMarkup:
    """Create monitoring menu keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("➕ Tambah Akun TikTok", callback_data="add_monitor_tiktok"),
            InlineKeyboardButton("➕ Tambah Akun Bigo", callback_data="add_monitor_bigo")
        ],
        [
            InlineKeyboardButton("📋 Daftar Akun Terpantau", callback_data="list_monitored")
        ],
        [InlineKeyboardButton("« Kembali", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_settings_keyboard() -> InlineKeyboardMarkup:
    """Create settings keyboard"""
    compression_status = "✅ ON" if COMPRESSION_ENABLED else "❌ OFF"
    
    keyboard = [
        [
            InlineKeyboardButton(f"🗜️ Kompresi: {compression_status}", callback_data="toggle_compression")
        ],
        [
            InlineKeyboardButton("🎬 Kualitas TikTok", callback_data="quality_tiktok"),
            InlineKeyboardButton("🎬 Kualitas Bigo", callback_data="quality_bigo")
        ],
        [InlineKeyboardButton("« Kembali", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_monitored_accounts_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create keyboard for monitored accounts"""
    accounts = get_monitored_accounts(user_id)
    
    keyboard = []
    for account in accounts:
        acc_id = account["id"]
        platform = account["platform"].upper()
        username = account["username"]
        is_live = bool(account["is_live"])
        auto_record = bool(account["auto_record"])
        notify_only = bool(account["notify_only"])
        
        # Status indicators
        live_status = "🔴 LIVE" if is_live else "⚫"
        auto_status = "🔄 AUTO" if auto_record else ""
        notify_status = "🔔 NOTIFY" if notify_only else ""
        
        status = f"{live_status} {auto_status} {notify_status}".strip()
        button_text = f"{platform}: {username} ({status})"
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"monitor_{acc_id}")])
    
    # Add back button
    keyboard.append([InlineKeyboardButton("➕ Tambah Akun", callback_data="monitor_accounts")])
    keyboard.append([InlineKeyboardButton("« Kembali", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_account_details_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Create keyboard for account details"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM monitored_accounts WHERE id = ?", (account_id,))
    account = cursor.fetchone()
    conn.close()
    
    if not account:
        # Return to monitored accounts list if account not found
        return InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali", callback_data="list_monitored")]])
    
    auto_record = bool(account["auto_record"])
    notify_only = bool(account["notify_only"])
    
    keyboard = []
    
    # Toggle auto-record
    auto_text = "❌ Matikan Auto-Record" if auto_record else "✅ Aktifkan Auto-Record"
    keyboard.append([InlineKeyboardButton(auto_text, callback_data=f"toggle_auto_{account_id}")])
    
    # Toggle notify-only
    notify_text = "❌ Matikan Notifikasi Saja" if notify_only else "✅ Aktifkan Notifikasi Saja"
    keyboard.append([InlineKeyboardButton(notify_text, callback_data=f"toggle_notify_{account_id}")])
    
    # Record now if live
    if bool(account["is_live"]):
        platform = account["platform"]
        username = account["username"]
        
        if platform == "tiktok":
            record_data = f"record_monitor_tiktok_{username}"
        else:
            record_data = f"record_monitor_bigo_{username}"
            
        keyboard.append([InlineKeyboardButton("🎥 Rekam Sekarang", callback_data=record_data)])
    
    # Delete account
    keyboard.append([InlineKeyboardButton("🗑️ Hapus Akun", callback_data=f"delete_account_{account_id}")])
    
    # Back button
    keyboard.append([InlineKeyboardButton("« Kembali", callback_data="list_monitored")])
    
    return InlineKeyboardMarkup(keyboard)

def get_back_button() -> InlineKeyboardMarkup:
    """Create back button keyboard"""
    keyboard = [[InlineKeyboardButton("« Kembali", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Create cancel keyboard"""
    keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="cancel")]]
    return InlineKeyboardMarkup(keyboard)

def get_active_recordings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create keyboard for active recordings"""
    recordings = get_user_recordings(user_id, status="recording")
    
    keyboard = []
    for recording in recordings:
        rec_id = recording["id"]
        platform = recording["platform"].upper()
        target = recording["target"]
        
        # Extract username from target for shorter display
        username = target
        if platform == "TIKTOK":
            match = re.search(r'@([^/?]+)', target)
            username = f"@{match.group(1)}" if match else target
        elif platform == "BIGO":
            match = re.search(r'bigo\.(?:tv|live)/([^/?]+)', target)
            username = match.group(1) if match else target
        
        button_text = f"{platform}: {username}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_{rec_id}")])
    
    # Add back button
    keyboard.append([InlineKeyboardButton("« Kembali", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_completed_recordings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Create keyboard for completed recordings"""
    # Get completed and stopped recordings
    recordings = get_user_recordings(user_id, status="completed")
    recordings += get_user_recordings(user_id, status="stopped")
    
    # Sort by start time (newest first)
    recordings.sort(key=lambda x: x["start_time"], reverse=True)
    
    keyboard = []
    for recording in recordings[:10]:  # Limit to 10 most recent
        rec_id = recording["id"]
        platform = recording["platform"].upper()
        target = recording["target"]
        status = recording["status"].upper()
        
        # Extract username from target for shorter display
        username = target
        if platform == "TIKTOK":
            match = re.search(r'@([^/?]+)', target)
            username = f"@{match.group(1)}" if match else target
        elif platform == "BIGO":
            match = re.search(r'bigo\.(?:tv|live)/([^/?]+)', target)
            username = match.group(1) if match else target
        
        button_text = f"{platform}: {username} ({status})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_{rec_id}")])
    
    # Add back button
    keyboard.append([InlineKeyboardButton("« Kembali", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_recording_details_keyboard(recording_id: str) -> InlineKeyboardMarkup:
    """Create keyboard for recording details"""
    recording = get_recording_by_id(recording_id)
    
    keyboard = []
    status = recording["status"]
    
    if status == "recording":
        # Add stop button
        keyboard.append([InlineKeyboardButton("⏹️ Stop Recording", callback_data=f"stop_{recording_id}")])
    elif status in ["completed", "stopped"]:
        # Check if file exists
        file_path = recording["file_path"]
        if os.path.exists(file_path) and get_file_size(file_path) > 0:
            # Add download button
            keyboard.append([InlineKeyboardButton("⬇️ Download", callback_data=f"download_{recording_id}")])
            # Add delete button
            keyboard.append([InlineKeyboardButton("🗑️ Hapus File", callback_data=f"delete_{recording_id}")])
    
    # Add back button
    if status == "recording":
        keyboard.append([InlineKeyboardButton("« Kembali", callback_data="list_active")])
    else:
        keyboard.append([InlineKeyboardButton("« Kembali", callback_data="list_completed")])
    
    return InlineKeyboardMarkup(keyboard)

# ========== COMMAND HANDLERS ==========

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /start command"""
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    
    # Register user in database
    register_user(user_id, username, first_name, last_name)
    
    # Welcome message with stylish formatting
    welcome_text = (
        f"🎬 <b>Selamat Datang di Livestream Recorder Bot</b> 🎬\n\n"
        f"Hai {first_name}, bot ini bisa merekam livestream dari TikTok dan Bigo.\n\n"
        f"<b>Fitur-fitur:</b>\n"
        f"• Rekam livestream TikTok dan Bigo\n"
        f"• Bisa menggunakan username atau link\n"
        f"• Pantau akun dan dapatkan notifikasi saat live\n"
        f"• Auto-record saat akun yang dipantau mulai live\n"
        f"• Kualitas HD dan kompresi cerdas\n"
        f"• Download hasil rekaman langsung dari bot\n\n"
        f"Pilih menu di bawah untuk memulai."
    )
    
    # Add special admin notice if user is admin
    if is_admin(user_id):
        welcome_text += "\n\n🔐 <b>Status Admin Terdeteksi!</b>"
    
    await update.message.reply_html(
        welcome_text,
        reply_markup=get_main_menu_keyboard()
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /help command"""
    help_text = (
        f"📖 <b>BANTUAN PENGGUNAAN BOT</b> 📖\n\n"
        f"<b>Cara Merekam Livestream:</b>\n"
        f"1. Pilih platform (TikTok atau Bigo)\n"
        f"2. Masukkan username atau link livestream\n"
        f"3. Bot akan mulai merekam\n\n"
        
        f"<b>Cara Memantau Akun:</b>\n"
        f"1. Pilih 'Monitor Akun' di menu utama\n"
        f"2. Tambahkan akun TikTok atau Bigo\n"
        f"3. Bot akan memberi tahu saat akun mulai live\n"
        f"4. Aktifkan auto-record untuk merekam otomatis\n\n"
        
        f"<b>Format username/link yang didukung:</b>\n"
        f"• TikTok: @username atau https://www.tiktok.com/@username/live\n"
        f"• Bigo: username atau https://www.bigo.tv/username\n\n"
        
        f"<b>Perintah yang tersedia:</b>\n"
        f"/start - Memulai bot dan menampilkan menu utama\n"
        f"/help - Menampilkan bantuan ini\n"
        f"/record - Memulai proses rekaman\n"
        f"/active - Melihat rekaman yang sedang berlangsung\n"
        f"/monitor - Mengelola akun yang dipantau\n"
        f"/settings - Mengubah pengaturan bot\n"
        f"/cancel - Membatalkan proses yang sedang berjalan\n\n"
        
        f"<b>Catatan:</b>\n"
        f"• Hasil rekaman disimpan dalam kualitas HD\n"
        f"• File besar akan dikompresi secara otomatis tanpa mengurangi kualitas\n"
        f"• Bot akan merekam livestream sampai selesai atau dihentikan manual"
    )
    
    await update.message.reply_html(
        help_text,
        reply_markup=get_back_button()
    )

async def cmd_record(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /record command"""
    await update.message.reply_html(
        "Pilih platform yang ingin direkam:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("TikTok", callback_data="record_tiktok"),
                InlineKeyboardButton("Bigo", callback_data="record_bigo")
            ],
            [InlineKeyboardButton("« Batal", callback_data="main_menu")]
        ])
    )

async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /active command"""
    user_id = update.effective_user.id
    
    # Get active recordings for user
    recordings = get_user_recordings(user_id, status="recording")
    
    if not recordings:
        await update.message.reply_text(
            "Tidak ada rekaman yang sedang aktif saat ini.",
            reply_markup=get_back_button()
        )
        return
    
    # Format message
    text = "📋 <b>REKAMAN AKTIF</b> 📋\n\n"
    for idx, rec in enumerate(recordings, 1):
        platform = rec["platform"].upper()
        target = rec["target"]
        start_time = rec["start_time"]
        
        text += f"{idx}. <b>{platform}</b>: {target}\n"
        text += f"   Mulai: {start_time}\n\n"
    
    await update.message.reply_html(
        text,
        reply_markup=get_active_recordings_keyboard(user_id)
    )

async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /monitor command"""
    user_id = update.effective_user.id
    
    await update.message.reply_html(
        "🔔 <b>MONITOR AKUN LIVESTREAM</b> 🔔\n\n"
        "Tambahkan akun untuk dipantau. Bot akan memberi tahu Anda saat akun mulai livestream.\n\n"
        "Anda juga dapat mengaktifkan <b>Auto-Record</b> agar bot otomatis merekam saat akun mulai livestream.",
        reply_markup=get_monitor_menu_keyboard()
    )

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /settings command"""
    user_id = update.effective_user.id
    
    await update.message.reply_html(
        "⚙️ <b>PENGATURAN</b> ⚙️\n\n"
        "Ubah pengaturan untuk bot recorder:\n\n"
        f"🗜️ <b>Kompresi Video:</b> {'✅ Aktif' if COMPRESSION_ENABLED else '❌ Nonaktif'}\n"
        f"📊 <b>Batas Kompresi:</b> {format_size(COMPRESSION_THRESHOLD)}\n"
        f"🎬 <b>Kualitas TikTok:</b> {TIKTOK_QUALITY}\n"
        f"🎬 <b>Kualitas Bigo:</b> {BIGO_QUALITY}\n"
        f"⏰ <b>Interval Cek Akun:</b> {CHECK_INTERVAL} detik\n",
        reply_markup=get_settings_keyboard()
    )

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /cancel command"""
    if 'waiting_for_input' in context.user_data:
        del context.user_data['waiting_for_input']
        await update.message.reply_text(
            "Operasi dibatalkan.",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "Tidak ada operasi yang sedang berlangsung.",
            reply_markup=get_main_menu_keyboard()
        )

# ========== CALLBACK QUERY HANDLERS ==========

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for callback queries from inline keyboards"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Always acknowledge the callback query
    await query.answer()
    
    # Main menu
    if query.data == "main_menu":
        await query.message.edit_text(
            "🎬 <b>MENU UTAMA</b> 🎬\n\nPilih opsi di bawah ini:",
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
    
    # Help
    elif query.data == "help":
        help_text = (
            f"📖 <b>BANTUAN PENGGUNAAN BOT</b> 📖\n\n"
            f"<b>Cara Merekam Livestream:</b>\n"
            f"1. Pilih platform (TikTok atau Bigo)\n"
            f"2. Masukkan username atau link livestream\n"
            f"3. Bot akan mulai merekam\n\n"
            
            f"<b>Format username/link yang didukung:</b>\n"
            f"• TikTok: @username atau https://www.tiktok.com/@username/live\n"
            f"• Bigo: username atau https://www.bigo.tv/username\n\n"
            
            f"<b>Perintah yang tersedia:</b>\n"
            f"/start - Memulai bot dan menampilkan menu utama\n"
            f"/help - Menampilkan bantuan ini\n"
            f"/record - Memulai proses rekaman\n"
            f"/active - Melihat rekaman yang sedang berlangsung\n"
            f"/cancel - Membatalkan proses yang sedang berjalan"
        )
        
        await query.message.edit_text(
            help_text,
            reply_markup=get_back_button(),
            parse_mode=ParseMode.HTML
        )
    
    # Info
    elif query.data == "info":
        info_text = (
            f"ℹ️ <b>INFORMASI BOT</b> ℹ️\n\n"
            f"<b>Livestream Recorder Bot</b>\n"
            f"Versi: 1.0.0\n\n"
            f"Bot ini dibuat untuk merekam livestream dari platform TikTok dan Bigo. "
            f"Hasil rekaman akan disimpan dalam format MP4 dan dapat diunduh langsung dari bot.\n\n"
            f"<b>Fitur:</b>\n"
            f"• Rekam TikTok dan Bigo livestream\n"
            f"• Support username dan link\n"
            f"• Multi-job processing\n"
            f"• Real-time progress monitoring\n\n"
            f"<b>Dibuat dengan:</b>\n"
            f"• Python Telegram Bot\n"
            f"• FFmpeg untuk processing video\n"
            f"• YT-DLP untuk TikTok\n"
            f"• Streamlink untuk Bigo"
        )
        
        await query.message.edit_text(
            info_text,
            reply_markup=get_back_button(),
            parse_mode=ParseMode.HTML
        )
    
    # Record TikTok
    elif query.data == "record_tiktok":
        await query.message.edit_text(
            "🎥 <b>RECORD TIKTOK LIVESTREAM</b> 🎥\n\n"
            "Masukkan username atau link livestream TikTok:\n\n"
            "<i>Contoh:</i>\n"
            "• @username\n"
            "• https://www.tiktok.com/@username/live",
            reply_markup=get_cancel_keyboard(),
            parse_mode=ParseMode.HTML
        )
        
        context.user_data['waiting_for_input'] = "tiktok"
    
    # Record Bigo
    elif query.data == "record_bigo":
        await query.message.edit_text(
            "🎥 <b>RECORD BIGO LIVESTREAM</b> 🎥\n\n"
            "Masukkan username atau link livestream Bigo:\n\n"
            "<i>Contoh:</i>\n"
            "• username\n"
            "• https://www.bigo.tv/username",
            reply_markup=get_cancel_keyboard(),
            parse_mode=ParseMode.HTML
        )
        
        context.user_data['waiting_for_input'] = "bigo"
    
    # List active recordings
    elif query.data == "list_active":
        # Get active recordings for user
        recordings = get_user_recordings(user_id, status="recording")
        
        if not recordings:
            await query.message.edit_text(
                "📋 <b>REKAMAN AKTIF</b> 📋\n\n"
                "Tidak ada rekaman yang sedang aktif saat ini.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        # Format message
        text = "📋 <b>REKAMAN AKTIF</b> 📋\n\n"
        for idx, rec in enumerate(recordings, 1):
            platform = rec["platform"].upper()
            target = rec["target"]
            start_time = rec["start_time"]
            
            # Calculate duration
            start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            duration = datetime.now() - start_dt
            hours, remainder = divmod(duration.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            
            text += f"{idx}. <b>{platform}</b>: {target}\n"
            text += f"   Mulai: {start_time}\n"
            text += f"   Durasi: {hours:02}:{minutes:02}:{seconds:02}\n\n"
        
        await query.message.edit_text(
            text,
            reply_markup=get_active_recordings_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )
    
    # List completed recordings
    elif query.data == "list_completed":
        # Get completed and stopped recordings
        recordings = get_user_recordings(user_id, status="completed")
        recordings += get_user_recordings(user_id, status="stopped")
        
        # Sort by start time (newest first)
        recordings.sort(key=lambda x: x["start_time"], reverse=True)
        
        if not recordings:
            await query.message.edit_text(
                "📂 <b>REKAMAN SELESAI</b> 📂\n\n"
                "Belum ada rekaman yang selesai.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        # Format message
        text = "📂 <b>REKAMAN SELESAI</b> 📂\n\n"
        for idx, rec in enumerate(recordings[:10], 1):  # Show only 10 most recent
            platform = rec["platform"].upper()
            target = rec["target"]
            status = rec["status"].upper()
            start_time = rec["start_time"]
            end_time = rec["end_time"] or ""
            file_size = rec["file_size"] or 0
            
            text += f"{idx}. <b>{platform}</b>: {target}\n"
            text += f"   Status: {status}\n"
            text += f"   Ukuran: {format_size(file_size)}\n"
            text += f"   Waktu: {start_time} s/d {end_time}\n\n"
        
        await query.message.edit_text(
            text,
            reply_markup=get_completed_recordings_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )
    
    # Cancel operation
    elif query.data == "cancel":
        if 'waiting_for_input' in context.user_data:
            del context.user_data['waiting_for_input']
        
        await query.message.edit_text(
            "Operasi dibatalkan.",
            reply_markup=get_main_menu_keyboard()
        )
    
    # View recording details
    elif query.data.startswith("view_"):
        recording_id = query.data.split("_")[1]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan atau telah dihapus.",
                reply_markup=get_back_button()
            )
            return
        
        platform = recording["platform"].upper()
        target = recording["target"]
        status = recording["status"].upper()
        start_time = recording["start_time"]
        end_time = recording["end_time"] or ""
        file_path = recording["file_path"]
        file_size = recording["file_size"] or 0
        
        # Calculate duration
        if end_time:
            start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
            duration = end_dt - start_dt
            hours, remainder = divmod(duration.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            duration_str = f"{hours:02}:{minutes:02}:{seconds:02}"
        else:
            duration_str = "Sedang berlangsung..."
        
        text = (
            f"📄 <b>DETAIL REKAMAN</b> 📄\n\n"
            f"<b>Platform:</b> {platform}\n"
            f"<b>Target:</b> {target}\n"
            f"<b>Status:</b> {status}\n"
            f"<b>Mulai:</b> {start_time}\n"
        )
        
        if end_time:
            text += f"<b>Selesai:</b> {end_time}\n"
        
        text += f"<b>Durasi:</b> {duration_str}\n"
        
        if os.path.exists(file_path):
            text += f"<b>File:</b> {os.path.basename(file_path)}\n"
            text += f"<b>Ukuran:</b> {format_size(file_size)}\n"
        else:
            text += "<b>File:</b> Tidak tersedia\n"
        
        await query.message.edit_text(
            text,
            reply_markup=get_recording_details_keyboard(recording_id),
            parse_mode=ParseMode.HTML
        )
    
    # Stop recording
    elif query.data.startswith("stop_"):
        recording_id = query.data.split("_")[1]
        recording = get_recording_by_id(recording_id)
        
        if not recording or recording["status"] != "recording":
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan atau sudah tidak aktif.",
                reply_markup=get_back_button()
            )
            return
        
        # Update message to show progress
        await query.message.edit_text(
            "⏳ Menghentikan rekaman...",
            parse_mode=ParseMode.HTML
        )
        
        # Stop recording
        success = await stop_recording(recording_id)
        
        if success:
            await query.message.edit_text(
                "✅ Rekaman berhasil dihentikan.",
                reply_markup=get_back_button()
            )
        else:
            await query.message.edit_text(
                "❌ Gagal menghentikan rekaman.",
                reply_markup=get_back_button()
            )
    
    # Download recording
    elif query.data.startswith("download_"):
        recording_id = query.data.split("_")[1]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan.",
                reply_markup=get_back_button()
            )
            return
        
        # Check if we have a compressed version
        compressed_path = recording.get("compressed_path")
        file_path = compressed_path if compressed_path and os.path.exists(compressed_path) else recording["file_path"]
        
        if not os.path.exists(file_path):
            await query.message.edit_text(
                "❌ File tidak ditemukan. Mungkin telah dihapus.",
                reply_markup=get_back_button()
            )
            return
        
        # Check file size
        file_size = get_file_size(file_path)
        
        if file_size == 0:
            await query.message.edit_text(
                "❌ File kosong atau rusak.",
                reply_markup=get_back_button()
            )
            return
        
        # Update message and show upload indicator
        await query.message.edit_text(
            f"⏳ <b>Mempersiapkan file untuk diunduh...</b>\n"
            f"Ukuran: {format_size(file_size)}\n\n"
            f"<i>File akan dikirim sebagai dokumen untuk menjaga kualitas HD.</i>",
            parse_mode=ParseMode.HTML
        )
        
        try:
            # Show upload status
            await context.bot.send_chat_action(
                chat_id=query.message.chat_id,
                action=ChatAction.UPLOAD_DOCUMENT
            )
            
            # Send as document to preserve quality
            with open(file_path, "rb") as file:
                # Get file basename and determine if it's compressed
                file_basename = os.path.basename(file_path)
                is_compressed = compressed_path and compressed_path == file_path
                
                # Create caption
                caption = (
                    f"📥 <b>File:</b> {file_basename}\n"
                    f"<b>Ukuran:</b> {format_size(file_size)}\n"
                    f"<b>Kualitas:</b> HD\n"
                    f"<b>Platform:</b> {recording['platform'].upper()}"
                )
                
                if is_compressed:
                    caption += f"\n<b>Status:</b> Dikompres (ukuran optimal tanpa mengurangi kualitas)"
                
                # Send document
                message = await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=file,
                    filename=file_basename,
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            
            # Delete the "preparing" message
            await query.message.delete()
            
        except Exception as e:
            logger.error(f"Error sending file: {str(e)}")
            
            # If file is too large, compress it further
            if "too large" in str(e).lower():
                await query.message.edit_text(
                    f"❌ <b>File terlalu besar untuk dikirim melalui Telegram (maks. 50MB).</b>\n"
                    f"Ukuran file: {format_size(file_size)}\n\n"
                    f"Sedang mengompres file lebih lanjut...",
                    parse_mode=ParseMode.HTML
                )
                
                # Try to compress the file further
                try:
                    # Create a more aggressively compressed version
                    emergency_compressed = f"{os.path.splitext(file_path)[0]}_telegram{os.path.splitext(file_path)[1]}"
                    
                    # Run FFmpeg with more aggressive compression
                    cmd = [
                        "ffmpeg",
                        "-i", file_path,
                        "-c:v", "libx264",
                        "-crf", "28",      # Higher CRF = more compression
                        "-preset", "fast",
                        "-c:a", "aac",
                        "-b:a", "96k",     # Lower audio bitrate
                        "-vf", "scale=-2:720", # Downscale to 720p
                        "-movflags", "+faststart",
                        emergency_compressed
                    ]
                    
                    # Run compression
                    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    
                    # Check new file size
                    if os.path.exists(emergency_compressed):
                        new_size = get_file_size(emergency_compressed)
                        
                        await query.message.edit_text(
                            f"⏳ <b>File telah dikompresi.</b>\n"
                            f"Ukuran baru: {format_size(new_size)}\n"
                            f"Mengirim file...",
                            parse_mode=ParseMode.HTML
                        )
                        
                        # Send the compressed file
                        with open(emergency_compressed, "rb") as file:
                            await context.bot.send_document(
                                chat_id=query.message.chat_id,
                                document=file,
                                filename=os.path.basename(emergency_compressed),
                                caption=f"📥 <b>File:</b> {os.path.basename(emergency_compressed)}\n"
                                        f"<b>Ukuran:</b> {format_size(new_size)}\n"
                                        f"<b>Status:</b> Dikompres untuk Telegram\n"
                                        f"<b>Kualitas:</b> Optimal untuk ukuran",
                                parse_mode=ParseMode.HTML
                            )
                        
                        # Delete temp file
                        try:
                            os.remove(emergency_compressed)
                        except:
                            pass
                            
                        # Delete the message
                        await query.message.delete()
                    else:
                        raise Exception("Kompresi darurat gagal")
                        
                except Exception as compress_error:
                    await query.message.edit_text(
                        f"❌ <b>Gagal mengompres dan mengirim file:</b> {str(compress_error)}\n\n"
                        f"File terlalu besar untuk Telegram. Silakan gunakan opsi lain untuk mentransfer file.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_back_button()
                    )
            else:
                await query.message.edit_text(
                    f"❌ <b>Gagal mengirim file:</b> {str(e)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_back_button()
                )
    
    # Delete recording
    elif query.data.startswith("delete_"):
        recording_id = query.data.split("_")[1]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan.",
                reply_markup=get_back_button()
            )
            return
        
        file_path = recording["file_path"]
        
        # Confirm deletion
        await query.message.edit_text(
            f"❓ Apakah Anda yakin ingin menghapus file ini?\n\n"
            f"File: {os.path.basename(file_path)}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Ya", callback_data=f"confirm_delete_{recording_id}"),
                    InlineKeyboardButton("❌ Tidak", callback_data=f"view_{recording_id}")
                ]
            ])
        )
    
    # Confirm delete recording
    elif query.data.startswith("confirm_delete_"):
        recording_id = query.data.split("_")[2]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan.",
                reply_markup=get_back_button()
            )
            return
        
        file_path = recording["file_path"]
        compressed_path = recording.get("compressed_path")
        
        # Delete files
        try:
            files_deleted = 0
            
            # Delete original file
            if os.path.exists(file_path):
                os.remove(file_path)
                files_deleted += 1
                
                # Also delete thumbnail if it exists
                thumbnail_path = f"{file_path}.jpg"
                if os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
            
            # Delete compressed file if it exists
            if compressed_path and os.path.exists(compressed_path) and compressed_path != file_path:
                os.remove(compressed_path)
                files_deleted += 1
                
                # Also delete thumbnail if it exists
                thumbnail_path = f"{compressed_path}.jpg"
                if os.path.exists(thumbnail_path):
                    os.remove(thumbnail_path)
            
            # Update status in database
            update_recording_status(recording_id, "deleted")
            
            if files_deleted > 0:
                await query.message.edit_text(
                    f"✅ {files_deleted} file berhasil dihapus.",
                    reply_markup=get_back_button()
                )
            else:
                await query.message.edit_text(
                    "⚠️ File sudah tidak ada.",
                    reply_markup=get_back_button()
                )
        except Exception as e:
            logger.error(f"Error deleting file: {str(e)}")
            
            await query.message.edit_text(
                f"❌ Gagal menghapus file: {str(e)}",
                reply_markup=get_back_button()
            )
    
    # Monitor accounts menu
    elif query.data == "monitor_accounts":
        await query.message.edit_text(
            "🔔 <b>MONITOR AKUN LIVESTREAM</b> 🔔\n\n"
            "Tambahkan akun untuk dipantau. Bot akan memberi tahu Anda saat akun mulai livestream.\n\n"
            "Anda juga dapat mengaktifkan <b>Auto-Record</b> agar bot otomatis merekam saat akun mulai livestream.",
            reply_markup=get_monitor_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
    
    # Add monitored account
    elif query.data.startswith("add_monitor_"):
        platform = query.data.split("_")[2]
        
        if platform == "tiktok":
            await query.message.edit_text(
                "🔔 <b>TAMBAH AKUN TIKTOK</b> 🔔\n\n"
                "Masukkan username TikTok yang ingin dipantau:\n\n"
                "<i>Contoh:</i> @username (tanpa @ juga bisa)",
                reply_markup=get_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            context.user_data['waiting_for_input'] = "monitor_tiktok"
        else:  # bigo
            await query.message.edit_text(
                "🔔 <b>TAMBAH AKUN BIGO</b> 🔔\n\n"
                "Masukkan username Bigo yang ingin dipantau:",
                reply_markup=get_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            context.user_data['waiting_for_input'] = "monitor_bigo"
    
    # List monitored accounts
    elif query.data == "list_monitored":
        accounts = get_monitored_accounts(user_id)
        
        if not accounts:
            await query.message.edit_text(
                "🔔 <b>AKUN TERPANTAU</b> 🔔\n\n"
                "Anda belum memantau akun manapun.\n\n"
                "Tambahkan akun untuk mendapatkan notifikasi saat mereka mulai livestream.",
                reply_markup=get_monitor_menu_keyboard(),
                parse_mode=ParseMode.HTML
            )
            return
        
        text = "🔔 <b>AKUN TERPANTAU</b> 🔔\n\n"
        
        for idx, acc in enumerate(accounts, 1):
            platform = acc["platform"].upper()
            username = acc["username"]
            is_live = bool(acc["is_live"])
            auto_record = bool(acc["auto_record"])
            notify_only = bool(acc["notify_only"])
            
            status = "🔴 LIVE" if is_live else "⚫ Offline"
            auto = "🔄 Auto-Record" if auto_record else ""
            notify = "🔔 Notifikasi Saja" if notify_only else ""
            
            text += f"{idx}. <b>{platform}:</b> {username}\n"
            text += f"   Status: {status}\n"
            
            if auto or notify:
                text += f"   Mode: {auto} {notify}\n"
            
            text += "\n"
        
        await query.message.edit_text(
            text,
            reply_markup=get_monitored_accounts_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )
    
    # View monitored account
    elif query.data.startswith("monitor_"):
        account_id = int(query.data.split("_")[1])
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM monitored_accounts WHERE id = ?", (account_id,))
        account = cursor.fetchone()
        
        if not account:
            await query.message.edit_text(
                "❌ Akun tidak ditemukan.",
                reply_markup=get_back_button()
            )
            conn.close()
            return
        
        platform = account["platform"].upper()
        username = account["username"]
        is_live = bool(account["is_live"])
        auto_record = bool(account["auto_record"])
        notify_only = bool(account["notify_only"])
        added_time = account["added_time"]
        last_check = account["last_check"]
        
        # Get livestream history
        cursor.execute(
            """SELECT * FROM livestream_history 
            WHERE account_id = ? 
            ORDER BY start_time DESC LIMIT 5""", 
            (account_id,)
        )
        
        history = cursor.fetchall()
        conn.close()
        
        text = f"🔔 <b>DETAIL AKUN {platform}</b> 🔔\n\n"
        text += f"<b>Username:</b> {username}\n"
        text += f"<b>Status:</b> {'🔴 LIVE' if is_live else '⚫ Offline'}\n"
        text += f"<b>Auto-Record:</b> {'✅ Aktif' if auto_record else '❌ Nonaktif'}\n"
        
        if notify_only:
            text += f"<b>Mode:</b> Notifikasi Saja (tidak merekam otomatis)\n"
        
        text += f"<b>Ditambahkan:</b> {added_time}\n"
        text += f"<b>Terakhir dicek:</b> {last_check}\n\n"
        
        if history:
            text += f"<b>Riwayat Livestream Terakhir:</b>\n"
            for idx, h in enumerate(history, 1):
                start_time = h["start_time"]
                end_time = h["end_time"] or "Masih berlangsung"
                was_recorded = bool(h["was_recorded"])
                
                text += f"{idx}. {start_time} s/d {end_time}\n"
                if was_recorded:
                    text += f"   ✅ Terekam\n"
                
                if idx < len(history):
                    text += "\n"
        
        await query.message.edit_text(
            text,
            reply_markup=get_account_details_keyboard(account_id),
            parse_mode=ParseMode.HTML
        )
    
    # Toggle auto-record
    elif query.data.startswith("toggle_auto_"):
        account_id = int(query.data.split("_")[2])
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get current value
        cursor.execute("SELECT auto_record FROM monitored_accounts WHERE id = ?", (account_id,))
        result = cursor.fetchone()
        
        if not result:
            await query.message.edit_text(
                "❌ Akun tidak ditemukan.",
                reply_markup=get_back_button()
            )
            conn.close()
            return
        
        auto_record = bool(result[0])
        
        # Toggle value
        cursor.execute(
            "UPDATE monitored_accounts SET auto_record = ? WHERE id = ?",
            (int(not auto_record), account_id)
        )
        
        conn.commit()
        conn.close()
        
        await query.message.edit_text(
            f"✅ Auto-Record berhasil {'dimatikan' if auto_record else 'diaktifkan'}!",
            reply_markup=get_account_details_keyboard(account_id)
        )
    
    # Toggle notify-only
    elif query.data.startswith("toggle_notify_"):
        account_id = int(query.data.split("_")[2])
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Get current value
        cursor.execute("SELECT notify_only FROM monitored_accounts WHERE id = ?", (account_id,))
        result = cursor.fetchone()
        
        if not result:
            await query.message.edit_text(
                "❌ Akun tidak ditemukan.",
                reply_markup=get_back_button()
            )
            conn.close()
            return
        
        notify_only = bool(result[0])
        
        # Toggle value
        cursor.execute(
            "UPDATE monitored_accounts SET notify_only = ? WHERE id = ?",
            (int(not notify_only), account_id)
        )
        
        conn.commit()
        conn.close()
        
        await query.message.edit_text(
            f"✅ Mode Notifikasi Saja berhasil {'dimatikan' if notify_only else 'diaktifkan'}!",
            reply_markup=get_account_details_keyboard(account_id)
        )
    
    # Delete monitored account
    elif query.data.startswith("delete_account_"):
        account_id = int(query.data.split("_")[2])
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT platform, username FROM monitored_accounts WHERE id = ?", (account_id,))
        account = cursor.fetchone()
        conn.close()
        
        if not account:
            await query.message.edit_text(
                "❌ Akun tidak ditemukan.",
                reply_markup=get_back_button()
            )
            return
        
        platform = account["platform"]
        username = account["username"]
        
        # Confirm deletion
        await query.message.edit_text(
            f"❓ Apakah Anda yakin ingin berhenti memantau akun ini?\n\n"
            f"Platform: {platform.upper()}\n"
            f"Username: {username}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Ya", callback_data=f"confirm_delete_account_{account_id}"),
                    InlineKeyboardButton("❌ Tidak", callback_data=f"monitor_{account_id}")
                ]
            ])
        )
    
    # Confirm delete account
    elif query.data.startswith("confirm_delete_account_"):
        account_id = int(query.data.split("_")[3])
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Delete account
        cursor.execute("DELETE FROM monitored_accounts WHERE id = ?", (account_id,))
        deleted = cursor.rowcount > 0
        
        # Also delete history
        cursor.execute("DELETE FROM livestream_history WHERE account_id = ?", (account_id,))
        
        conn.commit()
        conn.close()
        
        if deleted:
            await query.message.edit_text(
                "✅ Akun berhasil dihapus dari daftar pantauan.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Kembali ke Daftar", callback_data="list_monitored")]
                ])
            )
        else:
            await query.message.edit_text(
                "❌ Gagal menghapus akun.",
                reply_markup=get_back_button()
            )
    
    # Record from notification or monitor
    elif query.data.startswith("record_notif_") or query.data.startswith("record_monitor_"):
        parts = query.data.split("_")
        platform = parts[2]
        username = parts[3]
        
        # Update message
        await query.message.edit_text(
            f"⏳ Memulai rekaman {platform.upper()} untuk @{username}...",
            parse_mode=ParseMode.HTML
        )
        
        # Start recording
        if platform == "tiktok":
            success, recording_id, result = await start_tiktok_recording(username, user_id)
        else:  # bigo
            success, recording_id, result = await start_bigo_recording(username, user_id)
        
        if success:
            await query.message.edit_text(
                f"✅ <b>Rekaman {platform.upper()} Dimulai!</b>\n\n"
                f"Username: <code>{username}</code>\n"
                f"Status: <b>RECORDING</b>\n"
                f"ID: <code>{recording_id}</code>\n\n"
                f"Rekaman sedang berlangsung. Anda akan mendapatkan notifikasi ketika selesai.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏹️ Stop Recording", callback_data=f"stop_{recording_id}")],
                    [InlineKeyboardButton("📋 Rekaman Aktif", callback_data="list_active")],
                    [InlineKeyboardButton("« Menu Utama", callback_data="main_menu")]
                ])
            )
        else:
            await query.message.edit_text(
                f"❌ <b>Gagal Memulai Rekaman {platform.upper()}</b>\n\n"
                f"Username: <code>{username}</code>\n"
                f"Error: {result}",
                parse_mode=ParseMode.HTML,
                reply_markup=get_back_button()
            )
    
    # Settings menu
    elif query.data == "settings":
        await query.message.edit_text(
            "⚙️ <b>PENGATURAN</b> ⚙️\n\n"
            "Ubah pengaturan untuk bot recorder:\n\n"
            f"🗜️ <b>Kompresi Video:</b> {'✅ Aktif' if COMPRESSION_ENABLED else '❌ Nonaktif'}\n"
            f"📊 <b>Batas Kompresi:</b> {format_size(COMPRESSION_THRESHOLD)}\n"
            f"🎬 <b>Kualitas TikTok:</b> {TIKTOK_QUALITY}\n"
            f"🎬 <b>Kualitas Bigo:</b> {BIGO_QUALITY}\n"
            f"⏰ <b>Interval Cek Akun:</b> {CHECK_INTERVAL} detik\n",
            reply_markup=get_settings_keyboard(),
            parse_mode=ParseMode.HTML
        )
    
    # Toggle compression
    elif query.data == "toggle_compression":
        global COMPRESSION_ENABLED
        COMPRESSION_ENABLED = not COMPRESSION_ENABLED
        
        await query.message.edit_text(
            f"✅ Kompresi video {'diaktifkan' if COMPRESSION_ENABLED else 'dinonaktifkan'}.\n\n"
            f"Kompresi akan {'mengompres' if COMPRESSION_ENABLED else 'tidak mengompres'} video yang lebih besar dari {format_size(COMPRESSION_THRESHOLD)} secara otomatis untuk menghemat ruang penyimpanan tanpa mengurangi kualitas visual secara signifikan.",
            reply_markup=get_settings_keyboard()
        )

# ========== MESSAGE HANDLERS ==========

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for text messages"""
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # Check if we're waiting for input for recording or monitoring
    if 'waiting_for_input' in context.user_data:
        input_type = context.user_data['waiting_for_input']
        
        # Clear waiting state
        del context.user_data['waiting_for_input']
        
        # Handle recording request
        if input_type in ["tiktok", "bigo"]:
            # Send processing message
            processing_message = await update.message.reply_html(
                f"⏳ Memproses permintaan rekaman {input_type.upper()}...\n"
                f"Target: <code>{message_text}</code>"
            )
            
            if input_type == "tiktok":
                # Start TikTok recording
                success, recording_id, result = await start_tiktok_recording(message_text, user_id)
            elif input_type == "bigo":
                # Start Bigo recording
                success, recording_id, result = await start_bigo_recording(message_text, user_id)
            else:
                await processing_message.edit_text("❌ Platform tidak valid.")
                return
            
            if success:
                # Format success message
                success_text = (
                    f"✅ <b>Rekaman {input_type.upper()} Dimulai!</b>\n\n"
                    f"Target: <code>{message_text}</code>\n"
                    f"Status: <b>RECORDING</b>\n"
                    f"ID: <code>{recording_id}</code>\n\n"
                    f"Rekaman sedang berlangsung. Anda akan mendapatkan notifikasi ketika selesai."
                )
                
                # Add keyboard with options
                keyboard = [
                    [InlineKeyboardButton("⏹️ Stop Recording", callback_data=f"stop_{recording_id}")],
                    [InlineKeyboardButton("📋 Rekaman Aktif", callback_data="list_active")],
                    [InlineKeyboardButton("« Menu Utama", callback_data="main_menu")]
                ]
                
                await processing_message.edit_text(
                    success_text,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode=ParseMode.HTML
                )
            else:
                # Format error message
                error_text = (
                    f"❌ <b>Gagal Memulai Rekaman {input_type.upper()}</b>\n\n"
                    f"Target: <code>{message_text}</code>\n"
                    f"Error: {result}\n\n"
                    f"Pastikan:\n"
                    f"• Format username/URL sudah benar\n"
                    f"• Livestream sedang aktif\n"
                    f"• Coba lagi setelah beberapa saat"
                )
                
                await processing_message.edit_text(
                    error_text,
                    reply_markup=get_back_button(),
                    parse_mode=ParseMode.HTML
                )
        
        # Handle monitoring request
        elif input_type.startswith("monitor_"):
            platform = input_type.split("_")[1]
            username = message_text.strip()
            
            # Remove @ from TikTok username if present
            if platform == "tiktok" and username.startswith("@"):
                username = username[1:]
            
            # Validate username
            if platform == "tiktok" and not validate_tiktok_username(username):
                await update.message.reply_html(
                    "❌ <b>Format username TikTok tidak valid!</b>\n\n"
                    "Pastikan username hanya mengandung huruf, angka, titik, atau garis bawah.",
                    reply_markup=get_monitor_menu_keyboard()
                )
                return
            
            if platform == "bigo" and not validate_bigo_username(username):
                await update.message.reply_html(
                    "❌ <b>Format username Bigo tidak valid!</b>\n\n"
                    "Pastikan username hanya mengandung huruf, angka, atau garis bawah.",
                    reply_markup=get_monitor_menu_keyboard()
                )
                return
            
            # Process message
            processing_message = await update.message.reply_html(
                f"⏳ Menambahkan {platform.upper()} username <code>{username}</code> ke daftar pantauan..."
            )
            
            # Add to monitored accounts
            success, result = add_monitored_account(user_id, platform, username)
            
            if success:
                # Show success message
                await processing_message.edit_text(
                    f"✅ <b>Berhasil menambahkan ke daftar pantauan!</b>\n\n"
                    f"Platform: {platform.upper()}\n"
                    f"Username: <code>{username}</code>\n\n"
                    f"Bot akan memberi tahu Anda ketika <code>{username}</code> mulai livestream.\n\n"
                    f"<i>Tip: Anda dapat mengaktifkan auto-record di menu pengaturan akun.</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔔 Daftar Akun Terpantau", callback_data="list_monitored")],
                        [InlineKeyboardButton("« Menu Utama", callback_data="main_menu")]
                    ])
                )
                
                # Immediately check if account is livestreaming
                account_id = result if isinstance(result, int) else 0
                if account_id > 0:
                    threading.Thread(
                        target=lambda: asyncio.run(check_livestream_status(account_id, platform, username, user_id)),
                        daemon=True
                    ).start()
                
            else:
                # Show error message
                await processing_message.edit_text(
                    f"❌ <b>Gagal menambahkan ke daftar pantauan!</b>\n\n"
                    f"Error: {result}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_monitor_menu_keyboard()
                )
    
    # Handle normal messages
    else:
        # Check if input might be a livestream URL
        platform = get_platform_from_url(message_text)
        
        if platform:
            # It's likely a livestream URL, offer to record
            await update.message.reply_html(
                f"Sepertinya Anda mengirimkan link livestream {platform.upper()}.\n"
                f"Apakah Anda ingin merekam livestream ini?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Rekam {platform.upper()}", callback_data=f"record_{platform}")],
                    [InlineKeyboardButton("➕ Pantau Akun Ini", callback_data=f"add_monitor_{platform}")],
                    [InlineKeyboardButton("❌ Tidak", callback_data="main_menu")]
                ])
            )
            
            # Save the URL for later use
            context.user_data['last_url'] = message_text
        else:
            # Unknown command/text, show main menu
            await update.message.reply_text(
                "Silakan pilih menu atau gunakan perintah /help untuk bantuan.",
                reply_markup=get_main_menu_keyboard()
            )

# ========== MAIN FUNCTION ==========

def main() -> None:
    """Start the bot"""
    # Initialize database
    init_db()
    
    # Create application instance
    application = Application.builder().token(TOKEN).build()
    
    # Add command handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("record", cmd_record))
    application.add_handler(CommandHandler("active", cmd_active))
    application.add_handler(CommandHandler("monitor", cmd_monitor))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    
    # Add callback query handler
    application.add_handler(CallbackQueryHandler(button_click))
    
    # Add message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Start account monitoring in background
    async def start_monitoring():
        await run_account_monitor()
    
    # Schedule the account monitor to run in the background
    application.job_queue.run_once(
        lambda context: asyncio.create_task(start_monitoring()), 
        when=10  # Start monitoring after 10 seconds
    )
    
    # Log startup message
    logger.info("Bot started. Press Ctrl+C to stop.")
    
    # Start the Bot
    application.run_polling()

if __name__ == "__main__":
    main()
