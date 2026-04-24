# complete_handler.py – UPDATED for new Shopify API endpoint
# Fast stop, 30 concurrent, progress bar with count, site IDs in hits.

import requests
import time
import threading
import random
import logging
import re
import csv
import os
import urllib3
import traceback
import json
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from telebot import types
from datetime import datetime, date

# Import the new Shopify functions from gates
from gates import (
    check_shopify_api,
    process_shopify_api_response,
    # Other gate functions
    check_paypal_onyx,
    check_stripe_api,
    check_chaos,
    check_adyen,
    check_app_auth,
    check_arcenus,
    check_paypal_fixed,
    check_paypal_general,
    check_b3_auth,
    check_stripe_onyx,
    check_stripe_working,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OWNER_ID = [5963548505, 1614278744]

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
                logger.warning(f"Rate limited (429). Waiting {retry_after} seconds.")
                time.sleep(retry_after)
                rate_limiter.wait()
                continue
            else:
                raise

# ============================================================================
# STOP COMMAND HANDLING – FAST ABORT
# ============================================================================
stop_events = {}
stop_lock = threading.Lock()

def set_stop(chat_id):
    with stop_lock:
        stop_events[chat_id] = True

def clear_stop(chat_id):
    with stop_lock:
        stop_events.pop(chat_id, None)

def is_stop_requested(chat_id):
    with stop_lock:
        return stop_events.get(chat_id, False)

# ============================================================================
# CONCURRENCY CONTROL – 30 SIMULTANEOUS CHECKS
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
    try:
        parts = proxy.strip().split(':')
        if len(parts) == 2:
            formatted = f"http://{parts[0]}:{parts[1]}"
        elif len(parts) == 4:
            formatted = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        else:
            return None
        proxies_dict = {'http': formatted, 'https': formatted}
        r = requests.get("http://httpbin.org/ip", proxies=proxies_dict, timeout=5, verify=False)
        if r.status_code == 200:
            return proxy
    except:
        pass
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
    total_to_test = len(to_test)
    status_msg = safe_send(bot.send_message, message.chat.id, f"🛡️ <b>Verifying {total_to_test} Proxies...</b>", parse_mode='HTML')
    last_ui_update = time.time()
    tested = 0
    newly_live = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(check_proxy_live, p): p for p in to_test}
        for future in as_completed(futures):
            tested += 1
            p = futures[future]
            result = future.result()
            with proxy_cache_lock:
                proxy_cache[p] = {'live': bool(result), 'time': now}
            if result:
                newly_live.append(p)
            if time.time() - last_ui_update > 2:
                try:
                    safe_send(bot.edit_message_text,
                        f"🛡️ <b>Verifying Proxies</b>\n✅ Live: {len(live_proxies)+len(newly_live)}\n💀 Dead: {tested - len(newly_live)}\n📊 {tested}/{total_to_test}",
                        message.chat.id, status_msg.message_id, parse_mode='HTML')
                    last_ui_update = time.time()
                except:
                    pass
    try:
        safe_send(bot.delete_message, message.chat.id, status_msg.message_id)
    except:
        pass
    live_proxies.extend(newly_live)
    return live_proxies

# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [%(levelname)s] - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================================
# BIN DATABASE
# ============================================================================
BINS_CSV_FILE = 'bins_all.csv'
BIN_DB = {}

def load_bin_database():
    global BIN_DB
    if not os.path.exists(BINS_CSV_FILE):
        logger.warning(f"⚠️ BIN CSV file not found.")
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
        logger.error(f"❌ Error loading BIN CSV: {e}")

def get_flag_emoji(country_code):
    if not country_code or len(country_code) != 2: return "🇺🇳"
    return "".join([chr(ord(c.upper()) + 127397) for c in country_code])

load_bin_database()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================
def get_bin_info(card_number):
    clean_cc = re.sub(r'\D', '', str(card_number))
    bin_code = clean_cc[:6]
    if bin_code in BIN_DB:
        return BIN_DB[bin_code]
    try:
        response = requests.get(f"https://bins.antipublic.cc/bins/{bin_code}", timeout=3)
        if response.status_code == 200:
            data = response.json()
            return {
                'country_name': data.get('country_name', 'Unknown'),
                'country_flag': data.get('country_flag', '🇺🇳'),
                'brand': data.get('brand', 'Unknown'),
                'type': data.get('type', 'Unknown'),
                'level': data.get('level', 'Unknown'),
                'bank': data.get('bank', 'Unknown')
            }
    except:
        pass
    return {'country_name': 'Unknown', 'country_flag': '🇺🇳', 'bank': 'UNKNOWN', 'brand': 'UNKNOWN', 'type': 'UNKNOWN', 'level': 'UNKNOWN'}

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

