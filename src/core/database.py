import logging
from decimal import Decimal as PyDecimal
from datetime import datetime
from typing import Optional, List, Dict, Any
from sqlalchemy import create_engine, Column, BigInteger, String, DateTime, Boolean, Integer, Text, JSON, Numeric, UniqueConstraint, text
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
    open = Column(Numeric(20, 8), nullable=False)
    high = Column(Numeric(20, 8), nullable=False)
    low = Column(Numeric(20, 8), nullable=False)
    close = Column(Numeric(20, 8), nullable=False)
    volume = Column(Numeric(30, 8), nullable=False)
    quote_volume = Column(Numeric(30, 8))
    trades_count = Column(Integer)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    
    __table_args__ = (
        # Unique constraint for ON CONFLICT to work
        UniqueConstraint('exchange', 'symbol', 'timeframe', 'timestamp', name='uix_ohlcv_unique'),
    )

class OHLCVProcessed(Base):
    __tablename__ = 'ohlcv_processed'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    exchange = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    timeframe = Column(String(10), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False)
    open = Column(Numeric(20, 8))
    high = Column(Numeric(20, 8))
    low = Column(Numeric(20, 8))
    close = Column(Numeric(20, 8))
    volume = Column(Numeric(30, 8))
    sma_20 = Column(Numeric(20, 8))
    sma_50 = Column(Numeric(20, 8))
    sma_200 = Column(Numeric(20, 8))
    ema_12 = Column(Numeric(20, 8))
    ema_26 = Column(Numeric(20, 8))
    rsi_14 = Column(Numeric(10, 4))
    macd = Column(Numeric(20, 8))
    macd_signal = Column(Numeric(20, 8))
    macd_hist = Column(Numeric(20, 8))
    atr_14 = Column(Numeric(20, 8))
    bollinger_upper = Column(Numeric(20, 8))
    bollinger_middle = Column(Numeric(20, 8))
    bollinger_lower = Column(Numeric(20, 8))
    css_value = Column(Numeric(10, 4))
    css_prior = Column(Numeric(10, 4))
    volume_sma_20 = Column(Numeric(30, 8))
    volume_ratio = Column(Numeric(10, 4))
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
    sentiment_score = Column(Numeric(3, 2))
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
    strength = Column(Numeric(5, 4), nullable=False)
    css_value = Column(Numeric(10, 4))
    rsi_14 = Column(Numeric(10, 4))
    macd = Column(Numeric(20, 8))
    atr_14 = Column(Numeric(20, 8))
    price = Column(Numeric(20, 8))
    sentiment_score = Column(Numeric(3, 2))
    news_volume = Column(Integer, default=0)
    volume_24h = Column(Numeric(30, 8))
    confidence = Column(Numeric(5, 4))
    model_version = Column(String(50))
    reasoning = Column(Text)
    status = Column(String(20), default='PENDING')
    executed_at = Column(DateTime(timezone=True))
    pnl_percent = Column(Numeric(10, 4))
    pnl_absolute = Column(Numeric(20, 8))
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
    quantity = Column(Numeric(30, 8), nullable=False)
    price = Column(Numeric(20, 8))
    fee = Column(Numeric(20, 8))
    pnl_percent = Column(Numeric(10, 4))
    pnl_absolute = Column(Numeric(20, 8))
    order_id = Column(String(255))
    order_link_id = Column(String(255))
    stop_loss = Column(Numeric(20, 8))
    take_profit = Column(Numeric(20, 8))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

class SelectedSymbol(Base):
    __tablename__ = 'selected_symbols'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    exchange = Column(String(50), nullable=False)
    volume_24h = Column(Numeric(30, 8))
    change_1h = Column(Numeric(10, 4))
    spread_percent = Column(Numeric(10, 4))
    selection_score = Column(Numeric(10, 4))
    is_active = Column(Boolean, default=True)
    selected_at = Column(DateTime(timezone=True), default=datetime.utcnow)


class Position(Base):
    __tablename__ = 'positions'
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    exchange = Column(String(50), nullable=False)
    side = Column(String(10), nullable=False, default='LONG')
    entry_price = Column(Numeric(20, 8), nullable=False)
    quantity = Column(Numeric(30, 8), nullable=False)
    cost_usdt = Column(Numeric(20, 8), nullable=False)
    stop_loss = Column(Numeric(20, 8))
    take_profit = Column(Numeric(20, 8))
    trailing_stop_activated = Column(Boolean, default=False)
    trailing_stop_price = Column(Numeric(20, 8))
    highest_price = Column(Numeric(20, 8))
    lowest_price = Column(Numeric(20, 8))
    unrealized_pnl = Column(Numeric(20, 8))
    unrealized_pnl_percent = Column(Numeric(10, 4))
    status = Column(String(20), default='OPEN')
    opened_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    closed_at = Column(DateTime(timezone=True))
    close_price = Column(Numeric(20, 8))
    realized_pnl = Column(Numeric(20, 8))
    realized_pnl_percent = Column(Numeric(10, 4))
    signal_id = Column(BigInteger)
    trade_id = Column(BigInteger)
    notes = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow)

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
        """Create all tables and ensure constraints"""
        Base.metadata.create_all(self.engine)
        
        # Ensure unique constraint on ohlcv_raw for ON CONFLICT to work
        with self.engine.connect() as conn:
            # Check if constraint exists
            result = conn.execute(text("""
                SELECT conname 
                FROM pg_constraint 
                WHERE conname = 'uix_ohlcv_unique' 
                AND conrelid = 'ohlcv_raw'::regclass;
            """)).fetchone()
            
            if not result:
                conn.execute(text("""
                    ALTER TABLE ohlcv_raw 
                    ADD CONSTRAINT uix_ohlcv_unique 
                    UNIQUE (exchange, symbol, timeframe, timestamp);
                """))
                conn.commit()
                self.logger.info("Added unique constraint uix_ohlcv_unique on ohlcv_raw")
            else:
                self.logger.debug("Constraint uix_ohlcv_unique already exists")
        
        self.logger.info("All database tables created")
    
    def drop_tables(self):
        """Drop all tables (use with caution!)"""
        Base.metadata.drop_all(self.engine)
        self.logger.warning("All database tables dropped")
    
    def test_connection(self) -> bool:
        """Test database connection"""
        try:
            from sqlalchemy import text
            with self.engine.connect() as conn:
                conn.execute(text('SELECT 1'))
            self.logger.info("Database connection successful")
            return True
        except Exception as e:
            self.logger.error(f"Database connection failed: {e}")
            return False
