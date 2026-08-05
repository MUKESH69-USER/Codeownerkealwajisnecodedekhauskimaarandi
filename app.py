#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NOVA CC CHECKER – MAIN BOT
Uses gates.py and complete_handler.py for all checker and mass‑check logic.
"""

import os
import re
import sys
import json
import time
import random
import threading
import functools
import html
import logging
import traceback
from datetime import datetime, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
import telebot
from telebot import types
from pymongo import MongoClient, server_api

# ---------- CONFIG ----------
BOT_TOKEN = "8935365174:AAELYaLVtNnmReCPrRaNuAPkRfn5K58Yttg"
OWNER_ID = [5963548505, 5547897619]    # keep your owner IDs
DARKS_ID = 5963548505

MONGO_URI = "mongodb+srv://Prince_Mine_pvt:Meandprincehitting@cluster0.blr8vex.mongodb.net/?appName=Cluster0"

# ---------- MONGODB ----------
client = MongoClient(MONGO_URI, server_api=server_api.ServerApi('1'))
db = client['nova_bot_db']    # your database name

# ---------- TELEBOT ----------
bot = telebot.TeleBot(BOT_TOKEN, num_threads=30)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ---------- GLOBAL SESSIONS ----------
user_sessions = {}   # for storing temporary data like uploaded CCs, pending actions
BOT_START_TIME = time.time()

# ---------- PREMIUM EMOJI INJECTOR ----------
PREMIUM_EMOJIS = {
    "🔥": "5424972470023104089", "✅": "6179298314953956852", "❌": "6181467651395558500",
    "⚠️": "5204047074668083678", "⏳": "5319090522470495400", "⌛": "5447385112612208213",
    "💎": "5359719332542718652", "⚡": "5085022089103016925", "⌚": "4904882772637648609",
    "🆓": "5316902932417885675", "✨": "5282793504743917359", "🎉": "5461151367559141950",
    "🔙": "5352759161945867747", "🔜": "5355075407743826720", "🚪": "5258024802010026053",
    "⚙️": "5258096772776991776", "📖": "5258328383183396223", "🏠": "5257963315258204021",
    "🔄": "5260687119092817530", "➕": "5274008024585871702", "🧹": "5316570171236694774",
    "🗑️": "5445005936953424165", "🚫": "5316538964004321334", "⛔": "4918014360267260850",
    "🔒": "5258476306152038031", "🔐": "5897604269141398480",
    "💳": "5447453226498552490", "📦": "5258134813302332906", "🛡️": "5197288647275071607",
    "🌐": "5447602197439218445", "📂": "5341492148468465410", "📥": "5443127283898405358",
    "📋": "5197269100878907942", "🔍": "5444989577422993015", "📝": "5447421246172069841",
    "🛍️": "5445146945024720188", "💵": "5283232570660634549", "💰": "5444960062407732826",
    "🛒": "5258024802010026053", "💸": "5447579253723918909", "🧾": "5444856076954520455",
    "🎟️": "6269340869795518262", "🏦": "5332455502917949981",
    "👑": "5316993667896981960", "👨‍💻": "6181483972271283011", "👤": "5316727448644103237",
    "👥": "5256143829672672750", "🎖️": "5316554189663385368", "🔰": "5033242607627535090",
    "📢": "6267129592998270736", "👋": "5458904472598095631", "📌": "5397782960512444700",
    "🔹": "6025825239248670891", "🔷": "5972072533833289156", "📅": "5274055917766202507",
    "💡": "5123359615727174427", "🔆": "5116296400274981694", "🟢": "6179295235462406768",
    "🔴": "4956395910306202687", "⏱️": "5258258882022612173", "🏓": "5283031441637148958",
    "♾️": "5316930493223025689", "🚀": "5372917041193828849", "📈": "5258391025281408576",
    "🪫": "5280652390532395919", "🌍": "5224450179368767019", "🚧": "6017332254953443667",
    "🛠️": "5462921117423384478", "📊": "5258024802010026053", "🔗": "5256143829672672750"
}

def inject_premium_emojis(text):
    if not text: return text
    if "<tg-emoji" in text: return text
    for emoji_char, emoji_id in PREMIUM_EMOJIS.items():
        if emoji_char in text:
            text = text.replace(emoji_char, f'<tg-emoji emoji-id="{emoji_id}">{emoji_char}</tg-emoji>')
    return text

# Patch bot send methods
_orig_send = bot.send_message
_orig_reply = bot.reply_to
_orig_edit = bot.edit_message_text

def _auto_emoji_send(chat_id, text, **kwargs):
    if kwargs.get('parse_mode') == 'HTML':
        text = inject_premium_emojis(text)
    return _orig_send(chat_id, text, **kwargs)

def _auto_emoji_reply(message, text, **kwargs):
    if kwargs.get('parse_mode') == 'HTML':
        text = inject_premium_emojis(text)
    return _orig_reply(message, text, **kwargs)

def _auto_emoji_edit(text, *args, **kwargs):
    if kwargs.get('parse_mode') == 'HTML':
        text = inject_premium_emojis(text)
    return _orig_edit(text, *args, **kwargs)

bot.send_message = _auto_emoji_send
bot.reply_to = _auto_emoji_reply
bot.edit_message_text = _auto_emoji_edit

# ---------- JSON HELPERS (MongoDB + local cache) ----------
def load_json(file_path, default_data):
    try:
        collection = file_path.replace('.json', '').replace('data/', '').replace('/', '_')
        doc = db[collection].find_one({"_id": "main_data"})
        if doc and 'data' in doc:
            data = doc['data']
            # ensure structure for known files
            if file_path == "sites.json" and isinstance(data, dict) and 'sites' in data:
                return data
            elif file_path == "proxies.json" and isinstance(data, dict) and 'proxies' in data:
                return data
            return data
    except:
        pass
    # fallback to local file
    try:
        os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    # create default
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(default_data, f, indent=2)
    return default_data

def save_json(file_path, data):
    try:
        collection = file_path.replace('.json', '').replace('data/', '').replace('/', '_')
        db[collection].update_one({"_id": "main_data"}, {"$set": {"data": data}}, upsert=True)
    except:
        pass
    try:
        os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except:
        pass

# ---------- GLOBAL DATA ----------
SITES_FILE = "sites.json"
PROXIES_FILE = "proxies.json"
USERS_FILE = "users.json"
GROUPS_FILE = "groups.json"
STATS_FILE = "stats.json"
USER_PROXIES_FILE = "user_proxies.json"
CODES_FILE = "codes.json"
REFERRALS_FILE = "referrals.json"
SETTINGS_FILE = "settings.json"
SINGLE_SITES_FILE = "single_sites.json"

sites_data = load_json(SITES_FILE, {"sites": []})
proxies_data = load_json(PROXIES_FILE, {"proxies": []})
users_data = load_json(USERS_FILE, {})
groups_data = load_json(GROUPS_FILE, {})
stats_data = load_json(STATS_FILE, {"approved":0, "declined":0, "cooked":0, "mass_approved":0, "mass_declined":0, "mass_cooked":0})
user_proxies_data = load_json(USER_PROXIES_FILE, {})
codes_data = load_json(CODES_FILE, {"codes": {}})
referrals_data = load_json(REFERRALS_FILE, {})
settings_data = load_json(SETTINGS_FILE, {"price_filter": None})
single_sites_data = load_json(SINGLE_SITES_FILE, {"sites": []})

price_filter = settings_data.get("price_filter")

# ---------- HELPERS ----------
def is_owner(user_id):
    return user_id in OWNER_ID

def is_approved(user_id):
    u = users_data.get(str(user_id))
    if not u:
        return False
    try:
        exp = datetime.fromisoformat(u.get('expiry', ''))
        return exp > datetime.now()
    except:
        return False

def is_group_approved(chat_id):
    return str(chat_id) in groups_data

def get_user_proxies(user_id):
    return user_proxies_data.get(str(user_id), [])

def get_filtered_sites():
    sites = sites_data.get('sites', [])
    if price_filter is None:
        return sites
    return [s for s in sites if float(s.get('price', 0)) <= price_filter]

# ---------- FORCE SUBSCRIBE & NAME ----------
REQUIRED_CHATS = ["@Nova_bot_update", "-1004293391598"]

def is_subscribed(user_id):
    for chat in REQUIRED_CHATS:
        try:
            m = bot.get_chat_member(chat, user_id)
            if m.status not in ['creator', 'administrator', 'member']:
                return False
        except:
            return False
    return True

def has_required_username(user):
    required = "@Nova_Shopify_Robot"
    first = (user.first_name or "").lower()
    last = (user.last_name or "").lower()
    return required.lower() in first or required.lower() in last

def force_subscribe_and_name(func):
    @functools.wraps(func)
    def wrapper(message):
        user_id = message.from_user.id
        if user_id in OWNER_ID:
            return func(message)
        if not is_subscribed(user_id):
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("📢 Join Channel", url="https://t.me/Nova_bot_update"))
            markup.add(types.InlineKeyboardButton("👥 Join Group", url="https://t.me/+HjnDnh6A98w0Yjk0"))
            markup.add(types.InlineKeyboardButton("🔄 I've Joined Both", callback_data="check_subscription"))
            bot.reply_to(message, "⚠️ You must join both channel & group to use this bot.", reply_markup=markup)
            return
        if message.chat.type == 'private':
            user_str = str(user_id)
            is_premium = is_approved(user_id)
            if not is_premium and not has_required_username(message.from_user):
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("📝 How to Fix", callback_data="help_name_requirement"))
                bot.reply_to(message, "⚠️ Free users must add @Nova_Shopify_Robot to their name.", reply_markup=markup)
                return
        return func(message)
    return wrapper

# ---------- EXTRACT CC ----------
def extract_cc(text):
    cleaned = re.sub(r'[^\d|:./ ]', '', text)
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
                    yyyy = rest[:4] if len(rest)>=4 else rest[:2]
                    rest = rest[4:] if len(rest)>=4 else rest[2:]
                    if len(rest) >= 3:
                        cvv = rest[:3]
                        parts = [cc, mm, yyyy, cvv]
    if not parts or len(parts) < 4:
        return None
    cc = parts[0].strip()
    mm = parts[1].strip().zfill(2)
    yyyy = parts[2].strip()
    cvv = parts[3].strip()
    if len(yyyy) == 2:
        yyyy = "20" + yyyy if int(yyyy) >= datetime.now().year % 100 else "19" + yyyy
    return f"{cc}|{mm}|{yyyy}|{cvv}"

def extract_ccs_from_text(text):
    lines = text.splitlines()
    ccs = []
    for line in lines:
        c = extract_cc(line)
        if c:
            ccs.append(c)
    return ccs

# ---------- IMPORT GATES & COMPLETE HANDLER ----------
from gates import (
    check_shopify_api, process_shopify_api_response,
    check_chaos, check_adyen, check_app_auth, check_stripe_onyx,
    check_arcenus, check_paypal_onyx, check_paypal_fixed,
    check_paypal_general, check_stripe_api, check_b3_auth,
    check_stripe_auth, check_stripe5, check_paypal_charge,
    check_razorpay, check_vbv_lookup, check_braintree_api
)

from complete_handler import setup_complete_handler

# ---------- DEFINE update_stats (used by complete_handler) ----------
def update_stats_func(status, mass_check=False):
    global stats_data
    # Ensure all keys exist
    for key in ['approved', 'declined', 'cooked', 'mass_approved', 'mass_declined', 'mass_cooked', 'error', 'mass_error']:
        stats_data.setdefault(key, 0)
    if status in ['APPROVED', 'APPROVED_OTP']:
        key = 'mass_approved' if mass_check else 'approved'
    elif status == 'COOKED':
        key = 'mass_cooked' if mass_check else 'cooked'
    elif status in ['DECLINED', 'EXPIRED']:
        key = 'mass_declined' if mass_check else 'declined'
    elif status == 'ERROR':
        key = 'mass_error' if mass_check else 'error'
    else:
        return
    stats_data[key] += 1
    save_json(STATS_FILE, stats_data)

# ---------- SETUP COMPLETE HANDLER (positional arguments) ----------
handler_utils = setup_complete_handler(
    bot,                          # bot
    get_filtered_sites,           # site_filter_func
    proxies_data,                 # proxies_data
    check_shopify_api,            # check_site_func
    lambda r: True,               # is_valid_response_func (dummy)
    process_shopify_api_response, # process_response_func
    update_stats_func,            # update_stats_func
    save_json,                    # save_json_func_param
    load_json,                    # load_json_func_param
    is_approved,                  # is_user_allowed_func
    users_data,                   # users_data_ref
    USERS_FILE,                   # users_file_param
    force_subscribe_and_name,     # force_subscribe_decorator
    user_sessions                 # user_sessions
)

# Extract utilities from handler_utils
set_user_busy = handler_utils['set_user_busy']
is_user_busy = handler_utils['is_user_busy']
set_stop = handler_utils['set_stop']
clear_stop = handler_utils['clear_stop']
is_stop_requested = handler_utils['is_stop_requested']
mass_check_semaphore = handler_utils['mass_check_semaphore']
handle_clean_my_proxies = handler_utils['handle_clean_my_proxies']
get_active_proxies = handler_utils['get_active_proxies']
add_proxy_to_user = handler_utils['add_proxy_to_user']
add_global_proxy = handler_utils['add_global_proxy']

# ---------- REFERRAL SYSTEM ----------
def get_referral_link(user_id):
    return f"https://t.me/Nova_Shopify_Robot?start=ref_{user_id}"

def add_referral(referrer_id, new_user_id):
    referrer = str(referrer_id)
    new_user = str(new_user_id)
    if referrer == new_user:
        return False
    if referrer not in referrals_data:
        referrals_data[referrer] = {"referred": [], "reward_claimed": 0, "referral_days_earned": 0}
    if new_user not in referrals_data[referrer]["referred"]:
        referrals_data[referrer]["referred"].append(new_user)
        total = len(referrals_data[referrer]["referred"])
        new_days = min(total // 5, 3)
        already = referrals_data[referrer]["referral_days_earned"]
        days_to_add = new_days - already
        if days_to_add > 0:
            referrals_data[referrer]["referral_days_earned"] = new_days
            referrals_data[referrer]["reward_claimed"] = new_days
            save_json(REFERRALS_FILE, referrals_data)
            user_str = str(referrer_id)
            if user_str in users_data:
                try:
                    cur = datetime.fromisoformat(users_data[user_str]['expiry'])
                    if cur < datetime.now():
                        cur = datetime.now()
                    new_exp = cur + timedelta(days=days_to_add)
                except:
                    new_exp = datetime.now() + timedelta(days=days_to_add)
            else:
                new_exp = datetime.now() + timedelta(days=days_to_add)
            users_data[user_str] = {
                "expiry": new_exp.isoformat(),
                "limit": users_data.get(user_str, {}).get("limit", 1000),
                "usage_today": users_data.get(user_str, {}).get("usage_today", 0),
                "daily_limit": users_data.get(user_str, {}).get("daily_limit", 10000)
            }
            save_json(USERS_FILE, users_data)
            try:
                bot.send_message(referrer_id, f"🎉 Referral Reward! +{days_to_add} days premium.")
            except:
                pass
        else:
            save_json(REFERRALS_FILE, referrals_data)
        return True
    return False

# ---------- START HANDLER ----------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"
    chat_id = message.chat.id

    if message.text and message.text.startswith('/start ref_'):
        try:
            referrer_id = int(message.text.split('_')[1])
            if referrer_id != user_id:
                add_referral(referrer_id, user_id)
                bot.send_message(referrer_id, f"🎉 New referral: {user_id}")
        except:
            pass

    is_premium = is_approved(user_id)
    status_badge = "💎 PREMIUM" if is_premium else "🆓 Freelancer Tier"
    ref_link = get_referral_link(user_id)
    ref_count = len(referrals_data.get(str(user_id), {}).get("referred", []))

    welcome_text = f"""