def format_progress_bar(processed, total, length=15):
    if total == 0:
        return ""
    percent = processed / total
    filled = int(length * percent)
    bar = '█' * filled + '▒' * (length - filled)
    return f"<code>{bar}</code> <b>{processed}/{total}</b>"

# ============================================================================
# USAGE TRACKING (DYNAMIC LOAD FIXED)
# ============================================================================
def reset_usage_if_needed(user_data):
    today = date.today().isoformat()
    if user_data.get('last_usage_reset') != today:
        user_data['usage_today'] = 0
        user_data['last_usage_reset'] = today

def get_user_upload_limit(user_id, load_json_func, users_file):
    users_data = load_json_func(users_file, {})
    user_str = str(user_id)
    user_info = users_data.get(user_str, {})
    if not user_info:
        return 50000
    limit = user_info.get('limit', 50000)
    return 50000 if limit <= 0 else limit

def get_user_daily_remaining(user_id, load_json_func, users_file):
    users_data = load_json_func(users_file, {})
    user_str = str(user_id)
    user_info = users_data.get(user_str, {})
    if not user_info:
        return 100000
    reset_usage_if_needed(user_info)
    daily_limit = user_info.get('daily_limit', 100000)
    used = user_info.get('usage_today', 0)
    return max(0, daily_limit - used)

def increment_usage(user_id, amount, load_json_func, save_json_func, users_file):
    users_data = load_json_func(users_file, {})
    user_str = str(user_id)
    user_info = users_data.get(user_str)
    if not user_info:
        return
    reset_usage_if_needed(user_info)
    user_info['usage_today'] = user_info.get('usage_today', 0) + amount
    save_json_func(users_file, users_data)

# ============================================================================
# PERSONAL SITES HANDLING
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

