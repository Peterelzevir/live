import os
import logging
import sqlite3
import time
import asyncio
import subprocess
import threading
import queue
import json
import re
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Konfigurasi logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Konfigurasi bot
TELEGRAM_BOT_TOKEN = "7839177497:AAFidAiXoNIJNMzby0-TbsH_FhavI_4w_eo"  # Replace with your new valid token
ADMIN_IDS = [1501581833]  # ID admin Telegram Anda
DEFAULT_RECORDING_QUALITY = "720p"  # Kualitas rekaman default
RECORDING_DIR = "recordings"  # Direktori untuk menyimpan hasil rekaman
CHECK_INTERVAL = 60  # Interval pengecekan dalam detik (1 menit)
BOT_VERSION = "1.3.0"  # Versi bot untuk tracking

# Daftar user agent untuk rotasi
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/123.0.6312.52 Mobile/15E148 Safari/604.1",
]

# Buat direktori rekaman jika belum ada
os.makedirs(RECORDING_DIR, exist_ok=True)

# Buat direktori untuk logs
LOGS_DIR = os.path.join(RECORDING_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# Konfigurasi database
DB_FILE = "tiktok_monitor.db"

def init_database():
    """Inisialisasi database SQLite untuk menyimpan data akun pantauan dan rekaman."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Tabel untuk akun yang dipantau
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS monitored_accounts (
        username TEXT PRIMARY KEY,
        added_by INTEGER,
        added_at TIMESTAMP
    )
    ''')
    
    # Tabel untuk rekaman
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS recordings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        start_time TIMESTAMP,
        end_time TIMESTAMP,
        status TEXT,
        file_path TEXT,
        quality TEXT
    )
    ''')
    
    # Tabel untuk pengaturan bot
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')
    
    # Tabel untuk log deteksi live
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS live_detection_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        timestamp TIMESTAMP,
        is_live BOOLEAN,
        detection_method TEXT,
        details TEXT
    )
    ''')
    
    # Masukkan pengaturan default jika belum ada
    cursor.execute('''
    INSERT OR IGNORE INTO settings (key, value)
    VALUES ('recording_quality', ?)
    ''', (DEFAULT_RECORDING_QUALITY,))
    
    conn.commit()
    conn.close()

# Helper function untuk logging ke database
def log_live_detection(username, is_live, method, details=""):
    """Log hasil deteksi live ke database untuk debugging."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO live_detection_logs (username, timestamp, is_live, detection_method, details) VALUES (?, ?, ?, ?, ?)",
            (username, datetime.now(), is_live, method, details)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging detection: {e}")

# Class untuk mengelola monitor dan rekaman TikTok
class TikTokMonitor:
    def __init__(self, app):
        self.app = app
        self.active_recordings = {}  # Dict[str, dict]
        self.recording_processes = {}  # Dict[str, subprocess.Popen]
        self.stop_event = threading.Event()
        self.monitor_thread = None
        self.notification_queue = queue.Queue()
        self.last_check_status = {}  # Menyimpan status terakhir checks
        self.last_activity_time = time.time()  # Waktu aktivitas terakhir
        self.error_count = 0  # Menghitung error konsekutif
        self.detection_history = {}  # Menyimpan riwayat deteksi untuk menghindari flapping
        self.active_callbacks = set()  # Track active button callbacks to prevent duplicates
        
    async def initialize(self):
        """Inisialisasi dan mulai memantau semua akun yang tersimpan di database."""
        logger.info("Initializing TikTok monitor...")
        
        # Mulai thread monitoring dengan watchdog
        await self._start_monitoring_thread()
        
        # Buat task untuk memproses notifikasi
        notification_task = asyncio.create_task(self._process_notifications())
        
        # Buat task untuk watchdog
        watchdog_task = asyncio.create_task(self._watchdog())
        
        accounts = self.get_monitored_accounts()
        logger.info(f"Initialized monitoring for {len(accounts)} accounts: {accounts}")
        
        # Kirim pesan startup ke admin
        for admin_id in ADMIN_IDS:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id,
                    text=f"🤖 Bot TikTok Monitor v{BOT_VERSION} telah aktif!\n\nMemantau {len(accounts)} akun."
                )
            except Exception as e:
                logger.error(f"Error sending startup message: {e}")
        
        # Kombinasikan task dan kembalikan
        return asyncio.gather(notification_task, watchdog_task)
        
    async def _start_monitoring_thread(self):
        """Memulai thread monitoring dengan watchdog."""
        # Pastikan tidak ada thread yang berjalan
        if self.monitor_thread and self.monitor_thread.is_alive():
            logger.info("Stopping existing monitoring thread before starting a new one")
            old_stop_event = self.stop_event
            old_stop_event.set()
            self.monitor_thread.join(timeout=5)
            
        # Reset stop event
        self.stop_event = threading.Event()
        
        # Mulai thread baru
        self.monitor_thread = threading.Thread(target=self._monitoring_thread_wrapper, daemon=True)
        self.monitor_thread.start()
        logger.info("Monitoring thread started with watchdog")
        
    def _monitoring_thread_wrapper(self):
        """Wrapper untuk monitoring thread yang memungkinkan restart otomatis."""
        while not self.stop_event.is_set():
            try:
                # Jalankan loop monitoring
                needs_restart = self._monitoring_loop()
                
                # Jika perlu restart, tunggu sebentar lalu mulai ulang
                if needs_restart and not self.stop_event.is_set():
                    logger.info("Monitoring thread restarting...")
                    time.sleep(5)  # Tunggu 5 detik sebelum restart
                    continue
                else:
                    # Loop berhenti normal, keluar dari wrapper
                    break
                    
            except Exception as e:
                logger.critical(f"Fatal error in monitoring thread: {e}")
                if not self.stop_event.is_set():
                    # Jika tidak diminta berhenti, coba lagi setelah delay
                    time.sleep(10)
                    continue
                else:
                    break
                    
        logger.info("Monitoring thread wrapper exiting")
        
    async def _watchdog(self):
        """Watchdog untuk memastikan thread monitoring tetap berjalan."""
        while not self.stop_event.is_set():
            try:
                # Periksa apakah thread monitoring masih hidup
                if self.monitor_thread and not self.monitor_thread.is_alive():
                    logger.critical("Monitoring thread died, restarting it")
                    # Kirim notifikasi
                    self.notification_queue.put({
                        "type": "error",
                        "message": "Thread pemantauan mati. Menghidupkan ulang thread."
                    })
                    # Restart thread
                    await self._start_monitoring_thread()
                
                # Periksa jika tidak ada aktivitas dalam waktu lama
                if time.time() - self.last_activity_time > 600:  # 10 menit
                    logger.critical("No activity for 10 minutes, restarting monitoring thread")
                    # Kirim notifikasi
                    self.notification_queue.put({
                        "type": "error",
                        "message": "Tidak ada aktivitas selama 10 menit. Menghidupkan ulang thread pemantauan."
                    })
                    
                    # Paksa restart thread
                    if self.monitor_thread and self.monitor_thread.is_alive():
                        # Tidak bisa menghentikan thread langsung, jadi tandai untuk restart
                        self.stop_event.set()
                        time.sleep(2)
                        await self._start_monitoring_thread()
                    else:
                        await self._start_monitoring_thread()
                    
                    # Reset waktu aktivitas
                    self.last_activity_time = time.time()
                
                # Tunggu interval watchdog
                await asyncio.sleep(30)  # Periksa setiap 30 detik
                
            except Exception as e:
                logger.error(f"Error in watchdog: {e}")
                await asyncio.sleep(60)  # Jika error, tunggu lebih lama
    
    def get_monitored_accounts(self) -> List[str]:
        """Dapatkan semua akun TikTok yang dipantau dari database."""
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT username FROM monitored_accounts")
            accounts = [row[0] for row in cursor.fetchall()]
            conn.close()
            return accounts
        except Exception as e:
            logger.error(f"Error getting monitored accounts: {e}")
            return []
    
    def add_account(self, username: str, added_by: int) -> bool:
        """Tambahkan akun TikTok ke daftar pantauan."""
        try:
            # Bersihkan username dari karakter @
            username = username.strip().replace("@", "")
            
            # Validasi username
            if not self._validate_tiktok_username(username):
                return False
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO monitored_accounts (username, added_by, added_at) VALUES (?, ?, ?)",
                (username, added_by, datetime.now())
            )
            conn.commit()
            conn.close()
            
            logger.info(f"Added account {username} to monitoring list")

            # Segera cek status live setelah menambahkan akun
            is_live = self._check_if_live(username)
            if is_live:
                self.notification_queue.put({
                    "type": "live_start",
                    "username": username
                })
                self.start_recording(username)
                
            return True
        except Exception as e:
            logger.error(f"Error menambahkan akun {username}: {e}")
            return False
    
    def remove_account(self, username: str) -> bool:
        """Hapus akun TikTok dari daftar pantauan."""
        try:
            # Hentikan rekaman jika ada
            if username in self.recording_processes:
                self.stop_recording(username)
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM monitored_accounts WHERE username = ?", (username,))
            conn.commit()
            conn.close()
            
            # Hapus dari riwayat deteksi dan status terakhir
            if username in self.detection_history:
                del self.detection_history[username]
            if username in self.last_check_status:
                del self.last_check_status[username]
            
            logger.info(f"Removed account {username} from monitoring list")
            return True
        except Exception as e:
            logger.error(f"Error menghapus akun {username}: {e}")
            return False
    
    def _validate_tiktok_username(self, username: str) -> bool:
        """Validasi apakah username TikTok valid."""
        if not username or len(username) < 2:
            return False
        
        # Validasi karakter yang diperbolehkan
        allowed_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._")
        if not all(c in allowed_chars for c in username):
            return False
            
        return True
    
    def _monitoring_loop(self):
        """Loop untuk memantau akun TikTok secara berkala."""
        logger.info("Monitoring thread started")
        consecutive_errors = 0
        
        while not self.stop_event.is_set():
            try:
                # Update waktu aktivitas terakhir
                self.last_activity_time = time.time()
                
                accounts = self.get_monitored_accounts()
                if not accounts:
                    logger.info("No accounts to monitor, waiting...")
                    time.sleep(CHECK_INTERVAL)
                    consecutive_errors = 0  # Reset error counter
                    continue
                
                # Periksa setiap akun dengan interval 5 detik antar akun untuk menghindari rate limiting
                for username in accounts:
                    if self.stop_event.is_set():
                        break  # Periksa lagi jika diminta berhenti
                        
                    try:
                        # Cek status live dengan lebih banyak verifikasi
                        is_live = self._check_if_live(username)
                        logger.info(f"Checked {username}: {'LIVE' if is_live else 'NOT LIVE'}")
                        
                        # Simpan status sebelumnya
                        was_live = self.last_check_status.get(username, False)
                        
                        # Gunakan voting sistem dengan history untuk mencegah flapping
                        if username not in self.detection_history:
                            self.detection_history[username] = []
                        
                        # Tambahkan hasil deteksi baru ke history
                        self.detection_history[username].append(is_live)
                        
                        # Batasi history hanya 3 entri terakhir
                        self.detection_history[username] = self.detection_history[username][-3:]
                        
                        # Voting: Mayoritas wins (2 dari 3)
                        if len(self.detection_history[username]) >= 3:
                            majority_vote = sum(self.detection_history[username]) >= 2
                            is_live = majority_vote
                            
                        # Perubahan status: offline -> live
                        if is_live and not was_live:
                            logger.info(f"Status change detected: {username} is now LIVE")
                            # Verifikasi double bahwa akun benar-benar live
                            confirmed_live = self._verify_live_status(username)
                            
                            if confirmed_live:
                                # Tambahkan notifikasi ke queue
                                self.notification_queue.put({
                                    "type": "live_start",
                                    "username": username
                                })
                                
                                # Mulai merekam
                                self.start_recording(username)
                                
                                # Update status terakhir
                                self.last_check_status[username] = True
                            else:
                                logger.warning(f"False positive: {username} failed verification")
                                # Update detection history setelah verifikasi gagal
                                self.detection_history[username][-1] = False
                        
                        # Perubahan status: live -> offline
                        elif not is_live and was_live:
                            logger.info(f"Status change detected: {username} is no longer live")
                            
                            # Verifikasi double bahwa akun benar-benar tidak live
                            # Cek sekali lagi dengan timeout yang lebih lama untuk memastikan
                            confirmed_offline = not self._verify_live_status(username, extra_timeout=True)
                            
                            if confirmed_offline:
                                # Tambahkan notifikasi ke queue
                                self.notification_queue.put({
                                    "type": "live_end",
                                    "username": username
                                })
                                
                                # Hentikan rekaman
                                self.stop_recording(username)
                                
                                # Update status terakhir
                                self.last_check_status[username] = False
                            else:
                                logger.warning(f"False negative: {username} is still live upon verification")
                                # Update detection history karena verifikasi menunjukkan masih live
                                self.detection_history[username][-1] = True
                        
                        # Tidak ada perubahan status, tapi tetap update
                        else:
                            # Update status terakhir hanya jika voting konsisten
                            if len(self.detection_history[username]) >= 3 and all(x == is_live for x in self.detection_history[username]):
                                self.last_check_status[username] = is_live
                        
                        # Reset error counter karena berhasil
                        consecutive_errors = 0
                        
                        # Tunggu 5 detik antara pengecekan akun untuk menghindari rate limiting
                        time.sleep(5)
                        
                    except Exception as e:
                        logger.error(f"Error memantau akun {username}: {e}")
                        # Tidak increment error counter untuk error per akun
                
                # Verifikasi proses rekaman yang sedang berjalan
                self._check_recording_processes()
                
                # Tunggu interval sebelum memeriksa kembali
                time.sleep(CHECK_INTERVAL)
            
            except Exception as e:
                consecutive_errors += 1
                self.error_count += 1
                logger.error(f"Error dalam monitoring loop: {e} (Error #{consecutive_errors})")
                
                if consecutive_errors >= 5:
                    logger.critical(f"Too many consecutive errors ({consecutive_errors}), restarting monitoring thread")
                    
                    # Kirim notifikasi ke admin via queue
                    self.notification_queue.put({
                        "type": "error",
                        "message": f"Monitoring thread mengalami {consecutive_errors} error berturut-turut. Thread akan direstart."
                    })
                    
                    # Restart thread dengan mengembalikan True (thread perlu direstart)
                    # Tapi pastikan thread saat ini berhenti dulu
                    return True
                
                # Tunggu sebentar sebelum mencoba lagi
                time.sleep(min(10 * consecutive_errors, 60))  # Makin banyak error, makin lama menunggu (maksimal 1 menit)
                
        logger.info("Monitoring thread stopped gracefully")
        return False  # Thread berhenti normal, tidak perlu direstart
    
    def _check_recording_processes(self):
        """Verifikasi bahwa semua proses rekaman berjalan dengan baik."""
        usernames_to_check = list(self.recording_processes.keys())
        
        for username in usernames_to_check:
            try:
                process = self.recording_processes[username]
                
                # Periksa apakah proses masih berjalan
                if process.poll() is not None:
                    # Proses berhenti tanpa diminta - ini error
                    logger.error(f"Recording process for {username} terminated unexpectedly with code {process.poll()}")
                    
                    # Tambahkan notifikasi
                    self.notification_queue.put({
                        "type": "recording_error",
                        "username": username,
                        "error_code": process.poll()
                    })
                    
                    # Hapus dari daftar recording dan proses
                    self.stop_recording(username)
                    
                    # Coba mulai ulang rekaman jika masih live
                    if self.last_check_status.get(username, False):
                        logger.info(f"Attempting to restart recording for {username}")
                        self.start_recording(username)
            
            except Exception as e:
                logger.error(f"Error checking recording process for {username}: {e}")

    async def _process_notifications(self):
        """Proses notifikasi dari queue dan kirim ke admin."""
        logger.info("Notification processing task started")
        consecutive_errors = 0
        
        while not self.stop_event.is_set():
            try:
                # Update waktu aktivitas terakhir
                self.last_activity_time = time.time()
                
                # Periksa queue untuk notifikasi (non-blocking)
                try:
                    notification = self.notification_queue.get_nowait()
                    logger.info(f"Processing notification: {notification}")
                    
                    if notification["type"] == "live_start":
                        await self.notify_admins(f"🟢 <b>{notification['username']}</b> sedang LIVE! Rekaman dimulai otomatis.")
                    
                    elif notification["type"] == "live_end":
                        await self.notify_admins(f"🔴 Livestream <b>{notification['username']}</b> telah berakhir. Rekaman selesai.")
                    
                    elif notification["type"] == "force_record":
                        await self.notify_admins(f"▶️ Rekaman untuk <b>{notification['username']}</b> dipaksa dimulai secara manual.")
                    
                    elif notification["type"] == "recording_error":
                        await self.notify_admins(f"⚠️ Error rekaman untuk <b>{notification['username']}</b> (kode: {notification['error_code']}). Mencoba ulang...")
                    
                    elif notification["type"] == "error":
                        await self.notify_admins(f"⚠️ ERROR: {notification['message']}")
                    
                    # Mark task as done
                    self.notification_queue.task_done()
                    
                    # Reset error counter karena berhasil
                    consecutive_errors = 0
                
                except queue.Empty:
                    # No notifications in queue
                    pass
                
                # Periksa jika thread monitoring sudah lama tidak aktif
                if time.time() - self.last_activity_time > 300:  # 5 menit
                    logger.warning("No monitoring activity for 5 minutes, sending notification")
                    await self.notify_admins("⚠️ Peringatan: Tidak ada aktivitas pemantauan selama 5 menit. Bot mungkin mengalami masalah.")
                    self.last_activity_time = time.time()  # Reset waktu
                
                # Tunggu sebentar sebelum memeriksa lagi
                await asyncio.sleep(1)
                
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error memproses notifikasi: {e} (Error #{consecutive_errors})")
                
                if consecutive_errors >= 10:
                    logger.critical("Too many errors in notification processing, restarting task")
                    # Kirim notifikasi emergency ke semua admin
                    for admin_id in ADMIN_IDS:
                        try:
                            await self.app.bot.send_message(
                                chat_id=admin_id,
                                text="⚠️ CRITICAL ERROR: Notification processing failed repeatedly. Bot might need manual restart."
                            )
                        except:
                            pass
                    
                    # Reset error counter
                    consecutive_errors = 0
                
                # Tunggu lebih lama jika error konsekutif
                await asyncio.sleep(5 + (consecutive_errors * 2))
    
    def _check_if_live(self, username: str) -> bool:
        """Cek apakah akun TikTok sedang live menggunakan multiple methods."""
        try:
            # Format URL TikTok Live
            tiktok_url = f"https://www.tiktok.com/@{username}/live"
            
            # Simpan hasil dari setiap metode
            results = {}
            
            # 1. Cek dengan yt-dlp
            yt_dlp_result = self._check_live_with_ytdlp(username, tiktok_url)
            results["yt-dlp"] = yt_dlp_result
            
            # 2. Cek dengan curl
            curl_result = self._check_live_with_curl(username, tiktok_url)
            results["curl"] = curl_result
            
            # 3. Cek dengan info JSON (jika metode lain berbeda hasilnya)
            if yt_dlp_result != curl_result:
                json_result = self._check_live_with_json(username, tiktok_url)
                results["json"] = json_result
            else:
                # Jika metode 1 dan 2 sepakat, gunakan hasil yang sama untuk json
                results["json"] = yt_dlp_result
            
            # Ambil keputusan berdasarkan voting
            live_votes = sum(1 for method, result in results.items() if result)
            total_votes = len(results)
            
            is_live = live_votes > (total_votes // 2)  # Majority wins
            
            # Log hasil deteksi untuk debugging
            detection_details = f"Methods: ytdlp={yt_dlp_result}, curl={curl_result}, json={results.get('json', False)}. Votes: {live_votes}/{total_votes}"
            log_live_detection(username, is_live, "multiple", detection_details)
            
            logger.info(f"Live detection for {username}: {detection_details} => {'LIVE' if is_live else 'NOT LIVE'}")
            
            return is_live
            
        except Exception as e:
            logger.error(f"Error checking if {username} is live: {e}")
            # Default ke nilai terakhir yang diketahui, atau False jika tidak ada
            return self.last_check_status.get(username, False)
            
    def _check_live_with_ytdlp(self, username: str, tiktok_url: str) -> bool:
        """Cek live dengan yt-dlp."""
        try:
            # Pilih user agent acak
            user_agent = random.choice(USER_AGENTS)
            
            cmd = [
                "yt-dlp", 
                "--no-check-certificate",
                "--no-warnings",
                "--skip-download",
                "--quiet",
                "--ignore-no-formats-error",
                "--ignore-config",
                "--force-ipv4",
                "--extractor-retries", "3",
                "--socket-timeout", "15",
                "--user-agent", user_agent,
                tiktok_url
            ]
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            stdout, stderr = process.communicate(timeout=20)
            
            # Sukses dengan kode 0 biasanya berarti live
            if process.returncode == 0:
                logger.info(f"Account {username} is LIVE (yt-dlp detected)")
                log_live_detection(username, True, "yt-dlp", "returncode=0")
                return True
            
            # Cek output dan stderr untuk tanda-tanda khusus
            if stdout:
                # Jika ada output yang mengandung "live", kemungkinan live
                if "live" in stdout.lower():
                    log_live_detection(username, True, "yt-dlp", f"stdout contains 'live': {stdout[:100]}")
                    return True
            
            # Cek error spesifik yang menunjukkan tidak live
            not_live_indicators = [
                "This video is not available",
                "HTTP Error 404", 
                "Este video no está disponible",
                "This account doesn't exist",
                "Requested format is not available"
            ]
            
            for indicator in not_live_indicators:
                if indicator in stderr:
                    logger.debug(f"{username} is NOT live (yt-dlp found: {indicator})")
                    log_live_detection(username, False, "yt-dlp", f"stderr contains '{indicator}'")
                    return False
            
            # Cek stdout untuk indikasi tidak live
            if "404" in stdout:
                logger.debug(f"{username} is NOT live (yt-dlp found 404 in stdout)")
                log_live_detection(username, False, "yt-dlp", "stdout contains 404")
                return False
                
            # Default ke false jika tidak ada indikasi jelas
            log_live_detection(username, False, "yt-dlp", f"default to false: rc={process.returncode}")
            return False
                
        except subprocess.TimeoutExpired:
            if 'process' in locals():
                process.kill()
            logger.warning(f"Timeout running yt-dlp for {username}")
            log_live_detection(username, False, "yt-dlp", "timeout")
            return False
        except Exception as e:
            logger.error(f"Error in yt-dlp check for {username}: {e}")
            log_live_detection(username, False, "yt-dlp", f"error: {str(e)}")
            return False
            
    def _check_live_with_curl(self, username: str, tiktok_url: str) -> bool:
        """Cek live dengan curl."""
        try:
            # Pilih user agent acak
            user_agent = random.choice(USER_AGENTS)
            
            # Gunakan cURL dengan header yang tepat
            curl_cmd = [
                "curl", 
                "-s",           # Silent mode
                "-L",           # Follow redirects
                "-A", user_agent,
                "--connect-timeout", "10",
                "-H", "Accept-Language: en-US,en;q=0.9",
                tiktok_url
            ]
            
            curl_process = subprocess.Popen(
                curl_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            curl_output, curl_error = curl_process.communicate(timeout=15)
            
            # 1. Pastikan output tidak kosong atau error
            if not curl_output or curl_process.returncode != 0:
                logger.debug(f"{username} curl check failed: empty response or error code {curl_process.returncode}")
                log_live_detection(username, False, "curl", f"empty output or error: rc={curl_process.returncode}")
                return False
                
            # 2. Cek frasa yang pasti hanya muncul di live stream
            live_indicators = [
                "isLiveNow\":true",
                "data-e2e=\"live-status\"",
                "\"isLive\":true",
                "LIVE_ROOM_INFO",
                "\"roomID\":",
                "\"liveStatusTag\"",
                "\"LiveStatusTag\"",
                "is-live",
                "\"liveRoom\"",
                "<title>LIVE |"
            ]
            
            for indicator in live_indicators:
                if indicator in curl_output:
                    logger.info(f"Account {username} is LIVE (curl found indicator: {indicator})")
                    log_live_detection(username, True, "curl", f"found indicator: {indicator}")
                    return True
                    
            # 3. Cek halaman kosong atau error
            not_live_indicators = [
                "Este video no está disponible", 
                "This video is not available", 
                "Page not found", 
                "404 - Halaman Tidak Ditemukan",
                "doesn't exist",
                "temporarily unavailable",
                "couldn't find this account"
            ]
            
            for indicator in not_live_indicators:
                if indicator in curl_output:
                    logger.debug(f"{username} is NOT live (curl page indicates: {indicator})")
                    log_live_detection(username, False, "curl", f"found not-live indicator: {indicator}")
                    return False
                    
            # 4. Pengecekan meta tag atau struktur HTML untuk live
            if re.search(r'<meta\s+property="og:url"\s+content="[^"]*?/live"', curl_output):
                logger.info(f"Account {username} is LIVE (detected with curl meta tag)")
                log_live_detection(username, True, "curl", "meta tag indicates live")
                return True
            
            # 5. Pengecekan judul halaman
            title_match = re.search(r'<title>(.*?)</title>', curl_output)
            if title_match:
                title = title_match.group(1)
                if "LIVE" in title and f"@{username}" in title:
                    logger.info(f"Account {username} is LIVE (detected with curl title)")
                    log_live_detection(username, True, "curl", f"title indicates live: {title}")
                    return True
            
            # 6. Pengecekan tambahan untuk pattern TikTok baru
            if "liveMode" in curl_output or "LiveMode" in curl_output:
                logger.info(f"Account {username} is LIVE (detected with curl liveMode)")
                log_live_detection(username, True, "curl", "liveMode detected")
                return True
            
            # Default: tidak live jika tidak ada indikator yang jelas
            logger.debug(f"{username} is NOT live (curl found no live indicators)")
            log_live_detection(username, False, "curl", "no live indicators found")
            return False
            
        except subprocess.TimeoutExpired:
            if 'curl_process' in locals():
                curl_process.kill()
            logger.warning(f"Timeout running curl for {username}")
            log_live_detection(username, False, "curl", "timeout")
            return False
        except Exception as e:
            logger.error(f"Error in curl check for {username}: {e}")
            log_live_detection(username, False, "curl", f"error: {str(e)}")
            return False
            
    def _check_live_with_json(self, username: str, tiktok_url: str) -> bool:
        """Cek live dengan mengambil info JSON."""
        try:
            # Pilih user agent acak
            user_agent = random.choice(USER_AGENTS)
            
            cmd = [
                "yt-dlp", 
                "--no-check-certificate",
                "--dump-json",
                "--skip-download",
                "--quiet",
                "--force-ipv4",
                "--socket-timeout", "15",
                "--user-agent", user_agent,
                tiktok_url
            ]
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            stdout, stderr = process.communicate(timeout=20)
            
            # Cek jika berhasil mendapatkan JSON
            if process.returncode == 0 and stdout.strip():
                try:
                    # Coba parse JSON
                    data = json.loads(stdout)
                    
                    # Cek indikator status live dalam data
                    live_indicators = ['is_live', 'live', 'livestream', 'isLive', 'is_livestream']
                    
                    for indicator in live_indicators:
                        if indicator in data and data[indicator]:
                            logger.info(f"Account {username} is LIVE (confirmed by JSON data)")
                            log_live_detection(username, True, "json", f"indicator found: {indicator}")
                            return True
                    
                    # Cek format video yang biasanya menunjukkan live
                    if 'format' in data:
                        format_str = data.get('format', '').lower()
                        if 'live' in format_str or 'dash' in format_str or 'hls' in format_str:
                            logger.info(f"Account {username} is LIVE (format contains live/hls)")
                            log_live_detection(username, True, "json", f"format indicates live: {format_str}")
                            return True
                    
                    # Cek URL yang biasanya mengandung 'live' untuk stream yang aktif
                    if 'url' in data:
                        url = data.get('url', '').lower()
                        if 'live' in url and ('m3u8' in url or '.ts' in url):
                            logger.info(f"Account {username} is LIVE (URL indicates live stream)")
                            log_live_detection(username, True, "json", "URL indicates live")
                            return True
                    
                    # Jika tidak menemukan indikator live
                    log_live_detection(username, False, "json", "no live indicators in JSON")
                    return False
                    
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse JSON for {username}")
                    log_live_detection(username, False, "json", "JSON parse error")
                    return False
            
            # Periksa jika error spesifik di stderr
            not_live_indicators = [
                "This video is not available",
                "HTTP Error 404", 
                "Este video no está disponible",
                "This account doesn't exist"
            ]
            
            for indicator in not_live_indicators:
                if indicator in stderr:
                    logger.debug(f"{username} is NOT live (json found: {indicator})")
                    log_live_detection(username, False, "json", f"stderr contains '{indicator}'")
                    return False
                    
            log_live_detection(username, False, "json", "default to false")
            return False
                
        except subprocess.TimeoutExpired:
            if 'process' in locals():
                process.kill()
            logger.warning(f"Timeout running JSON check for {username}")
            log_live_detection(username, False, "json", "timeout")
            return False
        except Exception as e:
            logger.error(f"Error in JSON check for {username}: {e}")
            log_live_detection(username, False, "json", f"error: {str(e)}")
            return False
    
    def _verify_live_status(self, username: str, extra_timeout: bool = False) -> bool:
        """Verifikasi ulang status live dengan metode alternatif."""
        try:
            # Tunggu sedikit lebih lama untuk verifikasi
            time.sleep(3)
            
            # Format URL
            tiktok_url = f"https://www.tiktok.com/@{username}/live"
            verified_live = False
            
            # Metode 1: Verifikasi dengan ffprobe
            try:
                timeout = 25 if extra_timeout else 15
                ffmpeg_cmd = [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    "-user_agent", random.choice(USER_AGENTS),
                    tiktok_url
                ]
                
                ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                output, error = ffmpeg_process.communicate(timeout=timeout)
                
                # Jika berhasil akses stream, maka live
                if ffmpeg_process.returncode == 0 or "description" in error or "Stream #0" in error:
                    logger.info(f"Account {username} is verified LIVE (ffprobe method)")
                    log_live_detection(username, True, "verification_ffprobe", "succeeded")
                    verified_live = True
                else:
                    logger.debug(f"FFprobe verification failed for {username}: {error}")
                    log_live_detection(username, False, "verification_ffprobe", f"failed: {error[:100]}")
            except Exception as e:
                logger.debug(f"FFprobe check error for {username}: {e}")
                log_live_detection(username, False, "verification_ffprobe", f"error: {str(e)}")
            
            # Metode 2: Verifikasi tambahan menggunakan streamlink
            try:
                timeout = 25 if extra_timeout else 15
                streamlink_cmd = [
                    "streamlink", 
                    "--stream-url",
                    "--stream-timeout", "10",
                    "--player-timeout", "10", 
                    "--retry-max", "1",
                    "--retry-streams", "2",
                    "--http-header", f"User-Agent={random.choice(USER_AGENTS)}",
                    tiktok_url, 
                    "best"
                ]
                
                streamlink_process = subprocess.Popen(
                    streamlink_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                
                output, error = streamlink_process.communicate(timeout=timeout)
                
                # Jika berhasil mendapatkan URL stream, maka live
                if streamlink_process.returncode == 0 and output.strip():
                    logger.info(f"Account {username} is verified LIVE (streamlink method)")
                    log_live_detection(username, True, "verification_streamlink", "succeeded")
                    verified_live = True
                else:
                    logger.debug(f"Streamlink verification failed for {username}: {error}")
                    log_live_detection(username, False, "verification_streamlink", f"failed: {error[:100]}")
            except Exception as e:
                logger.debug(f"Streamlink check error for {username}: {e}")
                log_live_detection(username, False, "verification_streamlink", f"error: {str(e)}")
            
            # Metode 3: Verifikasi dengan yt-dlp lagi tetapi dengan parameter berbeda
            try:
                timeout = 25 if extra_timeout else 15
                ytdlp_verify_cmd = [
                    "yt-dlp",
                    "--no-check-certificate",
                    "--list-formats",
                    "--no-warnings",
                    "--force-ipv4",
                    "--socket-timeout", "15",
                    "--user-agent", random.choice(USER_AGENTS),
                    tiktok_url
                ]
                
                ytdlp_verify_process = subprocess.Popen(
                    ytdlp_verify_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                
                output, error = ytdlp_verify_process.communicate(timeout=timeout)
                
                # Cek untuk format live stream seperti m3u8 atau hls
                live_format_indicators = ['m3u8', 'hls', 'dash', 'live']
                if ytdlp_verify_process.returncode == 0:
                    for indicator in live_format_indicators:
                        if indicator in output.lower():
                            logger.info(f"Account {username} is verified LIVE (yt-dlp formats method)")
                            log_live_detection(username, True, "verification_ytdlp_formats", f"found format: {indicator}")
                            verified_live = True
                            break
                else:
                    logger.debug(f"yt-dlp formats verification failed for {username}")
                    log_live_detection(username, False, "verification_ytdlp_formats", "failed")
            except Exception as e:
                logger.debug(f"yt-dlp formats check error for {username}: {e}")
                log_live_detection(username, False, "verification_ytdlp_formats", f"error: {str(e)}")
            
            return verified_live
            
        except Exception as e:
            logger.error(f"Error verifying live status for {username}: {e}")
            log_live_detection(username, False, "verification", f"error: {str(e)}")
            # Default ke hasil pengecekan sebelumnya jika verifikasi gagal
            return self.last_check_status.get(username, False)
    
    def start_recording(self, username: str):
        """Mulai merekam livestream TikTok."""
        try:
            # Periksa apakah sudah merekam
            if username in self.recording_processes:
                logger.info(f"Already recording {username}, skipping")
                return True
            
            # Lakukan verifikasi ulang sebelum memulai rekaman
            if not self._verify_live_status(username):
                logger.warning(f"Verified {username} is NOT actually live, cancelling recording")
                # Update status terakhir
                self.last_check_status[username] = False
                return False
                
            # Dapatkan kualitas rekaman dari pengaturan
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = 'recording_quality'")
            quality_result = cursor.fetchone()
            quality = quality_result[0] if quality_result else DEFAULT_RECORDING_QUALITY
            
            # Buat entri rekaman baru
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            filename = f"{username}_{timestamp}.mp4"
            file_path = os.path.join(RECORDING_DIR, filename)
            
            cursor.execute(
                "INSERT INTO recordings (username, start_time, status, file_path, quality) VALUES (?, ?, ?, ?, ?)",
                (username, now, "recording", file_path, quality)
            )
            recording_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            # Format URL TikTok Live
            tiktok_url = f"https://www.tiktok.com/@{username}/live"
            
            # Parameter yt-dlp yang lebih baik untuk merekam
            cmd = [
                "yt-dlp", 
                "--no-check-certificate",
                "--hls-use-mpegts",         # Gunakan format MPEG-TS untuk HLS
                "--no-part",                # Gunakan file normal daripada .part file
                "--no-mtime",               # Jangan ubah waktu modifikasi file
                "--no-warnings",            # Kurangi pesan warning
                "--user-agent", random.choice(USER_AGENTS),
                "--retries", "10",          # Coba ulang jika ada error
                "--fragment-retries", "10", # Coba ulang download fragment
                "--force-ipv4",             # Paksa IPv4
                "--extractor-retries", "3", # Coba beberapa kali jika extractor gagal
                "-o", file_path,
                tiktok_url
            ]
            
            # Log command yang dijalankan
            logger.info(f"Starting recording with command: {' '.join(cmd)}")
            
            # Jalankan proses dengan output dialihkan ke file log
            log_file_path = os.path.join(LOGS_DIR, f"{username}_{timestamp}_log.txt")
            log_file = open(log_file_path, "w")
            
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                text=True
            )
            
            # Simpan proses rekaman
            self.recording_processes[username] = process
            self.active_recordings[username] = {
                "id": recording_id,
                "file_path": file_path,
                "start_time": now,
                "quality": quality,
                "log_file": log_file,
                "log_file_path": log_file_path
            }
            
            logger.info(f"Started recording livestream for {username} with quality {quality}")
            return True
        except Exception as e:
            logger.error(f"Error memulai rekaman {username}: {e}")
            return False
    
    def stop_recording(self, username: str):
        """Hentikan rekaman livestream TikTok."""
        if username in self.recording_processes:
            try:
                # Hentikan proses rekaman
                process = self.recording_processes[username]
                logger.info(f"Stopping recording for {username}")
                
                # Kirim SIGTERM untuk berhenti dengan baik
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Termination timeout for {username}, sending SIGKILL")
                    process.kill()
                
                # Update database
                if username in self.active_recordings:
                    recording = self.active_recordings[username]
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE recordings SET end_time = ?, status = ? WHERE id = ?",
                        (datetime.now(), "completed", recording["id"])
                    )
                    conn.commit()
                    conn.close()
                    
                    # Tutup file log
                    if "log_file" in recording and not recording["log_file"].closed:
                        recording["log_file"].close()
                    
                    # Verifikasi file terekam dan punya ukuran > 0
                    if os.path.exists(recording["file_path"]) and os.path.getsize(recording["file_path"]) > 0:
                        logger.info(f"Recording for {username} completed successfully: {recording['file_path']}")
                    else:
                        logger.error(f"Recording file missing or empty: {recording['file_path']}")
                        # Coba salin log file sebagai bukti
                        if "log_file_path" in recording and os.path.exists(recording["log_file_path"]):
                            with open(recording["log_file_path"], "r") as log:
                                log_content = log.read()
                                logger.error(f"Recording log: {log_content}")
                    
                    # Hapus dari daftar rekaman aktif
                    del self.active_recordings[username]
                
                # Hapus dari daftar proses rekaman
                del self.recording_processes[username]
                
                logger.info(f"Stopped recording livestream {username}")
                return True
            except Exception as e:
                logger.error(f"Error menghentikan rekaman {username}: {e}")
                return False
        return False
    
    async def notify_admins(self, message: str):
        """Kirim notifikasi ke semua admin."""
        logger.info(f"Sending notification to admins: {message}")
        success = False
        
        for admin_id in ADMIN_IDS:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    parse_mode="HTML"
                )
                success = True
                logger.info(f"Notification sent to admin {admin_id}")
            except Exception as e:
                logger.error(f"Error mengirim notifikasi ke admin {admin_id}: {e}")
                
        return success
    
    def get_active_recordings(self) -> List[Dict]:
        """Dapatkan daftar rekaman yang sedang aktif."""
        result = []
        for username, recording in self.active_recordings.items():
            result.append({
                "username": username,
                "start_time": recording["start_time"],
                "quality": recording["quality"],
                "id": recording["id"],
                "file_path": recording["file_path"]
            })
        return result
    
    def get_recording_history(self, limit: int = 10) -> List[Dict]:
        """Dapatkan riwayat rekaman yang sudah selesai."""
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM recordings WHERE status = 'completed' ORDER BY end_time DESC LIMIT ?",
                (limit,)
            )
            recordings = [dict(row) for row in cursor.fetchall()]
            conn.close()
            
            # Verifikasi file ada
            for rec in recordings:
                rec["file_exists"] = os.path.exists(rec["file_path"]) and os.path.getsize(rec["file_path"]) > 0
                
            return recordings
        except Exception as e:
            logger.error(f"Error getting recording history: {e}")
            return []
    
    def set_recording_quality(self, quality: str) -> bool:
        """Ubah kualitas rekaman default."""
        try:
            if quality not in ["480p", "720p", "1080p"]:
                return False
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = 'recording_quality'",
                (quality,)
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error mengubah kualitas rekaman: {e}")
            return False
    
    def get_recording_file(self, recording_id: int) -> Optional[Tuple[str, str]]:
        """Dapatkan file rekaman berdasarkan ID."""
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_path, username FROM recordings WHERE id = ?",
                (recording_id,)
            )
            result = cursor.fetchone()
            conn.close()
            
            if result and os.path.exists(result[0]) and os.path.getsize(result[0]) > 0:
                return (result[0], result[1])
                
            # Jika file tidak ada, coba cari alternatif
            if result:
                # Coba cari file dengan pattern yang sama (mungkin nama berubah sedikit)
                base_dir = os.path.dirname(result[0])
                base_name = os.path.basename(result[0])
                username = result[1]
                
                # Cari file dengan username yang sama
                matching_files = [f for f in os.listdir(base_dir) 
                                if f.startswith(username) and f.endswith('.mp4')]
                
                if matching_files:
                    # Ambil file terbaru
                    newest_file = max(matching_files, 
                                    key=lambda f: os.path.getmtime(os.path.join(base_dir, f)))
                    file_path = os.path.join(base_dir, newest_file)
                    
                    # Update database dengan path yang benar
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE recordings SET file_path = ? WHERE id = ?",
                        (file_path, recording_id)
                    )
                    conn.commit()
                    conn.close()
                    
                    logger.info(f"Found alternative file for recording {recording_id}: {file_path}")
                    return (file_path, username)
            
            return None
        except Exception as e:
            logger.error(f"Error mendapatkan file rekaman {recording_id}: {e}")
            return None
    
    def force_check_all(self):
        """Paksa pengecekan semua akun sekarang."""
        accounts = self.get_monitored_accounts()
        results = {}
        
        for username in accounts:
            # Reset detection history untuk mendapatkan hasil fresh
            if username in self.detection_history:
                self.detection_history[username] = []
                
            is_live = self._check_if_live(username)
            results[username] = is_live
            
            # Update status
            was_live = self.last_check_status.get(username, False)
            
            # Perubahan status: offline -> live
            if is_live and not was_live:
                # Verifikasi double
                confirmed_live = self._verify_live_status(username)
                
                if confirmed_live:
                    self.notification_queue.put({
                        "type": "live_start",
                        "username": username
                    })
                    self.start_recording(username)
                    self.last_check_status[username] = True
                else:
                    # False positive, jangan ubah status
                    results[username] = False
            
            # Perubahan status: live -> offline
            elif not is_live and was_live:
                # Verifikasi double
                confirmed_offline = not self._verify_live_status(username, extra_timeout=True)
                
                if confirmed_offline:
                    self.notification_queue.put({
                        "type": "live_end",
                        "username": username
                    })
                    self.stop_recording(username)
                    self.last_check_status[username] = False
                else:
                    # False negative, jangan ubah status
                    results[username] = True
            
            # Update status terakhir sesuai hasil
            self.last_check_status[username] = results[username]
            
            # Tunggu sebentar antara cek untuk menghindari rate limiting
            time.sleep(2)
        
        return results
    
    def force_record(self, username: str) -> bool:
        """Paksa mulai merekam akun tertentu, terlepas dari status live."""
        try:
            # Pastikan akun ada dalam daftar pantauan
            accounts = self.get_monitored_accounts()
            if username not in accounts:
                logger.error(f"Cannot force record: {username} not in monitored accounts")
                return False
                
            # Mulai merekam
            if self.start_recording(username):
                # Update status
                self.last_check_status[username] = True
                
                # Kirim notifikasi
                self.notification_queue.put({
                    "type": "force_record",
                    "username": username
                })
                
                return True
            else:
                return False
        except Exception as e:
            logger.error(f"Error force recording {username}: {e}")
            return False
    
    def stop_monitoring(self):
        """Hentikan seluruh pemantauan."""
        self.stop_event.set()
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=10)
        
        # Hentikan semua rekaman yang masih berjalan
        for username in list(self.recording_processes.keys()):
            self.stop_recording(username)

