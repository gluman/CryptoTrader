-- ============================================
-- CryptoTrader PostgreSQL Schema
-- ============================================

-- Create database and user (run as postgres superuser)
-- CREATE DATABASE cryptotrader;
-- CREATE USER cryptotrader WITH PASSWORD 'cryptotrader123';
-- GRANT ALL PRIVILEGES ON DATABASE cryptotrader TO cryptotrader;
-- ALTER DATABASE cryptotrader SET TIMEZONE TO 'UTC';

-- ============================================
-- Table: OHLCV Raw Data
-- ============================================
CREATE TABLE IF NOT EXISTS ohlcv_raw (
    id BIGSERIAL PRIMARY KEY,
    exchange VARCHAR(50) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open DECIMAL(20,8) NOT NULL,
    high DECIMAL(20,8) NOT NULL,
    low DECIMAL(20,8) NOT NULL,
    close DECIMAL(20,8) NOT NULL,
    volume DECIMAL(30,8) NOT NULL,
    quote_volume DECIMAL(30,8),
    trades_count INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(exchange, symbol, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_raw_exchange_symbol ON ohlcv_raw(exchange, symbol, timeframe, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ohlcv_raw_timestamp ON ohlcv_raw(timestamp DESC);

-- ============================================
-- Table: OHLCV Processed (with indicators)
-- ============================================
CREATE TABLE IF NOT EXISTS ohlcv_processed (
    id BIGSERIAL PRIMARY KEY,
    exchange VARCHAR(50) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open DECIMAL(20,8),
    high DECIMAL(20,8),
    low DECIMAL(20,8),
    close DECIMAL(20,8),
    volume DECIMAL(30,8),
    -- Moving Averages
    sma_20 DECIMAL(20,8),
    sma_50 DECIMAL(20,8),
    sma_200 DECIMAL(20,8),
    ema_12 DECIMAL(20,8),
    ema_26 DECIMAL(20,8),
    -- Oscillators
    rsi_14 DECIMAL(10,4),
    macd DECIMAL(20,8),
    macd_signal DECIMAL(20,8),
    macd_hist DECIMAL(20,8),
    -- Volatility
    atr_14 DECIMAL(20,8),
    bollinger_upper DECIMAL(20,8),
    bollinger_middle DECIMAL(20,8),
    bollinger_lower DECIMAL(20,8),
    -- CSS (Currency Slope Strength)
    css_value DECIMAL(10,4),
    css_prior DECIMAL(10,4),
    -- Volume
    volume_sma_20 DECIMAL(30,8),
    volume_ratio DECIMAL(10,4),
    -- Metadata
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(exchange, symbol, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_proc_symbol_tf ON ohlcv_processed(symbol, timeframe, timestamp DESC);

-- ============================================
-- Table: News Raw
-- ============================================
CREATE TABLE IF NOT EXISTS news_raw (
    id BIGSERIAL PRIMARY KEY,
    source VARCHAR(100) NOT NULL,
    title TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    summary TEXT,
    language VARCHAR(10) DEFAULT 'en',
    sentiment_score DECIMAL(3,2),
    sentiment_source VARCHAR(50),
    ragflow_document_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_news_published ON news_raw(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_news_source ON news_raw(source);
CREATE INDEX IF NOT EXISTS idx_news_sentiment ON news_raw(sentiment_score);

-- ============================================
-- Table: Trading Signals
-- ============================================
CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(50) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    signal_type VARCHAR(10) NOT NULL,        -- BUY, SELL, HOLD
    strength DECIMAL(5,4) NOT NULL,          -- 0.0 - 1.0
    css_value DECIMAL(10,4),
    rsi_14 DECIMAL(10,4),
    macd DECIMAL(20,8),
    atr_14 DECIMAL(20,8),
    price DECIMAL(20,8),
    sentiment_score DECIMAL(3,2),
    news_volume INTEGER DEFAULT 0,
    volume_24h DECIMAL(30,8),
    confidence DECIMAL(5,4),
    model_version VARCHAR(50),
    reasoning TEXT,
    status VARCHAR(20) DEFAULT 'PENDING',
    executed_at TIMESTAMPTZ,
    pnl_percent DECIMAL(10,4),
    pnl_absolute DECIMAL(20,8),
    ragflow_decision_id VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals(symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(signal_type);

-- ============================================
-- Table: LLM Decisions Log
-- ============================================
CREATE TABLE IF NOT EXISTS decisions (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT REFERENCES signals(id),
    timestamp TIMESTAMPTZ NOT NULL,
    market_data_json JSONB NOT NULL,
    sentiment_data_json JSONB,
    ragflow_context TEXT,
    news_context TEXT,
    llm_model VARCHAR(100) NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    latency_ms INTEGER,
    decision_json JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_signal ON decisions(signal_id);

-- ============================================
-- Table: Executed Trades
-- ============================================
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT REFERENCES signals(id),
    exchange VARCHAR(50) NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(10) NOT NULL,               -- BUY, SELL
    order_type VARCHAR(20) NOT NULL,          -- MARKET, LIMIT
    quantity DECIMAL(30,8) NOT NULL,
    price DECIMAL(20,8),
    fee DECIMAL(20,8),
    pnl_percent DECIMAL(10,4),
    pnl_absolute DECIMAL(20,8),
    order_id VARCHAR(255),
    order_link_id VARCHAR(255),
    stop_loss DECIMAL(20,8),
    take_profit DECIMAL(20,8),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_signal ON trades(signal_id);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at DESC);

-- ============================================
-- Table: Selected Symbols (dynamic)
-- ============================================
CREATE TABLE IF NOT EXISTS selected_symbols (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(50) NOT NULL,
    volume_24h DECIMAL(30,8),
    change_1h DECIMAL(10,4),
    spread_percent DECIMAL(10,4),
    selection_score DECIMAL(10,4),
    is_active BOOLEAN DEFAULT true,
    selected_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(exchange, symbol)
);

CREATE INDEX IF NOT EXISTS idx_selected_active ON selected_symbols(is_active, selected_at DESC);

-- ============================================
-- Table: Agent Logs
-- ============================================
CREATE TABLE IF NOT EXISTS agent_logs (
    id BIGSERIAL PRIMARY KEY,
    agent_name VARCHAR(50) NOT NULL,
    level VARCHAR(10) NOT NULL,
    message TEXT NOT NULL,
    data_json JSONB,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_logs_agent ON agent_logs(agent_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_logs_level ON agent_logs(level, timestamp DESC);

-- ============================================
-- Table: Export History
-- ============================================
CREATE TABLE IF NOT EXISTS export_history (
    id BIGSERIAL PRIMARY KEY,
    export_type VARCHAR(20) NOT NULL,         -- xlsx, csv
    file_path TEXT NOT NULL,
    records_count INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