<pre>┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  <b>🔥   N O V A   ·   V E R I F Y   🔥</b>  ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛</pre>

<b>👋 Welcome, {html.escape(user_name)}!</b>
<b>📊 Status:</b> {status_badge}

<pre>┌─────────────────────────────────┐
│ <b>📌  FREE DM USAGE RULES</b>          │
├─────────────────────────────────┤
│ 🔹 Refer 5 friends → 1 Premium Day  │
│ 🔹 Add <code>@Nova_Shopify_Robot</code> to   │
│    your name to unlock free     │
│    single check commands in DM!  │
└─────────────────────────────────┘</pre>

<b>🔗 Your Referral Link:</b>
<code>{ref_link}</code>
👥 <b>Referrals:</b> {ref_count} (5 = 1 day)

<i>⚡ NOVA · <a href="tg://user?id={DARKS_ID}">特Unknownop 𮕌</a></i>
"""

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

    bot.send_message(chat_id, welcome_text, parse_mode='HTML', reply_markup=markup)

# ---------- CALLBACKS ----------
@bot.callback_query_handler(func=lambda call: call.data == "menu_single_gate")
def single_gate_menu(call):
    bot.answer_callback_query(call.id)
    help_text = """
<pre>┌─────────────────────────────────┐
│      💳  SINGLE  CHECK  GATES    │
└─────────────────────────────────┘</pre>