# Command handler bot Telegram
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk command /start."""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Maaf, bot ini hanya dapat digunakan oleh admin.")
        return
    
    # Reset any user data state
    if "waiting_for" in context.user_data:
        del context.user_data["waiting_for"]
    
    keyboard = [
        [
            InlineKeyboardButton("➕ Tambah Akun", callback_data="add_account"),
            InlineKeyboardButton("🗑️ Hapus Akun", callback_data="remove_account"),
        ],
        [
            InlineKeyboardButton("📋 Daftar Akun", callback_data="list_accounts"),
            InlineKeyboardButton("📊 Rekaman Aktif", callback_data="active_recordings"),
        ],
        [
            InlineKeyboardButton("📚 Riwayat Rekaman", callback_data="recording_history"),
            InlineKeyboardButton("⚙️ Pengaturan", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("🔄 Cek Status Sekarang", callback_data="force_check"),
            InlineKeyboardButton("▶️ Paksa Rekam", callback_data="force_record"),
        ],
        [
            InlineKeyboardButton("🔄 Restart Bot", callback_data="restart_bot"),
            InlineKeyboardButton("⏹️ Stop Bot", callback_data="stop_bot"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"Selamat datang di Bot Pemantau & Perekam TikTok Live v{BOT_VERSION}!\n\n"
        "Bot ini akan memantau akun TikTok dan otomatis merekam saat mereka sedang live.\n"
        "Silakan pilih opsi di bawah:",
        reply_markup=reply_markup
    )

async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk command /restart - memulai ulang bot."""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Maaf, bot ini hanya dapat digunakan oleh admin.")
        return
    
    await update.message.reply_text("⚙️ Memulai ulang bot... Bot akan kembali online dalam beberapa detik.")
    
    # Ini akan mengakhiri aplikasi dengan kode keluar 42
    # Anda harus membuat script wrapper yang akan restart program jika exit code = 42
    logger.info("Admin requested restart. Exiting with code 42 for restart.")
    
    # Matikan semua pemantauan sebelum restart
    monitor = context.bot_data.get("monitor")
    if monitor:
        monitor.stop_monitoring()
    
    # Beri waktu untuk cleanup
    await asyncio.sleep(2)
    
    # Siapkan restart
    os._exit(42)  # Hard exit yang akan ditangkap oleh script wrapper

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk callback button."""
    global CHECK_INTERVAL  # Deklarasi global di awal fungsi
    
    query = update.callback_query
    user_id = query.from_user.id
    callback_id = query.id
    
    # Cek jika user bukan admin
    if user_id not in ADMIN_IDS:
        await query.answer("Maaf, bot ini hanya dapat digunakan oleh admin.")
        return
    
    # Cek jika callback sudah diproses (untuk mencegah double klik)
    monitor = context.bot_data.get("monitor")
    if monitor and callback_id in monitor.active_callbacks:
        await query.answer("Permintaan sedang diproses, harap tunggu...")
        return
    
    # Tambahkan callback ke daftar aktif
    if monitor:
        monitor.active_callbacks.add(callback_id)
    
    try:
        # Answer callback query
        await query.answer()
        
        action = query.data
        
        if action == "add_account":
            await query.edit_message_text(
                "Silakan kirim username TikTok yang ingin dipantau.\n"
                "Format: @username atau username saja"
            )
            context.user_data["waiting_for"] = "add_account"
        
        elif action == "remove_account":
            # Tampilkan daftar akun untuk dihapus
            monitor = context.bot_data["monitor"]
            accounts = monitor.get_monitored_accounts()
            
            if not accounts:
                await query.edit_message_text(
                    "Tidak ada akun yang dipantau saat ini.\n\n"
                    "Kembali ke menu utama: /start"
                )
                return
            
            keyboard = []
            for account in accounts:
                keyboard.append([InlineKeyboardButton(f"@{account}", callback_data=f"delete_{account}")])
            
            keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "Pilih akun yang ingin dihapus:",
                reply_markup=reply_markup
            )
        
        elif action.startswith("delete_"):
            username = action.replace("delete_", "")
            monitor = context.bot_data["monitor"]
            
            if monitor.remove_account(username):
                await query.edit_message_text(
                    f"✅ Akun @{username} berhasil dihapus dari daftar pantauan.\n\n"
                    "Kembali ke menu utama: /start"
                )
            else:
                await query.edit_message_text(
                    f"❌ Gagal menghapus akun @{username}.\n\n"
                    "Kembali ke menu utama: /start"
                )
        
        elif action == "list_accounts":
            # Tampilkan daftar akun yang dipantau
            monitor = context.bot_data["monitor"]
            accounts = monitor.get_monitored_accounts()
            
            if not accounts:
                message = "Tidak ada akun yang dipantau saat ini."
            else:
                message = "📋 Daftar Akun yang Dipantau:\n\n"
                for i, account in enumerate(accounts, 1):
                    # Cek status saat ini
                    status = "🟢 LIVE" if monitor.last_check_status.get(account, False) else "🔴 Tidak Live"
                    message += f"{i}. @{account} - {status}\n"
            
            keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup)
        
        elif action == "active_recordings":
            # Tampilkan daftar rekaman aktif
            monitor = context.bot_data["monitor"]
            recordings = monitor.get_active_recordings()
            
            if not recordings:
                message = "Tidak ada rekaman yang sedang berlangsung saat ini."
            else:
                message = "📊 Rekaman Aktif:\n\n"
                for i, rec in enumerate(recordings, 1):
                    duration = datetime.now() - rec["start_time"]
                    hours, remainder = divmod(duration.seconds, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    message += (
                        f"{i}. @{rec['username']}\n"
                        f"   ⏱️ Durasi: {hours:02}:{minutes:02}:{seconds:02}\n"
                        f"   🎬 Kualitas: {rec['quality']}\n"
                        f"   🆔 ID: {rec['id']}\n\n"
                    )
            
            keyboard = []
            for rec in recordings:
                keyboard.append([
                    InlineKeyboardButton(
                        f"⏹️ Stop @{rec['username']}", 
                        callback_data=f"stop_recording_{rec['username']}"
                    )
                ])
            
            keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="active_recordings")])
            keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup)
        
        elif action.startswith("stop_recording_"):
            username = action.replace("stop_recording_", "")
            monitor = context.bot_data["monitor"]
            
            monitor.stop_recording(username)
            await query.edit_message_text(
                f"✅ Rekaman untuk @{username} telah dihentikan.\n\n"
                "Kembali ke menu utama: /start"
            )
        
        elif action == "recording_history":
            # Tampilkan riwayat rekaman
            monitor = context.bot_data["monitor"]
            recordings = monitor.get_recording_history()
            
            if not recordings:
                message = "Belum ada riwayat rekaman."
            else:
                message = "📚 Riwayat Rekaman:\n\n"
                for i, rec in enumerate(recordings, 1):
                    start_time = datetime.fromisoformat(rec["start_time"])
                    end_time = datetime.fromisoformat(rec["end_time"]) if rec["end_time"] else None
                    
                    if end_time:
                        duration = end_time - start_time
                        hours, remainder = divmod(duration.seconds, 3600)
                        minutes, seconds = divmod(remainder, 60)
                        duration_str = f"{hours:02}:{minutes:02}:{seconds:02}"
                    else:
                        duration_str = "N/A"
                    
                    # Cek apakah file ada
                    file_status = "✅" if rec.get("file_exists", False) else "❌"
                    
                    message += (
                        f"{i}. @{rec['username']}\n"
                        f"   📅 Tanggal: {start_time.strftime('%d-%m-%Y')}\n"
                        f"   🕒 Waktu: {start_time.strftime('%H:%M:%S')}\n"
                        f"   ⏱️ Durasi: {duration_str}\n"
                        f"   🎬 Kualitas: {rec['quality']}\n"
                        f"   🆔 ID: {rec['id']} {file_status}\n\n"
                    )
            
            keyboard = []
            for rec in recordings:
                if rec.get("file_exists", False):
                    keyboard.append([
                        InlineKeyboardButton(
                            f"📥 Download @{rec['username']} ({rec['id']})", 
                            callback_data=f"download_{rec['id']}"
                        )
                    ])
            
            keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(message, reply_markup=reply_markup)
        
        elif action.startswith("download_"):
            recording_id = int(action.replace("download_", ""))
            monitor = context.bot_data["monitor"]
            
            file_info = monitor.get_recording_file(recording_id)
            if file_info:
                file_path, username = file_info
                await query.edit_message_text(f"⏳ Mengirim rekaman @{username}... Mohon tunggu.")
                
                try:
                    # Cek ukuran file
                    file_size = os.path.getsize(file_path)
                    if file_size > 50 * 1024 * 1024:  # 50 MB (batas Telegram)
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"⚠️ File rekaman terlalu besar ({file_size/1024/1024:.1f} MB) untuk dikirim via Telegram (batas 50 MB).\n"
                            f"File tersimpan di server: {file_path}\n\n"
                            "Kembali ke menu utama: /start"
                        )
                    else:
                        # Kirim file
                        with open(file_path, "rb") as video_file:
                            await context.bot.send_document(
                                chat_id=user_id,
                                document=video_file,
                                filename=os.path.basename(file_path),
                                caption=f"📹 Rekaman @{username}"
                            )
                        
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"✅ Rekaman @{username} berhasil dikirim.\n\n"
                            "Kembali ke menu utama: /start"
                        )
                except Exception as e:
                    logger.error(f"Error mengirim file rekaman: {e}")
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ Gagal mengirim rekaman: {str(e)}\n\n"
                        "Kembali ke menu utama: /start"
                    )
            else:
                await query.edit_message_text(
                    "❌ File rekaman tidak ditemukan atau kosong.\n\n"
                    "Kembali ke menu utama: /start"
                )
        
        elif action == "settings":
            # Tampilkan menu pengaturan
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = 'recording_quality'")
            quality = cursor.fetchone()[0]
            conn.close()
            
            keyboard = [
                [
                    InlineKeyboardButton("480p", callback_data="set_quality_480p"),
                    InlineKeyboardButton("720p", callback_data="set_quality_720p"),
                    InlineKeyboardButton("1080p", callback_data="set_quality_1080p"),
                ],
                [
                    InlineKeyboardButton("Interval: 1m", callback_data="set_interval_60"),
                    InlineKeyboardButton("Interval: 2m", callback_data="set_interval_120"),
                    InlineKeyboardButton("Interval: 5m", callback_data="set_interval_300"),
                ],
                [InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                f"⚙️ Pengaturan\n\n"
                f"Kualitas Rekaman Saat Ini: {quality}\n"
                f"Interval Pengecekan: {CHECK_INTERVAL} detik\n\n"
                f"Pilih pengaturan yang ingin diubah:",
                reply_markup=reply_markup
            )
        
        elif action.startswith("set_quality_"):
            quality = action.replace("set_quality_", "")
            monitor = context.bot_data["monitor"]
            
            if monitor.set_recording_quality(quality):
                await query.edit_message_text(
                    f"✅ Kualitas rekaman berhasil diubah menjadi {quality}.\n\n"
                    "Kembali ke menu utama: /start"
                )
            else:
                await query.edit_message_text(
                    f"❌ Gagal mengubah kualitas rekaman.\n\n"
                    "Kembali ke menu utama: /start"
                )
        
        elif action.startswith("set_interval_"):
            seconds = int(action.replace("set_interval_", ""))
            
            # Ubah interval global
            CHECK_INTERVAL = seconds
            
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("check_interval", str(seconds))
            )
            conn.commit()
            conn.close()
            
            await query.edit_message_text(
                f"✅ Interval pengecekan berhasil diubah menjadi {seconds} detik.\n\n"
                "Kembali ke menu utama: /start"
            )
            
        elif action == "force_check":
            monitor = context.bot_data["monitor"]
            await query.edit_message_text("⏳ Sedang memeriksa status semua akun... Mohon tunggu.")
            
            try:
                # Paksa pengecekan semua akun
                results = monitor.force_check_all()
                
                if not results:
                    message = "Tidak ada akun yang dipantau saat ini."
                else:
                    message = "📊 Hasil Pengecekan Status:\n\n"
                    for username, is_live in results.items():
                        status = "🟢 LIVE" if is_live else "🔴 Tidak Live"
                        message += f"@{username}: {status}\n"
                
                keyboard = [[InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(message, reply_markup=reply_markup)
            except Exception as e:
                logger.error(f"Error saat force check: {e}")
                await query.edit_message_text(
                    f"❌ Terjadi kesalahan saat memeriksa status: {str(e)}\n\n"
                    "Kembali ke menu utama: /start"
                )
                
        elif action == "force_record":
            # Tampilkan daftar akun untuk dipaksa merekam
            monitor = context.bot_data["monitor"]
            accounts = monitor.get_monitored_accounts()
            
            if not accounts:
                await query.edit_message_text(
                    "Tidak ada akun yang dipantau saat ini.\n\n"
                    "Kembali ke menu utama: /start"
                )
                return
            
            keyboard = []
            for account in accounts:
                keyboard.append([InlineKeyboardButton(f"@{account}", callback_data=f"force_record_{account}")])
            
            keyboard.append([InlineKeyboardButton("🔙 Kembali", callback_data="back_to_main")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "Pilih akun yang ingin dipaksa merekam:",
                reply_markup=reply_markup
            )
            
        elif action.startswith("force_record_"):
            username = action.replace("force_record_", "")
            monitor = context.bot_data["monitor"]
            
            if monitor.force_record(username):
                await query.edit_message_text(
                    f"✅ Rekaman untuk @{username} telah dipaksa dimulai.\n\n"
                    "Kembali ke menu utama: /start"
                )
            else:
                await query.edit_message_text(
                    f"❌ Gagal memulai rekaman untuk @{username}.\n\n"
                    "Kembali ke menu utama: /start"
                )
        
        elif action == "restart_bot":
            await query.edit_message_text("⚙️ Memulai ulang bot... Bot akan kembali online dalam beberapa detik.")
            
            logger.info("Admin requested restart via button. Exiting with code 42 for restart.")
            
            # Matikan semua pemantauan sebelum restart
            monitor = context.bot_data.get("monitor")
            if monitor:
                monitor.stop_monitoring()
            
            # Beri waktu untuk cleanup
            await asyncio.sleep(2)
            
            # Siapkan restart
            os._exit(42)  # Hard exit yang akan ditangkap oleh script wrapper
        
        elif action == "stop_bot":
            keyboard = [
                [
                    InlineKeyboardButton("✅ Ya, Matikan Bot", callback_data="confirm_stop_bot"),
                    InlineKeyboardButton("❌ Tidak, Batal", callback_data="back_to_main"),
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                "⚠️ PERHATIAN: Anda akan mematikan bot\n\n"
                "Bot akan berhenti memantau akun dan tidak akan merekam livestream.\n"
                "Anda harus mengaktifkan kembali bot secara manual.\n\n"
                "Apakah Anda yakin?",
                reply_markup=reply_markup
            )
        
        elif action == "confirm_stop_bot":
            await query.edit_message_text("🛑 Mematikan bot...")
            
            logger.info("Admin requested to stop the bot")
            
            # Matikan semua pemantauan
            monitor = context.bot_data.get("monitor")
            if monitor:
                monitor.stop_monitoring()
            
            # Kirim pesan terakhir
            await context.bot.send_message(
                chat_id=user_id,
                text="🛑 Bot telah dimatikan. Untuk menjalankan kembali, aktifkan bot secara manual."
            )
            
            # Keluar dengan kode normal
            os._exit(0)
            
        elif action == "back_to_main":
            # Kembali ke menu utama
            # Reset any user data state
            if "waiting_for" in context.user_data:
                del context.user_data["waiting_for"]
                
            await start_command(update, context)
        
    except Exception as e:
        logger.error(f"Error in button callback {action}: {e}")
        # Try to send an error message
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ Terjadi kesalahan: {str(e)}\n\nCoba gunakan /start untuk memulai ulang bot."
            )
        except:
            pass
    finally:
        # Hapus callback dari daftar aktif
        if monitor and callback_id in monitor.active_callbacks:
            monitor.active_callbacks.remove(callback_id)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk command /check"""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Maaf, bot ini hanya dapat digunakan oleh admin.")
        return
    
    args = context.args
    monitor = context.bot_data["monitor"]
    
    if not args:
        await update.message.reply_text(
            "Gunakan: /check <username> untuk memeriksa status satu akun\n"
            "atau /check all untuk memeriksa semua akun"
        )
        return
    
    if args[0].lower() == "all":
        await update.message.reply_text("⏳ Sedang memeriksa status semua akun... Mohon tunggu.")
        results = monitor.force_check_all()
        
        if not results:
            await update.message.reply_text("Tidak ada akun yang dipantau saat ini.")
            return
            
        message = "📊 Hasil Pengecekan Status:\n\n"
        for username, is_live in results.items():
            status = "🟢 LIVE" if is_live else "🔴 Tidak Live"
            message += f"@{username}: {status}\n"
            
        await update.message.reply_text(message)
    else:
        username = args[0].replace("@", "")
        await update.message.reply_text(f"⏳ Sedang memeriksa status @{username}... Mohon tunggu.")
        
        is_live = monitor._check_if_live(username)
        # Verifikasi hasil
        if is_live:
            is_live = monitor._verify_live_status(username)
            
        status = "🟢 LIVE" if is_live else "🔴 Tidak Live"
        await update.message.reply_text(f"Status @{username}: {status}")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk pesan."""
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("Maaf, bot ini hanya dapat digunakan oleh admin.")
        return
    
    # Tangani input pengguna sesuai dengan state
    if "waiting_for" in context.user_data:
        if context.user_data["waiting_for"] == "add_account":
            username = update.message.text.strip().replace("@", "")
            monitor = context.bot_data["monitor"]
            
            if monitor.add_account(username, user_id):
                await update.message.reply_text(
                    f"✅ Akun @{username} berhasil ditambahkan ke daftar pantauan.\n\n"
                    "Bot akan otomatis merekam saat akun ini mulai live.\n"
                    "Kembali ke menu utama: /start"
                )
            else:
                await update.message.reply_text(
                    f"❌ Gagal menambahkan akun @{username}.\n"
                    "Pastikan username benar dan coba lagi.\n\n"
                    "Kembali ke menu utama: /start"
                )
            
            # Reset state
            del context.user_data["waiting_for"]

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk error."""
    logger.error(f"Error: {context.error}")
    try:
        if update and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Terjadi kesalahan: {str(context.error)}\n\nCoba gunakan /start untuk memulai ulang bot."
            )
    except Exception as e:
        logger.error(f"Error sending error message: {e}")

