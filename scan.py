import logging
import os
import re
import json
from io import BytesIO
import vobject
import asyncio
from datetime import datetime
import html
import time
import random
import sys
from typing import Dict, List, Set, Tuple, Union, Optional, Any
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants, ChatPermissions, Message
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters, ConversationHandler
from telethon import TelegramClient, functions, types, errors, events
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.tl.functions.channels import JoinChannelRequest, GetFullChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest, AddChatUserRequest
from telethon.tl.functions.contacts import GetContactsRequest

# Konfigurasi logging dengan warna
class ColorFormatter(logging.Formatter):
    """Kelas untuk memberikan warna pada log"""
    COLORS = {
        'DEBUG': '\033[94m',     # Biru
        'INFO': '\033[92m',      # Hijau
        'WARNING': '\033[93m',   # Kuning
        'ERROR': '\033[91m',     # Merah
        'CRITICAL': '\033[91m\033[1m',  # Merah tebal
        'RESET': '\033[0m'       # Reset warna
    }

    def format(self, record):
        log_message = super().format(record)
        level_name = record.levelname
        if level_name in self.COLORS:
            return f"{self.COLORS[level_name]}{log_message}{self.COLORS['RESET']}"
        return log_message

# Setup logging
log_format = '[%(asctime)s] - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
formatter = ColorFormatter(log_format)

# File handler untuk menyimpan log ke file
file_handler = logging.FileHandler("vcf_checker_bot.log")
file_handler.setFormatter(logging.Formatter(log_format))

# Console handler untuk menampilkan log di console
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

# Konfigurasi logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Konfigurasi Telegram Bot 
BOT_TOKEN = "8192964371:AAGYOYY-V4fdbJHUQQdbbQkRzQiwxSe0LPk"

# API Telegram
API_ID = 25483326  # Ganti dengan API ID Anda dari my.telegram.org
API_HASH = "062d1366c8f3641ae906c05168e33d08"  # Ganti dengan API HASH Anda dari my.telegram.org

# Owner ID (digunakan untuk menambahkan admin pertama)
OWNER_ID = 5988451717  # Ganti dengan ID Telegram Anda

# Dictionary untuk menyimpan result, pagination, admin, dll
RESULTS = {}
PAGINATION = {}  # Untuk menyimpan state pagination
ADMIN_LIST = {OWNER_ID}  # Set berisi ID admin
PROCESSING_STATUS = {}  # Untuk menyimpan status pemrosesan
USER_ACCOUNTS = {}  # Untuk menyimpan akun pengguna
CURRENT_ACCOUNT = {}  # Untuk menyimpan akun aktif untuk tiap chat
SESSIONS_DIR = "sessions"  # Direktori untuk menyimpan sesi
CHECK_DELAY = {}  # Delay antara pengecekan (dalam detik)
TARGET_GROUPS = {}  # Grup target untuk mengirim kontak
ACCOUNT_SETUP_DATA = {}  # Data sementara untuk setup akun

# Konstanta untuk pagination dan conversation states
ITEMS_PER_PAGE = 1

# States untuk conversation handler
PHONE, CODE, PASSWORD, ACCOUNT_NAME, GROUP_LINK, DELAY_INPUT = range(6)

# Membuat direktori sessions jika belum ada
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)
    logger.info(f"🗂️ Direktori sessions dibuat: {SESSIONS_DIR}")

# Fungsi utility untuk menyimpan data
def save_admin_list():
    with open('admin_list.json', 'w') as f:
        json.dump(list(ADMIN_LIST), f)
    logger.info(f"👮‍♂️ Daftar admin disimpan: {ADMIN_LIST}")

def save_user_accounts():
    with open('user_accounts.json', 'w') as f:
        json.dump(USER_ACCOUNTS, f)
    logger.info(f"👥 {len(USER_ACCOUNTS)} akun pengguna disimpan")

def save_target_groups():
    with open('target_groups.json', 'w') as f:
        json.dump(TARGET_GROUPS, f)
    logger.info("👥 Target grup disimpan")

# Memuat data admin dari file (jika ada)
try:
    with open('admin_list.json', 'r') as f:
        ADMIN_LIST = set(json.load(f))
        ADMIN_LIST.add(OWNER_ID)  # Pastikan owner selalu ada di daftar admin
        logger.info(f"👮‍♂️ Daftar admin dimuat: {ADMIN_LIST}")
except FileNotFoundError:
    logger.info("👮‍♂️ File daftar admin tidak ditemukan, menggunakan default")
    save_admin_list()

# Memuat akun pengguna dari file (jika ada)
try:
    with open('user_accounts.json', 'r') as f:
        USER_ACCOUNTS = json.load(f)
        logger.info(f"👥 Memuat {len(USER_ACCOUNTS)} akun pengguna")
except FileNotFoundError:
    logger.info("👥 File akun pengguna tidak ditemukan")
    save_user_accounts()

# Memuat grup target dari file (jika ada)
try:
    with open('target_groups.json', 'r') as f:
        TARGET_GROUPS = json.load(f)
        logger.info(f"👥 Target grup dimuat: {len(TARGET_GROUPS)}")
except FileNotFoundError:
    logger.info("👥 File target grup tidak ditemukan")
    save_target_groups()

# Emoji dan Styling
EMOJI = {
    "start": "🚀",
    "help": "ℹ️",
    "processing": "⏳",
    "success": "✅",
    "fail": "❌",
    "phone": "📱",
    "name": "👤",
    "id": "🆔",
    "username": "👤",
    "bio": "ℹ️",
    "photo": "🖼️",
    "bot": "🤖",
    "premium": "⭐",
    "last_seen": "🕒",
    "download": "⬇️",
    "view": "📋",
    "next": "⏩",
    "prev": "⏪",
    "page": "📄",
    "registered": "✅",
    "not_registered": "❌",
    "first_name": "📝",
    "last_name": "📝",
    "next_page": "➡️",
    "prev_page": "⬅️",
    "calendar": "📅",
    "clock": "🕙",
    "info": "ℹ️",
    "warning": "⚠️",
    "telegram": "📨",
    "check": "✓",
    "cross": "✗",
    "loading": "⌛",
    "complete": "🏁",
    "search": "🔍",
    "contact": "👥",
    "trash": "🗑️",
    "settings": "⚙️",
    "refresh": "🔄",
    "link": "🔗",
    "save": "💾",
    "gift": "🎁",
    "fire": "🔥",
    "rocket": "🚀",
    "diamond": "💎",
    "magic": "✨",
    "crown": "👑",
    "admin": "👮‍♂️",
    "lock": "🔒",
    "unlock": "🔓",
    "key": "🔑",
    "denied": "🚫",
    "alert": "🚨",
    "user": "👨‍💻",
    "code": "🔐",
    "adduser": "➕",
    "group": "👥",
    "delay": "⏱️",
    "edit": "✏️",
    "send": "📤",
}

# ASCII Art & Styling untuk banner
BANNER = """
<pre>╭━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╮
┃  %s 𝚅𝙲𝙵 𝙲𝙷𝙴𝙲𝙺𝙴𝚁 𝙱𝙾𝚃 %s  ┃
┃  %s 𝚃𝙴𝙻𝙴𝙶𝚁𝙰𝙼 𝙳𝙴𝚃𝙴𝙲𝚃𝙾𝚁 %s  ┃
╰━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╯</pre>"""

LOADING_PATTERNS = [
    "<pre>[□□□□□□□□□□] 0%%</pre>",
    "<pre>[■□□□□□□□□□] 10%%</pre>",
    "<pre>[■■□□□□□□□□] 20%%</pre>",
    "<pre>[■■■□□□□□□□] 30%%</pre>",
    "<pre>[■■■■□□□□□□] 40%%</pre>",
    "<pre>[■■■■■□□□□□] 50%%</pre>",
    "<pre>[■■■■■■□□□□] 60%%</pre>",
    "<pre>[■■■■■■■□□□] 70%%</pre>",
    "<pre>[■■■■■■■■□□] 80%%</pre>",
    "<pre>[■■■■■■■■■□] 90%%</pre>",
    "<pre>[■■■■■■■■■■] 100%%</pre>"
]

# Fungsi untuk memeriksa apakah pengguna adalah admin
def is_admin(user_id):
    """Memeriksa apakah user_id adalah admin."""
    return user_id in ADMIN_LIST

# Decorator untuk memeriksa admin
def admin_only(func):
    """Decorator untuk membatasi akses hanya untuk admin."""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_admin(user_id):
            if update.callback_query:
                await update.callback_query.answer(f"{EMOJI['denied']} Akses ditolak! Hanya admin yang dapat menggunakan fitur ini.")
                return
            else:
                await update.message.reply_text(
                    f"{EMOJI['denied']} <b>AKSES DITOLAK</b> {EMOJI['denied']}\n\n"
                    f"{EMOJI['lock']} Maaf, hanya <b>admin</b> yang dapat menggunakan bot ini.\n"
                    f"{EMOJI['id']} User ID Anda: <code>{user_id}</code>",
                    parse_mode=constants.ParseMode.HTML
                )
                return
        return await func(update, context, *args, **kwargs)
    return wrapped

# Fungsi untuk memformat teks Telegram
def format_text(text, style="normal"):
    """Memformat teks dengan gaya Telegram yang berbeda."""
    if style == "bold":
        return f"<b>{text}</b>"
    elif style == "italic":
        return f"<i>{text}</i>"
    elif style == "code":
        return f"<code>{text}</code>"
    elif style == "pre":
        return f"<pre>{text}</pre>"
    elif style == "underline":
        return f"<u>{text}</u>"
    elif style == "strikethrough":
        return f"<s>{text}</s>"
    elif style == "spoiler":
        return f"<tg-spoiler>{text}</tg-spoiler>"
    else:
        return text

