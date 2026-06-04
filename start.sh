#!/bin/bash
# AI Dynamic Weather Query Service - Quick Start
# Usage: bash start.sh

cd "$(dirname "$0")"

echo "============================================================"
echo "  [W] AI Dynamic Weather Query Service"
echo "============================================================"

# 1. Load .env
if [ -f ".env" ]; then
    echo "[OK] Loading .env configuration..."
    set -a
    source .env
    set +a
    echo "[OK] Environment variables loaded from .env"
else
    echo "[WARN] .env file not found"
fi

echo ""
echo "  Local:  http://localhost:5000"
echo "  Tunnel: cpolar http 5000"
echo "============================================================"
echo ""
echo "Starting server..."
python test.py
