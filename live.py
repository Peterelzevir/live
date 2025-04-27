import logging
import asyncio
import os
import sqlite3
import time
import json
import random
import httpx
import re
import uuid
import threading
import queue
import signal
import psutil
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Dict, List, Optional, Tuple, Union, Any

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.constants import ChatAction

# Konfigurasi logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='recorder_bot.log'  # Log ke file untuk debugging
)
logger = logging.getLogger(__name__)

# Konfigurasi bot
TOKEN = "7839177497:AAFS7PtzQFXmaMkucUUgbdT5SjmEiWAJVRQ"  # Ganti dengan token bot Anda
ADMIN_IDS = [5988451717]  # Ganti dengan ID admin Anda
DOWNLOAD_PATH = "downloads/"  # Folder untuk menyimpan hasil rekaman
TEMP_PATH = "temp/"  # Folder untuk menyimpan file sementara

# Konfigurasi kualitas dan kompresi
TIKTOK_QUALITY = "best"  # Kualitas terbaik untuk TikTok
BIGO_QUALITY = "best"  # Kualitas terbaik untuk Bigo
COMPRESSION_ENABLED = True  # Aktifkan kompresi otomatis untuk file besar
COMPRESSION_THRESHOLD = 45 * 1024 * 1024  # Batas ukuran file untuk kompresi (45MB)
COMPRESSION_CRF = 23  # Constant Rate Factor (18-28, semakin rendah semakin berkualitas)
CHECK_INTERVAL = 120  # Interval pemeriksaan livestream (dalam detik), dipersingkat untuk respon lebih cepat
RECORDING_TIMEOUT = 3600  # Timeout untuk recording (dalam detik) untuk mencegah proses stuck/zombie

# Pastikan folder ada
os.makedirs(DOWNLOAD_PATH, exist_ok=True)
os.makedirs(TEMP_PATH, exist_ok=True)

# Database untuk menyimpan data pengguna dan job
DB_PATH = "recorder_bot.db"

# Buat thread-safe queue untuk komunikasi antara thread dan event loop
notification_queue = queue.Queue()

# Flag untuk mengecek apakah notification processor running
notification_processor_running = False

# Status record
active_recordings: Dict[str, Dict] = {}
recording_processes: Dict[str, Any] = {}  # Menyimpan proses dan informasi tambahan
monitored_accounts: Dict[str, Dict] = {}  # Akun yang dipantau

# User-agent untuk requests
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
]

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
        original_link TEXT,
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
        current_recording_id TEXT,
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
    
    # Tambahkan kolom original_link jika belum ada
    try:
        cursor.execute("SELECT original_link FROM recordings LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE recordings ADD COLUMN original_link TEXT")
        
    # Tambahkan kolom current_recording_id jika belum ada
    try:
        cursor.execute("SELECT current_recording_id FROM monitored_accounts LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("ALTER TABLE monitored_accounts ADD COLUMN current_recording_id TEXT")
    
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
                  file_path: str = None, file_size: int = 0, quality: str = "HD",
                  original_link: str = None):
    """Save recording data to database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        """INSERT INTO recordings 
        (id, user_id, platform, target, status, start_time, end_time, file_path, file_size, quality, original_link) 
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (recording_id, user_id, platform, target, status, start_time, end_time, file_path, file_size, quality, original_link)
    )
    
    conn.commit()
    conn.close()

def update_recording_status(recording_id: str, status: str, end_time: str = None, 
                           file_path: str = None, file_size: int = None,
                           compressed_path: str = None, compressed_size: int = None,
                           original_link: str = None):
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
        
    if original_link:
        update_fields.append("original_link = ?")
        update_values.append(original_link)
    
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

def update_account_recording_id(account_id: int, recording_id: str = None):
    """Update current recording ID for a monitored account"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute(
        "UPDATE monitored_accounts SET current_recording_id = ? WHERE id = ?",
        (recording_id, account_id)
    )
    
    conn.commit()
    conn.close()

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

def get_account_by_id(account_id: int):
    """Get monitored account by ID"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM monitored_accounts WHERE id = ?", (account_id,))
    result = cursor.fetchone()
    
    conn.close()
    return dict(result) if result else None

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

def get_account_by_username(user_id: int, platform: str, username: str):
    """Get monitored account by username"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute(
        "SELECT * FROM monitored_accounts WHERE user_id = ? AND platform = ? AND username = ?",
        (user_id, platform, username)
    )
    result = cursor.fetchone()
    
    conn.close()
    return dict(result) if result else None
    
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

def get_user_recordings(user_id: int, status: str = None, limit: int = 50) -> List[Dict]:
    """Get recordings for a specific user"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    query = "SELECT * FROM recordings WHERE user_id = ?"
    params = [user_id]
    
    if status:
        if status == "active":
            # Active includes recording and processing
            query += " AND (status = 'recording' OR status = 'processing')"
        elif status == "completed":
            # Completed includes completed and stopped
            query += " AND (status = 'completed' OR status = 'stopped')"
        else:
            query += " AND status = ?"
            params.append(status)
    
    query += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)
    
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

def get_random_user_agent():
    """Get random user agent for requests"""
    return random.choice(USER_AGENTS)

def is_process_running(process):
    """Check if a process is still running"""
    try:
        if process is None:
            return False
        return process.poll() is None
    except:
        return False

def is_valid_tiktok_url(url: str) -> bool:
    """Check if URL is a valid TikTok URL"""
    tiktok_patterns = [
        r'tiktok\.com',
        r'vt\.tiktok\.com',
        r'vm\.tiktok\.com',
        r'm\.tiktok\.com',
        r't\.tiktok\.com'
    ]
    
    for pattern in tiktok_patterns:
        if re.search(pattern, url):
            return True
    
    return False

def is_valid_bigo_url(url: str) -> bool:
    """Check if URL is a valid Bigo URL"""
    bigo_patterns = [
        r'bigo\.tv',
        r'bigo\.live'
    ]
    
    for pattern in bigo_patterns:
        if re.search(pattern, url):
            return True
    
    return False

def get_platform_from_url(url: str) -> Optional[str]:
    """Detect platform from URL"""
    if not url:
        return None
        
    if is_valid_tiktok_url(url):
        return "tiktok"
    elif is_valid_bigo_url(url):
        return "bigo"
    else:
        return None

async def resolve_shortened_url(url: str) -> str:
    """Resolve shortened URLs to their original form"""
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, timeout=30)
            return str(response.url)
    except Exception as e:
        logger.error(f"Error resolving shortened URL: {str(e)}")
        return url  # Return original if we can't resolve

