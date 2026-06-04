#!/bin/bash
echo 'Glumov555' | sudo -S -u postgres psql -d cryptotrader -c "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'ohlcv_raw';"