# ============================================================================
# MASS CHECK ENGINE – SHOPIFY (using new API) WITH PER‑USER LIMIT
# ============================================================================
def process_shopify_mass_check(bot, message, start_msg, ccs, site_list, proxies,
                               user_id, load_json_func, save_json_func, users_file, hit_pref="both"):
    total = len(ccs)
    results = {"cooked": [], "approved": [], "dead": [], "error": []}
    chat_id = message.chat.id
    sites_lock = threading.Lock()
    temp_site_ban = {}
    TEMP_BAN_TIME = 120
    current_sites = site_list.copy()
    current_proxies = proxies.copy()
    clear_stop(chat_id)
    unchecked_ccs = []

    def error_result(cc, reason):
        return {
            'cc': cc, 'response': reason, 'status': 'ERROR', 'gateway': 'Unknown',
            'price': '0.00', 'site': 'N/A', 'site_url': 'N/A', 'site_obj': {}, 'proxy_used': None,
            'bin_info': get_bin_info(cc.split('|')[0]), 'timestamp': datetime.now().isoformat()
        }

    def check_card_concurrent(cc, site_list, proxy_list, max_retries=3):
        if is_stop_requested(chat_id):
            return error_result(cc, "Stopped by user"), True
        try:
            with sites_lock:
                available_sites = []
                now = time.time()
                for s in site_list:
                    url = s['url']
                    if url in temp_site_ban:
                        if now < temp_site_ban[url]:
                            continue
                        else:
                            del temp_site_ban[url]
                    available_sites.append(s)
            if not available_sites:
                return error_result(cc, 'All sites temporarily paused'), False
            sites_to_try = random.sample(available_sites, min(max_retries, len(available_sites)))
            for site_obj in sites_to_try:
                if is_stop_requested(chat_id):
                    return error_result(cc, "Stopped by user"), True
                site_url = site_obj['url']
                site_name = site_obj.get('name', site_url)
                price = site_obj.get('price', '0.00')
                gateway = site_obj.get('gateway', 'Unknown')
                tried_proxies = []
                for _ in range(2):
                    if is_stop_requested(chat_id):
                        return error_result(cc, "Stopped by user"), True
                    available_proxies = [p for p in proxy_list if p not in tried_proxies]
                    if not available_proxies:
                        break
                    proxy = random.choice(available_proxies)
                    tried_proxies.append(proxy)
                    try:
                        api_resp = check_shopify_api(site_url, cc, proxy)
                        response_text, status, gateway_result = process_shopify_api_response(api_resp, price)
                        response_upper = (response_text or "").upper()
                        is_captcha = "CAPTCHA" in response_upper
                        if not is_captcha and status in ['APPROVED', 'APPROVED_OTP', 'DECLINED']:
                            bin_info = get_bin_info(cc.split('|')[0])
                            return {
                                'cc': cc,
                                'response': response_text,
                                'status': status,
                                'gateway': gateway_result or gateway,
                                'price': price,
                                'site': site_name,
                                'site_url': site_url,
                                'site_obj': site_obj,
                                'proxy_used': proxy,
                                'bin_info': bin_info,
                                'timestamp': datetime.now().isoformat()
                            }, False
                        elif is_captcha:
                            with sites_lock:
                                temp_site_ban[site_url] = time.time() + TEMP_BAN_TIME
                            break
                    except:
                        continue
            return error_result(cc, 'All retries failed'), False
        except Exception as e:
            return error_result(cc, str(e)), False

    processed = 0
    start_time = time.time()
    last_update_time = time.time()
    last_card_result = ""
    status_msg = start_msg

    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {}
        for i, cc in enumerate(ccs):
            if is_stop_requested(chat_id):
                unchecked_ccs = ccs[i:]
                break
            future = executor.submit(check_card_concurrent, cc, current_sites, current_proxies, 3)
            futures[future] = cc

        for future in as_completed(futures):
            if is_stop_requested(chat_id):
                for f in futures:
                    if not f.done():
                        f.cancel()
            processed += 1
            try:
                timeout = 15 if is_stop_requested(chat_id) else 120
                res, stopped = future.result(timeout=timeout)
                if stopped:
                    cc = futures[future]
                    unchecked_ccs.insert(0, cc)
                    last_card_result = f"⏸️ Stop requested"
                    break
                status = res['status']
                if status == 'APPROVED':
                    results['cooked'].append(res)
                    if hit_pref in ["both", "cooked"]:
                        send_hit(bot, chat_id, res, "🔥 COOKED")
                    last_card_result = f"🔥 {res['cc'][:16]}... | COOKED"
                elif status == 'APPROVED_OTP':
                    results['approved'].append(res)
                    if hit_pref == "both":
                        send_hit(bot, chat_id, res, "✅ APPROVED")
                    last_card_result = f"✅ {res['cc'][:16]}... | OTP"
                elif status == 'DECLINED':
                    results['dead'].append(res)
                    last_card_result = f"❌ {res['cc'][:16]}... | DECLINED"
                else:
                    results['error'].append(res)
                    last_card_result = f"⚠️ {res['cc'][:16]}... | ERROR"
            except FutureTimeoutError:
                cc = futures[future]
                results['error'].append(error_result(cc, 'Timeout'))
                last_card_result = f"⏱️ {cc[:16]}... | TIMEOUT"
            except Exception as e:
                cc = futures[future]
                results['error'].append(error_result(cc, str(e)))
                last_card_result = f"⚠️ {cc[:16]}... | {str(e)[:30]}"

            if time.time() - last_update_time > 5.0 or processed == len(futures) or is_stop_requested(chat_id):
                try:
                    elapsed = time.time() - start_time
                    cpm = (processed / elapsed) * 60 if elapsed > 0 else 0
                    avg_time = elapsed / processed if processed > 0 else 0
                    progress_bar = format_progress_bar(processed, total)
                    msg_text = f"""
<pre>┌──────────────────────────────────┐
│ <b>📊  SHOPIFY MASS SCAN</b>          │
├──────────────────────────────────┤
│  {progress_bar}            
│                                  
│  🔥  Cooked     :  <b>{len(results['cooked']):>4}</b>        
│  ✅  Approved   :  <b>{len(results['approved']):>4}</b>      
│  ❌  Dead       :  <b>{len(results['dead']):>4}</b>        
│  ⚠️  Errors     :  <b>{len(results['error']):>4}</b>        
│                                  
│  ⚡ CPM : {cpm:.1f}   |  ⏱️ Avg : {avg_time:.1f}s
└──────────────────────────────────┘</pre>
<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
                    try:
                        safe_send(bot.edit_message_text, msg_text, chat_id, status_msg.message_id, parse_mode="HTML")
                    except:
                        new_msg = safe_send(bot.send_message, chat_id, msg_text, parse_mode="HTML")
                        status_msg = new_msg
                    last_update_time = time.time()
                except:
                    pass

            if is_stop_requested(chat_id):
                break

    clear_stop(chat_id)
    increment_usage(user_id, processed, load_json_func, save_json_func, users_file)

    duration = time.time() - start_time
    final_text = (f"<b>{'⏸️ STOPPED' if is_stop_requested(chat_id) else '✅ Shopify Completed'}</b>\n"
                  f"━━━━━━━━━━━━━━━━\n"
                  f"💳 <b>Checked:</b> {processed}\n"
                  f"🔥 <b>Cooked:</b> {len(results['cooked'])}\n"
                  f"✅ <b>Approved:</b> {len(results['approved'])}\n"
                  f"❌ <b>Dead:</b> {len(results['dead'])}\n"
                  f"⚠️ <b>Errors:</b> {len(results['error'])}\n"
                  f"⏱️ <b>Time:</b> {duration:.2f}s\n\n"
                  f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>")
    try:
        safe_send(bot.edit_message_text, final_text, chat_id, status_msg.message_id, parse_mode="HTML")
    except:
        safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")

    if results['cooked']:
        cooked_text = "\n".join([f"{r['cc']} | {r['response']}" for r in results['cooked']])
        filename = f"cooked_{chat_id}.txt"
        with open(filename, 'w') as f: f.write(cooked_text)
        with open(filename, 'rb') as f: safe_send(bot.send_document, chat_id, f, caption="🔥 Cooked cards")
        os.remove(filename)

    if results['approved']:
        approved_text = "\n".join([f"{r['cc']} | {r['response']}" for r in results['approved']])
        filename = f"approved_{chat_id}.txt"
        with open(filename, 'w') as f: f.write(approved_text)
        with open(filename, 'rb') as f: safe_send(bot.send_document, chat_id, f, caption="✅ Approved cards (OTP)")
        os.remove(filename)

    if unchecked_ccs:
        unchecked_text = "\n".join(unchecked_ccs)
        filename = f"unchecked_{chat_id}.txt"
        with open(filename, 'w') as f: f.write(unchecked_text)
        with open(filename, 'rb') as f: safe_send(bot.send_document, chat_id, f, caption="📋 Unchecked cards (stopped early)")
        os.remove(filename)

    if results['error'] and not is_stop_requested(chat_id):
        error_text = "\n".join([f"{r['cc']} | {r['response']}" for r in results['error']])
        filename = f"errors_{chat_id}.txt"
        with open(filename, 'w') as f: f.write(error_text)
        with open(filename, 'rb') as f: safe_send(bot.send_document, chat_id, f, caption="⚠️ Error cards")
        os.remove(filename)

# ============================================================================
# MASS CHECK ENGINE – GENERIC GATE WITH GLOBAL GATE LIMITS
# ============================================================================
def process_gate_mass_check(bot, message, start_msg, ccs, gate_func, gate_name,
                            proxies, user_id, load_json_func, save_json_func, users_file):
    total = len(ccs)
    results = {"approved": [], "declined": [], "error": []}
    chat_id = message.chat.id
    clear_stop(chat_id)
    unchecked_ccs = []
    status_msg = start_msg

    try:
        processed = 0
        start_time = time.time()
        last_update_time = time.time()

        def worker(cc):
            if is_stop_requested(chat_id):
                return cc, "Stopped by user", "STOPPED"
            proxy = random.choice(proxies) if proxies else None
            try:
                msg, status = gate_func(cc, proxy=proxy)
                return cc, msg, status
            except Exception as e:
                return cc, str(e), "ERROR"

        with ThreadPoolExecutor(max_workers=30) as executor:
            futures = {}
            for i, cc in enumerate(ccs):
                if is_stop_requested(chat_id):
                    unchecked_ccs = ccs[i:]
                    break
                future = executor.submit(worker, cc)
                futures[future] = cc

            for future in as_completed(futures):
                if is_stop_requested(chat_id):
                    for f in futures:
                        if not f.done():
                            f.cancel()
                processed += 1
                try:
                    timeout = 10 if is_stop_requested(chat_id) else 60
                    cc, msg, status = future.result(timeout=timeout)
                    if status == 'STOPPED':
                        unchecked_ccs.insert(0, cc)
                        break
                    elif status == 'APPROVED':
                        results['approved'].append((cc, msg))
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

                if time.time() - last_update_time > 5.0 or processed == len(futures) or is_stop_requested(chat_id):
                    try:
                        elapsed = time.time() - start_time
                        cpm = (processed / elapsed) * 60 if elapsed > 0 else 0
                        avg_time = elapsed / processed if processed > 0 else 0
                        progress_bar = format_progress_bar(processed, total)
                        msg_text = f"""
