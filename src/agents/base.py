import logging
from abc import ABC, abstractmethod
from typing import Dict, Any
from datetime import datetime

class BaseAgent(ABC):
    """Base class for all agents"""
    
    def __init__(self, name: str, logger: logging.Logger):
        self.name = name
        self.logger = logger
        self.running = False
        self.last_run = None
    
    @abstractmethod
    def run_once(self) -> Dict[str, Any]:
        """Execute one cycle of the agent. Returns result dict."""
        pass
    
    def start(self):
        """Start the agent"""
        self.running = True
        self.log('info', f"{self.name} started")
    
    def stop(self):
        """Stop the agent"""
        self.running = False
        self.log('info', f"{self.name} stopped")
    
    def log(self, level: str, message: str):
        """Log with agent name prefix"""
        getattr(self.logger, level)(f"[{self.name}] {message}")
    
    def log_to_db(self, level: str, message: str, data: Any = None):
        """Log to database (agent_logs table)"""
        self.log(level, message)
        # Database logging handled by DatabaseManager