<b>🔹 All Users (Subscription Required)</b>
<code>/sh  CC|MM|YYYY|CVV</code>      – 🛍️ Shopify (Multi‑Site)
<code>cook  CC|MM|YYYY|CVV</code>   – 🛍️ Shopify (Alias)
<code>/stripe  CC|MM|YYYY|CVV</code> – 💳 Stripe $5 Charge
<code>/chk  CC|MM|YYYY|CVV</code>    – 🔐 Stripe Auth (0$)
<code>/pp  CC|MM|YYYY|CVV</code>     – 💵 PayPal $1 Charge

<b>🔸 Premium Users Only</b>
<code>/rz  CC|MM|YYYY|CVV</code>      – 💎 Razorpay ₹1 Charge
<code>/vbv  CC|MM|YYYY|CVV</code>     – 🛡️ 3DS / OTP Lookup
<code>/b3  CC|MM|YYYY|CVV</code>      – 🔷 Braintree Auth

<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "menu_mass_gate")
def mass_gate_menu(call):
    bot.answer_callback_query(call.id)
    info_text = """
<pre>┌─────────────────────────────────┐
│       🚪  MASS  CHECK  GATES       │
└─────────────────────────────────┘</pre>

<b>📦 How to Mass Check:</b>
1️⃣ Simply <b>send/upload your <code>.txt</code> file</b> containing your cards.
   <i>Format: <code>CC|MM|YYYY|CVV</code> (one per line)</i>
2️⃣ Once uploaded, the gate selection buttons will automatically pop up!
3️⃣ Click your desired gate option from that popup to start checking.

<b>🚧 Mass Gate Status:</b>
• 🛍️ Shopify Multi‑Site ──> ✅ Online
• 🔐 Stripe Auth ────────> ✅ Online
• 💳 Stripe Charge ──────> ✅ Online
• 💵 PayPal Charge ──────> ✅ Online
• 💎 Razorpay ─────────> ✅ Online

<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    bot.edit_message_text(info_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "menu_proxy")
def proxy_menu(call):
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
        call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup
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
    # Delegate to /addpro command logic
    message.text = "/addpro " + message.text
    add_proxy_cmd(message)

@bot.callback_query_handler(func=lambda call: call.data == "proxy_upload_prompt")
def proxy_upload_prompt(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "📂 <b>Send me a .txt file with proxies (one per line).</b>",
        parse_mode='HTML')
    user_id = call.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = {}
    user_sessions[user_id]['awaiting_proxy_file'] = True

@bot.callback_query_handler(func=lambda call: call.data == "proxy_view")
def proxy_view(call):
    user_id = call.from_user.id
    proxies = get_user_proxies(user_id)
    if not proxies:
        text = "❌ You have no personal proxies."
    else:
        text = f"<b>🌐 Your Proxies ({len(proxies)}):</b>\n\n" + "\n".join(proxies[:20])
        if len(proxies) > 20:
            text += f"\n... and {len(proxies)-20} more"
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, text, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "proxy_clean")
def proxy_clean(call):
    bot.answer_callback_query(call.id, "Running proxy cleanup...")
    class FakeMessage:
        def __init__(self, chat, from_user):
            self.chat = chat
            self.from_user = from_user
    fake_msg = FakeMessage(call.message.chat, call.from_user)
    handle_clean_my_proxies(fake_msg)

@bot.callback_query_handler(func=lambda call: call.data == "menu_sites")
def site_menu(call):
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
        "Commands:\n"
        "<code>/addmysite &lt;url&gt;</code> – Add one or multiple sites\n"
        "<code>/mysites</code> – List your sites\n"
        "<code>/rmmyid &lt;id&gt;</code> – Remove a site by ID\n"
        "<code>/clearmysites</code> – Remove all your sites",
        call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_settings")
def settings_menu(call):
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💰 Set Price Filter", callback_data="set_price_menu"),
        types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start")
    )
    bot.edit_message_text(
        "<b>⚙️ Settings</b>\n\nCustomize your experience.",
        call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "set_price_menu")
def set_price_menu(call):
    global price_filter
    markup = types.InlineKeyboardMarkup(row_width=2)
    for price in [5,10,20,30,50,100]:
        markup.add(types.InlineKeyboardButton(f"≤ ${price}", callback_data=f"set_price_{price}"))
    markup.add(types.InlineKeyboardButton("❌ No Filter", callback_data="set_price_none"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="menu_settings"))
    bot.edit_message_text(
        f"💰 <b>Set Price Filter</b>\nCurrent: {price_filter or 'None'}\n\nSelect max price:",
        call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_price_"))
def handle_price_callback(call):
    global price_filter
    val = call.data.replace("set_price_", "")
    if val == "none":
        price_filter = None
    else:
        try:
            price_filter = float(val)
        except:
            bot.answer_callback_query(call.id, "Invalid price", show_alert=True)
            return
    settings_data['price_filter'] = price_filter
    save_json(SETTINGS_FILE, settings_data)
    bot.answer_callback_query(call.id, f"✅ Filter set to {price_filter or 'None'}")
    bot.edit_message_text(
        f"✅ Price filter updated to {price_filter or 'None'}",
        call.message.chat.id, call.message.message_id, parse_mode='HTML'
    )

@bot.callback_query_handler(func=lambda call: call.data == "show_plans")
def show_plans(call):
    plans_text = """