# Fungsi untuk inisialisasi client Telethon
async def init_telethon_client(phone_number, chat_id=None):
    """Inisialisasi client Telethon dan mengembalikan instance client."""
    session_file = os.path.join(SESSIONS_DIR, f"{phone_number}")
    
    # Cek jika sudah ada session yang disimpan
    client = TelegramClient(session_file, API_ID, API_HASH)
    
    try:
        logger.info(f"📱 Menghubungkan ke Telegram dengan akun {phone_number}")
        await client.connect()
        
        if not await client.is_user_authorized():
            logger.warning(f"⚠️ Sesi untuk {phone_number} ada tapi tidak terotorisasi")
            await client.disconnect()
            return None
        
        me = await client.get_me()
        logger.info(f"✅ Berhasil terhubung ke Telegram sebagai {me.first_name} (@{me.username or 'no_username'})")
        return client
    except Exception as e:
        logger.error(f"❌ Error menghubungkan ke Telegram dengan akun {phone_number}: {e}")
        try:
            await client.disconnect()
        except:
            pass
        return None

# Command handlers
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengirim pesan saat command /start dijalankan."""
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    chat_id = str(update.effective_chat.id)
    
    # Jika dipanggil dari callback_query
    if update.callback_query:
        query = update.callback_query
        banner = BANNER % (EMOJI['fire'], EMOJI['fire'], EMOJI['rocket'], EMOJI['rocket'])
        
        message = (
            f"{banner}\n\n"
            f"{EMOJI['admin']} {format_text('AKSES ADMIN DIBERIKAN', 'bold')} {EMOJI['admin']}\n\n"
            f"Halo {format_text(first_name, 'bold')}! Selamat datang di {format_text('VCF Checker Bot', 'bold')}.\n\n"
            f"{EMOJI['info']} Bot ini membantu Anda {format_text('mengecek nomor telepon', 'italic')} dari file VCF yang terdaftar di Telegram.\n\n"
            f"{EMOJI['phone']} Kirim file {format_text('.vcf', 'code')} untuk mulai memeriksa nomor telepon.\n"
            f"{EMOJI['view']} Bot akan menampilkan {format_text('detail lengkap', 'italic')} untuk nomor yang terdaftar.\n"
            f"{EMOJI['download']} Anda dapat mengunduh file VCF yang hanya berisi kontak Telegram.\n\n"
            f"{EMOJI['user']} Anda memerlukan {format_text('akun Telegram', 'bold')} untuk menggunakan bot ini.\n"
            f"{EMOJI['help']} Ketik {format_text('/help', 'code')} untuk bantuan lebih lanjut."
        )
        
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI['help']} Bantuan", callback_data="help"),
                InlineKeyboardButton(f"{EMOJI['admin']} Admin Menu", callback_data="admin_menu")
            ],
            [
                InlineKeyboardButton(f"{EMOJI['user']} Akun Telegram", callback_data="account_menu"),
                InlineKeyboardButton(f"{EMOJI['group']} Grup Target", callback_data="group_menu")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
        return
    
    # Jika dipanggil dari pesan biasa
    banner = BANNER % (EMOJI['fire'], EMOJI['fire'], EMOJI['rocket'], EMOJI['rocket'])
    
    message = (
        f"{banner}\n\n"
        f"{EMOJI['admin']} {format_text('AKSES ADMIN DIBERIKAN', 'bold')} {EMOJI['admin']}\n\n"
        f"Halo {format_text(first_name, 'bold')}! Selamat datang di {format_text('VCF Checker Bot', 'bold')}.\n\n"
        f"{EMOJI['info']} Bot ini membantu Anda {format_text('mengecek nomor telepon', 'italic')} dari file VCF yang terdaftar di Telegram.\n\n"
        f"{EMOJI['phone']} Kirim file {format_text('.vcf', 'code')} untuk mulai memeriksa nomor telepon.\n"
        f"{EMOJI['view']} Bot akan menampilkan {format_text('detail lengkap', 'italic')} untuk nomor yang terdaftar.\n"
        f"{EMOJI['download']} Anda dapat mengunduh file VCF yang hanya berisi kontak Telegram.\n\n"
        f"{EMOJI['user']} Anda memerlukan {format_text('akun Telegram', 'bold')} untuk menggunakan bot ini.\n"
        f"{EMOJI['help']} Ketik {format_text('/help', 'code')} untuk bantuan lebih lanjut."
    )
    
    # Membuat keyboard inline
    keyboard = [
        [
            InlineKeyboardButton(f"{EMOJI['help']} Bantuan", callback_data="help"),
            InlineKeyboardButton(f"{EMOJI['admin']} Admin Menu", callback_data="admin_menu")
        ],
        [
            InlineKeyboardButton(f"{EMOJI['user']} Akun Telegram", callback_data="account_menu"),
            InlineKeyboardButton(f"{EMOJI['group']} Grup Target", callback_data="group_menu")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        message, 
        parse_mode=constants.ParseMode.HTML,
        reply_markup=reply_markup
    )

@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengirim pesan saat command /help dijalankan."""
    banner = BANNER % (EMOJI['info'], EMOJI['info'], EMOJI['info'], EMOJI['info'])
    
    message = (
        f"{banner}\n\n"
        f"{EMOJI['help']} {format_text('PETUNJUK PENGGUNAAN', 'bold')} {EMOJI['help']}\n\n"
        f"1️⃣ Tambahkan {format_text('Akun Telegram', 'bold')} melalui menu akun\n"
        f"2️⃣ Atur {format_text('Grup Target', 'bold')} untuk mengirim kontak\n"
        f"3️⃣ Kirim file {format_text('.vcf', 'code')} ke bot ini\n"
        f"4️⃣ Bot akan memeriksa semua nomor telepon dan mengirim ke grup\n"
        f"5️⃣ Lihat detail kontak yang terdaftar di Telegram\n\n"
        f"{format_text('DAFTAR PERINTAH:', 'bold')}\n"
        f"{EMOJI['rocket']} {format_text('/start', 'code')} - Memulai bot\n"
        f"{EMOJI['help']} {format_text('/help', 'code')} - Menampilkan bantuan\n"
        f"{EMOJI['admin']} {format_text('/admin', 'code')} - Menampilkan menu admin\n"
        f"{EMOJI['user']} {format_text('/accounts', 'code')} - Mengelola akun Telegram\n"
        f"{EMOJI['group']} {format_text('/groups', 'code')} - Mengelola grup target\n"
        f"{EMOJI['settings']} {format_text('/addadmin', 'code')} - Menambahkan admin baru\n"
        f"{EMOJI['trash']} {format_text('/removeadmin', 'code')} - Menghapus admin\n\n"
        f"{EMOJI['warning']} {format_text('CATATAN:', 'bold')} Bot memerlukan akun Telegram aktif."
    )
    
    # Membuat keyboard inline
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['rocket']} Kembali ke Menu Utama", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.message:
        await update.message.reply_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
    else:
        await update.callback_query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )

@admin_only
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan menu admin."""
    banner = BANNER % (EMOJI['admin'], EMOJI['admin'], EMOJI['crown'], EMOJI['crown'])
    
    admin_list = "\n".join([f"{EMOJI['id']} {format_text(str(admin_id), 'code')}" for admin_id in ADMIN_LIST])
    
    message = (
        f"{banner}\n\n"
        f"{EMOJI['admin']} {format_text('MENU ADMIN', 'bold')} {EMOJI['admin']}\n\n"
        f"{format_text('Daftar Admin:', 'bold')}\n"
        f"{admin_list}\n\n"
        f"{format_text('Menambahkan Admin:', 'bold')}\n"
        f"{EMOJI['settings']} Gunakan {format_text('/addadmin [user_id]', 'code')}\n\n"
        f"{format_text('Menghapus Admin:', 'bold')}\n"
        f"{EMOJI['trash']} Gunakan {format_text('/removeadmin [user_id]', 'code')}"
    )
    
    # Membuat keyboard inline
    keyboard = [
        [
            InlineKeyboardButton(f"{EMOJI['settings']} Tambah Admin", callback_data="add_admin_prompt"),
            InlineKeyboardButton(f"{EMOJI['trash']} Hapus Admin", callback_data="remove_admin_prompt")
        ],
        [InlineKeyboardButton(f"{EMOJI['rocket']} Kembali ke Menu Utama", callback_data="start")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        message, 
        parse_mode=constants.ParseMode.HTML,
        reply_markup=reply_markup
    )

@admin_only
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menambahkan admin baru."""
    if not context.args:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Anda harus menyertakan user ID.\n"
            f"Contoh: {format_text('/addadmin 123456789', 'code')}",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    try:
        new_admin_id = int(context.args[0])
        
        if new_admin_id in ADMIN_LIST:
            await update.message.reply_text(
                f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} User ID {format_text(str(new_admin_id), 'code')} sudah menjadi admin.",
                parse_mode=constants.ParseMode.HTML
            )
            return
        
        ADMIN_LIST.add(new_admin_id)
        save_admin_list()
        
        await update.message.reply_text(
            f"{EMOJI['success']} {format_text('BERHASIL:', 'bold')} User ID {format_text(str(new_admin_id), 'code')} telah ditambahkan sebagai admin.",
            parse_mode=constants.ParseMode.HTML
        )
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR:', 'bold')} User ID harus berupa angka.",
            parse_mode=constants.ParseMode.HTML
        )

