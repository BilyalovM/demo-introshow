import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

WAHA_URL = "http://127.0.0.1:3000"
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")

# Web Push
try:
    from pywebpush import webpush, WebPushException
except ImportError:
    webpush = None
    WebPushException = Exception

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS = {"sub": "mailto:admin@example.com"}

def send_wa_message(phone: str, text: str):
    if not phone:
        return False
    clean_phone = ''.join(filter(str.isdigit, phone))
    if clean_phone.startswith('8'): clean_phone = '7' + clean_phone[1:]
    if not clean_phone:
        return False
    payload = {
        "session": "default",
        "chatId": f"{clean_phone}@c.us",
        "text": text
    }
    try:
        r = requests.post(f"{WAHA_URL}/api/sendText", json=payload, timeout=5)
        return r.status_code in [200, 201]
    except Exception as e:
        print("WA Error:", e)
        return False

def send_tg_message(chat_id: str, text: str):
    if not chat_id or not TG_TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        r = requests.post(url, json=payload, timeout=5)
        return r.status_code == 200
    except Exception as e:
        print("TG Error:", e)
        return False

def send_web_push(subscription_info: dict, text: str):
    if not webpush or not VAPID_PRIVATE_KEY:
        print("WebPush skipped: no library or VAPID key")
        return False
    try:
        webpush(
            subscription_info=subscription_info,
            data=text,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS
        )
        return True
    except WebPushException as ex:
        print("Web Push Error:", repr(ex))
        if ex.response and ex.response.text:
            print("Web Push Response:", ex.response.text)
        return False
