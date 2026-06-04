#!/usr/bin/env python3
"""
CryptoTrader Health Monitor
Checks system health and restarts services if needed
"""

import sys
import os
import time
import json
import logging
import subprocess
import requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import Config
from src.core.logger import setup_logger


class HealthMonitor:
    """Monitors CryptoTrader services health"""
    
    def __init__(self):
        self.config = Config.load()
        self.logger = setup_logger(
            'cryptotrader.monitor',
            level='INFO',
            log_file=self.config.logging.get('file'),
        )
        
        self.api_url = "http://localhost:8000"
        self.check_interval = 60  # seconds
        self.max_failures = 3
        self.failure_counts = {
            'api': 0,
            'database': 0,
            'scheduler': 0,
        }
        
        self.last_notification = datetime.min
    
    def check_api_health(self) -> bool:
        """Check if API server is responding"""
        try:
            resp = requests.get(f"{self.api_url}/health", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self.logger.debug(f"API health: {data}")
                return data.get('status') == 'ok'
            return False
        except Exception as e:
            self.logger.warning(f"API health check failed: {e}")
            return False
    
    def check_database(self) -> bool:
        """Check database connectivity"""
        try:
            from src.core.database import DatabaseManager
            db = DatabaseManager(self.config.postgresql, self.logger)
            return db.test_connection()
        except Exception as e:
            self.logger.warning(f"Database check failed: {e}")
            return False
    
    def check_systemd_service(self, service_name: str) -> str:
        """Check systemd service status"""
        try:
            result = subprocess.run(
                ['systemctl', 'is-active', service_name],
                capture_output=True, text=True, timeout=10
            )
            return result.stdout.strip()
        except Exception:
            return 'unknown'
    
    def restart_service(self, service_name: str) -> bool:
        """Restart a systemd service"""
        try:
            self.logger.warning(f"Restarting {service_name}...")
            result = subprocess.run(
                ['sudo', 'systemctl', 'restart', service_name],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                self.logger.info(f"Service {service_name} restarted successfully")
                return True
            else:
                self.logger.error(f"Failed to restart {service_name}: {result.stderr}")
                return False
        except Exception as e:
            self.logger.error(f"Restart error: {e}")
            return False
    
    def check_disk_space(self) -> dict:
        """Check disk space"""
        try:
            result = subprocess.run(
                ['df', '-h', '/opt/cryptotrader'],
                capture_output=True, text=True, timeout=10
            )
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = lines[1].split()
                return {
                    'total': parts[1],
                    'used': parts[2],
                    'available': parts[3],
                    'percent': parts[4],
                }
        except Exception:
            pass
        return {}
    
    def check_memory(self) -> dict:
        """Check memory usage"""
        try:
            result = subprocess.run(
                ['free', '-h'],
                capture_output=True, text=True, timeout=10
            )
            lines = result.stdout.strip().split('\n')
            if len(lines) > 1:
                parts = lines[1].split()
                return {
                    'total': parts[1],
                    'used': parts[2],
                    'free': parts[3],
                }
        except Exception:
            pass
        return {}
    
    def send_telegram_alert(self, message: str):
        """Send alert via Telegram"""
        try:
            bot_token = self.config.telegram.get('bot_token', '')
            chat_id = self.config.telegram.get('chat_id', '')
            
            if not bot_token or not chat_id:
                return
            
            # Rate limit: max 1 notification per 5 minutes
            if datetime.utcnow() - self.last_notification < timedelta(minutes=5):
                return
            
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': f"🚨 CryptoTrader Alert\n\n{message}",
                'parse_mode': 'Markdown',
            }
            requests.post(url, json=data, timeout=10)
            self.last_notification = datetime.utcnow()
        except Exception as e:
            self.logger.error(f"Telegram alert failed: {e}")
    
    def run_checks(self) -> dict:
        """Run all health checks"""
        results = {
            'timestamp': datetime.utcnow().isoformat(),
            'api': False,
            'database': False,
            'scheduler_service': 'unknown',
            'api_service': 'unknown',
            'disk': {},
            'memory': {},
            'issues': [],
        }
        
        # Check API
        results['api'] = self.check_api_health()
        if not results['api']:
            self.failure_counts['api'] += 1
            results['issues'].append(f"API not responding ({self.failure_counts['api']}/{self.max_failures})")
        else:
            self.failure_counts['api'] = 0
        
        # Check database
        results['database'] = self.check_database()
        if not results['database']:
            self.failure_counts['database'] += 1
            results['issues'].append(f"Database connection failed ({self.failure_counts['database']}/{self.max_failures})")
        else:
            self.failure_counts['database'] = 0
        
        # Check services
        results['api_service'] = self.check_systemd_service('cryptotrader-api.service')
        results['scheduler_service'] = self.check_systemd_service('cryptotrader-scheduler.service')
        
        if results['api_service'] != 'active':
            results['issues'].append(f"API service is {results['api_service']}")
        
        if results['scheduler_service'] != 'active':
            results['issues'].append(f"Scheduler service is {results['scheduler_service']}")
        
        # System resources
        results['disk'] = self.check_disk_space()
        results['memory'] = self.check_memory()
        
        # Check disk space warning
        disk_pct = results['disk'].get('percent', '0%').replace('%', '')
        if int(disk_pct) > 90:
            results['issues'].append(f"Disk space critical: {disk_pct}% used")
        
        return results
    
    def auto_recover(self, results: dict):
        """Attempt automatic recovery"""
        # Restart API if down
        if self.failure_counts['api'] >= self.max_failures:
            self.logger.warning("API failures exceeded threshold, restarting...")
            self.restart_service('cryptotrader-api.service')
            self.failure_counts['api'] = 0
            self.send_telegram_alert("API service restarted due to health check failures")
        
        # Restart scheduler if not active
        if results['scheduler_service'] in ['failed', 'inactive']:
            self.logger.warning("Scheduler not active, restarting...")
            self.restart_service('cryptotrader-scheduler.service')
            self.send_telegram_alert(f"Scheduler service restarted (was {results['scheduler_service']})")
    
    def run(self):
        """Main monitoring loop"""
        self.logger.info("Health Monitor started")
        
        while True:
            try:
                results = self.run_checks()
                
                if results['issues']:
                    self.logger.warning(f"Health issues: {results['issues']}")
                    self.auto_recover(results)
                    
                    # Send alert for critical issues
                    if len(results['issues']) >= 3:
                        alert_msg = "\n".join(results['issues'])
                        self.send_telegram_alert(alert_msg)
                else:
                    self.logger.debug("All health checks passed")
                
                time.sleep(self.check_interval)
            
            except KeyboardInterrupt:
                self.logger.info("Health Monitor stopped")
                break
            except Exception as e:
                self.logger.error(f"Monitor error: {e}")
                time.sleep(60)


def main():
    monitor = HealthMonitor()
    monitor.run()


if __name__ == '__main__':
    main()