def extract_tiktok_username_from_url(url: str) -> Optional[str]:
    """Extract TikTok username from URL"""
    # Try different patterns
    patterns = [
        r'@([a-zA-Z0-9_.]+)',  # @username
        r'tiktok\.com/@([a-zA-Z0-9_.]+)',  # tiktok.com/@username
        r'tiktok\.com/([a-zA-Z0-9_.]+)',  # tiktok.com/username
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None

def extract_bigo_username_from_url(url: str) -> Optional[str]:
    """Extract Bigo username from URL"""
    patterns = [
        r'bigo\.tv/([a-zA-Z0-9_]+)',  # bigo.tv/username
        r'bigo\.live/([a-zA-Z0-9_]+)',  # bigo.live/username
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
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
        import subprocess
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
    except Exception as e:
        logger.error(f"Failed to generate thumbnail for {file_path}: {str(e)}")
        return None

def kill_process_tree(pid):
    """Kill process and all its children"""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        
        for child in children:
            try:
                child.terminate()
            except:
                try:
                    child.kill()
                except:
                    pass
        
        gone, still_alive = psutil.wait_procs(children, timeout=5)
        
        for p in still_alive:
            try:
                p.kill()
            except:
                pass
        
        try:
            parent.terminate()
            parent.wait(5)
        except:
            try:
                parent.kill()
            except:
                pass
                
    except psutil.NoSuchProcess:
        pass
    except Exception as e:
        logger.error(f"Error killing process tree: {str(e)}")

# ========== LIVESTREAM DETECTION FUNCTIONS ==========

async def check_tiktok_live(username_or_url: str) -> Tuple[bool, str, Optional[str]]:
    """Check if TikTok user is currently live
    
    Returns:
        Tuple[bool, str, Optional[str]]: (is_live, username, live_url or None)
    """
    username = username_or_url
    
    # If URL, extract username
    if is_valid_tiktok_url(username_or_url):
        resolved_url = await resolve_shortened_url(username_or_url)
        username_from_url = extract_tiktok_username_from_url(resolved_url)
        if username_from_url:
            username = username_from_url
    
    # Remove @ if present
    username = username.strip('@')
    
    # First method: check live URL directly
    live_url = f"https://www.tiktok.com/@{username}/live"
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "sec-ch-ua": '"Not.A/Brand";v="8", "Chromium";v="114", "Google Chrome";v="114"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    
    try:
        response = requests.get(live_url, headers=headers, timeout=15)
        
        # Look for indicators that user is live
        live_indicators = [
            "LIVE", 
            "isLive", 
            "liveBitrate",
            "liveRoom",
            "TikTok LIVE",
            "LiveButton"
        ]
        
        html_content = response.text.lower()
        username_lower = username.lower()
        
        # Check if the page contains the user's name and live indicators
        if username_lower in html_content and any(indicator.lower() in html_content for indicator in live_indicators):
            logger.info(f"TikTok user {username} is live based on URL check")
            return True, username, live_url
            
        # Second method: check through API
        alternate_url = f"https://www.tiktok.com/api/live/detail/?aid=1988&roomID={username}"
        
        try:
            api_response = requests.get(alternate_url, headers=headers, timeout=15)
            data = api_response.json()
            
            if 'status_code' in data and data['status_code'] == 0 and 'LiveRoomInfo' in data.get('data', {}):
                live_info = data['data']['LiveRoomInfo']
                if live_info.get('status', 0) == 2:  # 2 = live
                    logger.info(f"TikTok user {username} is live based on API check")
                    return True, username, live_url
        except Exception as api_error:
            logger.error(f"Error checking TikTok API for {username}: {str(api_error)}")
        
        return False, username, None
        
    except Exception as e:
        logger.error(f"Error checking TikTok livestream status: {str(e)}")
        return False, username, None

async def check_bigo_live(username_or_url: str) -> Tuple[bool, str, Optional[str]]:
    """Check if Bigo user is currently live
    
    Returns:
        Tuple[bool, str, Optional[str]]: (is_live, username, live_url or None)
    """
    username = username_or_url
    
    # If URL, extract username
    if is_valid_bigo_url(username_or_url):
        username_from_url = extract_bigo_username_from_url(username_or_url)
        if username_from_url:
            username = username_from_url
    
    # Build live URL
    live_url = f"https://www.bigo.tv/{username}"
    
    headers = {
        "User-Agent": get_random_user_agent(),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Referer": "https://www.bigo.tv/",
    }
    
    try:
        response = requests.get(live_url, headers=headers, timeout=15)
        
        # Look for indicators that user is live
        live_indicators = [
            "isLive", 
            "liveRoom",
            "live_room",
            "onlive",
            "goInLive",
            "inLiveRoom"
        ]
        
        html_content = response.text.lower()
        username_lower = username.lower()
        
        # Check if the page contains the user's name and live indicators
        if username_lower in html_content and any(indicator.lower() in html_content for indicator in live_indicators):
            logger.info(f"Bigo user {username} is live")
            
            # Try to extract the stream URL from Bigo API
            api_url = f"https://www.bigo.tv/studio/getInLive?roomId={username}"
            try:
                api_response = requests.get(api_url, headers=headers, timeout=15)
                data = api_response.json()
                if data.get('code') == 1 and 'roomData' in data:
                    actual_live_url = data['roomData'].get('liveUrl', live_url)
                    if actual_live_url:
                        live_url = actual_live_url
            except Exception as api_error:
                logger.error(f"Error checking Bigo API for {username}: {str(api_error)}")
            
            return True, username, live_url
            
        return False, username, None
        
    except Exception as e:
        logger.error(f"Error checking Bigo livestream status: {str(e)}")
        return False, username, None

# ========== RECORDING FUNCTIONS ==========

async def start_tiktok_recording(username_or_url: str, user_id: int, auto_record: bool = False, 
                                account_id: int = None) -> Tuple[bool, str, str]:
    """Start recording TikTok livestream with improved pre-checks and error handling"""
    recording_id = str(uuid.uuid4())
    current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    try:
        # Pre-check if stream is live
        is_live, username, live_url = await check_tiktok_live(username_or_url)
        
        if not is_live:
            return False, recording_id, "Stream tidak ditemukan atau tidak sedang live."
        
        # Store original link
        original_link = username_or_url if is_valid_tiktok_url(username_or_url) else live_url
        
        if not validate_tiktok_username(username):
            return False, recording_id, "Format username tidak valid"
        
        # Prepare output file
        output_file = os.path.join(DOWNLOAD_PATH, f"tiktok_{username}_{current_time}.mp4")
        
        # Prepare log file
        log_file = os.path.join(TEMP_PATH, f"log_tiktok_{username}_{current_time}.txt")
        
        # Berdasarkan setting kualitas
        quality_option = "-f best"
        if TIKTOK_QUALITY == "720p":
            quality_option = "-f 'best[height<=720]'"
        elif TIKTOK_QUALITY == "480p":
            quality_option = "-f 'best[height<=480]'"
        
        # Prepare command to record using yt-dlp with improved parameters
        import subprocess
        cmd = [
            "yt-dlp",
            "--no-part",
            "--no-mtime",
            "--no-playlist",
            "-f", "best",  # Always use best quality
            "--hls-use-mpegts",  # Use MPEG-TS for better streaming
            "--live-from-start",  # Record from start of livestream
            "--wait-for-video", "30",  # Wait up to 30 seconds for video
            "--retries", "10",  # Retry 10 times if download fails
            "--fragment-retries", "10",  # Retry 10 times if fragment download fails
            "--retry-sleep", "5",  # Sleep 5 seconds between retries 
            "--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com",  # Improved TikTok API endpoint
            "-o", output_file,
            live_url
        ]
        
        # Log command yang akan dijalankan
        logger.info(f"Starting TikTok recording command: {' '.join(cmd)}")
        
        # Open log file
        with open(log_file, 'w') as log:
            # Start recording process with log redirection
            process = subprocess.Popen(
                cmd, 
                stdout=log, 
                stderr=log,
                text=True,
                bufsize=1  # Line buffered untuk logging realtime
            )
        
        # Check immediate failure
        time.sleep(5)  # Wait for process to start properly
        return_code = process.poll()
        if return_code is not None and return_code != 0:
            # Read error from log
            with open(log_file, 'r') as log:
                error_output = log.read()
            
            return False, recording_id, f"Error: Stream tidak ditemukan atau tidak sedang live. {error_output}"
        
        # Save to database
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_recording(
            recording_id=recording_id,
            user_id=user_id,
            platform="tiktok",
            target=f"@{username}",
            status="recording",
            start_time=start_time,
            file_path=output_file,
            quality="HD",
            original_link=original_link
        )
        
        # Add to active recordings
        active_recordings[recording_id] = {
            "user_id": user_id,
            "platform": "tiktok",
            "target": f"@{username}",
            "start_time": start_time,
            "output_file": output_file,
            "username": username,
            "auto_record": auto_record,
            "account_id": account_id,
            "log_file": log_file,
            "live_url": live_url,
            "original_link": original_link
        }
        
        # Buat pid file untuk membantu recovery
        pid_file = os.path.join(TEMP_PATH, f"pid_{recording_id}.txt")
        with open(pid_file, 'w') as pf:
            pf.write(f"{process.pid}\n{output_file}\n{start_time}")
        
        # Store process reference with additional monitoring info
        recording_processes[recording_id] = {
            "process": process,
            "start_time": datetime.now(),
            "pid": process.pid,
            "platform": "tiktok",
            "status_check_time": datetime.now(),
            "is_alive": True,
            "pid_file": pid_file,
            "quality": TIKTOK_QUALITY
        }
        
        # If recording is for a monitored account, update the account's current recording ID
        if account_id:
            update_account_recording_id(account_id, recording_id)
        
        # Start monitoring thread
        monitoring_thread = threading.Thread(
            target=monitor_recording_process,
            args=(recording_id, process),
            daemon=True
        )
        monitoring_thread.name = f"monitor-{recording_id}"
        monitoring_thread.start()
        
        logger.info(f"Started TikTok recording for {username}, ID: {recording_id}, PID: {process.pid}")
        return True, recording_id, output_file
    
    except Exception as e:
        logger.error(f"Error starting TikTok recording: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False, recording_id, f"Error: {str(e)}"

async def start_bigo_recording(username_or_url: str, user_id: int, auto_record: bool = False,
                              account_id: int = None) -> Tuple[bool, str, str]:
    """Start recording Bigo livestream with improved pre-checks and error handling"""
    recording_id = str(uuid.uuid4())
    current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    
    try:
        # Pre-check if stream is live
        is_live, username, live_url = await check_bigo_live(username_or_url)
        
        if not is_live:
            return False, recording_id, "Stream tidak ditemukan atau tidak sedang live."
        
        # Store original link
        original_link = username_or_url if is_valid_bigo_url(username_or_url) else live_url
        
        if not validate_bigo_username(username):
            return False, recording_id, "Format username tidak valid"
        
        # Prepare output file
        output_file = os.path.join(DOWNLOAD_PATH, f"bigo_{username}_{current_time}.mp4")
        
        # Prepare log file
        log_file = os.path.join(TEMP_PATH, f"log_bigo_{username}_{current_time}.txt")
        
        # Berdasarkan setting kualitas
        quality_opt = "best"
        if BIGO_QUALITY == "720p":
            quality_opt = "720p,720p_alt,720p60,best"
        elif BIGO_QUALITY == "480p":
            quality_opt = "480p,480p_alt,worst"
        
        # Prepare command to record using streamlink with improved parameters
        import subprocess
        cmd = [
            "streamlink",
            "--force",
            "--hls-live-restart",
            "--hls-segment-threads", "3",
            "--hls-segment-timeout", "10",
            "--hls-timeout", "180",
            "--retry-streams", "10",
            "--retry-max", "20",
            "--retry-open", "10",
            "--stream-timeout", "120",
            "--ringbuffer-size", "64M",
            "--loglevel", "debug",  # More detailed logging
            "-o", output_file,
            live_url, quality_opt
        ]
        
        # Log command yang akan dijalankan
        logger.info(f"Starting Bigo recording command: {' '.join(cmd)}")
        
        # Open log file
        with open(log_file, 'w') as log:
            # Start recording process with log redirection
            process = subprocess.Popen(
                cmd, 
                stdout=log, 
                stderr=log,
                text=True,
                bufsize=1  # Line buffered untuk logging realtime
            )
        
        # Check immediate failure
        time.sleep(5)  # Wait for process to start properly
        return_code = process.poll()
        if return_code is not None and return_code != 0:
            # Read error from log
            with open(log_file, 'r') as log:
                error_output = log.read()
            
            return False, recording_id, f"Error: Stream tidak ditemukan atau tidak sedang live. {error_output}"
        
        # Save to database
        start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_recording(
            recording_id=recording_id,
            user_id=user_id,
            platform="bigo",
            target=username,
            status="recording",
            start_time=start_time,
            file_path=output_file,
            quality="HD",
            original_link=original_link
        )
        
        # Add to active recordings
        active_recordings[recording_id] = {
            "user_id": user_id,
            "platform": "bigo",
            "target": username,
            "start_time": start_time,
            "output_file": output_file,
            "username": username,
            "auto_record": auto_record,
            "account_id": account_id,
            "log_file": log_file,
            "live_url": live_url,
            "original_link": original_link
        }
        
        # Buat pid file untuk membantu recovery
        pid_file = os.path.join(TEMP_PATH, f"pid_{recording_id}.txt")
        with open(pid_file, 'w') as pf:
            pf.write(f"{process.pid}\n{output_file}\n{start_time}")
        
        # Store process reference with additional monitoring info
        recording_processes[recording_id] = {
            "process": process,
            "start_time": datetime.now(),
            "pid": process.pid,
            "platform": "bigo",
            "status_check_time": datetime.now(),
            "is_alive": True,
            "pid_file": pid_file,
            "quality": BIGO_QUALITY
        }
        
        # If recording is for a monitored account, update the account's current recording ID
        if account_id:
            update_account_recording_id(account_id, recording_id)
        
        # Start monitoring thread dengan nama teridentifikasi
        monitoring_thread = threading.Thread(
            target=monitor_recording_process,
            args=(recording_id, process),
            daemon=True
        )
        monitoring_thread.name = f"monitor-{recording_id}"
        monitoring_thread.start()
        
        logger.info(f"Started Bigo recording for {username}, ID: {recording_id}, PID: {process.pid}")
        return True, recording_id, output_file
    
    except Exception as e:
        logger.error(f"Error starting Bigo recording: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return False, recording_id, f"Error: {str(e)}"

def monitor_recording_process(recording_id: str, process):
    """Monitor recording process dan update status ketika selesai, dengan improved file handling"""
    try:
        if recording_id not in recording_processes:
            logger.error(f"Recording process {recording_id} not found in recording_processes")
            return
            
        process_info = recording_processes[recording_id]
        start_time = process_info["start_time"]
        platform = process_info.get("platform", "unknown")
        quality = process_info.get("quality", "HD")
        
        # Dapatkan informasi file dan akun
        if recording_id not in active_recordings:
            logger.error(f"Recording {recording_id} not found in active_recordings")
            return
            
        recording_info = active_recordings[recording_id]
        output_file = recording_info.get("output_file", "")
        account_id = recording_info.get("account_id")
        log_file = recording_info.get("log_file", "")
        
        # Catat initial file size jika ada
        initial_file_exists = os.path.exists(output_file)
        initial_file_size = get_file_size(output_file) if initial_file_exists else 0
        last_size_check_time = datetime.now()
        last_checked_size = initial_file_size
        size_stall_count = 0
        
        logger.info(f"Started monitoring {platform} recording {recording_id}, output file: {output_file}")
        
        while True:
            # Check if process is still running
            is_running = is_process_running(process)
            
            # Update status
            process_info["is_alive"] = is_running
            process_info["status_check_time"] = datetime.now()
            
            # Cek ukuran file secara berkala
            current_time = datetime.now()
            time_since_size_check = (current_time - last_size_check_time).total_seconds()
            
            if time_since_size_check > 30:  # Cek ukuran file setiap 30 detik
                current_size = get_file_size(output_file) if os.path.exists(output_file) else 0
                
                # Log progress
                if current_size > 0:
                    logger.info(f"Recording {recording_id} progress: {format_size(current_size)}")
                
                # Deteksi jika ukuran file tidak bertambah
                if current_size == last_checked_size and current_size > 0:
                    size_stall_count += 1
                    logger.warning(f"Recording {recording_id} size hasn't changed in {size_stall_count * 30} seconds")
                    
                    # Cek log file untuk mendeteksi end of stream
                    if log_file and os.path.exists(log_file):
                        try:
                            with open(log_file, 'r') as log:
                                log_tail = "".join(log.readlines()[-20:])  # Ambil 20 baris terakhir
                                if "end of file" in log_tail.lower() or "stream ended" in log_tail.lower() or "finished" in log_tail.lower():
                                    logger.info(f"Detected end of stream in log for {recording_id}")
                                    break
                        except Exception as log_error:
                            logger.error(f"Error reading log file: {str(log_error)}")
                else:
                    size_stall_count = 0
                
                # Jika ukuran stall terlalu lama (5 menit) tapi file sudah cukup besar, anggap selesai
                if size_stall_count >= 10 and current_size > 1024 * 1024 * 5:  # 5MB minimum
                    logger.warning(f"Recording {recording_id} size stalled for 5 minutes. Ending recording.")
                    break
                
                last_checked_size = current_size
                last_size_check_time = current_time
            
            # Perbarui status untuk UI
            try:
                update_recording_status(recording_id, "recording", file_size=last_checked_size)
            except Exception as status_error:
                logger.error(f"Error updating status: {str(status_error)}")
            
            # Check if process has been running too long (timeout)
            elapsed_time = (datetime.now() - start_time).total_seconds()
            
            # Check if recording has been manually stopped
            if recording_id not in active_recordings:
                logger.info(f"Recording {recording_id} has been manually stopped")
                break
                
            # Check for marker file indicating stop command
            marker_file = f"{TEMP_PATH}/stopping_{recording_id}.marker"
            if os.path.exists(marker_file):
                logger.info(f"Found stop marker file for {recording_id}")
                break
                
            # If process ended or timed out
            if not is_running or elapsed_time > RECORDING_TIMEOUT:
                if elapsed_time > RECORDING_TIMEOUT:
                    logger.warning(f"Recording {recording_id} timed out after {elapsed_time} seconds")
                    # Kill the process if it's still running
                    try:
                        if is_running:
                            kill_process_tree(process.pid)
                    except Exception as kill_error:
                        logger.error(f"Error killing process {recording_id}: {str(kill_error)}")
                else:
                    logger.info(f"Process for recording {recording_id} ended naturally")
                
                # Process has ended, break the loop
                break
            
            # Sleep before next check
            time.sleep(10)
        
        # Finalize recording
        finalize_recording(recording_id, process)
        
    except Exception as e:
        logger.error(f"Error in monitor_recording_process: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        
        # Attempt to finalize recording even after error
        try:
            finalize_recording(recording_id, process)
        except Exception as finalize_error:
            logger.error(f"Error in finalize_recording after monitor error: {str(finalize_error)}")
            
def finalize_recording(recording_id: str, process):
    """Finalize recording after process ends"""
    try:
        # Ambil waktu saat ini
        end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Cek apakah recording_id masih ada di active_recordings
        if recording_id in active_recordings:
            recording_info = active_recordings[recording_id]
            output_file = recording_info.get("output_file", "")
            account_id = recording_info.get("account_id")
            log_file = recording_info.get("log_file")
            username = recording_info.get("username", "unknown")
            platform = recording_info.get("platform", "unknown")
            
            logger.info(f"Finalizing {platform} recording for {username}, ID: {recording_id}")
            
            # Wait a moment to ensure file is fully written
            time.sleep(5)
            
            # Cek file status
            file_exists = os.path.exists(output_file)
            file_size = get_file_size(output_file) if file_exists else 0
            
            logger.info(f"Recording file {output_file} exists: {file_exists}, size: {format_size(file_size)}")
            
            # Check if process ended normally or was forced to stop
            status = "unknown"
            
            if file_exists and file_size > 5 * 1024 * 1024:  # >5MB
                status = "completed"
                logger.info(f"Recording {recording_id} completed with good file size")
            elif file_exists and file_size > 0:
                # File exists but small, check log for clues
                status = "partial"
                
                # Check log file for specific errors
                if log_file and os.path.exists(log_file):
                    try:
                        with open(log_file, 'r') as log:
                            log_content = log.read()
                            if "ERROR: Unable to download webpage" in log_content:
                                status = "ended"
                                logger.info(f"Recording {recording_id} ended due to webpage access error")
                            elif "Stream ended" in log_content or "Stream offline" in log_content or "end of file" in log_content:
                                status = "ended"
                                logger.info(f"Recording {recording_id} ended normally because stream ended")
                    except Exception as log_error:
                        logger.error(f"Error reading log file: {str(log_error)}")
            else:
                # No file or zero size
                status = "failed"
                logger.warning(f"Recording {recording_id} failed - no output file or zero size")
            
            # Check if file was stop requested
            marker_file = f"{TEMP_PATH}/stopping_{recording_id}.marker"
            if os.path.exists(marker_file):
                logger.info(f"Recording {recording_id} was manually stopped")
                if file_exists and file_size > 0:
                    status = "stopped"
            
            logger.info(f"Final status for recording {recording_id}: {status}")
            
            # Update database
            update_recording_status(
                recording_id=recording_id,
                status=status,
                end_time=end_time,
                file_path=output_file,
                file_size=file_size
            )
            
            # If this was a monitored account recording, update the account
            if account_id:
                update_account_recording_id(account_id, None)
                logger.info(f"Reset current_recording_id for account {account_id}")
            
            # Check if need to compress file
            compressed_path = None
            compressed_size = None
            
            if status in ["completed", "stopped", "partial"] and file_exists and file_size > COMPRESSION_THRESHOLD:
                # If file size is large, compress
                if COMPRESSION_ENABLED:
                    logger.info(f"Starting compression for {output_file} ({format_size(file_size)})")
                    compressed_path = compress_video(output_file)
                    if compressed_path:
                        compressed_size = get_file_size(compressed_path)
                        logger.info(f"Compressed {output_file} from {format_size(file_size)} to {format_size(compressed_size)}")
                        
                        # Update database with compression info
                        update_recording_status(
                            recording_id=recording_id,
                            status=status,
                            compressed_path=compressed_path,
                            compressed_size=compressed_size
                        )
            
            # Save info for notification
            notification_info = {
                "recording_id": recording_id,
                "user_id": recording_info.get("user_id"),
                "status": status,
                "platform": platform,
                "target": recording_info.get("target", ""),
                "file_path": output_file,
                "file_size": file_size,
                "compressed_path": compressed_path,
                "compressed_size": compressed_size,
                "username": username
            }
            
            # Add to notification queue
            notification_queue.put(notification_info)
            logger.info(f"Added notification to queue for recording {recording_id}")
            
            # Clean up
            if recording_id in active_recordings:
                del active_recordings[recording_id]
            if recording_id in recording_processes:
                del recording_processes[recording_id]
                
            # Remove marker file if exists
            try:
                if os.path.exists(marker_file):
                    os.remove(marker_file)
            except:
                pass
                
            # Remove pid file if exists
            pid_file = os.path.join(TEMP_PATH, f"pid_{recording_id}.txt")
            try:
                if os.path.exists(pid_file):
                    os.remove(pid_file)
            except:
                pass
                
            logger.info(f"Recording {recording_id} finalized successfully with status: {status}")
    except Exception as e:
        logger.error(f"Error in finalize_recording: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())

def compress_video(input_file: str) -> Optional[str]:
    """Compress video using FFmpeg with high quality"""
    try:
        # Get file info
        file_name, file_ext = os.path.splitext(input_file)
        output_file = f"{file_name}_compressed{file_ext}"
        
        # FFmpeg command for high quality compression
        import subprocess
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
    """Check if an account is currently livestreaming with improved detection"""
    try:
        is_live = False
        live_url = None
        
        # Get account info
        account = get_account_by_id(account_id)
        if not account:
            logger.error(f"Account with ID {account_id} not found")
            return
            
        # Check if already recording
        current_recording_id = account.get("current_recording_id")
        already_recording = False
        
        if current_recording_id:
            # Check if recording is active
            recording = get_recording_by_id(current_recording_id)
            if recording and recording["status"] == "recording":
                already_recording = True
                logger.info(f"Account {username} on {platform} is already being recorded, ID: {current_recording_id}")
                
                # Jika recording ID ada tapi tidak ada di active_recordings, reset state
                if current_recording_id not in active_recordings:
                    already_recording = False
                    update_account_recording_id(account_id, None)
                    logger.warning(f"Reset recording state for {username} on {platform} (recording not found in active_recordings)")
        
        # Use improved livestream detection
        if platform == "tiktok":
            is_live, username, live_url = await check_tiktok_live(username)
        elif platform == "bigo":
            is_live, username, live_url = await check_bigo_live(username)
        
        # Get current account status
        was_live = bool(account["is_live"])
        auto_record = bool(account["auto_record"])
        notify_only = bool(account["notify_only"])
        
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
                link = live_url or f"https://www.tiktok.com/@{username}/live"
            else:
                message = f"🔴 <b>{username} sedang LIVE di Bigo!</b>"
                link = live_url or f"https://www.bigo.tv/{username}"
                
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
            
            # Auto-record if enabled and not notify-only and not already recording
            if auto_record and not notify_only and not already_recording:
                # Kirim notifikasi bahwa akan mulai auto-record
                await bot.send_message(
                    chat_id=user_id,
                    text=f"⏳ <b>Memulai Auto-Record untuk {platform.upper()}: {username}...</b>",
                    parse_mode=ParseMode.HTML
                )
                
                if platform == "tiktok":
                    success, recording_id, result = await start_tiktok_recording(
                        username, user_id, auto_record=True, account_id=account_id
                    )
                else:
                    success, recording_id, result = await start_bigo_recording(
                        username, user_id, auto_record=True, account_id=account_id
                    )
                
                if success:
                    # Update livestream history
                    update_livestream_history(history_id, None, 0, was_recorded=True, recording_id=recording_id)
                    
                    # Informasi recording untuk keyboard
                    file_path = ""
                    status = "recording"
                    
                    # Get recording info untuk ditampilkan
                    recording = get_recording_by_id(recording_id)
                    if recording:
                        file_path = recording.get("file_path", "")
                        
                    # Send success notification with more info
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"✅ <b>Auto-Record dimulai untuk {platform.upper()}: {username}</b>\n\n"
                             f"ID: <code>{recording_id}</code>\n"
                             f"Status: <b>RECORDING</b>\n"
                             f"Output: <code>{os.path.basename(file_path)}</code>\n\n"
                             f"<i>Recording akan berjalan sampai livestream berakhir atau dihentikan manual.</i>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⏹️ Stop Recording", callback_data=f"stop_{recording_id}")],
                            [InlineKeyboardButton("📋 Lihat Rekaman Aktif", callback_data="list_active")]
                        ])
                    )
                else:
                    # Send failure notification with more detail
                    await bot.send_message(
                        chat_id=user_id,
                        text=f"❌ <b>Gagal memulai Auto-Record untuk {platform.upper()}: {username}</b>\n\n"
                             f"Error: {result}\n\n"
                             f"<i>Bot akan mencoba lagi pada pemeriksaan berikutnya.</i>",
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
                
                # If there's an active recording for this account, stop it
                if current_recording_id:
                    recording = get_recording_by_id(current_recording_id)
                    if recording and recording["status"] == "recording":
                        # Kirim notifikasi bahwa auto-record akan dihentikan
                        await bot.send_message(
                            chat_id=user_id,
                            text=f"⏹️ <b>Menghentikan Auto-Record untuk {platform.upper()}: {username}</b>\n\n"
                                 f"<i>Livestream telah berakhir.</i>",
                            parse_mode=ParseMode.HTML
                        )
                        
                        # Stop the recording
                        success = await stop_recording(current_recording_id)
                        
                        if success:
                            logger.info(f"Auto-stopped recording {current_recording_id} for {platform}/{username} as stream ended")
                        else:
                            logger.error(f"Failed to auto-stop recording {current_recording_id} for {platform}/{username}")
        
        # If still live and auto-record is enabled but we're not recording (maybe bot restarted)
        elif is_live and was_live and auto_record and not notify_only and not already_recording:
            # Kirim notifikasi bahwa akan memulai rekaman yang terlewat
            bot = Application.get_instance().bot
            await bot.send_message(
                chat_id=user_id,
                text=f"⏳ <b>Mendeteksi livestream aktif yang belum direkam untuk {platform.upper()}: {username}</b>\n\n"
                     f"<i>Memulai Auto-Record...</i>",
                parse_mode=ParseMode.HTML
            )
            
            # Start recording
            if platform == "tiktok":
                success, recording_id, result = await start_tiktok_recording(
                    username, user_id, auto_record=True, account_id=account_id
                )
            else:
                success, recording_id, result = await start_bigo_recording(
                    username, user_id, auto_record=True, account_id=account_id
                )
            
            if success:
                # Informasi recording untuk keyboard
                file_path = ""
                status = "recording"
                
                # Get recording info untuk ditampilkan
                recording = get_recording_by_id(recording_id)
                if recording:
                    file_path = recording.get("file_path", "")
                    
                # Send notification with more details
                await bot.send_message(
                    chat_id=user_id,
                    text=(f"✅ <b>Auto-Record dilanjutkan untuk {platform.upper()}: {username}</b>\n\n"
                          f"ID: <code>{recording_id}</code>\n"
                          f"Status: <b>RECORDING</b>\n"
                          f"Output: <code>{os.path.basename(file_path)}</code>\n\n"
                          f"<i>Livestream sudah berlangsung dan sedang direkam.</i>"),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏹️ Stop Recording", callback_data=f"stop_{recording_id}")],
                        [InlineKeyboardButton("📋 Lihat Rekaman Aktif", callback_data="list_active")]
                    ])
                )
                
    except Exception as e:
        logger.error(f"Error checking livestream status: {str(e)}")
        # Log stack trace for debugging
        import traceback
        logger.error(traceback.format_exc())

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
    """Stop active recording with improved error handling and process management"""
    try:
        # Memastikan recording masih ada di active_recordings
        if recording_id not in active_recordings:
            logger.error(f"Recording ID {recording_id} not found in active_recordings")
            return False
            
        # Memastikan proses masih ada di recording_processes
        if recording_id not in recording_processes:
            logger.error(f"Recording ID {recording_id} not found in recording_processes")
            return False
        
        # Get recording info sebelum di-stop
        recording_info = active_recordings[recording_id]
        platform = recording_info.get("platform", "")
        output_file = recording_info.get("output_file", "")
        log_file = recording_info.get("log_file", "")
        account_id = recording_info.get("account_id")
        
        # Catat file size sebelum menghentikan proses
        initial_file_exists = os.path.exists(output_file)
        initial_file_size = get_file_size(output_file) if initial_file_exists else 0
        
        # Get process info
        process_info = recording_processes[recording_id]
        process = process_info.get("process")
        pid = process_info.get("pid")
        
        if not process:
            logger.error(f"Process object not found for recording {recording_id}")
            return False
        
        # Update status to stopping
        update_recording_status(recording_id, "stopping")
        logger.info(f"Stopping {platform} recording {recording_id} with PID {pid}")
        
        # Membuat file temporary marker sehingga tahu proses sedang dihentikan
        marker_file = f"{TEMP_PATH}/stopping_{recording_id}.marker"
        with open(marker_file, 'w') as f:
            f.write(f"Stopping recording {recording_id} at {datetime.now().isoformat()}")
        
        # Buat copy dari file output jika sudah ada (safeguard)
        if initial_file_exists and initial_file_size > 0:
            try:
                backup_file = f"{output_file}.stopping_backup"
                import shutil
                shutil.copy2(output_file, backup_file)
                logger.info(f"Created backup of recording file: {backup_file}")
            except Exception as backup_error:
                logger.error(f"Error creating backup: {str(backup_error)}")
        
        # Check if process is still running
        is_running = is_process_running(process)
        
        if is_running:
            # Coba soft terminate dulu (SIGTERM)
            try:
                process.terminate()
                
                # Tunggu 5 detik untuk proses berakhir secara bersih
                for _ in range(5):
                    if not is_process_running(process):
                        logger.info(f"Process {pid} terminated cleanly")
                        break
                    time.sleep(1)
                
                # Jika masih berjalan, kill process and all its children (force kill)
                if is_process_running(process):
                    logger.info(f"Process {pid} did not terminate, using kill_process_tree")
                    kill_process_tree(pid)
            except Exception as term_error:
                logger.error(f"Error terminating process: {str(term_error)}")
                # Tetap coba kill process tree jika terminate gagal
                try:
                    kill_process_tree(pid)
                except Exception as kill_error:
                    logger.error(f"Error killing process tree: {str(kill_error)}")
        
        # Record end time
        end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Tunggu lebih lama untuk memastikan file selesai ditulis/dirilis
        # Ini penting untuk memastikan file tidak corrupt
        time.sleep(5)
        
        # Check file status setelah proses dihentikan
        file_exists = os.path.exists(output_file)
        file_size = get_file_size(output_file) if file_exists else 0
        
        # Cek apakah ada backup file dan apakah perlu direstore
        backup_file = f"{output_file}.stopping_backup"
        if os.path.exists(backup_file):
            backup_size = get_file_size(backup_file)
            
            # Jika file asli hilang atau ukurannya berkurang, restore dari backup
            if (not file_exists or file_size < initial_file_size) and backup_size >= initial_file_size:
                try:
                    import shutil
                    shutil.copy2(backup_file, output_file)
                    logger.info(f"Restored recording file from backup: {backup_file} -> {output_file}")
                    file_exists = os.path.exists(output_file)
                    file_size = get_file_size(output_file)
                except Exception as restore_error:
                    logger.error(f"Error restoring from backup: {str(restore_error)}")
        
        # Determine status based on file
        if file_exists and file_size > 1024 * 1024:  # File lebih besar dari 1MB
            status = "stopped"  # Successfully stopped with content
        elif file_exists and file_size > 0:
            status = "partial"  # File ada tapi mungkin tidak lengkap
        else:
            # Coba cek log file untuk informasi tambahan
            log_content = ""
            if log_file and os.path.exists(log_file):
                try:
                    with open(log_file, 'r') as log:
                        log_content = log.read()
                except Exception as log_error:
                    logger.error(f"Error reading log file: {str(log_error)}")
                    
            # Jika log menunjukkan stream ended, set status ke ended
            if "Stream ended" in log_content or "stream ended" in log_content or "no longer live" in log_content:
                status = "ended"
            else:
                status = "failed"   # Stopped but no content
        
        # Update database
        update_recording_status(
            recording_id=recording_id,
            status=status,
            end_time=end_time,
            file_path=output_file,
            file_size=file_size
        )
        
        # If this was a monitored account recording, update the account
        if account_id:
            update_account_recording_id(account_id, None)
        
        # Save info for notification
        notification_info = {
            "recording_id": recording_id,
            "user_id": recording_info.get("user_id"),
            "status": status,
            "platform": recording_info.get("platform", ""),
            "target": recording_info.get("target", ""),
            "file_path": output_file,
            "file_size": file_size
        }
        
        # Add to notification queue
        notification_queue.put(notification_info)
        
        # Clean up
        if recording_id in active_recordings:
            del active_recordings[recording_id]
        if recording_id in recording_processes:
            del recording_processes[recording_id]
            
        # Remove marker file
        if os.path.exists(marker_file):
            try:
                os.remove(marker_file)
            except:
                pass
                
        # Remove backup file jika ada
        if os.path.exists(backup_file):
            try:
                os.remove(backup_file)
            except:
                pass
        
        logger.info(f"Recording {recording_id} stopped successfully with status: {status}")
        return True
    
    except Exception as e:
        logger.error(f"Error stopping recording {recording_id}: {str(e)}")
        # Tetap coba membersihkan resources untuk mencegah zombie process
        try:
            if recording_id in recording_processes:
                process_info = recording_processes[recording_id]
                pid = process_info.get("pid")
                if pid:
                    kill_process_tree(pid)
                del recording_processes[recording_id]
            
            if recording_id in active_recordings:
                del active_recordings[recording_id]
        except:
            pass
        return False

