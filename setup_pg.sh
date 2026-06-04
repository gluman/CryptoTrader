#!/bin/bash
# Setup PostgreSQL for CryptoTrader

echo "Creating user cryptotrader..."
sudo -u postgres psql -c "CREATE USER cryptotrader WITH PASSWORD 'cryptotrader123';" 2>&1 || echo "User may already exist"

echo "Creating database cryptotrader..."
sudo -u postgres psql -c "CREATE DATABASE cryptotrader OWNER cryptotrader;" 2>&1 || echo "DB may already exist"

echo "Setting timezone to UTC..."
sudo -u postgres psql -c "ALTER DATABASE cryptotrader SET TIMEZONE TO 'UTC';" 2>&1

echo "Granting privileges..."
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE cryptotrader TO cryptotrader;" 2>&1

echo "Granting schema access..."
sudo -u postgres psql -d cryptotrader -c "GRANT ALL ON SCHEMA public TO cryptotrader;" 2>&1

echo "Configuring remote access..."
PG_HBA="/etc/postgresql/16/main/pg_hba.conf"
PG_CONF="/etc/postgresql/16/main/postgresql.conf"

# Allow password auth from local network
if ! grep -q "192.168.0.0/16" "$PG_HBA"; then
    echo "host    all             all             192.168.0.0/16            md5" >> "$PG_HBA"
    echo "Added pg_hba rule for 192.168.0.0/16"
fi

# Listen on all interfaces
sudo sed -i "s/#listen_addresses = 'localhost'/listen_addresses = '*'/g" "$PG_CONF"
echo "Set listen_addresses to *"

echo "Restarting PostgreSQL..."
echo "$PASSWORD" | sudo -S systemctl restart postgresql

echo "Done! PostgreSQL is ready."
echo "Connection: 192.168.0.149:5432, user=cryptotrader, db=cryptotrader"