@admin_only
async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menghapus admin."""
    if not context.args:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Anda harus menyertakan user ID.\n"
            f"Contoh: {format_text('/removeadmin 123456789', 'code')}",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    try:
        admin_id = int(context.args[0])
        
        if admin_id == OWNER_ID:
            await update.message.reply_text(
                f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Anda tidak dapat menghapus owner bot.",
                parse_mode=constants.ParseMode.HTML
            )
            return
        
        if admin_id not in ADMIN_LIST:
            await update.message.reply_text(
                f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} User ID {format_text(str(admin_id), 'code')} bukan admin.",
                parse_mode=constants.ParseMode.HTML
            )
            return
        
        ADMIN_LIST.remove(admin_id)
        save_admin_list()
        
        await update.message.reply_text(
            f"{EMOJI['success']} {format_text('BERHASIL:', 'bold')} User ID {format_text(str(admin_id), 'code')} telah dihapus dari daftar admin.",
            parse_mode=constants.ParseMode.HTML
        )
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR:', 'bold')} User ID harus berupa angka.",
            parse_mode=constants.ParseMode.HTML
        )

# Fungsi untuk mengelola akun Telegram
@admin_only
async def account_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan menu akun Telegram."""
    query = update.callback_query
    if query:
        await query.answer()
    
    banner = BANNER % (EMOJI['user'], EMOJI['user'], EMOJI['phone'], EMOJI['phone'])
    
    account_list = ""
    if USER_ACCOUNTS:
        for phone, data in USER_ACCOUNTS.items():
            account_list += f"{EMOJI['phone']} {format_text(data['name'], 'bold')} ({format_text(phone, 'code')})\n"
    else:
        account_list = f"{EMOJI['warning']} Belum ada akun Telegram yang ditambahkan."
    
    message = (
        f"{banner}\n\n"
        f"{EMOJI['user']} {format_text('MANAJEMEN AKUN TELEGRAM', 'bold')} {EMOJI['user']}\n\n"
        f"{format_text('Daftar Akun:', 'bold')}\n"
        f"{account_list}\n\n"
        f"{format_text('Untuk menambahkan akun baru, klik tombol di bawah ini.', 'italic')}"
    )
    
    # Membuat keyboard inline
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['adduser']} Tambah Akun Baru", callback_data="add_account")],
    ]
    
    if USER_ACCOUNTS:
        keyboard.append([InlineKeyboardButton(f"{EMOJI['trash']} Hapus Akun", callback_data="delete_account")])
    
    keyboard.append([InlineKeyboardButton(f"{EMOJI['rocket']} Kembali ke Menu Utama", callback_data="start")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )

@admin_only
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memulai proses penambahan akun Telegram."""
    query = update.callback_query
    if query:
        await query.answer()
    
    message = (
        f"{EMOJI['phone']} {format_text('TAMBAH AKUN TELEGRAM', 'bold')} {EMOJI['phone']}\n\n"
        f"Silakan masukkan {format_text('nomor telepon', 'bold')} yang terdaftar di Telegram.\n\n"
        f"Format: {format_text('+62812345678', 'code')} (termasuk kode negara)\n\n"
        f"{EMOJI['info']} Ketik {format_text('/cancel', 'code')} untuk membatalkan."
    )
    
    if query:
        await query.edit_message_text(
            message,
            parse_mode=constants.ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            message,
            parse_mode=constants.ParseMode.HTML
        )
    
    return PHONE

@admin_only
async def phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input nomor telepon."""
    phone = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    if phone == '/cancel':
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('DIBATALKAN', 'bold')}\n\n"
            f"Proses penambahan akun dibatalkan.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        return ConversationHandler.END
    
    # Validasi nomor telepon
    if not phone.startswith('+'):
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Nomor telepon harus diawali dengan {format_text('+', 'code')} dan kode negara.\n\n"
            f"Contoh: {format_text('+62812345678', 'code')}\n\n"
            f"Silakan coba lagi:",
            parse_mode=constants.ParseMode.HTML
        )
        return PHONE
    
    # Cek apakah nomor sudah terdaftar
    if phone in USER_ACCOUNTS:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Nomor telepon {format_text(phone, 'code')} sudah terdaftar.\n\n"
            f"Silakan masukkan nomor telepon lain:",
            parse_mode=constants.ParseMode.HTML
        )
        return PHONE
    
    # Simpan nomor telepon untuk digunakan nanti
    ACCOUNT_SETUP_DATA[chat_id] = {'phone': phone}
    
    # Inisialisasi client Telethon
    session_file = os.path.join(SESSIONS_DIR, f"{phone}")
    client = TelegramClient(session_file, API_ID, API_HASH)
    
    try:
        await client.connect()
        
        # Cek apakah sudah login
        if await client.is_user_authorized():
            # Jika sudah login, tanyakan nama untuk akun
            me = await client.get_me()
            ACCOUNT_SETUP_DATA[chat_id]['user_info'] = {
                'id': me.id,
                'username': me.username,
                'first_name': me.first_name,
                'last_name': me.last_name,
                'phone': phone
            }
            
            await client.disconnect()
            
            await update.message.reply_text(
                f"{EMOJI['success']} {format_text('AKUN SUDAH LOGIN', 'bold')}\n\n"
                f"Nomor {format_text(phone, 'code')} sudah login ke Telegram.\n\n"
                f"Silakan berikan {format_text('nama', 'bold')} untuk akun ini (untuk mempermudah identifikasi):",
                parse_mode=constants.ParseMode.HTML
            )
            
            return ACCOUNT_NAME
        
        # Jika belum login, kirim kode verifikasi
        await update.message.reply_text(
            f"{EMOJI['code']} {format_text('KODE VERIFIKASI', 'bold')}\n\n"
            f"Kode verifikasi telah dikirim ke nomor {format_text(phone, 'code')}.\n\n"
            f"Silakan masukkan {format_text('kode verifikasi', 'bold')} yang Anda terima:",
            parse_mode=constants.ParseMode.HTML
        )
        
        # Kirim code request
        await client.send_code_request(phone)
        ACCOUNT_SETUP_DATA[chat_id]['client'] = client
        
        return CODE
    
    except Exception as e:
        logger.error(f"❌ Error mengirim kode verifikasi ke {phone}: {e}")
        await client.disconnect()
        
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Terjadi kesalahan saat mengirim kode verifikasi: {format_text(str(e), 'code')}\n\n"
            f"Silakan coba lagi dengan nomor telepon lain:",
            parse_mode=constants.ParseMode.HTML
        )
        
        return PHONE

@admin_only
async def code_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input kode verifikasi."""
    code = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    if code == '/cancel':
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('DIBATALKAN', 'bold')}\n\n"
            f"Proses penambahan akun dibatalkan.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        
        # Hapus client jika ada
        if 'client' in ACCOUNT_SETUP_DATA.get(chat_id, {}):
            try:
                await ACCOUNT_SETUP_DATA[chat_id]['client'].disconnect()
            except:
                pass
            del ACCOUNT_SETUP_DATA[chat_id]
        
        return ConversationHandler.END
    
    # Ambil client dari data setup
    if chat_id not in ACCOUNT_SETUP_DATA or 'client' not in ACCOUNT_SETUP_DATA[chat_id]:
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Data sesi tidak ditemukan. Silakan mulai dari awal.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        return ConversationHandler.END
    
    client = ACCOUNT_SETUP_DATA[chat_id]['client']
    phone = ACCOUNT_SETUP_DATA[chat_id]['phone']
    
    try:
        # Coba login dengan kode
        await client.sign_in(phone, code)
        
        # Jika berhasil
        me = await client.get_me()
        
        ACCOUNT_SETUP_DATA[chat_id]['user_info'] = {
            'id': me.id,
            'username': me.username,
            'first_name': me.first_name,
            'last_name': me.last_name,
            'phone': phone
        }
        
        await client.disconnect()
        
        await update.message.reply_text(
            f"{EMOJI['success']} {format_text('LOGIN BERHASIL', 'bold')}\n\n"
            f"Akun Telegram berhasil ditambahkan!\n\n"
            f"Nama: {format_text(me.first_name or 'Tidak ada', 'bold')}\n"
            f"Username: {format_text('@' + me.username if me.username else 'Tidak ada', 'code')}\n\n"
            f"Silakan berikan {format_text('nama', 'bold')} untuk akun ini (untuk mempermudah identifikasi):",
            parse_mode=constants.ParseMode.HTML
        )
        
        return ACCOUNT_NAME
    
    except errors.SessionPasswordNeededError:
        # Jika akun dilindungi dengan password
        await update.message.reply_text(
            f"{EMOJI['lock']} {format_text('PASSWORD DIPERLUKAN', 'bold')}\n\n"
            f"Akun ini dilindungi dengan password 2FA.\n\n"
            f"Silakan masukkan {format_text('password 2FA', 'bold')} Anda:",
            parse_mode=constants.ParseMode.HTML
        )
        
        return PASSWORD
    
    except Exception as e:
        logger.error(f"❌ Error login dengan kode untuk {phone}: {e}")
        
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Terjadi kesalahan saat login: {format_text(str(e), 'code')}\n\n"
            f"Silakan coba lagi dengan kode yang benar:",
            parse_mode=constants.ParseMode.HTML
        )
        
        return CODE

@admin_only
async def password_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input password 2FA."""
    password = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    if password == '/cancel':
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('DIBATALKAN', 'bold')}\n\n"
            f"Proses penambahan akun dibatalkan.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        
        # Hapus client jika ada
        if 'client' in ACCOUNT_SETUP_DATA.get(chat_id, {}):
            try:
                await ACCOUNT_SETUP_DATA[chat_id]['client'].disconnect()
            except:
                pass
            del ACCOUNT_SETUP_DATA[chat_id]
        
        return ConversationHandler.END
    
    # Ambil client dari data setup
    if chat_id not in ACCOUNT_SETUP_DATA or 'client' not in ACCOUNT_SETUP_DATA[chat_id]:
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Data sesi tidak ditemukan. Silakan mulai dari awal.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        return ConversationHandler.END
    
    client = ACCOUNT_SETUP_DATA[chat_id]['client']
    phone = ACCOUNT_SETUP_DATA[chat_id]['phone']
    
    try:
        # Coba login dengan password
        await client.sign_in(password=password)
        
        # Jika berhasil
        me = await client.get_me()
        
        ACCOUNT_SETUP_DATA[chat_id]['user_info'] = {
            'id': me.id,
            'username': me.username,
            'first_name': me.first_name,
            'last_name': me.last_name,
            'phone': phone
        }
        
        await client.disconnect()
        
        await update.message.reply_text(
            f"{EMOJI['success']} {format_text('LOGIN BERHASIL', 'bold')}\n\n"
            f"Akun Telegram berhasil ditambahkan!\n\n"
            f"Nama: {format_text(me.first_name or 'Tidak ada', 'bold')}\n"
            f"Username: {format_text('@' + me.username if me.username else 'Tidak ada', 'code')}\n\n"
            f"Silakan berikan {format_text('nama', 'bold')} untuk akun ini (untuk mempermudah identifikasi):",
            parse_mode=constants.ParseMode.HTML
        )
        
        return ACCOUNT_NAME
    
    except Exception as e:
        logger.error(f"❌ Error login dengan password untuk {phone}: {e}")
        
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Terjadi kesalahan saat login dengan password: {format_text(str(e), 'code')}\n\n"
            f"Silakan coba lagi dengan password yang benar:",
            parse_mode=constants.ParseMode.HTML
        )
        
        return PASSWORD

