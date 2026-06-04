#!/bin/bash
sudo -u postgres psql -d cryptotrader << 'EOF'
ALTER TABLE selected_symbols ADD CONSTRAINT selected_symbols_exchange_symbol_key UNIQUE (exchange, symbol);
EOF
echo "Constraint added"