<pre>┌──────────────────────────────────┐
│ <b>📊  {gate_name} MASS SCAN</b>        │
├──────────────────────────────────┤
│  {progress_bar}            
│                                  
│  ✅  Approved   :  <b>{len(results['approved']):>4}</b>        
│  ❌  Declined   :  <b>{len(results['declined']):>4}</b>      
│  ⚠️  Errors     :  <b>{len(results['error']):>4}</b>        
│                                  
│  ⚡ CPM : {cpm:.1f}   |  ⏱️ Avg : {avg_time:.1f}s
└──────────────────────────────────┘</pre>
<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
                        try:
                            safe_send(bot.edit_message_text, msg_text, chat_id, status_msg.message_id, parse_mode="HTML")
                        except:
                            new_msg = safe_send(bot.send_message, chat_id, msg_text, parse_mode="HTML")
                            status_msg = new_msg
                        last_update_time = time.time()
                    except:
                        pass

                if is_stop_requested(chat_id):
                    break

        clear_stop(chat_id)
        increment_usage(user_id, processed, load_json_func, save_json_func, users_file)

        duration = time.time() - start_time
        final_text = (f"<b>{'⏸️ STOPPED' if is_stop_requested(chat_id) else '✅ ' + gate_name + ' Completed'}</b>\n"
                      f"━━━━━━━━━━━━━━━━\n"
                      f"💳 <b>Checked:</b> {processed}\n"
                      f"✅ <b>Approved:</b> {len(results['approved'])}\n"
                      f"❌ <b>Declined:</b> {len(results['declined'])}\n"
                      f"⚠️ <b>Errors:</b> {len(results['error'])}\n"
                      f"⏱️ <b>Time:</b> {duration:.2f}s\n\n"
                      f"<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>")
        try:
            safe_send(bot.edit_message_text, final_text, chat_id, status_msg.message_id, parse_mode="HTML")
        except:
            safe_send(bot.send_message, chat_id, final_text, parse_mode="HTML")

        if results['approved']:
            approved_text = "\n".join([f"{cc} | {msg}" for cc, msg in results['approved']])
            filename = f"approved_{chat_id}.txt"
            with open(filename, 'w') as f: f.write(approved_text)
            with open(filename, 'rb') as f: safe_send(bot.send_document, chat_id, f, caption="✅ Approved cards")
            os.remove(filename)

        if unchecked_ccs:
            unchecked_text = "\n".join(unchecked_ccs)
            filename = f"unchecked_{chat_id}.txt"
            with open(filename, 'w') as f: f.write(unchecked_text)
            with open(filename, 'rb') as f: safe_send(bot.send_document, chat_id, f, caption="📋 Unchecked cards (stopped early)")
            os.remove(filename)

        if results['error'] and not is_stop_requested(chat_id):
            error_text = "\n".join([f"{cc} | {msg}" for cc, msg in results['error']])
            filename = f"errors_{chat_id}.txt"
            with open(filename, 'w') as f: f.write(error_text)
            with open(filename, 'rb') as f: safe_send(bot.send_document, chat_id, f, caption="⚠️ Error cards")
            os.remove(filename)

    except Exception as e:
        safe_send(bot.send_message, chat_id, f"❌ {gate_name} mass check crashed: {str(e)}")
        logger.error(traceback.format_exc())