@admin_only
async def account_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input nama akun."""
    name = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    if name == '/cancel':
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('DIBATALKAN', 'bold')}\n\n"
            f"Proses penambahan akun dibatalkan.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        return ConversationHandler.END
    
    # Ambil info user dari data setup
    if chat_id not in ACCOUNT_SETUP_DATA or 'user_info' not in ACCOUNT_SETUP_DATA[chat_id]:
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Data sesi tidak ditemukan. Silakan mulai dari awal.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        return ConversationHandler.END
    
    phone = ACCOUNT_SETUP_DATA[chat_id]['phone']
    user_info = ACCOUNT_SETUP_DATA[chat_id]['user_info']
    
    # Tambahkan akun ke daftar akun
    USER_ACCOUNTS[phone] = {
        'name': name,
        'user_id': user_info['id'],
        'username': user_info['username'],
        'first_name': user_info['first_name'],
        'last_name': user_info['last_name'],
        'added_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    # Simpan ke file
    save_user_accounts()
    
    # Hapus data setup
    if chat_id in ACCOUNT_SETUP_DATA:
        del ACCOUNT_SETUP_DATA[chat_id]
    
    # Beri tahu user
    await update.message.reply_text(
        f"{EMOJI['success']} {format_text('AKUN DITAMBAHKAN', 'bold')}\n\n"
        f"Akun {format_text(name, 'bold')} ({format_text(phone, 'code')}) berhasil ditambahkan!\n\n"
        f"Anda sekarang dapat menggunakan akun ini untuk memeriksa nomor telepon.",
        parse_mode=constants.ParseMode.HTML
    )
    
    # Tampilkan menu akun
    await account_menu(update, context)
    
    return ConversationHandler.END

@admin_only
async def cancel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Membatalkan proses setup akun."""
    chat_id = str(update.effective_chat.id)
    
    # Hapus client jika ada
    if chat_id in ACCOUNT_SETUP_DATA and 'client' in ACCOUNT_SETUP_DATA[chat_id]:
        try:
            await ACCOUNT_SETUP_DATA[chat_id]['client'].disconnect()
        except:
            pass
        
    # Hapus data setup
    if chat_id in ACCOUNT_SETUP_DATA:
        del ACCOUNT_SETUP_DATA[chat_id]
    
    await update.message.reply_text(
        f"{EMOJI['fail']} {format_text('DIBATALKAN', 'bold')}\n\n"
        f"Proses penambahan akun dibatalkan.",
        parse_mode=constants.ParseMode.HTML
    )
    
    return ConversationHandler.END

@admin_only
async def delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan daftar akun untuk dihapus."""
    query = update.callback_query
    if query:
        await query.answer()
    
    if not USER_ACCOUNTS:
        message = (
            f"{EMOJI['warning']} {format_text('TIDAK ADA AKUN', 'bold')}\n\n"
            f"Tidak ada akun Telegram yang dapat dihapus."
        )
        
        keyboard = [
            [InlineKeyboardButton(f"{EMOJI['rocket']} Kembali ke Menu Utama", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if query:
            await query.edit_message_text(
                message,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                message,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=reply_markup
            )
        return
    
    message = (
        f"{EMOJI['trash']} {format_text('HAPUS AKUN TELEGRAM', 'bold')} {EMOJI['trash']}\n\n"
        f"Pilih akun yang ingin dihapus:"
    )
    
    # Buat keyboard untuk memilih akun
    keyboard = []
    for phone, data in USER_ACCOUNTS.items():
        keyboard.append([
            InlineKeyboardButton(
                f"{EMOJI['phone']} {data['name']} ({phone})", 
                callback_data=f"confirm_delete_{phone}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(f"{EMOJI['rocket']} Kembali", callback_data="account_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(
            message,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            message,
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )

@admin_only
async def confirm_delete_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Konfirmasi penghapusan akun."""
    query = update.callback_query
    await query.answer()
    
    # Ambil nomor telepon dari callback data
    phone = query.data.replace("confirm_delete_", "")
    
    if phone not in USER_ACCOUNTS:
        await query.edit_message_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Akun dengan nomor {format_text(phone, 'code')} tidak ditemukan.",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    account_name = USER_ACCOUNTS[phone]['name']
    
    message = (
        f"{EMOJI['warning']} {format_text('KONFIRMASI HAPUS AKUN', 'bold')} {EMOJI['warning']}\n\n"
        f"Anda akan menghapus akun:\n"
        f"{EMOJI['phone']} {format_text(account_name, 'bold')} ({format_text(phone, 'code')})\n\n"
        f"Apakah Anda yakin ingin menghapus akun ini?"
    )
    
    keyboard = [
        [
            InlineKeyboardButton(f"{EMOJI['success']} Ya, Hapus", callback_data=f"delete_{phone}"),
            InlineKeyboardButton(f"{EMOJI['fail']} Batal", callback_data="delete_account")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        message,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=reply_markup
    )

@admin_only
async def delete_account_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menghapus akun Telegram."""
    query = update.callback_query
    await query.answer()
    
    # Ambil nomor telepon dari callback data
    phone = query.data.replace("delete_", "")
    
    if phone not in USER_ACCOUNTS:
        await query.edit_message_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Akun dengan nomor {format_text(phone, 'code')} tidak ditemukan.",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    account_name = USER_ACCOUNTS[phone]['name']
    
    # Hapus file sesi jika ada
    session_file = os.path.join(SESSIONS_DIR, f"{phone}.session")
    
    if os.path.exists(session_file):
        try:
            os.remove(session_file)
            logger.info(f"🗑️ File sesi {session_file} dihapus")
        except Exception as e:
            logger.error(f"❌ Error menghapus file sesi untuk {phone}: {e}")
    
    # Hapus akun dari daftar
    del USER_ACCOUNTS[phone]
    save_user_accounts()
    
    await query.edit_message_text(
        f"{EMOJI['success']} {format_text('AKUN DIHAPUS', 'bold')}\n\n"
        f"Akun {format_text(account_name, 'bold')} ({format_text(phone, 'code')}) berhasil dihapus.",
        parse_mode=constants.ParseMode.HTML
    )
    
    # Tampilkan menu akun setelah 2 detik
    await asyncio.sleep(2)
    await account_menu(update, context)

# Fungsi untuk mengelola grup target
@admin_only
async def group_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menampilkan menu grup target."""
    query = update.callback_query
    if query:
        await query.answer()
    
    banner = BANNER % (EMOJI['group'], EMOJI['group'], EMOJI['link'], EMOJI['link'])
    
    chat_id = str(update.effective_chat.id)
    group_info = TARGET_GROUPS.get(chat_id, None)
    
    if group_info:
        group_text = (
            f"{EMOJI['group']} {format_text('Grup Saat Ini:', 'bold')}\n"
            f"{EMOJI['link']} Link: {format_text(group_info['link'], 'code')}\n"
            f"{EMOJI['id']} ID: {format_text(str(group_info['id']), 'code')}\n"
            f"{EMOJI['name']} Nama: {format_text(group_info['title'], 'italic')}\n"
            f"{EMOJI['delay']} Delay: {format_text(str(group_info.get('delay', 5)), 'code')} detik\n\n"
        )
    else:
        group_text = f"{EMOJI['warning']} Belum ada grup target yang diatur.\n\n"
    
    message = (
        f"{banner}\n\n"
        f"{EMOJI['group']} {format_text('MANAJEMEN GRUP TARGET', 'bold')} {EMOJI['group']}\n\n"
        f"{group_text}"
        f"{format_text('Anda dapat mengatur grup target untuk mengirim kontak yang ditemukan.', 'italic')}"
    )
    
    # Membuat keyboard inline
    keyboard = []
    
    if group_info:
        keyboard.append([
            InlineKeyboardButton(f"{EMOJI['edit']} Ubah Grup", callback_data="set_group"),
            InlineKeyboardButton(f"{EMOJI['delay']} Atur Delay", callback_data="set_delay")
        ])
    else:
        keyboard.append([InlineKeyboardButton(f"{EMOJI['group']} Atur Grup Target", callback_data="set_group")])
    
    keyboard.append([InlineKeyboardButton(f"{EMOJI['rocket']} Kembali ke Menu Utama", callback_data="start")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )

@admin_only
async def set_group_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memulai proses pengaturan grup target."""
    query = update.callback_query
    if query:
        await query.answer()
    
    message = (
        f"{EMOJI['group']} {format_text('ATUR GRUP TARGET', 'bold')} {EMOJI['group']}\n\n"
        f"Silakan masukkan {format_text('link grup/channel', 'bold')} Telegram target.\n\n"
        f"Format: {format_text('https://t.me/username', 'code')} atau {format_text('@username', 'code')}\n\n"
        f"{EMOJI['info']} Ketik {format_text('/cancel', 'code')} untuk membatalkan."
    )
    
    if query:
        await query.edit_message_text(
            message,
            parse_mode=constants.ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            message,
            parse_mode=constants.ParseMode.HTML
        )
    
    return GROUP_LINK

@admin_only
async def group_link_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input link grup."""
    group_link = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    if group_link == '/cancel':
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('DIBATALKAN', 'bold')}\n\n"
            f"Proses pengaturan grup dibatalkan.",
            parse_mode=constants.ParseMode.HTML
        )
        await group_menu(update, context)
        return ConversationHandler.END
    
    # Cek jika tidak ada akun yang tersedia
    if not USER_ACCOUNTS:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Tidak ada akun Telegram yang tersedia.\n\n"
            f"Silakan tambahkan akun Telegram terlebih dahulu.",
            parse_mode=constants.ParseMode.HTML
        )
        await account_menu(update, context)
        return ConversationHandler.END
    
    # Handle username format
    if group_link.startswith('@'):
        group_link = f"https://t.me/{group_link[1:]}"
    elif not group_link.startswith('https://t.me/'):
        # Coba tambahkan https://t.me/ jika belum ada
        if not group_link.startswith('t.me/'):
            group_link = f"https://t.me/{group_link}"
        else:
            group_link = f"https://{group_link}"
    
    # Ambil username dari link
    try:
        username = group_link.split('t.me/')[1].split('/')[0]
    except:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Format link tidak valid.\n\n"
            f"Silakan masukkan format yang benar: {format_text('https://t.me/username', 'code')} atau {format_text('@username', 'code')}\n\n"
            f"Coba lagi:",
            parse_mode=constants.ParseMode.HTML
        )
        return GROUP_LINK
    
    # Verifikasi grup dengan akun Telegram
    await update.message.reply_text(
        f"{EMOJI['loading']} {format_text('MEMERIKSA GRUP', 'bold')}\n\n"
        f"Sedang memeriksa grup {format_text(group_link, 'code')}...\n\n"
        f"Harap tunggu sebentar.",
        parse_mode=constants.ParseMode.HTML
    )
    
    # Pilih akun pertama untuk verifikasi
    first_phone = list(USER_ACCOUNTS.keys())[0]
    client = await init_telethon_client(first_phone)
    
    if not client:
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Tidak dapat terhubung ke akun Telegram. Silakan coba lagi nanti.",
            parse_mode=constants.ParseMode.HTML
        )
        return ConversationHandler.END
    
    try:
        # Coba mendapatkan informasi tentang grup/channel
        entity = await client.get_entity(username)
        
        # Simpan informasi grup
        TARGET_GROUPS[chat_id] = {
            'link': group_link,
            'username': username,
            'id': entity.id,
            'title': entity.title if hasattr(entity, 'title') else username,
            'delay': 5  # Default delay
        }
        
        # Simpan ke file
        save_target_groups()
        
        # Tampilkan informasi grup
        await update.message.reply_text(
            f"{EMOJI['success']} {format_text('GRUP DIATUR', 'bold')}\n\n"
            f"Grup target berhasil diatur!\n\n"
            f"{EMOJI['name']} Nama: {format_text(entity.title if hasattr(entity, 'title') else username, 'bold')}\n"
            f"{EMOJI['link']} Link: {format_text(group_link, 'code')}\n"
            f"{EMOJI['id']} ID: {format_text(str(entity.id), 'code')}\n\n"
            f"Sekarang silakan atur {format_text('delay', 'bold')} antara pengecekan (dalam detik):",
            parse_mode=constants.ParseMode.HTML
        )
        
        await client.disconnect()
        return DELAY_INPUT
    
    except Exception as e:
        logger.error(f"❌ Error mendapatkan entitas grup untuk {username}: {e}")
        await client.disconnect()
        
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
            f"Terjadi kesalahan saat memeriksa grup: {format_text(str(e), 'code')}\n\n"
            f"Silakan coba lagi dengan link grup yang valid:",
            parse_mode=constants.ParseMode.HTML
        )
        
        return GROUP_LINK

