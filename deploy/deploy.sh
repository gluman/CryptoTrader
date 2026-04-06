#!/bin/bash
# ============================================
# CryptoTrader - Linux Deployment Script
# Tested on Linux Mint 21.x / Ubuntu 22.04
# ============================================

set -e  # Exit on error

# Configuration
APP_NAME="cryptotrader"
APP_DIR="/opt/${APP_NAME}"
APP_USER="${APP_NAME}"
APP_GROUP="${APP_NAME}"
PYTHON_VERSION="3.11"
REPO_URL="https://github.com/your-org/cryptotrader.git"  # Replace with actual repo

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Check root
if [[ $EUID -ne 0 ]]; then
   log_error "This script must be run as root"
   exit 1
fi

log_info "============================================"
log_info "CryptoTrader Deployment - Linux Mint"
log_info "============================================"

# ─── Step 1: System Dependencies ───
log_info "[1/8] Installing system dependencies..."

apt-get update
apt-get install -y \
    python${PYTHON_VERSION} \
    python${PYTHON_VERSION}-venv \
    python${PYTHON_VERSION}-dev \
    python3-pip \
    postgresql-client \
    git \
    curl \
    wget \
    build-essential \
    libpq-dev \
    jq

# ─── Step 2: Create User ───
log_info "[2/8] Creating system user..."

if id "${APP_USER}" &>/dev/null; then
    log_warn "User ${APP_USER} already exists"
else
    useradd --system --shell /bin/bash --home-dir "${APP_DIR}" --create-home "${APP_USER}"
    log_info "User ${APP_USER} created"
fi

# ─── Step 3: Create Directory Structure ───
log_info "[3/8] Creating directory structure..."

mkdir -p "${APP_DIR}"
mkdir -p "${APP_DIR}/logs"
mkdir -p "${APP_DIR}/config"
mkdir -p "${APP_DIR}/data/exports"
mkdir -p "${APP_DIR}/scripts"
mkdir -p "${APP_DIR}/deploy/systemd"

chown -R ${APP_USER}:${APP_GROUP} "${APP_DIR}"
chmod 750 "${APP_DIR}"
chmod 755 "${APP_DIR}/logs"

# ─── Step 4: Deploy Application ───
log_info "[4/8] Deploying application..."

# Option A: From git
if [[ -n "${REPO_URL}" && "${REPO_URL}" != *"your-org"* ]]; then
    if [[ -d "${APP_DIR}/.git" ]]; then
        log_info "Updating existing repository..."
        cd "${APP_DIR}"
        sudo -u ${APP_USER} git pull
    else
        log_info "Cloning repository..."
        sudo -u ${APP_USER} git clone "${REPO_URL}" "${APP_DIR}"
    fi
else
    # Option B: Copy from current directory (for local deploy)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(dirname "$(dirname "${SCRIPT_DIR}")")"
    
    log_info "Copying from ${PROJECT_ROOT} to ${APP_DIR}..."
    
    rsync -av --exclude='.git' \
              --exclude='.venv' \
              --exclude='__pycache__' \
              --exclude='*.pyc' \
              --exclude='logs/*.log' \
              --exclude='.env' \
              "${PROJECT_ROOT}/" "${APP_DIR}/"
    
    chown -R ${APP_USER}:${APP_GROUP} "${APP_DIR}"
fi

# ─── Step 5: Python Virtual Environment ───
log_info "[5/8] Setting up Python virtual environment..."

if [[ ! -d "${APP_DIR}/.venv" ]]; then
    sudo -u ${APP_USER} python${PYTHON_VERSION} -m venv "${APP_DIR}/.venv"
fi

sudo -u ${APP_USER} "${APP_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u ${APP_USER} "${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

log_info "Python packages installed"

# ─── Step 6: Environment Configuration ───
log_info "[6/8] Configuring environment..."

if [[ ! -f "${APP_DIR}/.env" ]]; then
    if [[ -f "${APP_DIR}/.env.example" ]]; then
        cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
        log_warn "Created .env from .env.example - YOU MUST EDIT IT!"
        log_warn "Run: sudo nano ${APP_DIR}/.env"
    else
        log_error ".env.example not found!"
        exit 1
    fi
fi

chmod 640 "${APP_DIR}/.env"
chown ${APP_USER}:${APP_GROUP} "${APP_DIR}/.env"

# ─── Step 7: Install systemd Services ───
log_info "[7/8] Installing systemd services..."

SYSTEMD_DIR="${APP_DIR}/deploy/systemd"

for service_file in "${SYSTEMD_DIR}"/*.service "${SYSTEMD_DIR}"/*.timer; do
    if [[ -f "${service_file}" ]]; then
        filename=$(basename "${service_file}")
        cp "${service_file}" "/etc/systemd/system/"
        log_info "Installed ${filename}"
    fi
done

systemctl daemon-reload

# Enable services
systemctl enable cryptotrader-api.service
systemctl enable cryptotrader-scheduler.service
# Individual timers (alternative to scheduler)
# systemctl enable cryptotrader-collect.timer
# systemctl enable cryptotrader-sentiment.timer
# systemctl enable cryptotrader-decide.timer
# systemctl enable cryptotrader-execute.timer

log_info "Services enabled"

# ─── Step 8: Start Services ───
log_info "[8/8] Starting services..."

systemctl start cryptotrader-api.service
sleep 3
systemctl start cryptotrader-scheduler.service

# ─── Status Check ───
log_info "============================================"
log_info "Deployment Complete!"
log_info "============================================"
echo ""
log_info "Service Status:"
systemctl status cryptotrader-api.service --no-pager -l || true
echo ""
systemctl status cryptotrader-scheduler.service --no-pager -l || true
echo ""

log_info "============================================"
log_info "Useful Commands:"
log_info "============================================"
echo ""
echo "  # View logs"
echo "  journalctl -u cryptotrader-api -f"
echo "  journalctl -u cryptotrader-scheduler -f"
echo ""
echo "  # Restart services"
echo "  systemctl restart cryptotrader-api"
echo "  systemctl restart cryptotrader-scheduler"
echo ""
echo "  # Check API health"
echo "  curl http://localhost:8000/health"
echo ""
echo "  # Edit configuration"
echo "  sudo nano ${APP_DIR}/.env"
echo "  sudo nano ${APP_DIR}/config/settings.yaml"
echo ""
echo "  # Manual run (single task)"
echo "  sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py --task collect"
echo ""
