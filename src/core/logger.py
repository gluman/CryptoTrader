import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional


def setup_logger(
    name: str = 'cryptotrader',
    level: str = 'INFO',
    log_file: Optional[str] = None,
    max_bytes: int = 10485760,
    backup_count: int = 5,
    format_string: Optional[str] = None,
    use_journald: bool = False,
) -> logging.Logger:
    """
    Setup a logger with console, file, and optional journald handlers
    
    Args:
        name: Logger name
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file (optional)
        max_bytes: Maximum log file size before rotation
        backup_count: Number of backup files to keep
        format_string: Custom format string
        use_journald: Use systemd journal logging (Linux only)
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    
    # Prevent duplicate handlers
    if logger.handlers:
        return logger
    
    # Format
    if format_string is None:
        format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    
    # Console handler (stdout for systemd journal capture)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler with rotation
    if log_file:
        log_dir = Path(log_file).parent
        log_dir.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    # Journald handler (Linux systemd)
    if use_journald and sys.platform == 'linux':
        try:
            from systemd.journal import JournalHandler
            journal_handler = JournalHandler(SYSLOG_IDENTIFIER=name)
            journal_handler.setLevel(logging.DEBUG)
            journal_handler.setFormatter(logging.Formatter(
                '%(name)s - %(levelname)s - %(message)s'
            ))
            logger.addHandler(journal_handler)
        except ImportError:
            pass  # systemd-python not installed, skip
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """Get an existing logger by name"""
    return logging.getLogger(name)
