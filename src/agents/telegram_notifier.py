import requests
import logging
from datetime import datetime
from typing import Dict, Any, Optional

class TelegramNotifier:
    """Telegram bot for error alerts and trade notifications"""
    
    def __init__(self, bot_token: str, chat_id: str = '', logger: Optional[logging.Logger] = None):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.logger = logger or logging.getLogger(__name__)
    
    def send_message(self, text: str, parse_mode: str = 'HTML') -> bool:
        """Send a message to Telegram"""
        if not self.chat_id:
            self.logger.warning("Telegram chat_id not configured, skipping notification")
            return False
        
        try:
            resp = requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    'chat_id': self.chat_id,
                    'text': text,
                    'parse_mode': parse_mode,
                },
                timeout=10
            )
            return resp.status_code == 200
        except Exception as e:
            self.logger.error(f"Telegram send failed: {e}")
            return False
    
    def notify_error(self, agent_name: str, error_message: str):
        """Send error notification"""
        text = f"""🚨 <b>CryptoTrader Error</b>
<b>Agent:</b> {agent_name}
<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
<b>Error:</b> <code>{error_message[:500]}</code>"""
        self.send_message(text)
    
    def notify_trade(self, symbol: str, side: str, amount: str, price: float, 
                     exchange: str, confidence: float):
        """Send trade notification"""
        emoji = "🟢" if side == "BUY" else "🔴"
        text = f"""{emoji} <b>Trade Executed</b>
<b>Symbol:</b> {symbol}
<b>Side:</b> {side}
<b>Amount:</b> {amount}
<b>Price:</b> ${price:,.2f}
<b>Exchange:</b> {exchange}
<b>Confidence:</b> {confidence:.0%}
<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"""
        self.send_message(text)
    
    def notify_signal(self, symbol: str, signal: str, confidence: float, reasoning: str):
        """Send signal notification"""
        emoji = "📈" if signal == "BUY" else "📉" if signal == "SELL" else "⏸️"
        text = f"""{emoji} <b>New Signal</b>
<b>Symbol:</b> {symbol}
<b>Signal:</b> {signal}
<b>Confidence:</b> {confidence:.0%}
<b>Reasoning:</b> {reasoning[:200]}
<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"""
        self.send_message(text)
    
    def notify_daily_summary(self, summary: Dict[str, Any]):
        """Send daily summary"""
        text = f"""📊 <b>Daily Summary</b>
<b>Date:</b> {datetime.utcnow().strftime('%Y-%m-%d')}
<b>Signals Generated:</b> {summary.get('total_signals', 0)}
<b>BUY:</b> {summary.get('buys', 0)} | <b>SELL:</b> {summary.get('sells', 0)} | <b>HOLD:</b> {summary.get('holds', 0)}
<b>Trades Executed:</b> {summary.get('trades_executed', 0)}
<b>Total P&L:</b> {summary.get('total_pnl', 'N/A')}
<b>Win Rate:</b> {summary.get('win_rate', 'N/A')}"""
        self.send_message(text)
    
    def notify_hourly_summary(self, summary: Dict[str, Any]):
        """Send hourly summary of operations"""
        buys = summary.get('buys', 0)
        sells = summary.get('sells', 0)
        errors = summary.get('errors', 0)
        
        emoji = "✅" if errors == 0 else "⚠️"
        text = f"""{emoji} <b>Hourly Report</b>
<b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC
<b>Collect:</b> {summary.get('ohlcv_records', 0)} OHLCV | {summary.get('news_records', 0)} News
<b>Sentiment:</b> avg={summary.get('avg_sentiment', 'N/A')}
<b>Decisions:</b> {buys} BUY | {sells} SELL | {summary.get('holds', 0)} HOLD
<b>Trades:</b> {summary.get('trades_executed', 0)}
<b>Errors:</b> {errors}"""
        
        if errors > 0:
            text += f"\n<b>Errors:</b> {summary.get('error_details', 'Check logs')}"
        
        self.send_message(text)
    
    def verify_connection(self) -> bool:
        """Verify bot is working"""
        try:
            resp = requests.get(f"{self.base_url}/getMe", timeout=10)
            data = resp.json()
            if data.get('ok'):
                bot_name = data['result'].get('username', 'unknown')
                self.logger.info(f"Telegram bot verified: @{bot_name}")
                return True
            return False
        except Exception as e:
            self.logger.error(f"Telegram verification failed: {e}")
            return False
