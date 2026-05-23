#!/usr/bin/env python3
"""
CryptoTrader Regression Cron
Runs cryptotrader_regression_analysis.py and sends the
plain-text report to Telegram DM 519881679 every 12h.
"""
import sys
import os
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

SCRIPT_PATH = '/home/andy/cryptotrader/scripts/cryptotrader_regression_analysis.py'
CHAT_ID = '519881679'
TELEGRAM_API = 'https://api.telegram.org'


def load_env():
    env = {}
    # First load /home/andy/.hermes/.env for TELEGRAM_BOT_TOKEN (real token)
    hermes_env_path = '/home/andy/.hermes/.env'
    if os.path.exists(hermes_env_path):
        for line in open(hermes_env_path).read().splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                # Only take TELEGRAM_BOT_TOKEN and TELEGRAM_PROXY from hermes env
                if k in ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_PROXY'):
                    env[k] = v
                    os.environ[k] = v

    # Then load /home/andy/.env for everything else (POSTGRES_*, exchanges, etc.)
    for line in open('/home/andy/.env').read().splitlines():
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            if k not in env:  # Don't override if already set from hermes
                env[k] = v
                os.environ[k] = v
    return env


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    """Send text message via Telegram Bot API. Split if > 4096 chars."""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    # Telegram message limit
    max_len = 4096
    sent_ok = True
    for i in range(0, len(text), max_len):
        chunk = text[i:i + max_len]
        data = urllib.parse.urlencode({'chat_id': chat_id, 'text': chunk})
        req = urllib.request.Request(
            url,
            data=data.encode('utf-8'),
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = resp.read().decode()
                if '"ok":true' not in result:
                    print(f"Telegram API error: {result}")
                    sent_ok = False
        except Exception as e:
            print(f"Failed to send Telegram message: {e}")
            sent_ok = False
    return sent_ok


def run():
    env = load_env()
    bot_token = env.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = CHAT_ID

    if not bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    # Run the analysis script
    import subprocess
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH],
        capture_output=True,
        text=True,
        timeout=120,
    )
    report = result.stdout
    if result.stderr:
        report += "\n\nSTDERR:\n" + result.stderr

    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    header = f"📊 *CryptoTrader Regression Report*\n🕓 {now}\n{'─' * 40}\n"
    full_text = header + report

    success = send_telegram_message(bot_token, chat_id, full_text)
    if success:
        print(f"Report sent to Telegram DM {chat_id} ({len(full_text)} chars)")
    else:
        print("Failed to send report to Telegram")
        sys.exit(1)


if __name__ == '__main__':
    run()