<pre>┌─────────────────────────────────┐
│      💎  PREMIUM  PLANS  💎      │
└─────────────────────────────────┘</pre>

🔹 <b>Trial</b> — 7 days · <code>$7</code> / <code>⭐ 500</code>
🔹 <b>Elite</b> — 15 days · <code>$14</code> / <code>⭐ 1000</code>
🔹 <b>Pro</b> — 30 days · <code>$20</code> (Crypto only)
🔹 <b>Quarterly</b> — 90 days · <code>$50</code> (Crypto only)

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
    markup.add(
        types.InlineKeyboardButton("⭐ Trial (500⭐)", callback_data="buy_star_trial"),
        types.InlineKeyboardButton("⭐ Elite (1000⭐)", callback_data="buy_star_elite")
    )
    markup.add(types.InlineKeyboardButton("🎟️ Redeem Code", callback_data="redeem_code"))
    markup.add(types.InlineKeyboardButton("👨‍💻 Contact Admin", url="https://t.me/Unknown_bolte"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    bot.edit_message_text(plans_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_"))
def buy_plan(call):
    bot.answer_callback_query(call.id, "Payment via CryptoBot or Stars. Contact admin for now.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "redeem_code")
def redeem_code_prompt(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Send the code via /use <code>")

@bot.callback_query_handler(func=lambda call: call.data == "show_info")
def show_info(call):
    user_id = call.from_user.id
    user_str = str(user_id)
    if is_owner(user_id):
        info = f"👑 GOD MODE\n🆔 {user_str}\n♾️ Unlimited"
    elif user_str in users_data:
        data = users_data[user_str]
        try:
            expiry = datetime.fromisoformat(data['expiry'])
            days_left = (expiry - datetime.now()).days
            status = "✅ Active" if days_left > 0 else "⏳ Expired"
            info = f"💎 Account\n🆔 {user_str}\nStatus: {status}\nExpires: {expiry.strftime('%Y-%m-%d')} ({days_left} days)\nLimit: {data.get('limit',1000)}\nDaily: {data.get('usage_today',0)}/{data.get('daily_limit',10000)}"
        except:
            info = "⚠️ Invalid expiry"
    else:
        info = "🆓 Free user – get a plan!"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💎 Plans", callback_data="show_plans"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    bot.edit_message_text(info, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "show_help")
def show_help(call):
    help_text = """
<b>📖 QUICK START</b>
1️⃣ Join our channel (required)
2️⃣ Add proxies: <code>/addpro ip:port:user:pass</code>
3️⃣ Upload cards (.txt) or use buttons
4️⃣ For mass check, upload file then select gate

<b>🛡️ PROXY</b>
<code>/addpro</code> – add proxy
<code>/cleanmyproxies</code> – remove dead

<b>💳 CARDS</b>
<code>/sh</code> – Shopify single check
<code>/stripe</code> / <code>/chk</code> – Stripe Auth
<code>cook</code> – Shopify alias

<b>🌐 PERSONAL SITES</b>
<code>/addmysite &lt;url&gt;</code> – Add your own Shopify site(s)
<code>/mysites</code> – List your sites
<code>/rmmyid &lt;id&gt;</code> – Remove a site
<code>/clearmysites</code> – Clear all your sites

<b>👤 ACCOUNT</b>
<code>/info</code> – Account status
<code>/start</code> – Main menu

<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    bot.edit_message_text(help_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "show_owner")
def show_owner(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Restricted", show_alert=True)
        return
    owner_text = """
👑 OWNER PANEL
Commands:
/pro <userid> <days> – approve user
/limit <userid> <limit> – set per‑upload limit
/setlimit <userid> <daily_limit> – set daily limit
/grant <chatid> – approve group
/users – list users
/rmuser <userid> – remove user
/broadcast <msg> – announcement
/addurls – add sites from .txt
/viewsites – list sites
/rmsiteid <id> – remove site
/rsite <url> – remove site(s)
/stats – bot stats
/ping – latency
/restart – restart bot
/setamo – set price filter
/addproxies – bulk add proxies
/cleanpro – clean dead proxies
"""
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_to_start"))
    bot.edit_message_text(owner_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_start")
def back_to_start(call):
    user_id = call.from_user.id
    is_premium = is_approved(user_id)
    status_badge = "💎 PREMIUM" if is_premium else "🆓 Freelancer"
    ref_link = get_referral_link(user_id)
    ref_count = len(referrals_data.get(str(user_id), {}).get("referred", []))
    welcome_text = f"""
<b>🔥 NOVA · VERIFY</b>

👋 Welcome back, {html.escape(call.from_user.first_name or 'User')}!
📊 Status: {status_badge}

🔗 Referral: <code>{ref_link}</code>
👥 Referrals: {ref_count} (5 = 1 day)

<i>⚡ NOVA · <a href="tg://user?id={DARKS_ID}">特Unknownop 𮕌</a></i>
"""
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💳 Single Check", callback_data="menu_single_gate"),
        types.InlineKeyboardButton("📦 Mass Check", callback_data="menu_mass_gate")
    )
    markup.add(
        types.InlineKeyboardButton("🛡️ Proxy", callback_data="menu_proxy"),
        types.InlineKeyboardButton("🌐 Sites", callback_data="menu_sites")
    )
    markup.add(
        types.InlineKeyboardButton("💎 Plans", callback_data="show_plans"),
        types.InlineKeyboardButton("👤 Account", callback_data="show_info")
    )
    markup.add(
        types.InlineKeyboardButton("📖 Help", callback_data="show_help"),
        types.InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")
    )
    if is_owner(user_id):
        markup.add(types.InlineKeyboardButton("👑 Owner Panel", callback_data="show_owner"))
    bot.edit_message_text(welcome_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
def check_subscription(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Verified!")
        bot.edit_message_text("✅ You're subscribed! Use /start", call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "❌ Still not joined.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "help_name_requirement")
def help_name(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id,
        "📝 Add @Nova_Shopify_Robot to your first or last name in Settings → Edit Profile.")

# ---------- COMMAND: /addpro ----------
@bot.message_handler(commands=['addpro'])
@force_subscribe_and_name
def add_proxy_cmd(message):
    user_id = message.from_user.id
    if not is_approved(user_id) and user_id not in OWNER_ID:
        bot.reply_to(message, "🚫 Premium required to add proxies.")
        return
    try:
        _, proxy = message.text.split(maxsplit=1)
        proxy = proxy.strip()
        parts = proxy.split(':')
        if len(parts) not in [2,4]:
            bot.reply_to(message, "❌ Format: ip:port or ip:port:user:pass")
            return
        status_msg = bot.reply_to(message, f"⏳ Testing {parts[0]}...")
        def test_and_save():
            try:
                if len(parts)==2:
                    proxy_url = f"http://{parts[0]}:{parts[1]}"
                else:
                    proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                r = requests.get("http://httpbin.org/ip", proxies={'http':proxy_url,'https':proxy_url}, timeout=5)
                if r.status_code == 200:
                    add_proxy_to_user(user_id, proxy, "shopify")
                    add_global_proxy(proxy, "shopify")
                    bot.edit_message_text(f"✅ Added: {proxy}\nLive ✅", message.chat.id, status_msg.message_id, parse_mode='HTML')
                else:
                    bot.edit_message_text(f"❌ Proxy {parts[0]} not reachable.", message.chat.id, status_msg.message_id)
            except:
                bot.edit_message_text(f"❌ Proxy {parts[0]} test failed.", message.chat.id, status_msg.message_id)
        threading.Thread(target=test_and_save).start()
    except:
        bot.reply_to(message, "Usage: /addpro ip:port:user:pass")

# ---------- COMMAND: /addproxies ----------
@bot.message_handler(commands=['addproxies'])
def add_proxies_bulk(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner only.")
        return
    bot.reply_to(message, "📂 Send a .txt file with proxies (one per line).")
    bot.register_next_step_handler(message, process_proxy_file_upload)

def process_proxy_file_upload(message):
    if not message.document or not message.document.file_name.endswith('.txt'):
        bot.reply_to(message, "❌ Send a .txt file.")
        return
    status = bot.reply_to(message, "⏳ Reading file...")
    try:
        file_info = bot.get_file(message.document.file_id)
        content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
        proxies = [line.strip() for line in content.split('\n') if line.strip() and ':' in line]
        if not proxies:
            bot.edit_message_text("❌ No proxies found.", message.chat.id, status.message_id)
            return
        added = 0
        for p in proxies:
            if p not in proxies_data['proxies']:
                proxies_data['proxies'].append(p)
                added += 1
        save_json(PROXIES_FILE, proxies_data)
        bot.edit_message_text(f"✅ Added {added} new proxies to global pool.", message.chat.id, status.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Error: {e}", message.chat.id, status.message_id)

# ---------- COMMAND: /cleanpro ----------
@bot.message_handler(commands=['cleanpro'])
def clean_proxies_cmd(message):
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "Owner only.")
        return
    status = bot.reply_to(message, "🧹 Testing global proxies...")
    proxies = proxies_data.get('proxies', [])
    if not proxies:
        bot.edit_message_text("No proxies to clean.", message.chat.id, status.message_id)
        return
    live = []
    total = len(proxies)
    def test_one(p):
        try:
            parts = p.split(':')
            if len(parts)==2:
                proxy_url = f"http://{parts[0]}:{parts[1]}"
            else:
                proxy_url = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            r = requests.get("http://httpbin.org/ip", proxies={'http':proxy_url,'https':proxy_url}, timeout=5)
            return r.status_code == 200
        except:
            return False
    with ThreadPoolExecutor(max_workers=30) as ex:
        futures = {ex.submit(test_one, p): p for p in proxies}
        for i, fut in enumerate(as_completed(futures)):
            p = futures[fut]
            if fut.result():
                live.append(p)
            if i % 50 == 0:
                bot.edit_message_text(f"🧹 Progress: {i+1}/{total} – Live: {len(live)}", message.chat.id, status.message_id)
    proxies_data['proxies'] = live
    save_json(PROXIES_FILE, proxies_data)
    bot.edit_message_text(f"✅ Cleaned. Live: {len(live)}/{total}", message.chat.id, status.message_id)

# ---------- OWNER COMMANDS ----------
@bot.message_handler(commands=['pro'])
def approve_user(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, uid, days = message.text.split()
        days = int(days)
        expiry = datetime.now() + timedelta(days=days)
        users_data[uid] = {
            'expiry': expiry.isoformat(),
            'limit': 1000,
            'usage_today': 0,
            'daily_limit': 10000
        }
        save_json(USERS_FILE, users_data)
        bot.reply_to(message, f"✅ User {uid} approved for {days} days.")
        try:
            bot.send_message(uid, f"🎉 Premium activated for {days} days!")
        except:
            pass
    except:
        bot.reply_to(message, "Usage: /pro <user_id> <days>")

@bot.message_handler(commands=['limit'])
def set_limit(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, uid, lim = message.text.split()
        lim = int(lim)
        if uid in users_data:
            users_data[uid]['limit'] = lim
            save_json(USERS_FILE, users_data)
            bot.reply_to(message, f"✅ Limit for {uid} set to {lim}.")
        else:
            bot.reply_to(message, "User not found.")
    except:
        bot.reply_to(message, "Usage: /limit <user_id> <limit>")

@bot.message_handler(commands=['setlimit'])
def set_daily_limit(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, uid, lim = message.text.split()
        lim = int(lim)
        if uid in users_data:
            users_data[uid]['daily_limit'] = lim
            save_json(USERS_FILE, users_data)
            bot.reply_to(message, f"✅ Daily limit for {uid} set to {lim}.")
        else:
            bot.reply_to(message, "User not found.")
    except:
        bot.reply_to(message, "Usage: /setlimit <user_id> <daily_limit>")

@bot.message_handler(commands=['grant'])
def grant_group(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, cid = message.text.split()
        groups_data[cid] = {'approved_date': datetime.now().isoformat(), 'title': 'Group'}
        save_json(GROUPS_FILE, groups_data)
        bot.reply_to(message, f"✅ Group {cid} approved.")
    except:
        bot.reply_to(message, "Usage: /grant <chat_id>")

@bot.message_handler(commands=['users'])
def list_users(message):
    if not is_owner(message.from_user.id):
        return
    if not users_data:
        bot.reply_to(message, "No users.")
        return
    text = "<b>Users:</b>\n"
    for uid, data in users_data.items():
        exp = data.get('expiry', 'N/A')
        text += f"<code>{uid}</code> – {exp}\n"
    bot.reply_to(message, text, parse_mode='HTML')

@bot.message_handler(commands=['rmuser'])
def remove_user(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, uid = message.text.split()
        if uid in users_data:
            del users_data[uid]
            save_json(USERS_FILE, users_data)
            bot.reply_to(message, f"✅ User {uid} removed.")
        else:
            bot.reply_to(message, "User not found.")
    except:
        bot.reply_to(message, "Usage: /rmuser <user_id>")

@bot.message_handler(commands=['broadcast'])
def broadcast(message):
    if not is_owner(message.from_user.id):
        return
    try:
        msg = message.text.split(maxsplit=1)[1]
    except:
        bot.reply_to(message, "Usage: /broadcast <message>")
        return
    count = 0
    for uid in users_data:
        try:
            bot.send_message(uid, f"📢 ANNOUNCEMENT\n\n{msg}")
            count += 1
            time.sleep(0.1)
        except:
            pass
    bot.reply_to(message, f"✅ Broadcast sent to {count} users.")

@bot.message_handler(commands=['addurls'])
def add_urls(message):
    if not is_owner(message.from_user.id):
        return
    bot.reply_to(message, "📂 Send a .txt file with site URLs (one per line).")
    bot.register_next_step_handler(message, process_sites_file)

def process_sites_file(message):
    if not message.document or not message.document.file_name.endswith('.txt'):
        bot.reply_to(message, "❌ Send a .txt file.")
        return
    status = bot.reply_to(message, "⏳ Processing sites...")
    try:
        file_info = bot.get_file(message.document.file_id)
        content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
        urls = [line.strip() for line in content.split('\n') if line.strip()]
        added = 0
        for url in urls:
            if not url.startswith('http'):
                url = 'https://' + url
            try:
                r = requests.get(url+'/products.json?limit=1', timeout=5)
                if r.status_code == 200:
                    if not any(s['url']==url for s in sites_data['sites']):
                        sites_data['sites'].append({'url': url, 'price': '0.00', 'gateway': 'Shopify'})
                        added += 1
            except:
                pass
        save_json(SITES_FILE, sites_data)
        bot.edit_message_text(f"✅ Added {added} sites. Total: {len(sites_data['sites'])}", message.chat.id, status.message_id)
    except Exception as e:
        bot.edit_message_text(f"❌ Error: {e}", message.chat.id, status.message_id)

@bot.message_handler(commands=['viewsites'])
def view_sites(message):
    if not is_owner(message.from_user.id):
        return
    sites = sites_data.get('sites', [])
    if not sites:
        bot.reply_to(message, "No sites.")
        return
    text = "🌐 Sites:\n"
    for site in sites[:30]:
        text += f"{site['url']} – ${site.get('price','0.00')}\n"
    if len(sites)>30:
        text += f"... and {len(sites)-30} more"
    bot.reply_to(message, text, parse_mode='HTML')

@bot.message_handler(commands=['rmsiteid'])
def remove_site_id(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, sid = message.text.split()
        sid = int(sid)
        sites_data['sites'] = [s for s in sites_data['sites'] if s.get('id') != sid]
        save_json(SITES_FILE, sites_data)
        bot.reply_to(message, f"✅ Removed site ID {sid}.")
    except:
        bot.reply_to(message, "Usage: /rmsiteid <id>")

@bot.message_handler(commands=['rsite'])
def remove_site_url(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, url = message.text.split(maxsplit=1)
        original = len(sites_data['sites'])
        sites_data['sites'] = [s for s in sites_data['sites'] if url not in s['url']]
        removed = original - len(sites_data['sites'])
        save_json(SITES_FILE, sites_data)
        bot.reply_to(message, f"✅ Removed {removed} sites matching.")
    except:
        bot.reply_to(message, "Usage: /rsite <url_part>")

@bot.message_handler(commands=['stats'])
def stats_cmd(message):
    if not is_owner(message.from_user.id):
        return
    total_sites = len(sites_data.get('sites', []))
    total_proxies = len(proxies_data.get('proxies', []))
    total_users = len(users_data)
    msg = f"📊 STATS\nSites: {total_sites}\nProxies: {total_proxies}\nUsers: {total_users}\nApproved: {stats_data.get('approved',0)}\nCooked: {stats_data.get('cooked',0)}\nDeclined: {stats_data.get('declined',0)}"
    bot.reply_to(message, msg, parse_mode='HTML')

@bot.message_handler(commands=['ping'])
def ping_cmd(message):
    start = time.time()
    m = bot.reply_to(message, "Pong!")
    end = time.time()
    bot.edit_message_text(f"Pong! {round((end-start)*1000,2)} ms", message.chat.id, m.message_id)

@bot.message_handler(commands=['restart'])
def restart_cmd(message):
    if not is_owner(message.from_user.id):
        return
    bot.reply_to(message, "🔄 Restarting... (simulated)")
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    bot.reply_to(message, "✅ Restarted.")

@bot.message_handler(commands=['setamo'])
def set_amo(message):
    if not is_owner(message.from_user.id):
        return
    try:
        _, val = message.text.split()
        price_filter = float(val)
        settings_data['price_filter'] = price_filter
        save_json(SETTINGS_FILE, settings_data)
        bot.reply_to(message, f"✅ Price filter set to ${price_filter}")
    except:
        bot.reply_to(message, "Usage: /setamo <price>")

# ---------- FILE UPLOAD HANDLER FOR PROXY FILES ----------
@bot.message_handler(content_types=['document'])
def document_handler(message):
    user_id = message.from_user.id
    if user_sessions.get(user_id, {}).get('awaiting_proxy_file'):
        # This is handled by the next_step callback we set, so we can ignore.
        return
    # Otherwise, let complete_handler handle it (it's already registered).
    pass

# ---------- START BOT ----------
if __name__ == "__main__":
    print("🚀 NOVA Bot started...")
    try:
        bot.remove_webhook()
    except:
        pass
    print("📡 Polling...")
    bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