# ============================================================================
# SEND HIT (Premium panel) – Now shows Site ID if available
# ============================================================================
def send_hit(bot, chat_id, res, title):
    try:
        bin_info = res['bin_info']
        site_obj = res.get('site_obj', {})
        site_id = site_obj.get('id')
        if site_id is not None:
            site_display = f"ID {site_id}"
        else:
            site_display = res.get('site_url', 'N/A').replace('https://', '').replace('http://', '').split('/')[0]
        header_emoji = "🔥" if "COOKED" in title else "✅"
        msg = f"""
<pre>┌─────────────────────────────────┐
│  <b>{title} HIT!</b> {header_emoji}                  
├─────────────────────────────────┤</pre>
<b>💳  Card</b>      :  <code>{res['cc']}</code>
<b>📋  Response</b>   :  {res['response']}
<b>🛡️  Gateway</b>    :  {res['gateway']}  ·  <b>${res.get('price', '0.00')}</b>
<b>🌐  Site</b>       :  {site_display}

<pre>├─────────────────────────────────┤</pre>
<b>🏦  Bank</b>       :  <b>{bin_info.get('bank', 'UNKNOWN')}</b>
<b>🌍  Country</b>    :  {bin_info.get('country_name', 'UNKNOWN')} {bin_info.get('country_flag', '🇺🇳')}
<b>💠  Brand</b>      :  {bin_info.get('brand', 'UNKNOWN')} {bin_info.get('type', 'UNKNOWN')}
<pre>└─────────────────────────────────┘</pre>
<i>⚡ NOVA · <a href='tg://user?id=5963548505'>⏤‌‌Unknownop ꯭𖠌</a></i>
"""
        safe_send(bot.send_message, chat_id, msg, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Error sending hit: {e}")

# ============================================================================
# MAIN SETUP FUNCTION – exports all needed functions
# ============================================================================
def setup_complete_handler(bot, get_filtered_sites_func, proxies_data,
                          check_site_func, is_valid_response_func,
                          process_response_func, update_stats_func,
                          save_json_func, load_json_func,
                          is_user_allowed_func, users_data_ref, users_file,
                          force_subscribe_decorator=None):

    user_sessions = {}
    settings = load_json_func("settings.json", {"gate_limits": {}})
    gate_limits = settings.get("gate_limits", {})
    DEFAULT_GATE_LIMITS = {
        "shopify": 1000,
        "chaos": 200,
        "adyen": 200,
        "app_auth": 200,
        "stripe_onyx": 200,
        "arcenus": 200,
        "paypal_onyx": 200,
        "stripe": 200,
        "paypal_fixed": 200,
        "paypal_general": 200,
        "b3_auth": 200,
    }
    for k, v in DEFAULT_GATE_LIMITS.items():
        if k not in gate_limits:
            gate_limits[k] = v

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

    @bot.message_handler(commands=['cleanmyproxies'])
    def handle_clean_my_proxies(message):
        user_id = message.from_user.id
        if not is_user_allowed_func(user_id) and user_id not in OWNER_ID:
            safe_send(bot.reply_to, message, "🚫 Access Denied")
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

    @bot.message_handler(commands=['stop'])
    def handle_stop(message):
        chat_id = message.chat.id
        set_stop(chat_id)
        safe_send(bot.reply_to, message,
            "⏸️ <b>Stop requested.</b>\n\n"
            "• Pending cards cancelled.\n"
            "• Current card finishes soon.\n"
            "• Unchecked cards will be saved.\n\n"
            "<i>Please wait...</i>",
            parse_mode='HTML')

    @bot.message_handler(commands=['msh', 'hardcook'])
    def handle_mass_check_command(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        if not is_user_allowed_func(user_id) and user_id not in OWNER_ID:
            safe_send(bot.send_message, chat_id, "🚫 Access Denied", parse_mode='HTML')
            return
        text = message.text or ''
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            cards_text = parts[1]
            ccs = extract_cards_from_text(cards_text)
            if not ccs:
                safe_send(bot.send_message, chat_id, "❌ No valid cards found.", parse_mode='Markdown')
                return
            if user_id not in user_sessions:
                user_sessions[user_id] = {}
            user_sessions[user_id]['ccs'] = ccs
        else:
            if user_id not in user_sessions or 'ccs' not in user_sessions[user_id]:
                safe_send(bot.send_message, chat_id, "⚠️ Upload CCs first!", parse_mode='HTML')
                return
            ccs = user_sessions[user_id]['ccs']

        if user_id not in OWNER_ID:
            remaining = get_user_daily_remaining(user_id, load_json_func, users_file)
            if remaining < len(ccs):
                safe_send(bot.send_message, chat_id, f"❌ Daily limit exceeded. You have {remaining} cards left.", parse_mode='HTML')
                return

        if is_user_busy(user_id) and user_id not in OWNER_ID:
            safe_send(bot.send_message, chat_id, "⏳ You already have a mass check in progress.")
            return
        if not mass_check_semaphore.acquire(blocking=False):
            safe_send(bot.send_message, chat_id, "⚠️ Too many mass checks running globally.")
            return

        set_user_busy(user_id, True)
        sites = get_filtered_sites_func()
        if not sites:
            safe_send(bot.send_message, chat_id, "❌ No sites available!")
            mass_check_semaphore.release()
            set_user_busy(user_id, False)
            return

        proxies = get_active_proxies(user_id)
        if not proxies:
            safe_send(bot.send_message, chat_id, "🚫 Proxy Required!")
            mass_check_semaphore.release()
            set_user_busy(user_id, False)
            return

        active_proxies = validate_proxies_strict(proxies, bot, message)
        if not active_proxies:
            safe_send(bot.send_message, chat_id, "❌ All your proxies are dead.")
            mass_check_semaphore.release()
            set_user_busy(user_id, False)
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("🛍️ Shopify Multi‑Site", callback_data="run_mass_shopify"),
            types.InlineKeyboardButton("🛍️ My Sites", callback_data="run_mass_mysites")
        )
        markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="action_cancel"))
        safe_send(bot.send_message, chat_id, f"💳 <b>Cards to check:</b> {len(ccs)}\n<b>⚡ Select Option:</b>", reply_markup=markup, parse_mode='HTML')

    def handle_file_upload_event(message):
        user_id = message.from_user.id
        if not is_user_allowed_func(user_id):
            safe_send(bot.reply_to, message, "🚫 Access Denied", parse_mode='HTML')
            return
        try:
            file_name = message.document.file_name.lower()
            if not file_name.endswith('.txt'):
                safe_send(bot.reply_to, message, "❌ Only .txt files allowed.", parse_mode='HTML')
                return
            msg_loading = safe_send(bot.reply_to, message, "⏳ Reading File...", parse_mode='HTML')
            file_info = bot.get_file(message.document.file_id)
            file_content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
            ccs = extract_cards_from_text(file_content)
            if not ccs:
                safe_send(bot.edit_message_text, "❌ No valid CCs found.", message.chat.id, msg_loading.message_id)
                return

            limit = get_user_upload_limit(user_id, load_json_func, users_file)
            if len(ccs) > limit and user_id not in OWNER_ID:
                safe_send(bot.edit_message_text,
                    f"⚠️ Limit exceeded. Only first {limit} cards will be checked.",
                    message.chat.id, msg_loading.message_id, parse_mode='HTML')
                ccs = ccs[:limit]
                time.sleep(2)

            if user_id not in user_sessions:
                user_sessions[user_id] = {}
            user_sessions[user_id]['ccs'] = ccs

            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(
                types.InlineKeyboardButton("🛍️ Shopify", callback_data="run_mass_shopify"),
                types.InlineKeyboardButton("🛍️ My Sites", callback_data="run_mass_mysites")
            )
            markup.add(types.InlineKeyboardButton("❌ Cancel", callback_data="action_cancel"))
            safe_send(bot.edit_message_text,
                f"📂 <b>File:</b> <code>{file_name}</code>\n💳 <b>Cards to check:</b> {len(ccs)}\n<b>⚡ Select Option:</b>",
                message.chat.id, msg_loading.message_id,
                reply_markup=markup, parse_mode='HTML')
            logger.info(f"User {user_id} uploaded {len(ccs)} CCs")
        except Exception as e:
            logger.error(f"File upload error: {traceback.format_exc()}")
            safe_send(bot.reply_to, message, f"❌ Error: {str(e)}")

    if force_subscribe_decorator:
        handle_file_upload_event = force_subscribe_decorator(handle_file_upload_event)
    bot.message_handler(content_types=['document'])(handle_file_upload_event)

    @bot.callback_query_handler(func=lambda call: call.data == "run_mass_shopify")
    def callback_shopify(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        if not is_user_allowed_func(user_id):
            safe_send(bot.answer_callback_query, call.id, "🚫 Access Denied!", show_alert=True)
            try: safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
            except: pass
            return
        try: safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
        except: pass
        if user_id not in user_sessions or 'ccs' not in user_sessions[user_id]:
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Upload CCs first!", parse_mode='HTML')
            return

        ccs = user_sessions[user_id]['ccs']

        if user_id not in OWNER_ID:
            remaining = get_user_daily_remaining(user_id, load_json_func, users_file)
            if remaining < len(ccs):
                safe_send(bot.send_message, call.message.chat.id, f"❌ Daily limit exceeded.", parse_mode='HTML')
                return

            shopify_limit = get_user_upload_limit(user_id, load_json_func, users_file)

            if len(ccs) > shopify_limit:
                ccs = ccs[:shopify_limit]
                user_sessions[user_id]['ccs'] = ccs
                safe_send(bot.send_message, call.message.chat.id, f"⚠️ Your Shopify limit is {shopify_limit} cards. Truncated.", parse_mode='HTML')

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
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        pref = call.data.replace("shopify_pref_", "")
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['hit_pref'] = pref
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
        if is_user_busy(user_id) and user_id not in OWNER_ID:
            safe_send(bot.send_message, call.message.chat.id, "⏳ Already busy.")
            return
        if not mass_check_semaphore.acquire(blocking=False):
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Too many mass checks globally.")
            return
        set_user_busy(user_id, True)
        start_msg = safe_send(bot.send_message, call.message.chat.id,
            f"🔥 <b>Starting Shopify Multi‑Site...</b>\n💳 {len(ccs)} Cards\n🔌 {len(active_proxies)} Proxies",
            parse_mode='HTML')
        def mass_thread():
            try:
                process_shopify_mass_check(
                    bot, call.message, start_msg, ccs, sites, active_proxies,
                    user_id, load_json_func, save_json_func, users_file, pref
                )
            finally:
                mass_check_semaphore.release()
                set_user_busy(user_id, False)
        threading.Thread(target=mass_thread).start()

    @bot.callback_query_handler(func=lambda call: call.data == "run_mass_mysites")
    def callback_mysites(call):
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        if not is_user_allowed_func(user_id):
            safe_send(bot.answer_callback_query, call.id, "🚫 Access Denied!", show_alert=True)
            return
        user_sites = get_user_sites(user_id)
        if not user_sites:
            safe_send(bot.answer_callback_query, call.id, "You have no personal sites. Add with /addmysite.", show_alert=True)
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
        bot.answer_callback_query(call.id)
        user_id = call.from_user.id
        pref = call.data.replace("mysites_pref_", "")
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
        if is_user_busy(user_id) and user_id not in OWNER_ID:
            safe_send(bot.send_message, call.message.chat.id, "⏳ Already busy.")
            return
        if not mass_check_semaphore.acquire(blocking=False):
            safe_send(bot.send_message, call.message.chat.id, "⚠️ Too many mass checks globally.")
            return
        set_user_busy(user_id, True)
        start_msg = safe_send(bot.send_message, call.message.chat.id,
            f"🔥 <b>Starting Personal Sites Check...</b>\n💳 {len(ccs)} Cards\n🔌 {len(active_proxies)} Proxies",
            parse_mode='HTML')
        def mass_thread():
            try:
                process_shopify_mass_check(
                    bot, call.message, start_msg, ccs, user_sites, active_proxies,
                    user_id, load_json_func, save_json_func, users_file, pref
                )
            finally:
                mass_check_semaphore.release()
                set_user_busy(user_id, False)
        threading.Thread(target=mass_thread).start()

    # Gate map for mass checks
    gate_map = {
        "chaos": (check_chaos, "Chaos Auth"),
        "adyen": (check_adyen, "Adyen Auth"),
        "app_auth": (check_app_auth, "App Based Auth"),
        "stripe_onyx": (check_stripe_onyx, "Stripe (Onyx)"),
        "arcenus": (check_arcenus, "Arcenus"),
        "paypal_onyx": (check_paypal_onyx, "PayPal (Onyx)"),
        "stripe": (check_stripe_api, "Stripe Auth"),
        "paypal_fixed": (check_paypal_fixed, "PayPal Fixed"),
        "paypal_general": (check_paypal_general, "PayPal General"),
        "b3_auth": (check_b3_auth, "Stripe Auth"),
    }

    for gate_key, (gate_func, gate_name) in gate_map.items():
        @bot.callback_query_handler(func=lambda call, gk=gate_key, gf=gate_func, gn=gate_name: call.data == f"run_mass_{gk}")
        def gate_callback(call, gate_key=gate_key, gate_func=gate_func, gate_name=gate_name):
            bot.answer_callback_query(call.id)
            user_id = call.from_user.id
            if not is_user_allowed_func(user_id):
                safe_send(bot.answer_callback_query, call.id, "🚫 Access Denied!", show_alert=True)
                try: safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
                except: pass
                return
            try: safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
            except: pass
            if user_id not in user_sessions or 'ccs' not in user_sessions[user_id]:
                safe_send(bot.send_message, call.message.chat.id, "⚠️ Upload CCs first!", parse_mode='HTML')
                return
            ccs = user_sessions[user_id]['ccs']

            if user_id not in OWNER_ID:
                remaining = get_user_daily_remaining(user_id, load_json_func, users_file)
                if remaining < len(ccs):
                    safe_send(bot.send_message, call.message.chat.id, f"❌ Daily limit exceeded.", parse_mode='HTML')
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
            if is_user_busy(user_id) and user_id not in OWNER_ID:
                safe_send(bot.send_message, call.message.chat.id, "⏳ Already busy.")
                return
            if not mass_check_semaphore.acquire(blocking=False):
                safe_send(bot.send_message, call.message.chat.id, "⚠️ Too many mass checks globally.")
                return
            set_user_busy(user_id, True)
            start_msg = safe_send(bot.send_message, call.message.chat.id,
                f"⚡ <b>{gate_name} Mass Check Started...</b>\n💳 Cards: {len(ccs)}\n🔌 Proxies: {len(active_proxies)}",
                parse_mode='HTML')
            def mass_thread():
                try:
                    process_gate_mass_check(
                        bot, call.message, start_msg, ccs, gate_func, gate_name,
                        active_proxies, user_id, load_json_func, save_json_func, users_file
                    )
                finally:
                    mass_check_semaphore.release()
                    set_user_busy(user_id, False)
            threading.Thread(target=mass_thread).start()

    @bot.callback_query_handler(func=lambda call: call.data == "action_cancel")
    def callback_cancel(call):
        bot.answer_callback_query(call.id)
        try: safe_send(bot.delete_message, call.message.chat.id, call.message.message_id)
        except: pass

    return {
        'get_user_sites': get_user_sites,
        'save_user_sites_list': save_user_sites_list,
        'set_user_busy': set_user_busy,
        'is_user_busy': is_user_busy,
        'set_stop': set_stop,
        'clear_stop': clear_stop,
        'is_stop_requested': is_stop_requested,
        'mass_check_semaphore': mass_check_semaphore,
    }
