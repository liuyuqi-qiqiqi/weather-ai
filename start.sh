#!/bin/bash
# AI Dynamic Weather Query Service - Quick Start
# Usage: bash start.sh

cd "$(dirname "$0")"

echo "============================================================"
echo "  [W] AI Dynamic Weather Query Service"
echo "  Local: http://localhost:5000"
echo "  Tunnel: use cpolar to map localhost:5000"
echo "============================================================"

if [ -z "$DASHSCOPE_API_KEY" ]; then
    echo "[WARN] DASHSCOPE_API_KEY not set - Qwen AI will be unavailable"
fi
if [ -z "$HEFENG_API_KEY" ]; then
    echo "[WARN] HEFENG_API_KEY not set - Weather API will fallback"
fi

echo ""
echo "Starting server..."
python test.py