async def main():
    """Fungsi utama untuk menjalankan bot."""
    # Inisialisasi database
    init_database()
    
    # Load saved interval if exists
    global CHECK_INTERVAL
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'check_interval'")
    result = cursor.fetchone()
    if result:
        CHECK_INTERVAL = int(result[0])
    conn.close()
    
    # Inisialisasi aplikasi bot Telegram
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("restart", restart_command))
    
    # Register callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Register message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Register error handler
    application.add_error_handler(error_handler)
    
    # Inisialisasi TikTok monitor
    monitor = TikTokMonitor(application)
    application.bot_data["monitor"] = monitor
    
    try:
        # Initialize the application
        await application.initialize()
        
        # Initialize the monitor
        tasks = await monitor.initialize()
        
        # Start the application
        await application.start()
        
        # Start polling updates from Telegram - HANYA SEKALI
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        
        # Log that the bot is ready
        logger.info(f"Bot v{BOT_VERSION} is running! Press Ctrl+C to stop.")
        
        # Buat task keepalive untuk mencegah bot mati
        async def keep_alive():
            while True:
                try:
                    # Periksa apakah bot masih hidup setiap menit
                    logger.info("Bot still running - keepalive check")
                    await asyncio.sleep(60)
                except Exception as e:
                    logger.error(f"Error in keepalive: {e}")
                    
        # Jalankan bot tanpa batas waktu sampai diinterupsi manual
        # CATATAN: Tidak menjalankan updater.start_polling lagi di sini
        await asyncio.gather(
            tasks,
            keep_alive(),
        )
        
    except asyncio.CancelledError:
        # Ini adalah interupsi normal, jangan lakukan apa-apa
        logger.info("Main task was cancelled, shutting down gracefully...")
    except Exception as e:
        logger.error(f"Critical error in main application: {e}")
        # Kirim notifikasi ke admin tentang error
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ BOT ERROR: {str(e)}\nBot akan dijalankan ulang."
                )
            except:
                pass
        
        # Coba jalankan ulang bot setelah error
        logger.info("Attempting to restart in 5 seconds...")
        await asyncio.sleep(5)
        await main()  # Restart bot
        return
    finally:
        # Ini hanya dijalankan jika bot dimatikan dengan sengaja (Ctrl+C)
        logger.info("Shutting down...")
        monitor.stop_monitoring()
        
        # Make sure the application shuts down properly
        try:
            if hasattr(application, 'updater') and application.updater.running:
                await application.updater.stop()
            if getattr(application, '_running', False):
                await application.stop()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
            
        logger.info("Bot has been shut down successfully.")

