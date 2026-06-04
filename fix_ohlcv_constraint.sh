#!/bin/bash
echo 'Glumov555' | sudo -S -u postgres psql -d cryptotrader -c "ALTER TABLE ohlcv_raw ADD CONSTRAINT ohlcv_raw_exchange_symbol_timeframe_timestamp_key UNIQUE (exchange, symbol, timeframe, timestamp);"