async def notify_recording_completed(notification):
    """Notify user about completed recording"""
    try:
        recording_id = notification.get("recording_id")
        status = notification.get("status")
        user_id = notification.get("user_id")
        platform = notification.get("platform", "").upper()
        target = notification.get("target", "")
        file_path = notification.get("file_path", "")
        file_size = notification.get("file_size", 0)
        compressed_path = notification.get("compressed_path")
        compressed_size = notification.get("compressed_size", 0)
        
        # Determine which file to use (compressed or original)
        use_compressed = compressed_path and os.path.exists(compressed_path) and compressed_size > 0
        actual_file = compressed_path if use_compressed else file_path
        actual_size = compressed_size if use_compressed else file_size
        
        # Status messages based on different completion states
        status_messages = {
            "completed": f"✅ <b>Recording Selesai!</b>",
            "partial": f"⚠️ <b>Recording Selesai (Tidak Lengkap)</b>",
            "stopped": f"🛑 <b>Recording Dihentikan Manual</b>",
            "failed": f"❌ <b>Recording Gagal</b>",
            "ended": f"🔚 <b>Livestream Berakhir</b>"
        }
        
        # Default to failed if status is unknown
        message_prefix = status_messages.get(status, f"❓ <b>Recording {status.upper()}</b>")
        
        # Prepare message
        if status in ["completed", "partial", "stopped"]:
            message = (
                f"{message_prefix}\n\n"
                f"<b>Platform:</b> {platform}\n"
                f"<b>Target:</b> {target}\n"
                f"<b>Ukuran:</b> {format_size(actual_size)}"
            )
            
            if use_compressed:
                message += f"\n<b>Kompresi:</b> {format_size(file_size)} → {format_size(compressed_size)}"
                
            message += f"\n<b>Status:</b> {status.upper()}"
        else:
            message = (
                f"{message_prefix}\n\n"
                f"<b>Platform:</b> {platform}\n"
                f"<b>Target:</b> {target}\n"
                f"<b>Status:</b> {status.upper()}"
            )
        
        # Add buttons
        keyboard = []
        
        if status in ["completed", "partial", "stopped"] and os.path.exists(actual_file) and actual_size > 0:
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
        if status in ["completed", "partial", "stopped"] and thumbnail and os.path.exists(thumbnail):
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
        [
            InlineKeyboardButton("⏰ Interval Cek", callback_data="check_interval")
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
        current_recording_id = account.get("current_recording_id")
        
        # Status indicators
        live_status = "🔴 LIVE" if is_live else "⚫"
        auto_status = "🔄 AUTO" if auto_record else ""
        notify_status = "🔔 NOTIFY" if notify_only else ""
        recording_status = "🎥 REC" if current_recording_id else ""
        
        status = f"{live_status} {recording_status} {auto_status} {notify_status}".strip()
        button_text = f"{platform}: {username} ({status})"
        
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"monitor_{acc_id}")])
    
    # Add back button
    keyboard.append([InlineKeyboardButton("➕ Tambah Akun", callback_data="monitor_accounts")])
    keyboard.append([InlineKeyboardButton("« Kembali", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_account_details_keyboard(account_id: int) -> InlineKeyboardMarkup:
    """Create keyboard for account details"""
    try:
        # Cek apakah account_id valid
        if not account_id or not isinstance(account_id, int):
            logger.error(f"Invalid account_id: {account_id}")
            return InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali", callback_data="list_monitored")]])
        
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM monitored_accounts WHERE id = ?", (account_id,))
        account = cursor.fetchone()
        
        if not account:
            # Return to monitored accounts list if account not found
            conn.close()
            logger.warning(f"Account with ID {account_id} not found in database")
            return InlineKeyboardMarkup([[InlineKeyboardButton("« Kembali", callback_data="list_monitored")]])
            
        # Convert SQLite row to dictionary
        account_dict = dict(account)
        
        # Safely get values with defaults
        auto_record = bool(account_dict.get("auto_record", 0))
        notify_only = bool(account_dict.get("notify_only", 0))
        is_live = bool(account_dict.get("is_live", 0))
        platform = account_dict.get("platform", "unknown")
        username = account_dict.get("username", "unknown")
        current_recording_id = account_dict.get("current_recording_id")
        
        conn.close()
        
        keyboard = []
        
        # Check if current_recording_id is valid and active
        recording_active = False
        if current_recording_id:
            recording = get_recording_by_id(current_recording_id)
            recording_active = recording and recording.get("status") == "recording"
            
            # If recording ID exists but not active, clean up the reference
            if not recording_active and recording:
                update_account_recording_id(account_id, None)
                current_recording_id = None
        
        # Toggle auto-record
        auto_text = "❌ Matikan Auto-Record" if auto_record else "✅ Aktifkan Auto-Record"
        keyboard.append([InlineKeyboardButton(auto_text, callback_data=f"toggle_auto_{account_id}")])
        
        # Toggle notify-only
        notify_text = "❌ Matikan Notifikasi Saja" if notify_only else "✅ Aktifkan Notifikasi Saja"
        keyboard.append([InlineKeyboardButton(notify_text, callback_data=f"toggle_notify_{account_id}")])
        
        # Status LIVE
        if is_live:
            # Refresh status
            keyboard.append([InlineKeyboardButton("🔄 Refresh Status LIVE", callback_data=f"refresh_live_{account_id}")])
            
            # If currently recording
            if recording_active and current_recording_id:
                # Add button to view recording details
                keyboard.append([InlineKeyboardButton("👁️ Lihat Rekaman Aktif", callback_data=f"view_{current_recording_id}")])
                keyboard.append([InlineKeyboardButton("⏹️ Stop Rekaman", callback_data=f"stop_{current_recording_id}")])
            else:
                # Otherwise, add record now button
                if platform == "tiktok":
                    record_data = f"record_monitor_tiktok_{username}"
                else:  # bigo
                    record_data = f"record_monitor_bigo_{username}"
                    
                keyboard.append([InlineKeyboardButton("🎥 Rekam Sekarang", callback_data=record_data)])
                
                # Add direct link to livestream
                if platform == "tiktok":
                    link = f"https://www.tiktok.com/@{username}/live"
                else:  # bigo
                    link = f"https://www.bigo.tv/{username}"
                    
                keyboard.append([InlineKeyboardButton("👁️ Tonton Livestream", url=link)])
        else:
            # Check now button when not live
            keyboard.append([InlineKeyboardButton("🔍 Cek Status Sekarang", callback_data=f"check_now_{account_id}")])
        
        # Test record (untuk debugging)
        if is_admin(account_dict.get("user_id", 0)):
            keyboard.append([InlineKeyboardButton("🧪 Test Record", callback_data=f"test_record_{account_id}")])
        
        # Delete account
        keyboard.append([InlineKeyboardButton("🗑️ Hapus Akun", callback_data=f"delete_account_{account_id}")])
        
        # Back button
        keyboard.append([InlineKeyboardButton("« Kembali", callback_data="list_monitored")])
        
        return InlineKeyboardMarkup(keyboard)
        
    except Exception as e:
        logger.error(f"Error creating account details keyboard: {str(e)}")
        # Fallback to simple keyboard
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ Error: Refresh", callback_data=f"monitor_{account_id}")],
            [InlineKeyboardButton("« Kembali", callback_data="list_monitored")]
        ])

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
    recordings = get_user_recordings(user_id, status="active")
    
    keyboard = []
    for recording in recordings:
        rec_id = recording["id"]
        platform = recording["platform"].upper()
        target = recording["target"]
        
        # Extract username from target for shorter display
        username = target
        if platform == "TIKTOK":
            if target.startswith("@"):
                username = target  # Already formatted
            else:
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
    # Get completed, stopped, and partial recordings
    recordings = get_user_recordings(user_id, status="completed", limit=20)
    
    keyboard = []
    for recording in recordings:
        rec_id = recording["id"]
        platform = recording["platform"].upper()
        target = recording["target"]
        status = recording["status"].upper()
        
        # Extract username from target for shorter display
        username = target
        if platform == "TIKTOK":
            if target.startswith("@"):
                username = target  # Already formatted
            else:
                match = re.search(r'@([^/?]+)', target)
                username = f"@{match.group(1)}" if match else target
        elif platform == "BIGO":
            match = re.search(r'bigo\.(?:tv|live)/([^/?]+)', target)
            username = match.group(1) if match else target
        
        button_text = f"{platform}: {username} ({status})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"view_{rec_id}")])
    
    # Add refresh button
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="list_completed")])
    
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
    elif status in ["completed", "stopped", "partial"]:
        # Check if file exists
        file_path = recording["compressed_path"] or recording["file_path"]
        if os.path.exists(file_path) and get_file_size(file_path) > 0:
            # Add download button
            keyboard.append([InlineKeyboardButton("⬇️ Download", callback_data=f"download_{recording_id}")])
            # Add delete button
            keyboard.append([InlineKeyboardButton("🗑️ Hapus File", callback_data=f"delete_{recording_id}")])
            # Add info button
            keyboard.append([InlineKeyboardButton("ℹ️ Info Teknis", callback_data=f"info_{recording_id}")])
    
    # Add back button
    if status in ["recording", "processing"]:
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
        f"• Support semua format link livestream\n"
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
        f"3. Bot akan mengecek status livestream\n"
        f"4. Jika sedang live, bot akan mulai merekam\n\n"
        
        f"<b>Cara Memantau Akun:</b>\n"
        f"1. Pilih 'Monitor Akun' di menu utama\n"
        f"2. Tambahkan akun TikTok atau Bigo\n"
        f"3. Bot akan memberi tahu saat akun mulai live\n"
        f"4. Aktifkan auto-record untuk merekam otomatis\n\n"
        
        f"<b>Format username/link yang didukung:</b>\n"
        f"• TikTok: @username, tiktok.com/@username/live, vt.tiktok.com/...\n"
        f"• Bigo: username, bigo.tv/username, bigo.live/username\n\n"
        
        f"<b>Perintah yang tersedia:</b>\n"
        f"/start - Memulai bot dan menampilkan menu utama\n"
        f"/help - Menampilkan bantuan ini\n"
        f"/record - Memulai proses rekaman\n"
        f"/active - Melihat rekaman yang sedang berlangsung\n"
        f"/monitor - Mengelola akun yang dipantau\n"
        f"/settings - Mengubah pengaturan bot\n"
        f"/cancel - Membatalkan proses yang sedang berjalan\n\n"
        
        f"<b>Catatan:</b>\n"
        f"• Bot akan otomatis mengecek apakah akun sedang live sebelum mulai merekam\n"
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
        "<b>Pilih platform yang ingin direkam:</b>",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("TikTok", callback_data="record_tiktok"),
                InlineKeyboardButton("Bigo", callback_data="record_bigo")
            ],
            [InlineKeyboardButton("« Batal", callback_data="main_menu")]
        ]),
        parse_mode=ParseMode.HTML
    )

