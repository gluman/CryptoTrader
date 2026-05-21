#!/usr/bin/env python3
"""
Complete diagnostic and startup script for CryptoTrader
Run this on the server: python setup_and_start.py
"""

import os
import sys
import subprocess
import time
from pathlib import Path
import logging

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('setup')

def print_header(text):
    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)

def run_command(cmd, cwd=None, capture=True):
    """Run shell command and return output"""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=capture, text=True, timeout=30
        )
        if capture:
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        return None, None, result.returncode
    except subprocess.TimeoutExpired:
        return None, "Timeout", -1
    except Exception as e:
        return None, str(e), -1

def check_venv():
    """Check if virtual environment exists and is activated"""
    print_header("1. Checking Virtual Environment")
    
    venv_path = Path.home() / 'cryptotrader-venv'
    if not venv_path.exists():
        logger.error(f"Virtual environment not found at {venv_path}")
        logger.info("Creating new virtual environment...")
        out, err, code = run_command("uv venv cryptotrader-venv", cwd=Path.home())
        if code != 0:
            logger.error(f"Failed to create venv: {err}")
            return False
        logger.info("Virtual environment created")
    
    # Check if we're in venv
    if not os.environ.get('VIRTUAL_ENV'):
        logger.warning("Not in virtual environment. Please activate:")
        logger.info(f"  source ~/cryptotrader-venv/bin/activate")
        return False
    
    logger.info(f"Virtual environment: {os.environ.get('VIRTUAL_ENV')}")
    return True

def check_dependencies():
    """Check and install missing dependencies"""
    print_header("2. Checking Dependencies")
    
    required = ['flask', 'psycopg2-binary', 'feedparser']
    missing = []
    
    for pkg in required:
        try:
            __import__(pkg.replace('-', '_'))
            logger.info(f"  ✓ {pkg} is installed")
        except ImportError:
            logger.warning(f"  ✗ {pkg} is missing")
            missing.append(pkg)
    
    if missing:
        logger.info(f"Installing missing packages: {', '.join(missing)}")
        cmd = f"pip install {' '.join(missing)}"
        out, err, code = run_command(cmd)
        if code != 0:
            logger.error(f"Installation failed: {err}")
            return False
        logger.info("All dependencies installed")
    
    return True

def check_project():
    """Check if project exists and is configured"""
    print_header("3. Checking Project")
    
    project_dir = Path.home() / 'CryptoTrader'
    if not project_dir.exists():
        logger.error(f"Project not found at {project_dir}")
        return False
    
    logger.info(f"Project directory: {project_dir}")
    
    # Check .env
    env_file = project_dir / '.env'
    if not env_file.exists():
        logger.error(".env file not found")
        return False
    
    logger.info("  ✓ .env exists")
    
    # Check config
    config_file = project_dir / 'config' / 'settings.yaml'
    if not config_file.exists():
        logger.error("Config file not found")
        return False
    
    logger.info("  ✓ config/settings.yaml exists")
    
    return True

def check_database():
    """Check database connectivity and schema"""
    print_header("4. Checking Database")
    
    os.chdir(Path.home() / 'CryptoTrader')
    sys.path.insert(0, '.')
    
    try:
        from src.core.config import Config
        from src.core.database import DatabaseManager
        import logging
        
        config = Config.load()
        db = DatabaseManager(config.postgresql, logger)
        
        # Test connection
        if db.test_connection():
            logger.info("  ✓ Database connection OK")
        else:
            logger.error("  ✗ Database connection failed")
            return False
        
        # Create tables if needed
        db.create_tables()
        logger.info("  ✓ Tables created/verified")
        
        return True
    except Exception as e:
        logger.error(f"Database check failed: {e}")
        return False

def start_pipeline():
    """Start the main pipeline"""
    print_header("5. Starting Pipeline")
    
    project_dir = Path.home() / 'CryptoTrader'
    log_file = project_dir / 'logs' / 'cryptotrader.log'
    
    # Check if already running
    out, err, code = run_command("ps aux | grep run_pipeline.py | grep -v grep")
    if out:
        logger.info("Pipeline is already running (PID found)")
        return True
    
    # Start pipeline in background
    logger.info("Starting pipeline...")
    cmd = f"nohup python run_pipeline.py > {log_file} 2>&1 &"
    out, err, code = run_command(cmd, cwd=project_dir, capture=False)
    
    if code != 0:
        logger.error(f"Failed to start pipeline: {err}")
        return False
    
    logger.info("Pipeline started in background")
    time.sleep(3)
    
    # Check if it's actually running
    out, err, code = run_command("sleep 2 && ps aux | grep run_pipeline.py | grep -v grep")
    if out:
        logger.info("✓ Pipeline is running")
        return True
    else:
        logger.error("Pipeline failed to start")
        return False

def start_status_server():
    """Start the status web server"""
    print_header("6. Starting Status Server")
    
    project_dir = Path.home() / 'CryptoTrader'
    log_file = project_dir / 'logs' / 'status_server.log'
    
    # Check if already running
    out, err, code = run_command("ps aux | grep status_server.py | grep -v grep")
    if out:
        logger.info("Status server is already running")
        return True
    
    # Check if Flask is available
    try:
        import flask
        logger.info(f"  ✓ Flask {flask.__version__} is installed")
    except ImportError:
        logger.error("Flask is not installed. Install with: pip install flask")
        return False
    
    # Start status server
    logger.info("Starting status server on port 5000...")
    cmd = f"nohup python status_server.py > {log_file} 2>&1 &"
    out, err, code = run_command(cmd, cwd=project_dir, capture=False)
    
    if code != 0:
        logger.error(f"Failed to start status server: {err}")
        return False
    
    logger.info("Status server started in background")
    time.sleep(2)
    
    # Verify it's running
    out, err, code = run_command("sleep 2 && ps aux | grep status_server.py | grep -v grep")
    if out:
        logger.info("✓ Status server is running on http://0.0.0.0:5000")
        return True
    else:
        logger.error("Status server failed to start")
        # Show logs
        if log_file.exists():
            logger.info(f"Last log lines:")
            with open(log_file, 'r') as f:
                lines = f.readlines()[-10:]
                for line in lines:
                    logger.info(f"  {line.strip()}")
        return False

