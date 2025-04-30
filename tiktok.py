import os
import logging
import sqlite3
import time
import asyncio
import subprocess
import threading
import queue
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
ADMIN_IDS = [5988451717]  # ID admin Telegram Anda
DEFAULT_RECORDING_QUALITY = "720p"  # Kualitas rekaman default
RECORDING_DIR = "recordings"  # Direktori untuk menyimpan hasil rekaman
CHECK_INTERVAL = 60  # Interval pengecekan dalam detik (1 menit)
BOT_VERSION = "1.2.0"  # Versi bot untuk tracking

# Buat direktori rekaman jika belum ada
os.makedirs(RECORDING_DIR, exist_ok=True)

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
    
    # Masukkan pengaturan default jika belum ada
    cursor.execute('''
    INSERT OR IGNORE INTO settings (key, value)
    VALUES ('recording_quality', ?)
    ''', (DEFAULT_RECORDING_QUALITY,))
    
    conn.commit()
    conn.close()

# Class untuk mengelola monitor dan rekaman TikTok
class TikTokMonitor:
    def __init__(self, app):
        self.app = app
        self.active_recordings: Dict[str, dict] = {}
        self.recording_processes: Dict[str, subprocess.Popen] = {}
        self.stop_event = threading.Event()
        self.monitor_thread = None
        self.notification_queue = queue.Queue()
        self.last_check_status = {}  # Menyimpan status terakhir checks
        self.last_activity_time = time.time()  # Waktu aktivitas terakhir
        self.error_count = 0  # Menghitung error konsekutif
        
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
                    text=f"🤖 Bot TikTok Monitor telah aktif!\n\nMemantau {len(accounts)} akun."
                )
            except Exception as e:
                logger.error(f"Error sending startup message: {e}")
        
        # Kombinasikan task dan kembalikan
        return asyncio.gather(notification_task, watchdog_task)
        
    async def _start_monitoring_thread(self):
        """Memulai thread monitoring dengan watchdog."""
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
                    if self.monitor_thread.is_alive():
                        # Tidak bisa menghentikan thread langsung, jadi tandai untuk restart
                        self.stop_event.set()
                        time.sleep(2)
                        self.stop_event.clear()
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
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM monitored_accounts")
        accounts = [row[0] for row in cursor.fetchall()]
        conn.close()
        return accounts
    
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
                    
                for username in accounts:
                    if self.stop_event.is_set():
                        break  # Periksa lagi jika diminta berhenti
                        
                    try:
                        # Cek status live
                        is_live = self._check_if_live(username)
                        logger.info(f"Checked {username}: {'LIVE' if is_live else 'NOT LIVE'}")
                        
                        # Simpan status sebelumnya
                        was_live = self.last_check_status.get(username, False)
                        
                        # Perubahan status: offline -> live
                        if is_live and not was_live:
                            logger.info(f"Status change detected: {username} is now LIVE")
                            # Tambahkan notifikasi ke queue
                            self.notification_queue.put({
                                "type": "live_start",
                                "username": username
                            })
                            
                            # Mulai merekam
                            self.start_recording(username)
                        
                        # Perubahan status: live -> offline
                        elif not is_live and was_live:
                            logger.info(f"Status change detected: {username} is no longer live")
                            # Tambahkan notifikasi ke queue
                            self.notification_queue.put({
                                "type": "live_end",
                                "username": username
                            })
                            
                            # Hentikan rekaman
                            self.stop_recording(username)
                        
                        # Update status terakhir
                        self.last_check_status[username] = is_live
                        
                        # Reset error counter karena berhasil
                        consecutive_errors = 0
                        
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
        """Cek apakah akun TikTok sedang live menggunakan yt-dlp."""
        try:
            # Format URL TikTok Live
            tiktok_url = f"https://www.tiktok.com/@{username}/live"
            
            # Pendekatan yang lebih efektif untuk memeriksa status live
            cmd = [
                "yt-dlp", 
                "--no-check-certificate",
                "--no-warnings",
                "--skip-download",
                "--quiet",
                "--ignore-no-formats-error",
                "--ignore-config",  # Abaikan konfigurasi lokal yang bisa menyebabkan masalah
                "--force-ipv4",     # Paksa menggunakan IPv4
                "--extractor-retries", "3",  # Coba beberapa kali jika gagal
                "--socket-timeout", "10",    # Timeout lebih cepat
                tiktok_url
            ]
            
            try:
                # Gunakan cURL sebagai backup untuk URL checking
                curl_cmd = [
                    "curl", 
                    "-s",           # Silent mode
                    "-L",           # Follow redirects
                    "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",  # User agent
                    "--connect-timeout", "5",
                    tiktok_url
                ]
                
                # Coba dengan yt-dlp dulu
                logger.debug(f"Checking if {username} is live with yt-dlp")
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                
                stdout, stderr = process.communicate(timeout=15)
                
                # Cek jika ada error yang menunjukkan stream tidak live
                if process.returncode != 0:
                    if "This video is not available" in stderr or "HTTP Error 404" in stderr:
                        logger.debug(f"{username} is NOT live (404 error)")
                        return False
                    
                    # Gunakan curl sebagai backup
                    logger.debug(f"yt-dlp failed, trying with curl for {username}")
                    curl_process = subprocess.Popen(
                        curl_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    
                    curl_output, _ = curl_process.communicate(timeout=10)
                    
                    # Cek output curl untuk indikator live
                    if "LIVE" in curl_output and f"@{username}" in curl_output:
                        logger.info(f"Account {username} is LIVE (detected with curl)")
                        return True
                    
                    # Tambahan: cek frasa tertentu yang muncul di halaman streaming
                    live_indicators = ["is LIVE now", "LIVE stream", "livestream", "Live viewers"]
                    for indicator in live_indicators:
                        if indicator in curl_output:
                            logger.info(f"Account {username} is LIVE (detected indicator: {indicator})")
                            return True
                    
                    logger.debug(f"{username} is NOT live (based on curl)")
                    return False
                
                # Jika yt-dlp berhasil, berarti stream live terdeteksi
                logger.info(f"Account {username} is LIVE! (yt-dlp success)")
                return True
            
            except subprocess.TimeoutExpired:
                process.kill()
                logger.warning(f"Timeout checking if {username} is live")
                return False
        
        except Exception as e:
            logger.error(f"Error checking if {username} is live: {e}")
            return False
    
    def start_recording(self, username: str):
        """Mulai merekam livestream TikTok."""
        try:
            # Periksa apakah sudah merekam
            if username in self.recording_processes:
                logger.info(f"Already recording {username}, skipping")
                return
                
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
                "--live-from-start",        # Rekam dari awal stream
                "--no-part",                # Gunakan file normal daripada .part file
                "--no-mtime",               # Jangan ubah waktu modifikasi file
                "--no-warnings",            # Kurangi pesan warning
                "--retries", "infinite",    # Coba ulang tanpa batas jika ada error
                "--fragment-retries", "infinite",  # Coba ulang download fragment tanpa batas
                "--force-ipv4",             # Paksa IPv4
                "--extractor-retries", "3", # Coba beberapa kali jika extractor gagal
                "-o", file_path,
                tiktok_url
            ]
            
            # Log command yang dijalankan
            logger.info(f"Starting recording with command: {' '.join(cmd)}")
            
            # Jalankan proses dengan output dialihkan ke file log
            log_file_path = os.path.join(RECORDING_DIR, f"{username}_{timestamp}_log.txt")
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
                "log_file": log_file
            }
            
            logger.info(f"Started recording livestream for {username} with quality {quality}")
        except Exception as e:
            logger.error(f"Error memulai rekaman {username}: {e}")
    
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
                    
                    # Hapus dari daftar rekaman aktif
                    del self.active_recordings[username]
                
                # Hapus dari daftar proses rekaman
                del self.recording_processes[username]
                
                logger.info(f"Stopped recording livestream {username}")
            except Exception as e:
                logger.error(f"Error menghentikan rekaman {username}: {e}")
    
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
                "id": recording["id"]
            })
        return result
    
    def get_recording_history(self, limit: int = 10) -> List[Dict]:
        """Dapatkan riwayat rekaman yang sudah selesai."""
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM recordings WHERE status = 'completed' ORDER BY end_time DESC LIMIT ?",
            (limit,)
        )
        recordings = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return recordings
    
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
            
            if result and os.path.exists(result[0]):
                return (result[0], result[1])
            return None
        except Exception as e:
            logger.error(f"Error mendapatkan file rekaman {recording_id}: {e}")
            return None
    
    def force_check_all(self):
        """Paksa pengecekan semua akun sekarang."""
        accounts = self.get_monitored_accounts()
        results = {}
        
        for username in accounts:
            is_live = self._check_if_live(username)
            results[username] = is_live
            
            # Update status
            was_live = self.last_check_status.get(username, False)
            self.last_check_status[username] = is_live
            
            # Perubahan status: offline -> live
            if is_live and not was_live:
                self.notification_queue.put({
                    "type": "live_start",
                    "username": username
                })
                self.start_recording(username)
            
            # Perubahan status: live -> offline
            elif not is_live and was_live:
                self.notification_queue.put({
                    "type": "live_end",
                    "username": username
                })
                self.stop_recording(username)
        
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
            self.start_recording(username)
            
            # Update status
            self.last_check_status[username] = True
            
            # Kirim notifikasi
            self.notification_queue.put({
                "type": "force_record",
                "username": username
            })
            
            return True
        except Exception as e:
            logger.error(f"Error force recording {username}: {e}")
            return False
    
    def stop_monitoring(self):
        """Hentikan seluruh pemantauan."""
        self.stop_event.set()
        if self.monitor_thread:
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
        "Selamat datang di Bot Pemantau & Perekam TikTok Live!\n\n"
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
    
    if user_id not in ADMIN_IDS:
        await query.answer("Maaf, bot ini hanya dapat digunakan oleh admin.")
        return
    
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
                
                message += (
                    f"{i}. @{rec['username']}\n"
                    f"   📅 Tanggal: {start_time.strftime('%d-%m-%Y')}\n"
                    f"   🕒 Waktu: {start_time.strftime('%H:%M:%S')}\n"
                    f"   ⏱️ Durasi: {duration_str}\n"
                    f"   🎬 Kualitas: {rec['quality']}\n"
                    f"   🆔 ID: {rec['id']}\n\n"
                )
        
        keyboard = []
        for rec in recordings:
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
                "❌ File rekaman tidak ditemukan.\n\n"
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
        await start_command(update, context)

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
                text="Terjadi kesalahan. Silakan coba lagi nanti."
            )
    except Exception:
        pass

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
        notification_task = await monitor.initialize()
        
        # Start the application
        await application.start()
        
        # Start polling updates from Telegram
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        
        # Log that the bot is ready
        logger.info("Bot is running! Press Ctrl+C to stop.")
        
        # Buat task keepalive untuk mencegah bot mati
        async def keep_alive():
            while True:
                try:
                    # Periksa apakah bot masih hidup setiap menit
                    logger.info("Bot still running - keepalive check")
                    await asyncio.sleep(60)
                except Exception as e:
                    logger.error(f"Error in keepalive: {e}")
        
        # Kumpulkan semua task yang harus dijaga tetap hidup
        tasks = [
            notification_task,
            keep_alive(),
        ]
        
        # Jalankan bot tanpa batas waktu sampai diinterupsi manual
        await asyncio.gather(*tasks)
        
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
