import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import create_engine, Column, BigInteger, String, Decimal, DateTime, Boolean, Integer, Text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager

Base = declarative_base()

# ============================================
# SQLAlchemy Models
# ============================================

class OHLCVRaw(Base):
    __tablename__ = 'ohlcv_raw'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    exchange = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    open = Column(Decimal(20, 8), nullable=False)
    high = Column(Decimal(20, 8), nullable=False)
    low = Column(Decimal(20, 8), nullable=False)
    close = Column(Decimal(20, 8), nullable=False)
    volume = Column(Decimal(30, 8), nullable=False)
    quote_volume = Column(Decimal(30, 8))
    trades_count = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class OHLCVProcessed(Base):
    __tablename__ = 'ohlcv_processed'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    exchange = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    open = Column(Decimal(20, 8))
    high = Column(Decimal(20, 8))
    low = Column(Decimal(20, 8))
    close = Column(Decimal(20, 8))
    volume = Column(Decimal(30, 8))
    sma_20 = Column(Decimal(20, 8))
    sma_50 = Column(Decimal(20, 8))
    sma_200 = Column(Decimal(20, 8))
    ema_12 = Column(Decimal(20, 8))
    ema_26 = Column(Decimal(20, 8))
    rsi_14 = Column(Decimal(10, 4))
    macd = Column(Decimal(20, 8))
    macd_signal = Column(Decimal(20, 8))
    macd_hist = Column(Decimal(20, 8))
    atr_14 = Column(Decimal(20, 8))
    bollinger_upper = Column(Decimal(20, 8))
    bollinger_middle = Column(Decimal(20, 8))
    bollinger_lower = Column(Decimal(20, 8))
    css_value = Column(Decimal(10, 4))
    css_prior = Column(Decimal(10, 4))
    volume_sma_20 = Column(Decimal(30, 8))
    volume_ratio = Column(Decimal(10, 4))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class NewsRaw(Base):
    __tablename__ = 'news_raw'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False)
    title = Column(Text, nullable=False)
    url = Column(Text, unique=True, nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=False)
    summary = Column(Text)
    language = Column(String(10), default='en')
    sentiment_score = Column(Decimal(3, 2))
    sentiment_source = Column(String(50))
    ragflow_document_id = Column(String(255))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class Signal(Base):
    __tablename__ = 'signals'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    exchange = Column(String(50), nullable=False)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    signal_type = Column(String(10), nullable=False)
    strength = Column(Decimal(5, 4), nullable=False)
    css_value = Column(Decimal(10, 4))
    rsi_14 = Column(Decimal(10, 4))
    macd = Column(Decimal(20, 8))
    atr_14 = Column(Decimal(20, 8))
    price = Column(Decimal(20, 8))
    sentiment_score = Column(Decimal(3, 2))
    news_volume = Column(Integer, default=0)
    volume_24h = Column(Decimal(30, 8))
    confidence = Column(Decimal(5, 4))
    model_version = Column(String(50))
    reasoning = Column(Text)
    status = Column(String(20), default='PENDING')
    executed_at = Column(DateTime(timezone=True))
    pnl_percent = Column(Decimal(10, 4))
    pnl_absolute = Column(Decimal(20, 8))
    ragflow_decision_id = Column(String(255))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class Decision(Base):
    __tablename__ = 'decisions'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    signal_id = Column(BigInteger)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    market_data_json = Column(JSON, nullable=False)
    sentiment_data_json = Column(JSON)
    ragflow_context = Column(Text)
    news_context = Column(Text)
    llm_model = Column(String(100), nullable=False)
    prompt_tokens = Column(Integer)
    completion_tokens = Column(Integer)
    total_tokens = Column(Integer)
    latency_ms = Column(Integer)
    decision_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class Trade(Base):
    __tablename__ = 'trades'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    signal_id = Column(BigInteger)
    exchange = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    side = Column(String(10), nullable=False)
    order_type = Column(String(20), nullable=False)
    quantity = Column(Decimal(30, 8), nullable=False)
    price = Column(Decimal(20, 8))
    fee = Column(Decimal(20, 8))
    pnl_percent = Column(Decimal(10, 4))
    pnl_absolute = Column(Decimal(20, 8))
    order_id = Column(String(255))
    order_link_id = Column(String(255))
    stop_loss = Column(Decimal(20, 8))
    take_profit = Column(Decimal(20, 8))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class SelectedSymbol(Base):
    __tablename__ = 'selected_symbols'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    exchange = Column(String(50), nullable=False)
    volume_24h = Column(Decimal(30, 8))
    change_1h = Column(Decimal(10, 4))
    spread_percent = Column(Decimal(10, 4))
    selection_score = Column(Decimal(10, 4))
    is_active = Column(Boolean, default=True)
    selected_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class AgentLog(Base):
    __tablename__ = 'agent_logs'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    agent_name = Column(String(50), nullable=False)
    level = Column(String(10), nullable=False)
    message = Column(Text, nullable=False)
    data_json = Column(JSON)
    timestamp = Column(DateTime(timezone=True), default=datetime.utcnow)

class ExportHistory(Base):
    __tablename__ = 'export_history'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    export_type = Column(String(20), nullable=False)
    file_path = Column(Text, nullable=False)
    records_count = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class DatabaseManager:
    """Manages PostgreSQL connections and sessions"""
    
    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = config
        self.logger = logger
        
        connection_string = (
            f"postgresql://{config['username']}:{config['password']}"
            f"@{config['host']}:{config['port']}/{config['database']}"
        )
        
        self.engine = create_engine(
            connection_string,
            pool_size=config.get('pool_size', 10),
            max_overflow=config.get('max_overflow', 20),
            pool_pre_ping=True,
            echo=False
        )
        
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.logger.info(f"DatabaseManager initialized for {config['host']}:{config['port']}")
    
    @contextmanager
    def get_session(self) -> Session:
        """Get a database session with automatic commit/rollback"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            self.logger.error(f"Database session error: {e}")
            raise
        finally:
            session.close()
    
    def create_tables(self):
        """Create all tables"""
        Base.metadata.create_all(self.engine)
        self.logger.info("All database tables created")
    
    def drop_tables(self):
        """Drop all tables (use with caution!)"""
        Base.metadata.drop_all(self.engine)
        self.logger.warning("All database tables dropped")
    
    def test_connection(self) -> bool:
        """Test database connection"""
        try:
            with self.engine.connect() as conn:
                conn.execute("SELECT 1")
            self.logger.info("Database connection successful")
            return True
        except Exception as e:
            self.logger.error(f"Database connection failed: {e}")
            return False