@admin_only
async def delay_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Menerima input delay."""
    delay_text = update.message.text.strip()
    chat_id = str(update.effective_chat.id)
    
    if delay_text == '/cancel':
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('DIBATALKAN', 'bold')}\n\n"
            f"Proses pengaturan delay dibatalkan.",
            parse_mode=constants.ParseMode.HTML
        )
        await group_menu(update, context)
        return ConversationHandler.END
    
    # Validasi delay
    try:
        delay = int(delay_text)
        if delay < 1 or delay > 60:
            raise ValueError("Delay harus antara 1-60 detik")
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Delay harus berupa angka antara 1-60 detik.\n\n"
            f"Silakan coba lagi:",
            parse_mode=constants.ParseMode.HTML
        )
        return DELAY_INPUT
    
    # Simpan delay
    if chat_id in TARGET_GROUPS:
        TARGET_GROUPS[chat_id]['delay'] = delay
        save_target_groups()
    
    # Beri tahu user
    await update.message.reply_text(
        f"{EMOJI['success']} {format_text('DELAY DIATUR', 'bold')}\n\n"
        f"Delay berhasil diatur menjadi {format_text(str(delay), 'bold')} detik.\n\n"
        f"Pengaturan grup target selesai!",
        parse_mode=constants.ParseMode.HTML
    )
    
    # Tampilkan menu grup
    await group_menu(update, context)
    
    return ConversationHandler.END

@admin_only
async def set_delay_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Memulai proses pengaturan delay."""
    query = update.callback_query
    if query:
        await query.answer()
    
    chat_id = str(update.effective_chat.id)
    current_delay = TARGET_GROUPS.get(chat_id, {}).get('delay', 5)
    
    message = (
        f"{EMOJI['delay']} {format_text('ATUR DELAY', 'bold')} {EMOJI['delay']}\n\n"
        f"Delay saat ini: {format_text(str(current_delay), 'bold')} detik\n\n"
        f"Silakan masukkan {format_text('delay', 'bold')} baru antara 1-60 detik:\n\n"
        f"{EMOJI['info']} Ketik {format_text('/cancel', 'code')} untuk membatalkan."
    )
    
    if query:
        await query.edit_message_text(
            message,
            parse_mode=constants.ParseMode.HTML
        )
    else:
        await update.message.reply_text(
            message,
            parse_mode=constants.ParseMode.HTML
        )
    
    return DELAY_INPUT

