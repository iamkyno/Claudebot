CREATE DATABASE IF NOT EXISTS claudebot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE claudebot;

CREATE TABLE IF NOT EXISTS trades (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    strategy VARCHAR(50) NOT NULL,
    side ENUM('buy', 'sell') NOT NULL,
    entry_price DECIMAL(18, 8) NOT NULL,
    exit_price DECIMAL(18, 8),
    quantity DECIMAL(18, 8) NOT NULL,
    pnl DECIMAL(18, 8),
    pnl_pct DECIMAL(10, 6),
    fees DECIMAL(18, 8) DEFAULT 0,
    entry_time DATETIME NOT NULL,
    exit_time DATETIME,
    duration_minutes INT,
    stop_loss DECIMAL(18, 8),
    take_profit DECIMAL(18, 8),
    status ENUM('open', 'closed', 'stopped') DEFAULT 'open',
    ml_confidence DECIMAL(5, 4),
    signal_id BIGINT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_symbol (symbol),
    INDEX idx_strategy (strategy),
    INDEX idx_status (status),
    INDEX idx_entry_time (entry_time)
);

CREATE TABLE IF NOT EXISTS signals (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    strategy VARCHAR(50) NOT NULL,
    signal_type ENUM('buy', 'sell', 'hold') NOT NULL,
    confidence DECIMAL(5, 4),
    rsi DECIMAL(10, 6),
    macd DECIMAL(18, 8),
    macd_signal DECIMAL(18, 8),
    bb_upper DECIMAL(18, 8),
    bb_lower DECIMAL(18, 8),
    bb_position DECIMAL(10, 6),
    ema_9 DECIMAL(18, 8),
    ema_21 DECIMAL(18, 8),
    ema_50 DECIMAL(18, 8),
    atr DECIMAL(18, 8),
    volume_ratio DECIMAL(10, 6),
    price_change_1h DECIMAL(10, 6),
    price_change_4h DECIMAL(10, 6),
    price_change_24h DECIMAL(10, 6),
    funding_rate DECIMAL(10, 8),
    orderbook_imbalance DECIMAL(10, 6),
    outcome TINYINT COMMENT '1=profitable, -1=loss, 0=breakeven',
    actual_pnl_pct DECIMAL(10, 6),
    trade_id BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_symbol (symbol),
    INDEX idx_strategy (strategy),
    INDEX idx_outcome (outcome),
    INDEX idx_created_at (created_at)
);

CREATE TABLE IF NOT EXISTS ohlcv_cache (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    timeframe VARCHAR(5) NOT NULL,
    open_time DATETIME NOT NULL,
    open_price DECIMAL(18, 8) NOT NULL,
    high_price DECIMAL(18, 8) NOT NULL,
    low_price DECIMAL(18, 8) NOT NULL,
    close_price DECIMAL(18, 8) NOT NULL,
    volume DECIMAL(24, 8) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_symbol_tf_time (symbol, timeframe, open_time),
    INDEX idx_symbol_tf (symbol, timeframe)
);

CREATE TABLE IF NOT EXISTS ml_models (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    model_name VARCHAR(100) NOT NULL,
    version INT NOT NULL,
    accuracy DECIMAL(5, 4),
    precision_score DECIMAL(5, 4),
    recall_score DECIMAL(5, 4),
    f1_score DECIMAL(5, 4),
    training_samples INT,
    features_used TEXT,
    is_active TINYINT DEFAULT 0,
    model_path VARCHAR(255),
    trained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_model_name (model_name),
    INDEX idx_is_active (is_active)
);

CREATE TABLE IF NOT EXISTS bot_state (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    total_balance DECIMAL(18, 8),
    available_balance DECIMAL(18, 8),
    unrealized_pnl DECIMAL(18, 8),
    daily_pnl DECIMAL(18, 8),
    daily_pnl_pct DECIMAL(10, 6),
    total_trades INT DEFAULT 0,
    winning_trades INT DEFAULT 0,
    losing_trades INT DEFAULT 0,
    win_rate DECIMAL(5, 4),
    is_killed TINYINT DEFAULT 0,
    kill_reason VARCHAR(255),
    snapshot_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_snapshot_time (snapshot_time)
);

CREATE TABLE IF NOT EXISTS liquidation_events (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    side ENUM('long', 'short') NOT NULL,
    quantity DECIMAL(18, 8) NOT NULL,
    price DECIMAL(18, 8) NOT NULL,
    usd_value DECIMAL(18, 2) NOT NULL,
    event_time DATETIME NOT NULL,
    traded_on TINYINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_symbol (symbol),
    INDEX idx_event_time (event_time)
);
