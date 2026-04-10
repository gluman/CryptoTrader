import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, Any, Optional


class EmailNotifier:
    """Email notifications for trading operations"""
    
    def __init__(self, smtp_host: str, smtp_port: int, sender: str, 
                 password: str, recipients: list, logger: Optional[logging.Logger] = None):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.sender = sender
        self.password = password
        self.recipients = recipients
        self.logger = logger or logging.getLogger(__name__)
    
    def send_email(self, subject: str, body: str) -> bool:
        """Send email to all recipients"""
        if not self.recipients:
            self.logger.warning("No email recipients configured")
            return False
        
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.sender
            
            # HTML body
            html_body = body.replace('\n', '<br>')
            msg.attach(MIMEText(html_body, 'html'))
            
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender, self.password)
                
                for recipient in self.recipients:
                    msg['To'] = recipient
                    server.sendmail(self.sender, recipient, msg.as_string())
            
            self.logger.info(f"Email sent: {subject}")
            return True
        except Exception as e:
            self.logger.error(f"Email send failed: {e}")
            return False
    
    def notify_error(self, agent_name: str, error_message: str):
        """Send error notification"""
        subject = f"🚨 CryptoTrader Error - {agent_name}"
        body = f"""
        <h2>CryptoTrader Error</h2>
        <p><b>Agent:</b> {agent_name}</p>
        <p><b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
        <p><b>Error:</b> <code>{error_message[:500]}</code></p>
        """
        self.send_email(subject, body)
    
    def notify_trade(self, symbol: str, side: str, amount: str, price: float,
                     exchange: str, confidence: float):
        """Send trade notification"""
        emoji = "🟢" if side == "BUY" else "🔴"
        subject = f"{emoji} Trade Executed - {symbol}"
        body = f"""
        <h2>Trade Executed</h2>
        <p><b>Symbol:</b> {symbol}</p>
        <p><b>Side:</b> {side}</p>
        <p><b>Amount:</b> {amount}</p>
        <p><b>Price:</b> ${price:,.2f}</p>
        <p><b>Exchange:</b> {exchange}</p>
        <p><b>Confidence:</b> {confidence:.0%}</p>
        <p><b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
        """
        self.send_email(subject, body)
    
    def notify_hourly_summary(self, summary: Dict[str, Any]):
        """Send hourly summary"""
        buys = summary.get('buys', 0)
        sells = summary.get('sells', 0)
        errors = summary.get('errors', 0)
        
        emoji = "✅" if errors == 0 else "⚠️"
        subject = f"{emoji} CryptoTrader Hourly Report"
        body = f"""
        <h2>Hourly Report</h2>
        <p><b>Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC</p>
        <hr>
        <h3>Data Collection</h3>
        <p>OHLCV: {summary.get('ohlcv_records', 0)}</p>
        <p>News: {summary.get('news_records', 0)}</p>
        <p>Avg Sentiment: {summary.get('avg_sentiment', 'N/A')}</p>
        <hr>
        <h3>Trading Decisions</h3>
        <p>BUY: {buys} | SELL: {sells} | HOLD: {summary.get('holds', 0)}</p>
        <p>Trades Executed: {summary.get('trades_executed', 0)}</p>
        <hr>
        <p><b>Errors:</b> {errors}</p>
        """
        if errors > 0:
            body += f"<p><b>Error Details:</b> {summary.get('error_details', 'Check logs')}</p>"
        
        self.send_email(subject, body)
    
    def verify_connection(self) -> bool:
        """Verify SMTP connection"""
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.sender, self.password)
            self.logger.info("Email connection verified")
            return True
        except Exception as e:
            self.logger.error(f"Email verification failed: {e}")
            return False