def verify_telegram():
    """Verify Telegram bot configuration"""
    print_header("7. Telegram Bot")
    
    os.chdir(Path.home() / 'CryptoTrader')
    sys.path.insert(0, '.')
    
    try:
        from src.core.config import Config
        from src.agents.telegram_notifier import TelegramNotifier
        
        config = Config.load()
        telegram = TelegramNotifier(
            bot_token=config.telegram.get('bot_token', ''),
            chat_id=config.telegram.get('chat_id', ''),
            logger=logger
        )
        
        if not config.telegram.get('bot_token'):
            logger.warning("Telegram bot token not configured")
            return False
        
        if not config.telegram.get('chat_id'):
            logger.warning("Telegram chat_id not configured")
            return False
        
        if telegram.verify_connection():
            logger.info(f"✓ Telegram bot connected: @{telegram.bot_token.split(':')[0]}")
            return True
        else:
            logger.error("Telegram connection failed")
            return False
            
    except Exception as e:
        logger.error(f"Telegram check failed: {e}")
        return False

def show_status():
    """Show final status"""
    print_header("Final Status")
    
    project_dir = Path.home() / 'CryptoTrader'
    
    print("\n📊 Services Status:")
    
    # Pipeline
    out, err, code = run_command("ps aux | grep run_pipeline.py | grep -v grep | wc -l")
    if out and int(out.strip()) > 0:
        print("  ✅ Pipeline: RUNNING")
    else:
        print("  ❌ Pipeline: NOT RUNNING")
    
    # Status server
    out, err, code = run_command("ps aux | grep status_server.py | grep -v grep | wc -l")
    if out and int(out.strip()) > 0:
        print("  ✅ Status Server: RUNNING on port 5000")
    else:
        print("  ❌ Status Server: NOT RUNNING")
    
    # PostgreSQL connection
    try:
        sys.path.insert(0, '.')
        os.chdir(project_dir)
        from src.core.config import Config
        from src.core.database import DatabaseManager
        config = Config.load()
        db = DatabaseManager(config.postgresql, logger)
        if db.test_connection():
            print("  ✅ PostgreSQL: CONNECTED")
        else:
            print("  ❌ PostgreSQL: CONNECTION FAILED")
    except Exception as e:
        print(f"  ❌ PostgreSQL: ERROR - {e}")
    
    # Telegram
    try:
        from src.agents.telegram_notifier import TelegramNotifier
        telegram = TelegramNotifier(
            config.telegram.get('bot_token', ''),
            config.telegram.get('chat_id', '')
        )
        if telegram.verify_connection():
            print("  ✅ Telegram: CONNECTED")
        else:
            print("  ⚠️  Telegram: NOT CONNECTED (check .env)")
    except:
        print("  ❌ Telegram: ERROR")
    
    print("\n🌐 Access URLs:")
    print("  Dashboard: http://192.168.0.43:5000")
    print("  API Status: http://192.168.0.43:5000/api/status")
    
    print("\n📝 Log Files:")
    print(f"  Pipeline: {project_dir}/logs/cryptotrader.log")
    print(f"  Status:   {project_dir}/logs/status_server.log")
    
    print("\n🔧 Manual Commands:")
    print("  Activate venv: source ~/cryptotrader-venv/bin/activate")
    print("  View pipeline log: tail -f ~/CryptoTrader/logs/cryptotrader.log")
    print("  View status log:  tail -f ~/CryptoTrader/logs/status_server.log")
    print("  Restart pipeline: pkill -f run_pipeline.py && nohup python run_pipeline.py > logs/cryptotrader.log 2>&1 &")
    print("  Restart status:   pkill -f status_server.py && nohup python status_server.py > logs/status_server.log 2>&1 &")

def main():
    print_header("🚀 CryptoTrader Setup & Diagnostics")
    
    # Change to project dir if not already
    project_dir = Path.home() / 'CryptoTrader'
    if project_dir.exists():
        os.chdir(project_dir)
    
    results = []
    
    # Run checks
    results.append(("Virtual Environment", check_venv()))
    results.append(("Dependencies", check_dependencies()))
    results.append(("Project Structure", check_project()))
    results.append(("Database", check_database()))
    
    # Start services if everything OK so far
    if all(r[1] for r in results):
        results.append(("Pipeline Start", start_pipeline()))
        results.append(("Status Server Start", start_status_server()))
        results.append(("Telegram", verify_telegram()))
    
    # Show status
    show_status()
    
    print_header("✅ Diagnostics Complete")
    
    # Summary
    failed = [name for name, ok in results if not ok]
    if failed:
        logger.error(f"\nIssues found: {', '.join(failed)}")
        logger.info("Fix the above issues and re-run this script.")
        sys.exit(1)
    else:
        logger.info("\nAll systems are operational!")
        logger.info("Check the dashboard at http://192.168.0.43:5000")
        sys.exit(0)

if __name__ == '__main__':
    main()
