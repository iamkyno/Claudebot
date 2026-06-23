-- Schema for Claudebot
-- Works with plain PostgreSQL 14+ or PostgreSQL + TimescaleDB
-- TimescaleDB: uncomment the CREATE EXTENSION and create_hypertable lines below
-- Run: psql -U postgres -d claudebot -f schema/schema.sql

-- CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS trades (
    id               BIGSERIAL PRIMARY KEY,
    symbol           VARCHAR(20)  NOT NULL,
    strategy         VARCHAR(50)  NOT NULL,
    side             VARCHAR(4)   NOT NULL CHECK (side IN ('buy', 'sell')),
    entry_price      NUMERIC(18, 8) NOT NULL,
    exit_price       NUMERIC(18, 8),
    quantity         NUMERIC(18, 8) NOT NULL,
    pnl              NUMERIC(18, 8),
    pnl_pct          NUMERIC(10, 6),
    fees             NUMERIC(18, 8) DEFAULT 0,
    entry_time       TIMESTAMP    NOT NULL,
    exit_time        TIMESTAMP,
    duration_minutes INT,
    stop_loss        NUMERIC(18, 8),
    take_profit      NUMERIC(18, 8),
    status           VARCHAR(8)   NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'closed', 'stopped')),
    ml_confidence    NUMERIC(5, 4),
    signal_id        BIGINT,
    notes            TEXT,
    created_at       TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol   ON trades (symbol);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades (strategy);
CREATE INDEX IF NOT EXISTS idx_trades_status   ON trades (status);
CREATE INDEX IF NOT EXISTS idx_trades_entry    ON trades (entry_time);

-- -----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS signals (
    id                   BIGSERIAL PRIMARY KEY,
    symbol               VARCHAR(20) NOT NULL,
    strategy             VARCHAR(50) NOT NULL,
    signal_type          VARCHAR(4)  NOT NULL CHECK (signal_type IN ('buy', 'sell', 'hold')),
    confidence           NUMERIC(5, 4),
    rsi                  NUMERIC(10, 6),
    macd                 NUMERIC(18, 8),
    macd_signal          NUMERIC(18, 8),
    bb_upper             NUMERIC(18, 8),
    bb_lower             NUMERIC(18, 8),
    bb_position          NUMERIC(10, 6),
    ema_9                NUMERIC(18, 8),
    ema_21               NUMERIC(18, 8),
    ema_50               NUMERIC(18, 8),
    atr                  NUMERIC(18, 8),
    volume_ratio         NUMERIC(10, 6),
    price_change_1h      NUMERIC(10, 6),
    price_change_4h      NUMERIC(10, 6),
    price_change_24h     NUMERIC(10, 6),
    funding_rate         NUMERIC(10, 8),
    orderbook_imbalance  NUMERIC(10, 6),
    tv_recommendation    NUMERIC(5, 4),   -- TradingView consensus score (-1..1)
    outcome              SMALLINT,    -- 1=profitable, -1=loss, 0=breakeven
    actual_pnl_pct       NUMERIC(10, 6),
    trade_id             BIGINT,
    created_at           TIMESTAMP   DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol     ON signals (symbol);
CREATE INDEX IF NOT EXISTS idx_signals_strategy   ON signals (strategy);
CREATE INDEX IF NOT EXISTS idx_signals_outcome    ON signals (outcome);
CREATE INDEX IF NOT EXISTS idx_signals_created    ON signals (created_at);

-- -----------------------------------------------------------------------
-- Hypertable: partitioned by time for fast range queries

CREATE TABLE IF NOT EXISTS ohlcv_cache (
    id          BIGSERIAL,
    symbol      VARCHAR(20)   NOT NULL,
    timeframe   VARCHAR(5)    NOT NULL,
    open_time   TIMESTAMP     NOT NULL,
    open_price  NUMERIC(18, 8) NOT NULL,
    high_price  NUMERIC(18, 8) NOT NULL,
    low_price   NUMERIC(18, 8) NOT NULL,
    close_price NUMERIC(18, 8) NOT NULL,
    volume      NUMERIC(24, 8) NOT NULL,
    created_at  TIMESTAMP     DEFAULT NOW(),
    UNIQUE (symbol, timeframe, open_time)
);
-- SELECT create_hypertable('ohlcv_cache', 'open_time', if_not_exists => TRUE);  -- uncomment if TimescaleDB installed
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_tf ON ohlcv_cache (symbol, timeframe, open_time DESC);

-- -----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ml_models (
    id               BIGSERIAL PRIMARY KEY,
    model_name       VARCHAR(100) NOT NULL,
    version          INT          NOT NULL,
    accuracy         NUMERIC(5, 4),
    precision_score  NUMERIC(5, 4),
    recall_score     NUMERIC(5, 4),
    f1_score         NUMERIC(5, 4),
    training_samples INT,
    features_used    TEXT,
    is_active        SMALLINT     DEFAULT 0,
    model_path       VARCHAR(255),
    trained_at       TIMESTAMP    DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ml_models_name   ON ml_models (model_name);
CREATE INDEX IF NOT EXISTS idx_ml_models_active ON ml_models (is_active);

-- -----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bot_state (
    id                BIGSERIAL,
    total_balance     NUMERIC(18, 8),
    available_balance NUMERIC(18, 8),
    unrealized_pnl    NUMERIC(18, 8),
    daily_pnl         NUMERIC(18, 8),
    daily_pnl_pct     NUMERIC(10, 6),
    total_trades      INT          DEFAULT 0,
    winning_trades    INT          DEFAULT 0,
    losing_trades     INT          DEFAULT 0,
    win_rate          NUMERIC(5, 4),
    is_killed         SMALLINT     DEFAULT 0,
    kill_reason       VARCHAR(255),
    snapshot_time     TIMESTAMP    DEFAULT NOW()
);
-- SELECT create_hypertable('bot_state', 'snapshot_time', if_not_exists => TRUE);  -- uncomment if TimescaleDB installed

-- -----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS liquidation_events (
    id          BIGSERIAL,
    symbol      VARCHAR(20)   NOT NULL,
    side        VARCHAR(5)    NOT NULL CHECK (side IN ('long', 'short')),
    quantity    NUMERIC(18, 8) NOT NULL,
    price       NUMERIC(18, 8) NOT NULL,
    usd_value   NUMERIC(18, 2) NOT NULL,
    event_time  TIMESTAMP     NOT NULL,
    traded_on   SMALLINT      DEFAULT 0,
    created_at  TIMESTAMP     DEFAULT NOW()
);
-- SELECT create_hypertable('liquidation_events', 'event_time', if_not_exists => TRUE);  -- uncomment if TimescaleDB installed
CREATE INDEX IF NOT EXISTS idx_liq_symbol ON liquidation_events (symbol, event_time DESC);
