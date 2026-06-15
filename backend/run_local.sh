#!/usr/bin/env bash
# Start the Anomaly Bio paper library locally.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r requirements.txt
echo "Library running at http://localhost:8000  (Ctrl+C to stop)"
uvicorn app:app --host 0.0.0.0 --port 8000
