#!/usr/bin/env python3
"""LLM Health Monitor — проверяет доступность LLM провайдеров и автоматически переключает на резервный."""
import os
import sys
import time
import logging
import requests
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('llm_health')

sys.path.insert(0, '/home/andy/CryptoTrader')
os.environ['CRYPTOTRADER_CONFIG'] = '/home/andy/CryptoTrader/config/settings.yaml'
os.chdir('/home/andy/CryptoTrader')

from dotenv import load_dotenv
load_dotenv('/home/andy/.env')

DEEPSEEK_KEY = os.getenv('DEEPSEEK_API_KEY', '')
RUAPI_KEY = os.getenv('RUAPI_API_KEY', '')
RUAPI_KEY_FULL = None

# Load full RuAPI key from .env
with open('/home/andy/.env', 'rb') as f:
    raw = f.read()
for line in raw.split(b'\n'):
    if line.startswith(b'RUAPI_API_KEY=') and b'***' not in line:
        RUAPI_KEY_FULL = line.decode('utf-8', errors='replace').strip().split('=', 1)[1]
        break

STATE_FILE = '/tmp/llm_health_state.json'
ALERT_FILE = '/tmp/llm_health_alerts.json'

def load_state():
    import json
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {'active': 'deepseek', 'deepseek_failures': 0, 'ruapi_failures': 0, 'last_check': None}

def save_state(state):
    import json
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def check_deepseek():
    """Check DeepSeek V4 Flash availability."""
    try:
        headers = {
            'Authorization': f'Bearer {DEEPSEEK_KEY}',
            'Content-Type': 'application/json',
        }
        data = {
            'model': 'deepseek-chat',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 5,
        }
        resp = requests.post(
            'https://api.deepseek.com/chat/completions',
            headers=headers, json=data, timeout=15
        )
        if resp.status_code == 200:
            return True, 'OK'
        elif resp.status_code == 402 or 'insufficient' in resp.text.lower():
            return False, f'HTTP {resp.status_code} — Insufficient Balance'
        elif resp.status_code == 401:
            return False, f'HTTP {resp.status_code} — Auth failed'
        else:
            return False, f'HTTP {resp.status_code}: {resp.text[:100]}'
    except Exception as e:
        return False, str(e)

def check_ruapi():
    """Check RuAPI (Stepanovikov) availability."""
    global RUAPI_KEY_FULL
    try:
        headers = {
            'Authorization': f'Bearer {RUAPI_KEY_FULL}',
            'Content-Type': 'application/json',
        }
        data = {
            'model': 'claude-opus-4.6',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 5,
        }
        resp = requests.post(
            'https://api.stepanovikov.uno/v1/chat/completions',
            headers=headers, json=data, timeout=15
        )
        if resp.status_code == 200:
            return True, 'OK'
        elif resp.status_code == 401:
            return False, f'HTTP 401 — Invalid key'
        else:
            return False, f'HTTP {resp.status_code}: {resp.text[:100]}'
    except Exception as e:
        return False, str(e)

def check_bybit_balance():
    """Check Bybit balance."""
    try:
        from src.gateways.bybit_api import BybitAPI
        api_key = os.getenv('BYBIT_API_KEY')
        api_secret = os.getenv('BYBIT_API_SECRET')
        g = BybitAPI(api_key, api_secret)
        bal = g.get_wallet_balance(account_type='UNIFIED')
        coins = bal.get('result', {}).get('list', [{}])[0].get('coin', [])
        for c in coins:
            if c.get('coin') == 'USDT':
                wb = float(c.get('walletBalance', 0))
                atw = float(c.get('availableToWithdraw', 0) or 0)
                return wb, atw
        return 0, 0
    except Exception as e:
        return None, f'Error: {e}'

def check_open_positions():
    """Check open positions on Bybit."""
    try:
        from src.gateways.bybit_api import BybitAPI
        api_key = os.getenv('BYBIT_API_KEY')
        api_secret = os.getenv('BYBIT_API_SECRET')
        g = BybitAPI(api_key, api_secret)
        count = 0
        for sym in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'DOGEUSDT', 'ENAUSDT']:
            pos_resp = g.get_positions(category='linear', symbol=sym)
            for p in pos_resp.get('result', {}).get('list', []):
                if float(p.get('size', 0)) > 0:
                    count += 1
        return count
    except Exception as e:
        return -1

def send_alert(message: str):
    """Send alert via Telegram."""
    try:
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')
        if not bot_token or not chat_id:
            return
        import urllib.request
        url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
        data = f'chat_id={chat_id}&text={urllib.parse.quote(message)}'
        req = urllib.request.Request(url, data=data.encode(), headers={'Content-Type': 'application/x-www-form-urlencoded'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning(f"Alert failed: {e}")

def main():
    state = load_state()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    logger.info("=" * 50)
    logger.info(f"LLM Health Check @ {now}")
    logger.info("=" * 50)

    # Check DeepSeek
    ds_ok, ds_msg = check_deepseek()
    logger.info(f"DeepSeek V4 Flash: {'✅ ' + ds_msg if ds_ok else '❌ ' + ds_msg}")

    # Check RuAPI
    ru_ok, ru_msg = check_ruapi()
    logger.info(f"RuAPI claude-opus-4.6: {'✅ ' + ru_msg if ru_ok else '❌ ' + ru_msg}")

    # Check Bybit
    wb, atw = check_bybit_balance()
    if isinstance(wb, float):
        logger.info(f"Bybit USDT: wallet={wb:.4f}, available={atw:.4f}")
    else:
        logger.error(f"Bybit balance: {atw}")

    # Check positions
    pos_count = check_open_positions()
    logger.info(f"Bybit open positions: {pos_count}")

    # Decision logic
    alerts = []

    if not ds_ok:
        state['deepseek_failures'] += 1
        logger.warning(f"DeepSeek FAILURE #{state['deepseek_failures']}: {ds_msg}")
        alerts.append(f"DeepSeek DOWN: {ds_msg}")

        if state['deepseek_failures'] >= 2:
            if ru_ok:
                if state['active'] != 'ruapi':
                    state['active'] = 'ruapi'
                    msg = f"⚠️ SWITCHED to RuAPI: DeepSeek failed {state['deepseek_failures']}x ({ds_msg})"
                    logger.warning(msg)
                    alerts.append(msg)
            else:
                msg = f"🚨 BOTH LLMs DOWN! DeepSeek={ds_msg}, RuAPI={ru_msg}"
                logger.error(msg)
                alerts.append(msg)
    else:
        if state['deepseek_failures'] > 0:
            state['deepseek_failures'] = 0
            logger.info("DeepSeek recovered")

        if state['active'] == 'ruapi' and ru_ok:
            state['active'] = 'deepseek'
            logger.info("Reverted to DeepSeek")

    # Check balance
    if isinstance(wb, float) and wb < 10:
        alerts.append(f"⚠️ LOW BALANCE: ${wb:.2f} USDT")

    # Check open positions
    if pos_count > 3:
        alerts.append(f"⚠️ MANY POSITIONS: {pos_count} open")

    state['last_check'] = now
    save_state(state)

    # Send alerts
    for alert in alerts:
        logger.warning(f"ALERT: {alert}")
        send_alert(f"[LLM Health] {alert}")

    logger.info(f"Active provider: {state['active']}")
    logger.info("=" * 50)

if __name__ == '__main__':
    main()