async def cmd_active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for /active command"""
    user_id = update.effective_user.id
    
    # Get active recordings for user
    recordings = get_user_recordings(user_id, status="active")
    
    if not recordings:
        await update.message.reply_html(
            "📋 <b>REKAMAN AKTIF</b> 📋\n\n"
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
        
        # Calculate duration
        start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        duration = datetime.now() - start_dt
        hours, remainder = divmod(duration.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        text += f"{idx}. <b>{platform}</b>: {target}\n"
        text += f"   Mulai: {start_time}\n"
        text += f"   Durasi: {hours:02}:{minutes:02}:{seconds:02}\n\n"
    
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
    # Declare globals at the beginning of the function
    global COMPRESSION_ENABLED, TIKTOK_QUALITY, BIGO_QUALITY, CHECK_INTERVAL
    
    query = update.callback_query
    user_id = query.from_user.id
    
    # Exception handling untuk callback
    try:
        # Always acknowledge the callback query
        await query.answer()
    except Exception as e:
        logger.error(f"Error acknowledging callback query: {str(e)}")
    
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
            f"3. Bot akan mengecek status livestream\n"
            f"4. Jika sedang live, bot akan mulai merekam\n\n"
            
            f"<b>Format username/link yang didukung:</b>\n"
            f"• TikTok: @username, tiktok.com/@username/live, vt.tiktok.com/...\n"
            f"• Bigo: username, bigo.tv/username, bigo.live/username\n\n"
            
            f"<b>Perintah yang tersedia:</b>\n"
            f"/start - Memulai bot dan menampilkan menu utama\n"
            f"/help - Menampilkan bantuan ini\n"
            f"/record - Memulai proses rekaman\n"
            f"/active - Melihat rekaman yang sedang berlangsung\n"
            f"/monitor - Mengelola akun yang dipantau\n"
            f"/settings - Mengubah pengaturan bot\n"
            f"/cancel - Membatalkan proses yang sedang berjalan"
        )
        
        await query.message.edit_text(
            help_text,
            reply_markup=get_back_button(),
            parse_mode=ParseMode.HTML
        )
    
    # Toggle compression
    elif query.data == "toggle_compression":
        COMPRESSION_ENABLED = not COMPRESSION_ENABLED
        
        await query.message.edit_text(
            f"✅ Kompresi video {'diaktifkan' if COMPRESSION_ENABLED else 'dinonaktifkan'}.\n\n"
            f"Kompresi akan {'mengompres' if COMPRESSION_ENABLED else 'tidak mengompres'} video yang lebih besar dari {format_size(COMPRESSION_THRESHOLD)} secara otomatis untuk menghemat ruang penyimpanan tanpa mengurangi kualitas visual secara signifikan.",
            reply_markup=get_settings_keyboard(),
            parse_mode=ParseMode.HTML
        )
    
    # Change check interval
    elif query.data == "check_interval":
        intervals = [60, 120, 300, 600]
        current_idx = intervals.index(CHECK_INTERVAL) if CHECK_INTERVAL in intervals else 0
        next_idx = (current_idx + 1) % len(intervals)
        CHECK_INTERVAL = intervals[next_idx]
        
        await query.message.edit_text(
            f"⏰ Interval pemeriksaan akun diubah menjadi {CHECK_INTERVAL} detik ({CHECK_INTERVAL//60} menit).\n\n"
            f"Bot akan memeriksa status livestream akun yang dipantau setiap {CHECK_INTERVAL//60} menit.",
            reply_markup=get_settings_keyboard(),
            parse_mode=ParseMode.HTML
        )
    
    # Info
    elif query.data == "info":
        info_text = (
            f"ℹ️ <b>INFORMASI BOT</b> ℹ️\n\n"
            f"<b>Livestream Recorder Bot</b>\n"
            f"Versi: 1.1.0\n\n"
            f"Bot ini dibuat untuk merekam livestream dari platform TikTok dan Bigo. "
            f"Hasil rekaman akan disimpan dalam format MP4 dan dapat diunduh langsung dari bot.\n\n"
            f"<b>Fitur:</b>\n"
            f"• Rekam TikTok dan Bigo livestream\n"
            f"• Support semua jenis link dan username\n"
            f"• Cek otomatis status livestream\n"
            f"• Multi-job processing dengan manajemen proses\n"
            f"• Deteksi live/offline akurat\n"
            f"• Auto-record dengan pemantauan\n\n"
            f"<b>Peningkatan Terbaru:</b>\n"
            f"• Perbaikan stop recording agar tidak error\n"
            f"• Support semua format link (termasuk shortened URLs)\n"
            f"• Deteksi livestream yang lebih akurat\n"
            f"• Perbaikan managemen proses untuk mencegah zombie process\n"
            f"• Peningkatan kualitas hasil rekaman"
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
            "• https://www.tiktok.com/@username/live\n"
            "• https://vt.tiktok.com/abcXYZ/\n"
            "• https://vm.tiktok.com/abcXYZ/",
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
            "• https://www.bigo.tv/username\n"
            "• https://bigo.live/username",
            reply_markup=get_cancel_keyboard(),
            parse_mode=ParseMode.HTML
        )
        
        context.user_data['waiting_for_input'] = "bigo"
    
    # List active recordings
    elif query.data == "list_active":
        # Get active recordings for user
        recordings = get_user_recordings(user_id, status="active")
        
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
        
        # Add refresh button
        text += "<i>Klik rekaman untuk menampilkan detail dan opsi</i>"
        
        await query.message.edit_text(
            text,
            reply_markup=get_active_recordings_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )
    
    # List completed recordings
    elif query.data == "list_completed":
        # Get completed recordings for user
        recordings = get_user_recordings(user_id, status="completed")
        
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
            file_size = rec["file_size"] or 0
            compressed_size = rec["compressed_size"] or 0
            
            actual_size = compressed_size if compressed_size > 0 else file_size
            
            text += f"{idx}. <b>{platform}</b>: {target}\n"
            text += f"   Status: {status}\n"
            text += f"   Ukuran: {format_size(actual_size)}\n\n"
        
        text += "<i>Klik rekaman untuk menampilkan detail dan opsi</i>"
        
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
            reply_markup=get_main_menu_keyboard(),
            parse_mode=ParseMode.HTML
        )
    
    # View recording details
    elif query.data.startswith("view_"):
        recording_id = query.data.split("_")[1]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan atau telah dihapus.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        platform = recording["platform"].upper()
        target = recording["target"]
        status = recording["status"].upper()
        start_time = recording["start_time"]
        end_time = recording["end_time"] or ""
        file_path = recording["file_path"]
        file_size = recording["file_size"] or 0
        compressed_path = recording.get("compressed_path")
        compressed_size = recording.get("compressed_size", 0)
        original_link = recording.get("original_link", "")
        
        # Determine actual file and size
        use_compressed = compressed_path and os.path.exists(compressed_path) and compressed_size > 0
        actual_file = compressed_path if use_compressed else file_path
        actual_size = compressed_size if use_compressed else file_size
        
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
        
        if os.path.exists(actual_file):
            text += f"<b>File:</b> {os.path.basename(actual_file)}\n"
            text += f"<b>Ukuran:</b> {format_size(actual_size)}\n"
            
            if use_compressed:
                text += f"<b>Kompresi:</b> {format_size(file_size)} → {format_size(compressed_size)}\n"
                
            if original_link:
                text += f"<b>Link Original:</b> {original_link}\n"
        else:
            text += "<b>File:</b> Tidak tersedia\n"
        
        await query.message.edit_text(
            text,
            reply_markup=get_recording_details_keyboard(recording_id),
            parse_mode=ParseMode.HTML
        )
    
    # View technical info
    elif query.data.startswith("info_"):
        recording_id = query.data.split("_")[1]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan atau telah dihapus.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        # Get recording details
        file_path = recording.get("compressed_path") or recording.get("file_path", "")
        
        if not os.path.exists(file_path):
            await query.message.edit_text(
                "❌ File tidak ditemukan atau telah dihapus.",
                reply_markup=get_recording_details_keyboard(recording_id),
                parse_mode=ParseMode.HTML
            )
            return
        
        # Get file info using ffprobe
        try:
            import subprocess
            import json
            
            cmd = [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                file_path
            ]
            
            result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            info = json.loads(result.stdout)
            
            # Format technical info
            text = f"🔍 <b>INFO TEKNIS REKAMAN</b> 🔍\n\n"
            
            if "format" in info:
                format_info = info["format"]
                text += f"<b>Format:</b> {format_info.get('format_name', 'Unknown')}\n"
                
                duration = float(format_info.get('duration', 0))
                minutes, seconds = divmod(duration, 60)
                hours, minutes = divmod(minutes, 60)
                text += f"<b>Durasi:</b> {int(hours):02}:{int(minutes):02}:{int(seconds):02}\n"
                
                bitrate = int(format_info.get('bit_rate', 0)) // 1000
                text += f"<b>Bitrate Total:</b> {bitrate} Kbps\n\n"
            
            if "streams" in info:
                # Video stream
                video_streams = [s for s in info["streams"] if s.get("codec_type") == "video"]
                if video_streams:
                    video = video_streams[0]
                    text += f"<b>Video Codec:</b> {video.get('codec_name', 'Unknown')}\n"
                    text += f"<b>Resolusi:</b> {video.get('width', '?')}x{video.get('height', '?')}\n"
                    
                    fps = eval(video.get('r_frame_rate', '0/1'))
                    text += f"<b>FPS:</b> {fps:.2f}\n"
                    
                    if 'bit_rate' in video:
                        v_bitrate = int(video.get('bit_rate', 0)) // 1000
                        text += f"<b>Video Bitrate:</b> {v_bitrate} Kbps\n\n"
                    else:
                        text += "\n"
                
                # Audio stream
                audio_streams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
                if audio_streams:
                    audio = audio_streams[0]
                    text += f"<b>Audio Codec:</b> {audio.get('codec_name', 'Unknown')}\n"
                    
                    sample_rate = int(audio.get('sample_rate', 0)) // 1000
                    text += f"<b>Sample Rate:</b> {sample_rate} KHz\n"
                    
                    channels = audio.get('channels', 0)
                    text += f"<b>Channels:</b> {channels} ({audio.get('channel_layout', 'Unknown')})\n"
                    
                    if 'bit_rate' in audio:
                        a_bitrate = int(audio.get('bit_rate', 0)) // 1000
                        text += f"<b>Audio Bitrate:</b> {a_bitrate} Kbps\n"
            
            await query.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Kembali", callback_data=f"view_{recording_id}")]
                ]),
                parse_mode=ParseMode.HTML
            )
            
        except Exception as e:
            logger.error(f"Error getting technical info: {str(e)}")
            await query.message.edit_text(
                f"❌ Gagal mendapatkan info teknis: {str(e)}",
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
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        # Update message to show progress
        await query.message.edit_text(
            "⏳ <b>Menghentikan rekaman...</b>\n\n"
            "Mohon tunggu, proses ini memerlukan waktu beberapa detik.",
            parse_mode=ParseMode.HTML
        )
        
        # Stop recording
        success = await stop_recording(recording_id)
        
        if success:
            await query.message.edit_text(
                "✅ <b>Rekaman berhasil dihentikan.</b>\n\n"
                "File akan diproses dan Anda akan menerima notifikasi ketika siap untuk diunduh.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
        else:
            await query.message.edit_text(
                "❌ <b>Gagal menghentikan rekaman.</b>\n\n"
                "Silakan coba lagi atau tunggu beberapa saat.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
    
    # Download recording
    elif query.data.startswith("download_"):
        recording_id = query.data.split("_")[1]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        # Check if we have a compressed version
        compressed_path = recording.get("compressed_path")
        file_path = compressed_path if compressed_path and os.path.exists(compressed_path) else recording["file_path"]
        
        if not os.path.exists(file_path):
            await query.message.edit_text(
                "❌ File tidak ditemukan. Mungkin telah dihapus.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        # Check file size
        file_size = get_file_size(file_path)
        
        if file_size == 0:
            await query.message.edit_text(
                "❌ File kosong atau rusak.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
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
                    
                    import subprocess
                    # Run FFmpeg with more aggressive compression
                    cmd = [
                        "ffmpeg",
                        "-i", file_path,
                        "-c:v", "libx264",
                        "-crf", "28",      # Higher CRF = more compression
                        "-preset", "medium",
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
                    logger.error(f"Error pada kompresi darurat: {str(compress_error)}")
                    await query.message.edit_text(
                        f"❌ <b>Gagal mengompres dan mengirim file:</b> {str(compress_error)}\n\n"
                        f"File terlalu besar untuk Telegram. Silakan gunakan opsi lain untuk mentransfer file.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_back_button()
                    )
            else:
                logger.error(f"Error mengirim file: {str(e)}")
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
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        file_path = recording["file_path"]
        
        # Confirm deletion
        await query.message.edit_text(
            f"❓ <b>Apakah Anda yakin ingin menghapus file ini?</b>\n\n"
            f"File: {os.path.basename(file_path)}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Ya", callback_data=f"confirm_delete_{recording_id}"),
                    InlineKeyboardButton("❌ Tidak", callback_data=f"view_{recording_id}")
                ]
            ]),
            parse_mode=ParseMode.HTML
        )
    
    # Confirm delete recording
    elif query.data.startswith("confirm_delete_"):
        recording_id = query.data.split("_")[2]
        recording = get_recording_by_id(recording_id)
        
        if not recording:
            await query.message.edit_text(
                "❌ Rekaman tidak ditemukan.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
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
            
            # Delete compressed file if it exists and is different from original
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
                    f"✅ <b>{files_deleted} file berhasil dihapus.</b>",
                    reply_markup=get_back_button(),
                    parse_mode=ParseMode.HTML
                )
            else:
                await query.message.edit_text(
                    "⚠️ <b>File sudah tidak ada.</b>",
                    reply_markup=get_back_button(),
                    parse_mode=ParseMode.HTML
                )
        except Exception as e:
            logger.error(f"Error deleting file: {str(e)}")
            
            await query.message.edit_text(
                f"❌ <b>Gagal menghapus file:</b> {str(e)}",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
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
                "<i>Contoh:</i>\n"
                "• @username (tanpa @ juga bisa)\n"
                "• https://www.tiktok.com/@username/\n"
                "• https://vt.tiktok.com/abcXYZ/",
                reply_markup=get_cancel_keyboard(),
                parse_mode=ParseMode.HTML
            )
            context.user_data['waiting_for_input'] = "monitor_tiktok"
        else:  # bigo
            await query.message.edit_text(
                "🔔 <b>TAMBAH AKUN BIGO</b> 🔔\n\n"
                "Masukkan username Bigo yang ingin dipantau:\n\n"
                "<i>Contoh:</i>\n"
                "• username\n"
                "• https://www.bigo.tv/username\n"
                "• https://bigo.live/username",
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
            current_recording_id = acc.get("current_recording_id")
            
            status = "🔴 LIVE" if is_live else "⚫ Offline"
            status += " 🎥 REC" if current_recording_id else ""
            auto = "🔄 Auto-Record" if auto_record else ""
            notify = "🔔 Notifikasi Saja" if notify_only else ""
            
            text += f"{idx}. <b>{platform}:</b> {username}\n"
            text += f"   Status: {status}\n"
            
            if auto or notify:
                text += f"   Mode: {auto} {notify}\n"
            
            text += "\n"
        
        text += "<i>Klik akun untuk menampilkan detail dan opsi</i>"
        
        await query.message.edit_text(
            text,
            reply_markup=get_monitored_accounts_keyboard(user_id),
            parse_mode=ParseMode.HTML
        )
    
    # View monitored account
    elif query.data.startswith("monitor_"):
        try:
            parts = query.data.split("_")
            if len(parts) < 2:
                raise ValueError(f"Invalid monitor command format: {query.data}")
                
            account_id = int(parts[1])
            
            # Get account with error handling
            account = get_account_by_id(account_id)
            
            if not account:
                await query.message.edit_text(
                    "❌ <b>Akun tidak ditemukan.</b>\n\n"
                    "Akun mungkin telah dihapus atau terjadi kesalahan database.",
                    reply_markup=get_back_button(),
                    parse_mode=ParseMode.HTML
                )
                return
            
            # Get account info dengan safely
            platform = account.get("platform", "unknown").upper()
            username = account.get("username", "unknown")
            is_live = bool(account.get("is_live", 0))
            auto_record = bool(account.get("auto_record", 0))
            notify_only = bool(account.get("notify_only", 0))
            added_time = account.get("added_time", "unknown")
            last_check = account.get("last_check", "unknown")
            current_recording_id = account.get("current_recording_id")
            
            # Verify current_recording_id is still valid
            recording_active = False
            if current_recording_id:
                recording = get_recording_by_id(current_recording_id)
                if recording and recording.get("status") == "recording":
                    recording_active = True
                else:
                    # Reset recording ID if not active
                    update_account_recording_id(account_id, None)
                    current_recording_id = None
            
            # Get livestream history
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute(
                """SELECT * FROM livestream_history 
                WHERE account_id = ? 
                ORDER BY start_time DESC LIMIT 5""", 
                (account_id,)
            )
            
            history = cursor.fetchall()
            conn.close()
            
            # Build message text
            text = f"🔔 <b>DETAIL AKUN {platform}</b> 🔔\n\n"
            text += f"<b>Username:</b> {username}\n"
            text += f"<b>Status:</b> {'🔴 LIVE' if is_live else '⚫ Offline'}\n"
            text += f"<b>Auto-Record:</b> {'✅ Aktif' if auto_record else '❌ Nonaktif'}\n"
            
            if notify_only:
                text += f"<b>Mode:</b> Notifikasi Saja (tidak merekam otomatis)\n"
            
            if recording_active and current_recording_id:
                text += f"<b>Sedang Direkam:</b> ✅ (ID: {current_recording_id[:8]}...)\n"
                
            text += f"<b>Ditambahkan:</b> {added_time}\n"
            text += f"<b>Terakhir dicek:</b> {last_check}\n\n"
            
            if history:
                text += f"<b>Riwayat Livestream Terakhir:</b>\n"
                for idx, h in enumerate(history, 1):
                    start_time = h["start_time"]
                    end_time = h["end_time"] or "Masih berlangsung"
                    was_recorded = bool(h["was_recorded"])
                    recording_id = h["recording_id"]
                    
                    duration_str = ""
                    if h["end_time"]:
                        start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
                        end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
                        duration = end_dt - start_dt
                        duration_str = f" ({format_duration(duration.seconds)})"
                    
                    text += f"{idx}. {start_time} s/d {end_time}{duration_str}\n"
                    if was_recorded:
                        text += f"   ✅ Terekam (ID: {recording_id[:8] if recording_id else 'N/A'})\n"
                    
                    if idx < len(history):
                        text += "\n"
            
            # Create keyboard dengan error handling
            keyboard = get_account_details_keyboard(account_id)
            
            await query.message.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        except Exception as e:
            logger.error(f"Error handling monitor command: {str(e)}")
            # Show error message with option to go back
            await query.message.edit_text(
                f"❌ <b>Terjadi kesalahan saat menampilkan akun:</b>\n\n"
                f"Error: {str(e)}\n\n"
                f"<i>Silakan coba lagi atau kembali ke menu sebelumnya.</i>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Coba Lagi", callback_data=query.data)],
                    [InlineKeyboardButton("« Kembali", callback_data="list_monitored")]
                ]),
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
                "❌ <b>Akun tidak ditemukan.</b>",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
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
            f"✅ <b>Auto-Record berhasil {'dimatikan' if auto_record else 'diaktifkan'}!</b>",
            reply_markup=get_account_details_keyboard(account_id),
            parse_mode=ParseMode.HTML
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
                "❌ <b>Akun tidak ditemukan.</b>",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
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
            f"✅ <b>Mode Notifikasi Saja berhasil {'dimatikan' if notify_only else 'diaktifkan'}!</b>",
            reply_markup=get_account_details_keyboard(account_id),
            parse_mode=ParseMode.HTML
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
                "❌ <b>Akun tidak ditemukan.</b>",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
        
        platform = account["platform"]
        username = account["username"]
        
        # Confirm deletion
        await query.message.edit_text(
            f"❓ <b>Apakah Anda yakin ingin berhenti memantau akun ini?</b>\n\n"
            f"Platform: {platform.upper()}\n"
            f"Username: {username}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Ya", callback_data=f"confirm_delete_account_{account_id}"),
                    InlineKeyboardButton("❌ Tidak", callback_data=f"monitor_{account_id}")
                ]
            ]),
            parse_mode=ParseMode.HTML
        )
    
    # Confirm delete account
    elif query.data.startswith("confirm_delete_account_"):
        account_id = int(query.data.split("_")[3])
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Check if account has active recording
        cursor.execute("SELECT current_recording_id FROM monitored_accounts WHERE id = ?", (account_id,))
        result = cursor.fetchone()
        
        if result and result[0]:
            current_recording_id = result[0]
            # Stop the recording first
            await stop_recording(current_recording_id)
        
        # Delete account
        cursor.execute("DELETE FROM monitored_accounts WHERE id = ?", (account_id,))
        deleted = cursor.rowcount > 0
        
        # Also delete history
        cursor.execute("DELETE FROM livestream_history WHERE account_id = ?", (account_id,))
        
        conn.commit()
        conn.close()
        
        if deleted:
            await query.message.edit_text(
                "✅ <b>Akun berhasil dihapus dari daftar pantauan.</b>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("« Kembali ke Daftar", callback_data="list_monitored")]
                ]),
                parse_mode=ParseMode.HTML
            )
        else:
            await query.message.edit_text(
                "❌ <b>Gagal menghapus akun.</b>",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
    
    # Record from notification or monitor
    elif query.data.startswith("record_notif_") or query.data.startswith("record_monitor_"):
        parts = query.data.split("_")
        platform = parts[2]
        username = parts[3]
        
        # Update message to show checking status
        await query.message.edit_text(
            f"⏳ <b>Memeriksa status livestream {platform.upper()} untuk @{username}...</b>",
            parse_mode=ParseMode.HTML
        )
        
        # Check if livestream is active
        is_live = False
        if platform == "tiktok":
            is_live, username, live_url = await check_tiktok_live(username)
        else:  # bigo
            is_live, username, live_url = await check_bigo_live(username)
            
        if not is_live:
            await query.message.edit_text(
                f"❌ <b>{username} tidak sedang live di {platform.upper()}.</b>\n\n"
                f"Silakan coba lagi nanti ketika livestream aktif.",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
            
        # Update message to show starting recording
        await query.message.edit_text(
            f"⏳ <b>Memulai rekaman {platform.upper()} untuk @{username}...</b>",
            parse_mode=ParseMode.HTML
        )
        
        # Get account_id if this is from monitor
        account_id = None
        if query.data.startswith("record_monitor_"):
            # Get account ID from database
            account = get_account_by_username(user_id, platform, username)
            if account:
                account_id = account["id"]
        
        # Start recording
        if platform == "tiktok":
            success, recording_id, result = await start_tiktok_recording(
                username, user_id, account_id=account_id
            )
        else:  # bigo
            success, recording_id, result = await start_bigo_recording(
                username, user_id, account_id=account_id
            )
        
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
            f"⏰ <b>Interval Cek Akun:</b> {CHECK_INTERVAL} detik ({CHECK_INTERVAL//60} menit)\n",
            reply_markup=get_settings_keyboard(),
            parse_mode=ParseMode.HTML
        )
    # Quality settings
    elif query.data == "quality_tiktok":
        qualities = ["best", "720p", "480p"]
        current_idx = qualities.index(TIKTOK_QUALITY) if TIKTOK_QUALITY in qualities else 0
        next_idx = (current_idx + 1) % len(qualities)
        TIKTOK_QUALITY = qualities[next_idx]
        
        await query.message.edit_text(
            f"🎬 <b>Kualitas rekaman TikTok diubah menjadi {TIKTOK_QUALITY}.</b>",
            reply_markup=get_settings_keyboard(),
            parse_mode=ParseMode.HTML
        )
        
    elif query.data == "quality_bigo":
        qualities = ["best", "720p", "480p"]
        current_idx = qualities.index(BIGO_QUALITY) if BIGO_QUALITY in qualities else 0
        next_idx = (current_idx + 1) % len(qualities)
        BIGO_QUALITY = qualities[next_idx]
        
        await query.message.edit_text(
            f"🎬 <b>Kualitas rekaman Bigo diubah menjadi {BIGO_QUALITY}.</b>",
            reply_markup=get_settings_keyboard(),
            parse_mode=ParseMode.HTML
        )

# ========== MESSAGE HANDLERS ==========

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for text messages with improved link detection and processing"""
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
                f"⏳ <b>Memeriksa status livestream {input_type.upper()}...</b>\n"
                f"Target: <code>{message_text}</code>"
            )
            
            # Check if livestream is active
            is_live = False
            username = message_text
            live_url = None
            
            if input_type == "tiktok":
                is_live, username, live_url = await check_tiktok_live(message_text)
            else:  # bigo
                is_live, username, live_url = await check_bigo_live(message_text)
                
            if not is_live:
                await processing_message.edit_text(
                    f"❌ <b>Livestream tidak ditemukan atau tidak sedang aktif.</b>\n\n"
                    f"Pastikan:\n"
                    f"• Format username/URL sudah benar\n"
                    f"• Akun tersebut sedang live\n"
                    f"• Coba lagi setelah beberapa saat",
                    reply_markup=get_back_button(),
                    parse_mode=ParseMode.HTML
                )
                return
            
            # Update message to show starting recording
            await processing_message.edit_text(
                f"⏳ <b>Memulai rekaman {input_type.upper()}...</b>\n"
                f"Target: <code>{username}</code>\n"
                f"Status: <b>LIVE</b>"
            )
            
            if input_type == "tiktok":
                # Start TikTok recording
                success, recording_id, result = await start_tiktok_recording(message_text, user_id)
            else:  # bigo
                # Start Bigo recording
                success, recording_id, result = await start_bigo_recording(message_text, user_id)
            
            if success:
                # Format success message
                success_text = (
                    f"✅ <b>Rekaman {input_type.upper()} Dimulai!</b>\n\n"
                    f"Target: <code>{username}</code>\n"
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
                    f"Target: <code>{username}</code>\n"
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
            original_input = message_text.strip()
            username = original_input
            
            # Processing message
            processing_message = await update.message.reply_html(
                f"⏳ <b>Memeriksa {platform.upper()} username...</b>"
            )
            
            # Process URL or username
            if platform == "tiktok":
                # Check if input might be a URL
                if is_valid_tiktok_url(original_input):
                    # Resolve URL to get username
                    resolved_url = await resolve_shortened_url(original_input)
                    extracted_username = extract_tiktok_username_from_url(resolved_url)
                    if extracted_username:
                        username = extracted_username
                        
                # Remove @ from TikTok username if present
                if username.startswith("@"):
                    username = username[1:]
                
                # Validate username
                if not validate_tiktok_username(username):
                    await processing_message.edit_text(
                        "❌ <b>Format username TikTok tidak valid!</b>\n\n"
                        "Pastikan username hanya mengandung huruf, angka, titik, atau garis bawah.",
                        reply_markup=get_monitor_menu_keyboard(),
                        parse_mode=ParseMode.HTML
                    )
                    return
            
            elif platform == "bigo":
                # Check if input might be a URL
                if is_valid_bigo_url(original_input):
                    # Extract username from URL
                    extracted_username = extract_bigo_username_from_url(original_input)
                    if extracted_username:
                        username = extracted_username
                
                # Validate username
                if not validate_bigo_username(username):
                    await processing_message.edit_text(
                        "❌ <b>Format username Bigo tidak valid!</b>\n\n"
                        "Pastikan username hanya mengandung huruf, angka, atau garis bawah.",
                        reply_markup=get_monitor_menu_keyboard(),
                        parse_mode=ParseMode.HTML
                    )
                    return
            
            # Update processing message
            await processing_message.edit_text(
                f"⏳ <b>Menambahkan {platform.upper()} username</b> <code>{username}</code> <b>ke daftar pantauan...</b>"
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
                    # Check in background thread to not block response
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

async def process_notifications(app):
    """Proses notifications dari queue and send ke user"""
    global notification_processor_running
    notification_processor_running = True
    
    while notification_processor_running:
        try:
            # Cek notification queue (non-blocking)
            while not notification_queue.empty():
                try:
                    # Ambil notification dari queue
                    notification = notification_queue.get(block=False)
                    
                    # Proses notification
                    recording_id = notification.get("recording_id")
                    status = notification.get("status")
                    user_id = notification.get("user_id")
                    
                    if recording_id and status and user_id:
                        # Kirim notifikasi ke user
                        try:
                            await notify_recording_completed(notification)
                        except Exception as notify_err:
                            logger.error(f"Error sending notification: {str(notify_err)}")
                    
                    # Mark task as done
                    notification_queue.task_done()
                except queue.Empty:
                    break
                except Exception as e:
                    logger.error(f"Error processing notification item: {str(e)}")
            
            # Sleep sedikit untuk menghemat CPU
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in notification processor: {str(e)}")
            await asyncio.sleep(5)  # Longer sleep on error

async def check_running_processes():
    """Periodically check running processes to ensure they're still alive and not stuck"""
    while True:
        try:
            current_time = datetime.now()
            
            # Check each recording process
            processes_to_stop = []
            
            for recording_id, process_info in recording_processes.items():
                # Skip if recording is not in active_recordings
                if recording_id not in active_recordings:
                    continue
                    
                process = process_info.get("process")
                start_time = process_info.get("start_time")
                is_alive = process_info.get("is_alive", False)
                status_check_time = process_info.get("status_check_time")
                
                # Skip if already marked as not alive
                if not is_alive:
                    continue
                
                # Check if process is still running
                if not is_process_running(process):
                    # Process died unexpectedly
                    logger.warning(f"Process for recording {recording_id} died unexpectedly")
                    processes_to_stop.append(recording_id)
                    continue
                
                # Check for timeout (process running too long)
                if start_time:
                    duration = (current_time - start_time).total_seconds()
                    if duration > RECORDING_TIMEOUT:
                        logger.warning(f"Recording {recording_id} exceeded timeout ({duration}s > {RECORDING_TIMEOUT}s)")
                        processes_to_stop.append(recording_id)
                        continue
                
                # Check if status hasn't been updated for a while (stuck process)
                if status_check_time:
                    time_since_check = (current_time - status_check_time).total_seconds()
                    if time_since_check > 300:  # 5 minutes without status update
                        logger.warning(f"Recording {recording_id} status hasn't been updated for {time_since_check}s")
                        processes_to_stop.append(recording_id)
                        continue
            
            # Stop processes that need to be stopped
            for recording_id in processes_to_stop:
                try:
                    await stop_recording(recording_id)
                    logger.info(f"Automatically stopped recording {recording_id} due to issues")
                except Exception as e:
                    logger.error(f"Error stopping problematic recording {recording_id}: {str(e)}")
            
            # Sleep before next check
            await asyncio.sleep(60)  # Check every minute
            
        except Exception as e:
            logger.error(f"Error in process checker: {str(e)}")
            await asyncio.sleep(60)  # Sleep and try again

# ========== CLEANUP FUNCTIONS ==========

def cleanup_on_shutdown():
    """Cleanup resources when bot is shutting down"""
    try:
        logger.info("Shutting down, cleaning up resources...")
        
        # Stop all active recordings
        for recording_id in list(recording_processes.keys()):
            try:
                process_info = recording_processes[recording_id]
                process = process_info.get("process")
                pid = process_info.get("pid")
                
                if process and is_process_running(process):
                    logger.info(f"Stopping recording {recording_id} with PID {pid}")
                    kill_process_tree(pid)
            except Exception as e:
                logger.error(f"Error stopping recording {recording_id} during shutdown: {str(e)}")
        
        logger.info("Cleanup completed")
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")

# ========== NEW CALLBACK HANDLERS ==========

async def handle_check_now(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int) -> None:
    """Handler for checking account status now"""
    try:
        # Get account info
        account = get_account_by_id(account_id)
        if not account:
            await update.callback_query.message.edit_text(
                "❌ <b>Akun tidak ditemukan.</b>",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
            
        user_id = account["user_id"]
        platform = account["platform"]
        username = account["username"]
        
        # Update message
        await update.callback_query.message.edit_text(
            f"⏳ <b>Memeriksa status {platform.upper()} untuk {username}...</b>",
            parse_mode=ParseMode.HTML
        )
        
        # Check livestream status
        if platform == "tiktok":
            is_live, username, live_url = await check_tiktok_live(username)
        else:  # bigo
            is_live, username, live_url = await check_bigo_live(username)
            
        # Update status in database
        update_account_live_status(account_id, is_live)
        
        # Reload account data
        account = get_account_by_id(account_id)
        
        # Show account details
        platform = account["platform"].upper()
        username = account["username"]
        is_live = bool(account["is_live"])
        auto_record = bool(account["auto_record"])
        notify_only = bool(account["notify_only"])
        added_time = account["added_time"]
        last_check = account["last_check"]
        current_recording_id = account.get("current_recording_id")
        
        # Get livestream history
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
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
        text += f"<b>Status:</b> {'🔴 LIVE' if is_live else '⚫ Offline'} (Baru dicek)\n"
        text += f"<b>Auto-Record:</b> {'✅ Aktif' if auto_record else '❌ Nonaktif'}\n"
        
        if notify_only:
            text += f"<b>Mode:</b> Notifikasi Saja (tidak merekam otomatis)\n"
        
        if current_recording_id:
            text += f"<b>Sedang Direkam:</b> ✅ (ID: {current_recording_id[:8]}...)\n"
            
        text += f"<b>Ditambahkan:</b> {added_time}\n"
        text += f"<b>Terakhir dicek:</b> {last_check}\n\n"
        
        if is_live:
            text += f"<b>Status Livestream:</b> 🔴 LIVE!\n\n"
        else:
            text += f"<b>Status Livestream:</b> ⚫ Offline\n\n"
        
        if history:
            text += f"<b>Riwayat Livestream Terakhir:</b>\n"
            for idx, h in enumerate(history, 1):
                start_time = h["start_time"]
                end_time = h["end_time"] or "Masih berlangsung"
                was_recorded = bool(h["was_recorded"])
                recording_id = h["recording_id"]
                
                duration_str = ""
                if h["end_time"]:
                    start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
                    end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
                    duration = end_dt - start_dt
                    duration_str = f" ({format_duration(duration.seconds)})"
                
                text += f"{idx}. {start_time} s/d {end_time}{duration_str}\n"
                if was_recorded:
                    text += f"   ✅ Terekam (ID: {recording_id[:8] if recording_id else 'N/A'})\n"
                
                if idx < len(history):
                    text += "\n"
        
        # Update message
        await update.callback_query.message.edit_text(
            text,
            reply_markup=get_account_details_keyboard(account_id),
            parse_mode=ParseMode.HTML
        )
        
        # If now live and auto-record enabled, start recording
        if is_live and auto_record and not notify_only and not current_recording_id:
            # Show recording message
            recording_message = await context.bot.send_message(
                chat_id=user_id,
                text=f"⏳ <b>Akun {username} terdeteksi LIVE! Memulai Auto-Record...</b>",
                parse_mode=ParseMode.HTML
            )
            
            # Start recording
            if platform == "tiktok":
                success, recording_id, result = await start_tiktok_recording(
                    username, user_id, auto_record=True, account_id=account_id
                )
            else:
                success, recording_id, result = await start_bigo_recording(
                    username, user_id, auto_record=True, account_id=account_id
                )
                
            if success:
                await recording_message.edit_text(
                    f"✅ <b>Auto-Record dimulai untuk {platform.upper()}: {username}</b>\n\n"
                    f"ID: <code>{recording_id}</code>\n",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 Lihat Rekaman Aktif", callback_data="list_active")]
                    ])
                )
            else:
                await recording_message.edit_text(
                    f"❌ <b>Gagal memulai Auto-Record untuk {platform.upper()}: {username}</b>\n\n"
                    f"Error: {result}",
                    parse_mode=ParseMode.HTML
                )
    
    except Exception as e:
        logger.error(f"Error in handle_check_now: {str(e)}")
        await update.callback_query.message.edit_text(
            f"❌ <b>Terjadi kesalahan:</b> {str(e)}",
            reply_markup=get_account_details_keyboard(account_id),
            parse_mode=ParseMode.HTML
        )

async def handle_test_record(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int) -> None:
    """Handler for testing recording (admin only)"""
    try:
        # Get account info
        account = get_account_by_id(account_id)
        if not account:
            await update.callback_query.message.edit_text(
                "❌ <b>Akun tidak ditemukan.</b>",
                reply_markup=get_back_button(),
                parse_mode=ParseMode.HTML
            )
            return
            
        user_id = account["user_id"]
        
        # Cek apakah user admin
        if not is_admin(user_id):
            await update.callback_query.message.edit_text(
                "❌ <b>Hanya admin yang dapat melakukan test record.</b>",
                reply_markup=get_account_details_keyboard(account_id),
                parse_mode=ParseMode.HTML
            )
            return
            
        platform = account["platform"]
        username = account["username"]
        
        # Update message
        await update.callback_query.message.edit_text(
            f"⏳ <b>Memulai test recording untuk {platform.upper()}: {username}...</b>",
            parse_mode=ParseMode.HTML
        )
        
        # Force record
        if platform == "tiktok":
            # Persiapkan test file dengan ffmpeg untuk TikTok
            current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            output_file = os.path.join(DOWNLOAD_PATH, f"tiktok_{username}_{current_time}_test.mp4")
            
            # Create test video file
            import subprocess
            cmd = [
                "ffmpeg", 
                "-f", "lavfi", 
                "-i", "testsrc=duration=10:size=1280x720:rate=30",
                "-f", "lavfi",
                "-i", "anoisesrc=duration=10:amplitude=0.1",
                "-c:v", "libx264",
                "-c:a", "aac",
                output_file
            ]
            
            subprocess.run(cmd, check=True)
            
            # Create test recording entry
            recording_id = str(uuid.uuid4())
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Save to database
            save_recording(
                recording_id=recording_id,
                user_id=user_id,
                platform="tiktok",
                target=f"@{username} (TEST)",
                status="completed",
                start_time=start_time,
                end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                file_path=output_file,
                file_size=get_file_size(output_file),
                quality="HD (TEST)",
                original_link=f"TEST:{username}"
            )
            
            # Generate thumbnail
            thumbnail = generate_thumbnail(output_file)
            
            # Send notification
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ <b>Test Recording Completed for {platform.upper()}: {username}</b>\n\n"
                     f"ID: <code>{recording_id}</code>\n"
                     f"File: <code>{os.path.basename(output_file)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬇️ Download", callback_data=f"download_{recording_id}")],
                    [InlineKeyboardButton("📋 Lihat Rekaman", callback_data=f"view_{recording_id}")]
                ])
            )
            
            # Send thumbnail if available
            if thumbnail and os.path.exists(thumbnail):
                with open(thumbnail, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo,
                        caption=f"Test recording thumbnail for {username}"
                    )
                    
        else:  # bigo
            # Similar test for Bigo
            current_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            output_file = os.path.join(DOWNLOAD_PATH, f"bigo_{username}_{current_time}_test.mp4")
            
            # Create test video file with different pattern
            import subprocess
            cmd = [
                "ffmpeg", 
                "-f", "lavfi", 
                "-i", "mandelbrot=duration=10:size=1280x720:rate=30",
                "-f", "lavfi",
                "-i", "anoisesrc=duration=10:amplitude=0.1",
                "-c:v", "libx264",
                "-c:a", "aac",
                output_file
            ]
            
            subprocess.run(cmd, check=True)
            
            # Create test recording entry
            recording_id = str(uuid.uuid4())
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Save to database
            save_recording(
                recording_id=recording_id,
                user_id=user_id,
                platform="bigo",
                target=f"{username} (TEST)",
                status="completed",
                start_time=start_time,
                end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                file_path=output_file,
                file_size=get_file_size(output_file),
                quality="HD (TEST)",
                original_link=f"TEST:{username}"
            )
            
            # Generate thumbnail
            thumbnail = generate_thumbnail(output_file)
            
            # Send notification
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ <b>Test Recording Completed for {platform.upper()}: {username}</b>\n\n"
                     f"ID: <code>{recording_id}</code>\n"
                     f"File: <code>{os.path.basename(output_file)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬇️ Download", callback_data=f"download_{recording_id}")],
                    [InlineKeyboardButton("📋 Lihat Rekaman", callback_data=f"view_{recording_id}")]
                ])
            )
            
            # Send thumbnail if available
            if thumbnail and os.path.exists(thumbnail):
                with open(thumbnail, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=photo,
                        caption=f"Test recording thumbnail for {username}"
                    )
        
        # Update message
        await update.callback_query.message.edit_text(
            f"✅ <b>Test recording selesai untuk {platform.upper()}: {username}</b>\n\n"
            f"Silakan cek daftar rekaman untuk melihat hasilnya.",
            reply_markup=get_account_details_keyboard(account_id),
            parse_mode=ParseMode.HTML
        )
        
    except Exception as e:
        logger.error(f"Error in handle_test_record: {str(e)}")
        await update.callback_query.message.edit_text(
            f"❌ <b>Terjadi kesalahan saat test recording:</b> {str(e)}",
            reply_markup=get_account_details_keyboard(account_id),
            parse_mode=ParseMode.HTML
        )