# Script wrapper untuk restart otomatis
if __name__ == "__main__":
    max_restarts = 10
    restart_count = 0
    
    # Tambahkan handler khusus untuk exit signal
    def signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, exiting cleanly")
        os._exit(0)
        
    # Register signal handlers
    import signal
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Cari proses lain yang mungkin masih berjalan
    try:
        logger.info("Checking for other running instances...")
        ps_cmd = ["ps", "aux"]
        ps_process = subprocess.Popen(ps_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ps_output, _ = ps_process.communicate()
        
        # Hitung jumlah instance script yang sedang berjalan
        script_name = os.path.basename(__file__)
        current_pid = os.getpid()
        running_instances = []
        
        for line in ps_output.splitlines():
            if script_name in line and str(current_pid) not in line.split()[1]:
                running_instances.append(line.split()[1])  # PID
                
        if running_instances:
            logger.warning(f"Found {len(running_instances)} other instances running: {running_instances}")
            
            # Kirim SIGTERM ke instance lainnya
            for pid in running_instances:
                try:
                    logger.info(f"Terminating other instance with PID {pid}")
                    os.kill(int(pid), signal.SIGTERM)
                except Exception as e:
                    logger.error(f"Error terminating PID {pid}: {e}")
                    
            # Tunggu beberapa detik agar instance lain keluar
            time.sleep(5)
    except Exception as e:
        logger.error(f"Error while checking for other instances: {e}")
    
    while restart_count < max_restarts:
        try:
            # Tambahkan log untuk membantu troubleshooting
            logger.info(f"Starting bot (restart #{restart_count})")
            
            # Jalankan aplikasi utama
            exit_code = 0
            try:
                asyncio.run(main())
            except SystemExit as e:
                exit_code = e.code
            
            # Periksa kode exit
            if exit_code == 42:
                # Kode restart yang diminta
                logger.info("Bot requested restart (exit code 42)")
                restart_count += 1
                # Tunggu sebentar sebelum restart
                time.sleep(5)
                continue
            else:
                # Exit normal atau error
                logger.info(f"Bot exited with code {exit_code}")
                break
                
        except Exception as e:
            restart_count += 1
            logger.critical(f"Fatal error: {e}")
            time.sleep(10)  # Tunggu sebelum coba lagi
    
    if restart_count >= max_restarts:
        logger.critical(f"Exceeded maximum number of restarts ({max_restarts}). Giving up.")
    
    logger.info("Bot has completely shut down.")
