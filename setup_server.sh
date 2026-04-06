#!/bin/bash
# Server-side setup script for CryptoTrader

# Activate venv
source ~/cryptotrader-venv/bin/activate

# Create project directory
mkdir -p ~/CryptoTrader

# Navigate to project dir
cd ~/CryptoTrader

echo "Ready to receive files via scp/rsync"
echo "Project directory: ~/CryptoTrader"
echo "Virtual env: ~/cryptotrader-venv"