async def handle_refresh_live(update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: int) -> None:
    """Handler for refreshing live status"""
    # Basically the same as check_now, but with different messaging
    await handle_check_now(update, context, account_id)

# ========== MAIN FUNCTION ==========

def main() -> None:
    """Start the bot with enhanced setup and monitoring"""
    try:
        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, lambda sig, frame: cleanup_on_shutdown())
        signal.signal(signal.SIGTERM, lambda sig, frame: cleanup_on_shutdown())
        
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
        
        # Try to recover any active recordings
        try:
            recover_active_recordings()
        except Exception as e:
            logger.error(f"Error recovering active recordings: {str(e)}")
        
        # Start background tasks
        async def start_background_tasks():
            # Start notification processor
            asyncio.create_task(process_notifications(application))
            # Start account monitor
            asyncio.create_task(run_account_monitor())
            # Start process checker
            asyncio.create_task(check_running_processes())
        
        # Schedule background tasks
        application.job_queue.run_once(
            lambda context: asyncio.create_task(start_background_tasks()), 
            when=5  # Start after 5 seconds
        )
        
        # Log startup message
        logger.info("Bot started. Press Ctrl+C to stop.")
        
        # Start the Bot with all updates
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            pool_timeout=30,
            read_timeout=30,
            write_timeout=30
        )
    
    except Exception as e:
        logger.error(f"Error in main function: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        cleanup_on_shutdown()
        raise

def recover_active_recordings():
    """Try to recover any active recordings from database"""
    logger.info("Attempting to recover active recordings...")
    
    # Get recordings with 'recording' status
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM recordings WHERE status = 'recording' OR status = 'processing' OR status = 'stopping'")
    recordings = cursor.fetchall()
    
    conn.close()
    
    recovered = 0
    
    for recording in recordings:
        recording_id = recording["id"]
        user_id = recording["user_id"]
        platform = recording["platform"]
        target = recording["target"]
        file_path = recording["file_path"]
        
        logger.info(f"Marking recording {recording_id} ({platform}: {target}) as 'interrupted'")
        
        # Update status to interrupted
        update_recording_status(
            recording_id=recording_id,
            status="interrupted",
            end_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        
        recovered += 1
    
    # Also check monitored accounts with active recordings
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM monitored_accounts WHERE current_recording_id IS NOT NULL")
    accounts = cursor.fetchall()
    
    conn.close()
    
    for account in accounts:
        account_id = account["id"]
        current_recording_id = account["current_recording_id"]
        
        # Reset recording ID
        update_account_recording_id(account_id, None)
        logger.info(f"Reset current_recording_id for account {account_id}")
    
    logger.info(f"Recovery completed: {recovered} recordings marked as interrupted")

if __name__ == "__main__":
    main()
