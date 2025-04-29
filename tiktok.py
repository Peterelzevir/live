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
TELEGRAM_BOT_TOKEN = "7839177497:AAE8SPVj8e4c0eLta7m9kB2cPq9w92OBHhw"  # Token bot Anda
ADMIN_IDS = [5988451717]  # ID admin Telegram Anda
DEFAULT_RECORDING_QUALITY = "720p"  # Kualitas rekaman default
RECORDING_DIR = "recordings"  # Direktori untuk menyimpan hasil rekaman
CHECK_INTERVAL = 60  # Interval pengecekan dalam detik (1 menit)

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
        
    def initialize(self):
        """Inisialisasi dan mulai memantau semua akun yang tersimpan di database."""
        # Mulai thread monitoring
        self.monitor_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self.monitor_thread.start()
        
        # Mulai task untuk memproses notifikasi
        asyncio.create_task(self._process_notifications())
        
        accounts = self.get_monitored_accounts()
        logger.info(f"Initialized monitoring for {len(accounts)} accounts")
    
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
        # Validasi sederhana
        return username and len(username) >= 2
    
    def _monitoring_loop(self):
        """Loop untuk memantau akun TikTok secara berkala."""
        while not self.stop_event.is_set():
            try:
                accounts = self.get_monitored_accounts()
                for username in accounts:
                    try:
                        # Cek status live
                        is_live = self._check_if_live(username)
                        
                        # Jika sedang live dan belum merekam, mulai rekaman
                        if is_live and username not in self.recording_processes:
                            # Tambahkan notifikasi ke queue
                            self.notification_queue.put({
                                "type": "live_start",
                                "username": username
                            })
                            
                            # Mulai merekam
                            self.start_recording(username)
                        
                        # Jika tidak live tapi masih merekam, hentikan rekaman
                        elif not is_live and username in self.recording_processes:
                            # Tambahkan notifikasi ke queue
                            self.notification_queue.put({
                                "type": "live_end",
                                "username": username
                            })
                            
                            # Hentikan rekaman
                            self.stop_recording(username)
                    
                    except Exception as e:
                        logger.error(f"Error memantau akun {username}: {e}")
                
                # Tunggu interval sebelum memeriksa kembali
                time.sleep(CHECK_INTERVAL)
            
            except Exception as e:
                logger.error(f"Error dalam monitoring loop: {e}")
                time.sleep(10)  # Tunggu sebentar sebelum mencoba lagi
    
    async def _process_notifications(self):
        """Proses notifikasi dari queue dan kirim ke admin."""
        while True:
            try:
                # Periksa queue untuk notifikasi (non-blocking)
                if not self.notification_queue.empty():
                    notification = self.notification_queue.get_nowait()
                    
                    if notification["type"] == "live_start":
                        await self.notify_admins(f"🟢 <b>{notification['username']}</b> sedang LIVE! Rekaman dimulai otomatis.")
                    
                    elif notification["type"] == "live_end":
                        await self.notify_admins(f"🔴 Livestream <b>{notification['username']}</b> telah berakhir. Rekaman selesai.")
                
                # Tunggu sebentar sebelum memeriksa lagi
                await asyncio.sleep(1)
                
            except queue.Empty:
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error memproses notifikasi: {e}")
                await asyncio.sleep(5)
    
    def _check_if_live(self, username: str) -> bool:
        """Cek apakah akun TikTok sedang live menggunakan yt-dlp."""
        try:
            # Format URL TikTok Live
            tiktok_url = f"https://www.tiktok.com/@{username}/live"
            
            # Gunakan yt-dlp untuk memeriksa status live
            cmd = [
                "yt-dlp", 
                "--no-check-certificate",
                "--skip-download",
                "--print", "title",
                tiktok_url
            ]
            
            # Jalankan perintah dengan timeout
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            try:
                stdout, stderr = process.communicate(timeout=30)
                
                # Jika proses keluar dengan kode 0 dan stdout tidak kosong, user sedang live
                if process.returncode == 0 and stdout.strip():
                    logger.info(f"Account {username} is LIVE: {stdout.strip()}")
                    return True
                
                # Jika error atau tidak ada output, user tidak sedang live
                logger.debug(f"Account {username} is NOT live")
                return False
            
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
            
            # Jalankan proses rekaman menggunakan yt-dlp
            cmd = [
                "yt-dlp", 
                "--no-check-certificate",
                "--hls-use-mpegts",
                "-o", file_path,
                tiktok_url
            ]
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Simpan proses rekaman
            self.recording_processes[username] = process
            self.active_recordings[username] = {
                "id": recording_id,
                "file_path": file_path,
                "start_time": now,
                "quality": quality
            }
            
            logger.info(f"Started recording livestream {username} with quality {quality}")
        except Exception as e:
            logger.error(f"Error memulai rekaman {username}: {e}")
    
    def stop_recording(self, username: str):
        """Hentikan rekaman livestream TikTok."""
        if username in self.recording_processes:
            try:
                # Hentikan proses rekaman
                process = self.recording_processes[username]
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
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
                    
                    # Hapus dari daftar rekaman aktif
                    del self.active_recordings[username]
                
                # Hapus dari daftar proses rekaman
                del self.recording_processes[username]
                
                logger.info(f"Stopped recording livestream {username}")
            except Exception as e:
                logger.error(f"Error menghentikan rekaman {username}: {e}")
    
    async def notify_admins(self, message: str):
        """Kirim notifikasi ke semua admin."""
        for admin_id in ADMIN_IDS:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id,
                    text=message,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error mengirim notifikasi ke admin {admin_id}: {e}")
    
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
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "Selamat datang di Bot Pemantau & Perekam TikTok Live!\n\n"
        "Bot ini akan memantau akun TikTok dan otomatis merekam saat mereka sedang live.\n"
        "Silakan pilih opsi di bawah:",
        reply_markup=reply_markup
    )

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
                message += f"{i}. @{account}\n"
        
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
    
    elif action == "back_to_main":
        # Kembali ke menu utama
        await start_command(update, context)

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
    
    # Register callback query handler
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Register message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Register error handler
    application.add_error_handler(error_handler)
    
    # Inisialisasi TikTok monitor
    monitor = TikTokMonitor(application)
    application.bot_data["monitor"] = monitor
    
    # Initialize monitor
    monitor.initialize()
    
    try:
        # Run the bot until the user presses Ctrl-C
        await application.run_polling()
    finally:
        # Stop the application and monitor
        monitor.stop_monitoring()

if __name__ == "__main__":
    asyncio.run(main())