@admin_only
async def process_vcf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Memproses file VCF yang dikirim."""
    # Cek jika ada akun Telegram yang tersedia
    if not USER_ACCOUNTS:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Tidak ada akun Telegram yang tersedia.\n\n"
            f"Silakan tambahkan akun Telegram terlebih dahulu dengan klik menu {format_text('Akun Telegram', 'bold')}.",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Cek jika ada grup target yang diatur
    chat_id = str(update.effective_chat.id)
    if chat_id not in TARGET_GROUPS:
        await update.message.reply_text(
            f"{EMOJI['warning']} {format_text('ERROR:', 'bold')} Tidak ada grup target yang diatur.\n\n"
            f"Silakan atur grup target terlebih dahulu dengan klik menu {format_text('Grup Target', 'bold')}.",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Mendapatkan file VCF
    file = await update.message.document.get_file()
    
    # Memberi tahu user bahwa proses sedang berjalan
    banner = BANNER % (EMOJI['loading'], EMOJI['loading'], EMOJI['search'], EMOJI['search'])
    processing_msg = await update.message.reply_text(
        f"{banner}\n\n"
        f"{EMOJI['loading']} {format_text('MEMPROSES FILE VCF', 'bold')}\n\n"
        f"Status: {format_text('Mengunduh file...', 'italic')}\n"
        f"{LOADING_PATTERNS[0]}",
        parse_mode=constants.ParseMode.HTML
    )
    
    # Inisialisasi status pemrosesan
    PROCESSING_STATUS[chat_id] = {
        'message_id': processing_msg.message_id,
        'step': 0,
        'total_steps': 100
    }
    
    # Download file VCF
    vcf_data = await file.download_as_bytearray()
    
    # Update status
    await update_progress_message(context, chat_id, 10, "Menganalisis file...")
    
    vcf_stream = BytesIO(vcf_data)
    vcf_content = vcf_stream.getvalue().decode('utf-8', errors='ignore')
    
    # Mengekstrak nomor telepon dari file VCF
    phone_numbers = extract_phone_numbers(vcf_content)
    
    if not phone_numbers:
        banner = BANNER % (EMOJI['fail'], EMOJI['fail'], EMOJI['fail'], EMOJI['fail'])
        await processing_msg.edit_text(
            f"{banner}\n\n"
            f"{EMOJI['fail']} {format_text('PROSES GAGAL', 'bold')}\n\n"
            f"Status: {format_text('Tidak ada nomor telepon yang ditemukan di file VCF.', 'italic')}\n"
            f"{LOADING_PATTERNS[0]}",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Update status
    await update_progress_message(context, chat_id, 20, f"Ditemukan {len(phone_numbers)} nomor telepon...")
    
    # Setup accounts for checking
    available_accounts = list(USER_ACCOUNTS.keys())
    if not available_accounts:
        banner = BANNER % (EMOJI['fail'], EMOJI['fail'], EMOJI['fail'], EMOJI['fail'])
        await processing_msg.edit_text(
            f"{banner}\n\n"
            f"{EMOJI['fail']} {format_text('PROSES GAGAL', 'bold')}\n\n"
            f"Status: {format_text('Tidak ada akun Telegram yang tersedia.', 'italic')}\n"
            f"{LOADING_PATTERNS[0]}",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Inisialisasi client Telethon untuk akun pertama
    current_account_index = 0
    current_phone = available_accounts[current_account_index]
    client = await init_telethon_client(current_phone)
    
    if not client:
        banner = BANNER % (EMOJI['fail'], EMOJI['fail'], EMOJI['fail'], EMOJI['fail'])
        await processing_msg.edit_text(
            f"{banner}\n\n"
            f"{EMOJI['fail']} {format_text('PROSES GAGAL', 'bold')}\n\n"
            f"Status: {format_text('Tidak dapat terhubung ke akun Telegram.', 'italic')}\n"
            f"{LOADING_PATTERNS[0]}",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Akses grup target
    target_group_info = TARGET_GROUPS[chat_id]
    target_group_entity = None
    try:
        target_group_entity = await client.get_entity(target_group_info['username'])
        logger.info(f"✅ Berhasil mengakses grup target: {target_group_info['title']}")
    except Exception as e:
        logger.error(f"❌ Error mengakses grup target: {e}")
        await client.disconnect()
        
        banner = BANNER % (EMOJI['fail'], EMOJI['fail'], EMOJI['fail'], EMOJI['fail'])
        await processing_msg.edit_text(
            f"{banner}\n\n"
            f"{EMOJI['fail']} {format_text('PROSES GAGAL', 'bold')}\n\n"
            f"Status: {format_text(f'Tidak dapat mengakses grup target: {str(e)}', 'italic')}\n"
            f"{LOADING_PATTERNS[0]}",
            parse_mode=constants.ParseMode.HTML
        )
        return
    
    # Bergabung dengan grup jika belum
    try:
        await client(JoinChannelRequest(target_group_entity))
        logger.info(f"✅ Bergabung dengan grup target: {target_group_info['title']}")
    except Exception as e:
        # Ignoring errors here, as the user might already be in the group
        logger.info(f"ℹ️ Error bergabung grup (kemungkinan sudah bergabung): {e}")
    
    # Memeriksa nomor telepon
    results = await check_telegram_accounts_and_send(
        client, 
        phone_numbers, 
        chat_id, 
        context, 
        target_group_entity,
        target_group_info['delay'],
        available_accounts,
        current_account_index
    )
    
    # Memproses hasil
    registered_count = sum(1 for result in results if result['registered'])
    sent_count = sum(1 for result in results if result.get('sent_to_group', False))
    
    # Menyimpan hasil untuk digunakan nanti
    RESULTS[chat_id] = results
    
    # Reset pagination untuk chat ini
    PAGINATION[chat_id] = {
        'current_page': 1,
        'registered_page': 1,
        'non_registered_page': 1,
        'total_registered': registered_count,
        'total_non_registered': len(phone_numbers) - registered_count,
        'items_per_page': ITEMS_PER_PAGE
    }
    
    # Update status
    await update_progress_message(context, chat_id, 100, "Selesai! Menyiapkan hasil...")
    
    # Menyiapkan pesan hasil
    banner = BANNER % (EMOJI['complete'], EMOJI['complete'], EMOJI['diamond'], EMOJI['diamond'])
    result_text = (
        f"{banner}\n\n"
        f"{EMOJI['complete']} {format_text('PROSES SELESAI', 'bold')} {EMOJI['complete']}\n\n"
        f"{EMOJI['success']} Berhasil memeriksa {format_text(str(len(phone_numbers)), 'code')} nomor telepon.\n"
        f"{EMOJI['registered']} {format_text(str(registered_count), 'bold')} nomor terdaftar di Telegram.\n"
        f"{EMOJI['send']} {format_text(str(sent_count), 'bold')} kontak telah dikirim ke grup target.\n"
        f"{EMOJI['not_registered']} {format_text(str(len(phone_numbers) - registered_count), 'bold')} nomor tidak terdaftar.\n\n"
        f"{format_text('Silakan pilih opsi di bawah ini:', 'italic')}"
    )
    
    # Membuat tombol inline
    keyboard = [
        [
            InlineKeyboardButton(f"{EMOJI['download']} Download VCF Telegram", callback_data="download_vcf")
        ],
        [
            InlineKeyboardButton(f"{EMOJI['registered']} Lihat Terdaftar", callback_data="view_registered"),
            InlineKeyboardButton(f"{EMOJI['not_registered']} Lihat Tidak Terdaftar", callback_data="view_non_registered")
        ],
        [
            InlineKeyboardButton(f"{EMOJI['refresh']} Proses Ulang", callback_data="reprocess"),
            InlineKeyboardButton(f"{EMOJI['rocket']} Menu Utama", callback_data="start")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Mengirim pesan ringkasan
    await processing_msg.edit_text(
        result_text, 
        parse_mode=constants.ParseMode.HTML,
        reply_markup=reply_markup
    )
    
    # Hapus status pemrosesan
    if chat_id in PROCESSING_STATUS:
        del PROCESSING_STATUS[chat_id]

async def update_progress_message(context, chat_id, progress, status_text):
    """Memperbarui pesan progress."""
    if chat_id not in PROCESSING_STATUS:
        return
    
    message_id = PROCESSING_STATUS[chat_id]['message_id']
    
    # Perbarui status
    PROCESSING_STATUS[chat_id]['step'] = progress
    
    # Hitung indeks loading pattern
    loading_index = min(int(progress / 10), 10)
    loading_pattern = LOADING_PATTERNS[loading_index]
    
    # Perbarui pesan
    banner = BANNER % (EMOJI['loading'], EMOJI['loading'], EMOJI['search'], EMOJI['search'])
    
    try:
        await context.bot.edit_message_text(
            chat_id=int(chat_id),
            message_id=message_id,
            text=(
                f"{banner}\n\n"
                f"{EMOJI['loading']} {format_text('MEMPROSES FILE VCF', 'bold')}\n\n"
                f"Status: {format_text(status_text, 'italic')}\n"
                f"{loading_pattern}"
            ),
            parse_mode=constants.ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"❌ Error memperbarui pesan progress: {e}")

def extract_phone_numbers(vcf_content):
    """Mengekstrak nomor telepon dari konten VCF."""
    phone_data = []
    
    # Split VCF file menjadi entri-entri individu
    vcards = vcf_content.split("BEGIN:VCARD")
    
    for vcard in vcards:
        if not vcard.strip():
            continue
        
        try:
            # Pastikan format vCard lengkap untuk parsing
            if not vcard.startswith("BEGIN:"):
                vcard = "BEGIN:VCARD" + vcard
                
            # Parse vCard
            parsed_card = vobject.readOne(vcard)
            
            name = ""
            if hasattr(parsed_card, 'fn'):
                name = parsed_card.fn.value
            
            # Mengambil semua nomor telepon dari vCard
            phone_numbers = []
            if hasattr(parsed_card, 'tel'):
                if isinstance(parsed_card.tel, list):
                    for tel in parsed_card.tel:
                        phone_numbers.append(tel.value)
                else:
                    phone_numbers.append(parsed_card.tel.value)
            
            # Sanitasi nomor telepon dan tambahkan ke hasil
            for phone in phone_numbers:
                # Membersihkan nomor telepon dari karakter non-digit
                clean_number = re.sub(r'\D', '', phone)
                
                # Jika nomor tidak dimulai dengan +, tambahkan +
                if not clean_number.startswith('+'):
                    if clean_number.startswith('0'):
                        # Asumsi nomor Indonesia, ganti 0 dengan kode negara +62
                        clean_number = '+62' + clean_number[1:]
                    else:
                        clean_number = '+' + clean_number
                
                phone_data.append({
                    'name': name,
                    'phone': clean_number,
                    'original_vcard': vcard
                })
        except Exception as e:
            logger.error(f"❌ Error parsing vCard: {e}")
            continue
    
    return phone_data

async def check_telegram_accounts_and_send(client, phone_data, chat_id, context, target_group, delay, available_accounts, current_account_index):
    """Memeriksa apakah nomor telepon memiliki akun Telegram dan mengirim ke grup target."""
    results = []
    total_numbers = len(phone_data)
    
    for i, data in enumerate(phone_data):
        phone = data['phone']
        name = data['name']
        original_vcard = data['original_vcard']
        
        # Update progress (dari 20% hingga 90%)
        progress = 20 + int((i / total_numbers) * 70)
        await update_progress_message(
            context, 
            chat_id, 
            progress, 
            f"Memeriksa {i+1}/{total_numbers}: {name} ({phone})"
        )
        
        try:
            # Membuat InputPhoneContact untuk diimpor
            contact = InputPhoneContact(
                client_id=0,
                phone=phone,
                first_name=name,
                last_name=""
            )
            
            # Mengimpor kontak
            logger.info(f"🔍 Memeriksa nomor: {phone} ({name})")
            imported = await client(ImportContactsRequest([contact]))
            
            # Jika user ditemukan
            if imported.users:
                user = imported.users[0]
                
                # Mendapatkan informasi lengkap tentang user
                try:
                    full_user = await client(functions.users.GetFullUserRequest(user.id))
                    
                    # Format last seen
                    last_seen = "Tidak tersedia"
                    if hasattr(user, 'status') and user.status:
                        if hasattr(user.status, 'was_online') and user.status.was_online:
                            last_seen = user.status.was_online.strftime("%Y-%m-%d %H:%M:%S")
                        elif isinstance(user.status, types.UserStatusOnline):
                            last_seen = "Online"
                        elif isinstance(user.status, types.UserStatusRecently):
                            last_seen = "Terlihat baru-baru ini"
                        elif isinstance(user.status, types.UserStatusLastWeek):
                            last_seen = "Terlihat minggu ini"
                        elif isinstance(user.status, types.UserStatusLastMonth):
                            last_seen = "Terlihat bulan ini"
                    
                    # Coba kirim kontak ke grup target
                    sent_to_group = False
                    send_error = None
                    
                    try:
                        # Update status
                        await update_progress_message(
                            context, 
                            chat_id, 
                            progress, 
                            f"Mengirim kontak {name} ({phone}) ke grup target..."
                        )
                        
                        # Kirim kontak ke grup
                        await client.send_file(
                            target_group,
                            types.InputMediaContact(
                                phone_number=phone,
                                first_name=name,
                                last_name="",
                                vcard=""
                            )
                        )
                        
                        sent_to_group = True
                        logger.info(f"✅ Kontak berhasil dikirim ke grup: {phone} ({name})")
                    except Exception as e:
                        send_error = str(e)
                        logger.error(f"❌ Error mengirim kontak ke grup: {e}")
                    
                    # Menyimpan hasil
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': True,
                        'original_vcard': original_vcard,
                        'sent_to_group': sent_to_group,
                        'send_error': send_error,
                        'telegram_info': {
                            'id': user.id,
                            'username': user.username if user.username else "Tidak ada",
                            'first_name': user.first_name if user.first_name else "Tidak ada",
                            'last_name': user.last_name if user.last_name else "Tidak ada",
                            'last_seen': last_seen,
                            'bio': full_user.full_user.about if full_user.full_user.about else "Tidak ada",
                            'profile_photo': user.photo is not None,
                            'is_bot': user.bot if hasattr(user, 'bot') else False,
                            'is_premium': user.premium if hasattr(user, 'premium') else False,
                        }
                    })
                except Exception as e:
                    logger.error(f"❌ Error mendapatkan info lengkap user: {e}")
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': True,
                        'original_vcard': original_vcard,
                        'sent_to_group': False,
                        'send_error': str(e),
                        'telegram_info': {
                            'id': user.id,
                            'username': user.username if user.username else "Tidak ada",
                            'first_name': user.first_name if user.first_name else "Tidak ada",
                            'last_name': user.last_name if user.last_name else "Tidak ada",
                            'last_seen': "Tidak tersedia",
                            'bio': "Tidak tersedia",
                            'profile_photo': user.photo is not None,
                            'is_bot': False,
                            'is_premium': False,
                        }
                    })
            else:
                # Nomor tidak terdaftar di Telegram
                logger.info(f"❌ Nomor tidak terdaftar di Telegram: {phone} ({name})")
                results.append({
                    'name': name,
                    'phone': phone,
                    'registered': False,
                    'original_vcard': original_vcard,
                    'sent_to_group': False,
                })
            
            # Menghapus kontak yang baru diimpor
            if imported.users:
                await client(functions.contacts.DeleteContactsRequest(id=[user.id]))
            
            # Delay untuk menghindari rate limiting
            await asyncio.sleep(delay)
            
        except errors.FloodWaitError as e:
            logger.warning(f"⚠️ Rate limit terlampaui, menunggu {e.seconds} detik")
            
            # Update status
            await update_progress_message(
                context, 
                chat_id, 
                progress, 
                f"⚠️ Batas rate terlampaui. Menunggu {e.seconds} detik..."
            )
            
            # Disconnect current client
            try:
                await client.disconnect()
            except:
                pass
            
            # Switch to next account if available
            current_account_index = (current_account_index + 1) % len(available_accounts)
            current_phone = available_accounts[current_account_index]
            
            # Update status
            await update_progress_message(
                context, 
                chat_id, 
                progress, 
                f"Beralih ke akun: {USER_ACCOUNTS[current_phone]['name']}..."
            )
            
            # Connect with new account
            client = await init_telethon_client(current_phone)
            
            if not client:
                logger.error(f"❌ Gagal terhubung dengan akun {current_phone}")
                # Try next account
                continue
            
            # Try to join the group with new account
            try:
                target_group_entity = await client.get_entity(target_group)
                await client(JoinChannelRequest(target_group_entity))
                logger.info(f"✅ Bergabung dengan grup target menggunakan akun baru: {current_phone}")
            except Exception as e:
                # Ignoring errors here, as the user might already be in the group
                logger.info(f"ℹ️ Error bergabung grup dengan akun baru (kemungkinan sudah bergabung): {e}")
            
            # Wait the required time from FloodWaitError
            if e.seconds > 0:
                await asyncio.sleep(e.seconds)
            
            # Try again with the same contact
            try:
                # Membuat InputPhoneContact untuk diimpor
                contact = InputPhoneContact(
                    client_id=0,
                    phone=phone,
                    first_name=name,
                    last_name=""
                )
                
                imported = await client(ImportContactsRequest([contact]))
                
                # Process like before
                if imported.users:
                    user = imported.users[0]
                    
                    # Menyimpan hasil (simplified)
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': True,
                        'original_vcard': original_vcard,
                        'sent_to_group': False,  # Don't try to send again
                        'send_error': "Rate limit exceeded, switched account",
                        'telegram_info': {
                            'id': user.id,
                            'username': user.username if user.username else "Tidak ada",
                            'first_name': user.first_name if user.first_name else "Tidak ada",
                            'last_name': user.last_name if user.last_name else "Tidak ada",
                            'last_seen': "Tidak tersedia",
                            'bio': "Tidak tersedia",
                            'profile_photo': user.photo is not None,
                            'is_bot': False,
                            'is_premium': False,
                        }
                    })
                    
                    # Menghapus kontak
                    await client(functions.contacts.DeleteContactsRequest(id=[user.id]))
                else:
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': False,
                        'original_vcard': original_vcard,
                        'sent_to_group': False,
                    })
            except Exception as e:
                logger.error(f"❌ Error memeriksa nomor setelah FloodWaitError: {e}")
                results.append({
                    'name': name,
                    'phone': phone,
                    'registered': False,
                    'original_vcard': original_vcard,
                    'sent_to_group': False,
                })
            
        except errors.AuthKeyUnregisteredError:
            logger.error(f"❌ Auth key akun kedaluwarsa, beralih ke akun lain...")
            
            # Update status
            await update_progress_message(
                context, 
                chat_id, 
                progress, 
                f"⚠️ Sesi akun kedaluwarsa. Beralih ke akun lain..."
            )
            
            # Disconnect current client
            try:
                await client.disconnect()
            except:
                pass
            
            # Switch to next account if available
            current_account_index = (current_account_index + 1) % len(available_accounts)
            current_phone = available_accounts[current_account_index]
            
            # Connect with new account
            client = await init_telethon_client(current_phone)
            
            if not client:
                logger.error(f"❌ Gagal terhubung dengan akun {current_phone}")
                # Try next account
                continue
            
            # Add contact to results as failed
            results.append({
                'name': name,
                'phone': phone,
                'registered': False,
                'original_vcard': original_vcard,
                'sent_to_group': False,
                'send_error': "Auth key expired, switched account",
            })
            
        except Exception as e:
            logger.error(f"❌ Error memeriksa nomor {phone}: {e}")
            results.append({
                'name': name,
                'phone': phone,
                'registered': False,
                'original_vcard': original_vcard,
                'sent_to_group': False,
                'send_error': str(e),
            })
    
    # Disconnect client at the end
    try:
        await client.disconnect()
    except:
        pass
    
    return results

@admin_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menangani callback dari tombol inline."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    chat_id = str(update.effective_chat.id)
    
    # Menu navigasi utama
    if data == "start":
        await start(update, context)
    elif data == "help":
        await help_command(update, context)
    elif data == "admin_menu":
        # Menampilkan menu admin
        banner = BANNER % (EMOJI['admin'], EMOJI['admin'], EMOJI['crown'], EMOJI['crown'])
        
        admin_list = "\n".join([f"{EMOJI['id']} {format_text(str(admin_id), 'code')}" for admin_id in ADMIN_LIST])
        
        message = (
            f"{banner}\n\n"
            f"{EMOJI['admin']} {format_text('MENU ADMIN', 'bold')} {EMOJI['admin']}\n\n"
            f"{format_text('Daftar Admin:', 'bold')}\n"
            f"{admin_list}\n\n"
            f"{format_text('Menambahkan Admin:', 'bold')}\n"
            f"{EMOJI['settings']} Gunakan {format_text('/addadmin [user_id]', 'code')}\n\n"
            f"{format_text('Menghapus Admin:', 'bold')}\n"
            f"{EMOJI['trash']} Gunakan {format_text('/removeadmin [user_id]', 'code')}"
        )
        
        # Membuat keyboard inline
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI['settings']} Tambah Admin", callback_data="add_admin_prompt"),
                InlineKeyboardButton(f"{EMOJI['trash']} Hapus Admin", callback_data="remove_admin_prompt")
            ],
            [InlineKeyboardButton(f"{EMOJI['rocket']} Kembali ke Menu Utama", callback_data="start")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
    elif data == "account_menu":
        await account_menu(update, context)
    elif data == "group_menu":
        await group_menu(update, context)
    
    # Menu admin
    elif data == "add_admin_prompt":
        message = (
            f"{EMOJI['settings']} {format_text('TAMBAH ADMIN', 'bold')}\n\n"
            f"Untuk menambahkan admin baru, gunakan perintah:\n"
            f"{format_text('/addadmin [user_id]', 'code')}\n\n"
            f"Contoh: {format_text('/addadmin 123456789', 'code')}"
        )
        
        keyboard = [
            [InlineKeyboardButton(f"{EMOJI['admin']} Kembali ke Menu Admin", callback_data="admin_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
    
    elif data == "remove_admin_prompt":
        message = (
            f"{EMOJI['trash']} {format_text('HAPUS ADMIN', 'bold')}\n\n"
            f"Untuk menghapus admin, gunakan perintah:\n"
            f"{format_text('/removeadmin [user_id]', 'code')}\n\n"
            f"Contoh: {format_text('/removeadmin 123456789', 'code')}"
        )
        
        keyboard = [
            [InlineKeyboardButton(f"{EMOJI['admin']} Kembali ke Menu Admin", callback_data="admin_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
    
    # Menu akun
    elif data == "add_account":
        return await add_account_start(update, context)
    
    elif data == "delete_account":
        await delete_account(update, context)
    
    elif data.startswith("confirm_delete_"):
        await confirm_delete_account(update, context)
    
    elif data.startswith("delete_"):
        await delete_account_confirmed(update, context)
    
    # Menu grup
    elif data == "set_group":
        return await set_group_start(update, context)
    
    elif data == "set_delay":
        return await set_delay_start(update, context)
    
    # Memproses hasil VCF
    elif data in ["download_vcf", "view_registered", "view_non_registered", "next_registered", "prev_registered", "next_non_registered", "prev_non_registered", "reprocess"]:
        if chat_id not in RESULTS:
            banner = BANNER % (EMOJI['fail'], EMOJI['fail'], EMOJI['warning'], EMOJI['warning'])
            await query.edit_message_text(
                f"{banner}\n\n"
                f"{EMOJI['fail']} {format_text('ERROR', 'bold')}\n\n"
                f"Data tidak ditemukan. Silakan kirim file VCF lagi.",
                parse_mode=constants.ParseMode.HTML
            )
            return
        
        results = RESULTS[chat_id]
        pagination = PAGINATION[chat_id]
        
        if data == "download_vcf":
            # Membuat file VCF baru yang hanya berisi kontak Telegram
            telegram_contacts = [r for r in results if r['registered']]
            
            if not telegram_contacts:
                banner = BANNER % (EMOJI['warning'], EMOJI['warning'], EMOJI['fail'], EMOJI['fail'])
                await query.edit_message_text(
                    f"{banner}\n\n"
                    f"{EMOJI['fail']} {format_text('TIDAK ADA KONTAK TELEGRAM', 'bold')}\n\n"
                    f"Tidak ada kontak yang terdaftar di Telegram.",
                    parse_mode=constants.ParseMode.HTML,
                    reply_markup=query.message.reply_markup
                )
                return
            
            # Membuat file VCF baru
            vcf_content = ""
            for contact in telegram_contacts:
                vcf_content += contact['original_vcard']
            
            # Mengirim file VCF
            vcf_bytes = BytesIO(vcf_content.encode('utf-8'))
            vcf_bytes.name = f"telegram_contacts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.vcf"
            
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=vcf_bytes,
                filename=vcf_bytes.name,
                caption=(
                    f"{EMOJI['download']} {format_text('FILE VCF TELEGRAM', 'bold')}\n\n"
                    f"File berisi {len(telegram_contacts)} kontak yang terdaftar di Telegram."
                ),
                parse_mode=constants.ParseMode.HTML
            )
        
        elif data == "view_registered":
            # Tampilkan kontak yang terdaftar di Telegram
            registered_contacts = [r for r in results if r['registered']]
            
            if not registered_contacts:
                banner = BANNER % (EMOJI['warning'], EMOJI['warning'], EMOJI['fail'], EMOJI['fail'])
                await query.edit_message_text(
                    f"{banner}\n\n"
                    f"{EMOJI['fail']} {format_text('TIDAK ADA KONTAK TELEGRAM', 'bold')}\n\n"
                    f"Tidak ada kontak yang terdaftar di Telegram.",
                    parse_mode=constants.ParseMode.HTML,
                    reply_markup=query.message.reply_markup
                )
                return
            
            # Set halaman saat ini
            current_page = pagination['registered_page']
            total_pages = (len(registered_contacts) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            
            # Ambil kontak untuk halaman saat ini
            start_idx = (current_page - 1) * ITEMS_PER_PAGE
            end_idx = min(start_idx + ITEMS_PER_PAGE, len(registered_contacts))
            current_contacts = registered_contacts[start_idx:end_idx]
            
            # Buat pesan detail
            detail_text = f"{EMOJI['registered']} {format_text('KONTAK TERDAFTAR DI TELEGRAM', 'bold')} {EMOJI['registered']}\n\n"
            detail_text += f"Halaman {format_text(str(current_page), 'code')} dari {format_text(str(total_pages), 'code')}\n\n"
            
            for contact in current_contacts:
                info = contact['telegram_info']
                
                # Format username sebagai link jika ada
                username_display = f"@{info['username']}" if info['username'] != "Tidak ada" else "Tidak ada"
                if info['username'] != "Tidak ada":
                    username_display = f"<a href='https://t.me/{info['username']}'>{username_display}</a>"
                
                # Format tampilan profil
                detail_text += f"{'━' * 30}\n\n"
                detail_text += f"{EMOJI['name']} {format_text('Nama:', 'bold')} {contact['name']}\n"
                detail_text += f"{EMOJI['phone']} {format_text('Nomor:', 'bold')} {format_text(contact['phone'], 'code')}\n"
                detail_text += f"{EMOJI['id']} {format_text('ID Telegram:', 'bold')} {format_text(str(info['id']), 'code')}\n"
                detail_text += f"{EMOJI['username']} {format_text('Username:', 'bold')} {username_display}\n"
                detail_text += f"{EMOJI['first_name']} {format_text('Nama Depan:', 'bold')} {info['first_name']}\n"
                
                if info['last_name'] != "Tidak ada":
                    detail_text += f"{EMOJI['last_name']} {format_text('Nama Belakang:', 'bold')} {info['last_name']}\n"
                
                detail_text += f"{EMOJI['last_seen']} {format_text('Terakhir Dilihat:', 'bold')} {info['last_seen']}\n"
                
                if info['bio'] != "Tidak ada":
                    detail_text += f"{EMOJI['bio']} {format_text('Bio:', 'bold')} {info['bio']}\n"
                
                detail_text += f"{EMOJI['photo']} {format_text('Foto Profil:', 'bold')} {'Ada' if info['profile_photo'] else 'Tidak ada'}\n"
                detail_text += f"{EMOJI['bot']} {format_text('Bot:', 'bold')} {'Ya' if info['is_bot'] else 'Tidak'}\n"
                detail_text += f"{EMOJI['premium']} {format_text('Premium:', 'bold')} {'Ya' if info['is_premium'] else 'Tidak'}\n"
                
                # Tampilkan status pengiriman ke grup
                if 'sent_to_group' in contact:
                    if contact['sent_to_group']:
                        detail_text += f"{EMOJI['send']} {format_text('Dikirim ke Grup:', 'bold')} {EMOJI['success']} Berhasil\n"
                    else:
                        error_msg = contact.get('send_error', 'Gagal')
                        detail_text += f"{EMOJI['send']} {format_text('Dikirim ke Grup:', 'bold')} {EMOJI['fail']} {error_msg}\n"
                
                detail_text += "\n"
            
            # Buat tombol navigasi
            keyboard = []
            
            # Tombol navigasi (prev/next)
            nav_buttons = []
            if current_page > 1:
                nav_buttons.append(InlineKeyboardButton(f"{EMOJI['prev_page']} Sebelumnya", callback_data="prev_registered"))
            
            if current_page < total_pages:
                nav_buttons.append(InlineKeyboardButton(f"{EMOJI['next_page']} Berikutnya", callback_data="next_registered"))
            
            if nav_buttons:
                keyboard.append(nav_buttons)
            
            # Tombol kembali
            keyboard.append([
                InlineKeyboardButton(f"{EMOJI['view']} Lihat Tidak Terdaftar", callback_data="view_non_registered"),
                InlineKeyboardButton(f"{EMOJI['download']} Download VCF", callback_data="download_vcf")
            ])
            keyboard.append([InlineKeyboardButton(f"{EMOJI['rocket']} Menu Utama", callback_data="start")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            banner = BANNER % (EMOJI['registered'], EMOJI['registered'], EMOJI['phone'], EMOJI['phone'])
            message = f"{banner}\n\n{detail_text}"
            
            # Kirim pesan
            await query.edit_message_text(
                message,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
        
        elif data == "next_registered":
            # Halaman berikutnya untuk kontak terdaftar
            pagination['registered_page'] += 1
            query.data = "view_registered"
            await button_callback(update, context)
        
        elif data == "prev_registered":
            # Halaman sebelumnya untuk kontak terdaftar
            pagination['registered_page'] -= 1
            query.data = "view_registered"
            await button_callback(update, context)
        
        elif data == "view_non_registered":
            # Tampilkan kontak yang tidak terdaftar di Telegram
            non_registered_contacts = [r for r in results if not r['registered']]
            
            if not non_registered_contacts:
                banner = BANNER % (EMOJI['warning'], EMOJI['warning'], EMOJI['success'], EMOJI['success'])
                await query.edit_message_text(
                    f"{banner}\n\n"
                    f"{EMOJI['success']} {format_text('SEMUA KONTAK TERDAFTAR', 'bold')}\n\n"
                    f"Semua kontak telah terdaftar di Telegram.",
                    parse_mode=constants.ParseMode.HTML,
                    reply_markup=query.message.reply_markup
                )
                return
            
            # Set halaman saat ini
            current_page = pagination['non_registered_page']
            total_pages = (len(non_registered_contacts) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
            
            # Ambil kontak untuk halaman saat ini
            start_idx = (current_page - 1) * ITEMS_PER_PAGE
            end_idx = min(start_idx + ITEMS_PER_PAGE, len(non_registered_contacts))
            current_contacts = non_registered_contacts[start_idx:end_idx]
            
            # Buat pesan detail
            detail_text = f"{EMOJI['not_registered']} {format_text('KONTAK TIDAK TERDAFTAR DI TELEGRAM', 'bold')} {EMOJI['not_registered']}\n\n"
            detail_text += f"Halaman {format_text(str(current_page), 'code')} dari {format_text(str(total_pages), 'code')}\n\n"
            
            for contact in current_contacts:
                detail_text += f"{'━' * 30}\n\n"
                detail_text += f"{EMOJI['name']} {format_text('Nama:', 'bold')} {contact['name']}\n"
                detail_text += f"{EMOJI['phone']} {format_text('Nomor:', 'bold')} {format_text(contact['phone'], 'code')}\n\n"
            
            # Buat tombol navigasi
            keyboard = []
            
            # Tombol navigasi (prev/next)
            nav_buttons = []
            if current_page > 1:
                nav_buttons.append(InlineKeyboardButton(f"{EMOJI['prev_page']} Sebelumnya", callback_data="prev_non_registered"))
            
            if current_page < total_pages:
                nav_buttons.append(InlineKeyboardButton(f"{EMOJI['next_page']} Berikutnya", callback_data="next_non_registered"))
            
            if nav_buttons:
                keyboard.append(nav_buttons)
            
            # Tombol kembali
            keyboard.append([
                InlineKeyboardButton(f"{EMOJI['view']} Lihat Terdaftar", callback_data="view_registered"),
                InlineKeyboardButton(f"{EMOJI['download']} Download VCF", callback_data="download_vcf")
            ])
            keyboard.append([InlineKeyboardButton(f"{EMOJI['rocket']} Menu Utama", callback_data="start")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            banner = BANNER % (EMOJI['not_registered'], EMOJI['not_registered'], EMOJI['phone'], EMOJI['phone'])
            message = f"{banner}\n\n{detail_text}"
            
            # Kirim pesan
            await query.edit_message_text(
                message,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=reply_markup
            )
        
        elif data == "next_non_registered":
            # Halaman berikutnya untuk kontak tidak terdaftar
            pagination['non_registered_page'] += 1
            query.data = "view_non_registered"
            await button_callback(update, context)
        
        elif data == "prev_non_registered":
            # Halaman sebelumnya untuk kontak tidak terdaftar
            pagination['non_registered_page'] -= 1
            query.data = "view_non_registered"
            await button_callback(update, context)
        
        elif data == "reprocess":
            # Kembali ke menu utama
            banner = BANNER % (EMOJI['refresh'], EMOJI['refresh'], EMOJI['info'], EMOJI['info'])
            await query.edit_message_text(
                f"{banner}\n\n"
                f"{EMOJI['refresh']} {format_text('PROSES ULANG', 'bold')}\n\n"
                f"Untuk memproses ulang, silakan kirim file VCF baru.",
                parse_mode=constants.ParseMode.HTML
            )
            return

def main() -> None:
    """Menjalankan bot."""
    logger.info("🚀 Memulai VCF Checker Bot...")
    
    # Membuat aplikasi
    application = Application.builder().token(BOT_TOKEN).build()

    # Menambahkan handlers untuk perintah dasar
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("addadmin", add_admin_command))
    application.add_handler(CommandHandler("removeadmin", remove_admin_command))
    
    # Conversation handler untuk setup akun
    account_setup_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_account_start, pattern="^add_account$")],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_input)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_input)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, password_input)],
            ACCOUNT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, account_name_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)]
    )
    application.add_handler(account_setup_handler)
    
    # Conversation handler untuk setup grup
    group_setup_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_group_start, pattern="^set_group$")],
        states={
            GROUP_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_link_input)],
            DELAY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delay_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)]
    )
    application.add_handler(group_setup_handler)
    
    # Conversation handler untuk setup delay
    delay_setup_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_delay_start, pattern="^set_delay$")],
        states={
            DELAY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, delay_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel_setup)]
    )
    application.add_handler(delay_setup_handler)
    
    # Handler untuk file VCF
    application.add_handler(MessageHandler(filters.Document.FileExtension("vcf"), process_vcf))
    
    # Handler untuk callback dari tombol inline
    application.add_handler(CallbackQueryHandler(button_callback))

    # Menjalankan bot
    logger.info("✅ Bot berjalan! Siap untuk melayani permintaan.")
    application.run_polling()

if __name__ == '__main__':
    main()
