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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants, ChatPermissions
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telethon import TelegramClient, functions, types, errors
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

# Konfigurasi logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Konfigurasi Telegram Bot dan Telethon Client
BOT_TOKEN = "8192964371:AAGYOYY-V4fdbJHUQQdbbQkRzQiwxSe0LPk"
API_ID = 25483326  # Ganti dengan API ID Anda dari my.telegram.org
API_HASH = "062d1366c8f3641ae906c05168e33d08"  # Ganti dengan API HASH Anda dari my.telegram.org

# Owner ID (digunakan untuk menambahkan admin pertama)
OWNER_ID = 5988451717  # Ganti dengan ID Telegram Anda

# Dictionary untuk menyimpan hasil pengecekan dan state pagination
RESULTS = {}
PAGINATION = {}  # Untuk menyimpan state pagination
ADMIN_LIST = {OWNER_ID}  # Set berisi ID admin, inisiasi dengan owner
PROCESSING_STATUS = {}  # Untuk menyimpan status pemrosesan

# Konstanta untuk pagination
ITEMS_PER_PAGE = 1

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

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mengirim pesan saat command /start dijalankan."""
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    
    banner = BANNER % (EMOJI['fire'], EMOJI['fire'], EMOJI['rocket'], EMOJI['rocket'])
    
    if is_admin(user_id):
        message = (
            f"{banner}\n\n"
            f"{EMOJI['admin']} {format_text('AKSES ADMIN DIBERIKAN', 'bold')} {EMOJI['admin']}\n\n"
            f"Halo {format_text(first_name, 'bold')}! Selamat datang di {format_text('VCF Checker Bot', 'bold')}.\n\n"
            f"{EMOJI['info']} Bot ini membantu Anda {format_text('mengecek nomor telepon', 'italic')} dari file VCF yang terdaftar di Telegram.\n\n"
            f"{EMOJI['phone']} Kirim file {format_text('.vcf', 'code')} untuk mulai memeriksa nomor telepon.\n"
            f"{EMOJI['view']} Bot akan menampilkan {format_text('detail lengkap', 'bold')} untuk nomor yang terdaftar.\n"
            f"{EMOJI['download']} Anda dapat mengunduh file VCF yang hanya berisi kontak Telegram.\n\n"
            f"{EMOJI['help']} Ketik {format_text('/help', 'code')} untuk bantuan lebih lanjut.\n"
            f"{EMOJI['settings']} Ketik {format_text('/admin', 'code')} untuk menu admin."
        )
    else:
        message = (
            f"{banner}\n\n"
            f"{EMOJI['denied']} {format_text('AKSES DITOLAK', 'bold')} {EMOJI['denied']}\n\n"
            f"Halo {format_text(first_name, 'bold')}!\n\n"
            f"{EMOJI['lock']} Maaf, hanya {format_text('admin', 'bold')} yang dapat menggunakan bot ini.\n"
            f"{EMOJI['id']} User ID Anda: {format_text(str(user_id), 'code')}\n\n"
            f"{EMOJI['info']} Hubungi admin bot untuk mendapatkan akses."
        )
    
    # Membuat keyboard inline
    keyboard = []
    if is_admin(user_id):
        keyboard = [
            [
                InlineKeyboardButton(f"{EMOJI['help']} Bantuan", callback_data="help"),
                InlineKeyboardButton(f"{EMOJI['admin']} Admin Menu", callback_data="admin_menu")
            ]
        ]
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
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
        f"1️⃣ Kirim file {format_text('.vcf', 'code')} ke bot ini\n"
        f"2️⃣ Bot akan memeriksa semua nomor telepon dalam file\n"
        f"3️⃣ Bot akan menampilkan detail lengkap untuk nomor yang terdaftar di Telegram\n"
        f"4️⃣ Gunakan tombol navigasi untuk melihat detail semua kontak\n"
        f"5️⃣ Unduh file VCF yang hanya berisi nomor Telegram\n\n"
        f"{format_text('DAFTAR PERINTAH:', 'bold')}\n"
        f"{EMOJI['rocket']} {format_text('/start', 'code')} - Memulai bot\n"
        f"{EMOJI['help']} {format_text('/help', 'code')} - Menampilkan bantuan\n"
        f"{EMOJI['admin']} {format_text('/admin', 'code')} - Menampilkan menu admin\n"
        f"{EMOJI['settings']} {format_text('/addadmin', 'code')} - Menambahkan admin baru\n"
        f"{EMOJI['trash']} {format_text('/removeadmin', 'code')} - Menghapus admin\n\n"
        f"{EMOJI['warning']} {format_text('CATATAN:', 'bold')} Bot ini membutuhkan waktu untuk memproses file besar."
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
        
        await update.message.reply_text(
            f"{EMOJI['success']} {format_text('BERHASIL:', 'bold')} User ID {format_text(str(admin_id), 'code')} telah dihapus dari daftar admin.",
            parse_mode=constants.ParseMode.HTML
        )
    except ValueError:
        await update.message.reply_text(
            f"{EMOJI['fail']} {format_text('ERROR:', 'bold')} User ID harus berupa angka.",
            parse_mode=constants.ParseMode.HTML
        )

@admin_only
async def process_vcf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Memproses file VCF yang dikirim."""
    # Mendapatkan file VCF
    file = await update.message.document.get_file()
    chat_id = str(update.effective_chat.id)
    
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
    
    # Inisialisasi client Telethon untuk memeriksa nomor
    client = TelegramClient('vcf_checker_bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    
    # Memeriksa nomor telepon
    results = await check_telegram_accounts(client, phone_numbers, chat_id, context)
    
    # Memproses hasil
    registered_count = sum(1 for result in results if result['registered'])
    
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
    
    # Menutup client Telethon
    await client.disconnect()

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
        logger.error(f"Error updating progress message: {e}")

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
            logger.error(f"Error parsing vCard: {e}")
            continue
    
    return phone_data

async def check_telegram_accounts(client, phone_data, chat_id, context):
    """Memeriksa apakah nomor telepon memiliki akun Telegram."""
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
                    
                    # Menyimpan hasil
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': True,
                        'original_vcard': original_vcard,
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
                    logger.error(f"Error getting full user info: {e}")
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': True,
                        'original_vcard': original_vcard,
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
                results.append({
                    'name': name,
                    'phone': phone,
                    'registered': False,
                    'original_vcard': original_vcard,
                })
            
            # Menghapus kontak yang baru diimpor
            await client(functions.contacts.DeleteContactsRequest(id=[user.id] if imported.users else []))
            
            # Delay untuk menghindari rate limiting
            await asyncio.sleep(1)
            
        except errors.FloodWaitError as e:
            logger.warning(f"Rate limit hit, waiting for {e.seconds} seconds")
            
            # Update status
            await update_progress_message(
                context, 
                chat_id, 
                progress, 
                f"⚠️ Batas rate terlampaui. Menunggu {e.seconds} detik..."
            )
            
            await asyncio.sleep(e.seconds)
            
            # Mencoba lagi
            try:
                imported = await client(ImportContactsRequest([contact]))
                
                # Proses seperti sebelumnya (copy-paste kode di atas)
                # Kode ini sengaja disingkat untuk menghindari duplikasi
                if imported.users:
                    user = imported.users[0]
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': True,
                        'original_vcard': original_vcard,
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
                    results.append({
                        'name': name,
                        'phone': phone,
                        'registered': False,
                        'original_vcard': original_vcard,
                    })
            except Exception as e:
                logger.error(f"Error checking phone after FloodWaitError: {e}")
                results.append({
                    'name': name,
                    'phone': phone,
                    'registered': False,
                    'original_vcard': original_vcard,
                })
        except Exception as e:
            logger.error(f"Error checking phone {phone}: {e}")
            results.append({
                'name': name,
                'phone': phone,
                'registered': False,
                'original_vcard': original_vcard,
            })
    
    return results

@admin_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menangani callback dari tombol inline."""
    query = update.callback_query
    await query.answer()
    
    chat_id = str(update.effective_chat.id)
    
    if query.data == "start":
        # Kembali ke menu utama
        await start(update, context)
        return
    
    elif query.data == "help":
        # Menampilkan bantuan
        await help_command(update, context)
        return
    
    elif query.data == "admin_menu":
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
        return
    
    elif query.data == "add_admin_prompt":
        # Prompt untuk menambahkan admin
        message = (
            f"{EMOJI['settings']} {format_text('TAMBAH ADMIN', 'bold')}\n\n"
            f"Untuk menambahkan admin baru, gunakan perintah:\n"
            f"{format_text('/addadmin [user_id]', 'code')}\n\n"
            f"Contoh: {format_text('/addadmin 123456789', 'code')}"
        )
        
        # Membuat keyboard inline
        keyboard = [
            [InlineKeyboardButton(f"{EMOJI['admin']} Kembali ke Menu Admin", callback_data="admin_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
        return
    
    elif query.data == "remove_admin_prompt":
        # Prompt untuk menghapus admin
        message = (
            f"{EMOJI['trash']} {format_text('HAPUS ADMIN', 'bold')}\n\n"
            f"Untuk menghapus admin, gunakan perintah:\n"
            f"{format_text('/removeadmin [user_id]', 'code')}\n\n"
            f"Contoh: {format_text('/removeadmin 123456789', 'code')}"
        )
        
        # Membuat keyboard inline
        keyboard = [
            [InlineKeyboardButton(f"{EMOJI['admin']} Kembali ke Menu Admin", callback_data="admin_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            message, 
            parse_mode=constants.ParseMode.HTML,
            reply_markup=reply_markup
        )
        return
    
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
    
    if query.data == "download_vcf":
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
    
    elif query.data == "view_registered":
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
            detail_text += f"{EMOJI['premium']} {format_text('Premium:', 'bold')} {'Ya' if info['is_premium'] else 'Tidak'}\n\n"
        
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
    
    elif query.data == "next_registered":
        # Halaman berikutnya untuk kontak terdaftar
        pagination['registered_page'] += 1
        await button_callback(update, context)  # Panggil kembali dengan data "view_registered"
        query.data = "view_registered"
        await button_callback(update, context)
    
    elif query.data == "prev_registered":
        # Halaman sebelumnya untuk kontak terdaftar
        pagination['registered_page'] -= 1
        query.data = "view_registered"
        await button_callback(update, context)
    
    elif query.data == "view_non_registered":
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
    
    elif query.data == "next_non_registered":
        # Halaman berikutnya untuk kontak tidak terdaftar
        pagination['non_registered_page'] += 1
        query.data = "view_non_registered"
        await button_callback(update, context)
    
    elif query.data == "prev_non_registered":
        # Halaman sebelumnya untuk kontak tidak terdaftar
        pagination['non_registered_page'] -= 1
        query.data = "view_non_registered"
        await button_callback(update, context)
    
    elif query.data == "reprocess":
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
    # Membuat aplikasi
    application = Application.builder().token(BOT_TOKEN).build()

    # Menambahkan handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("addadmin", add_admin_command))
    application.add_handler(CommandHandler("removeadmin", remove_admin_command))
    application.add_handler(MessageHandler(filters.Document.FileExtension("vcf"), process_vcf))
    application.add_handler(CallbackQueryHandler(button_callback))

    # Menjalankan bot
    application.run_polling()

if __name__ == '__main__':
    main()
