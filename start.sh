#!/usr/bin/env bash
# Run this script to set up and start the DSR Slack Bot

set -e

# Create virtualenv if it doesn't exist
if [ ! -d "venv" ]; then
  echo "📦 Creating virtual environment..."
  python3 -m venv venv
fi

echo "📦 Installing / updating dependencies..."
venv/bin/pip install -r requirements.txt --quiet

echo ""
echo "✅ Dependencies ready."
echo ""

if [ ! -f .env ]; then
  echo "⚠️  No .env file found. Copying from .env.example..."
  cp .env.example .env
  echo "👉 Please edit .env and fill in your credentials, then run this script again."
  exit 1
fi

echo "🚀 Starting DSR Slack Bot (FastAPI / uvicorn)..."
echo "📡 Slack events endpoint: POST http://localhost:3000/slack/events"
echo "📖 API docs:              http://localhost:3000/docs"
echo ""
venv/bin/uvicorn main:app --host 0.0.0.0 --port 3000 --reload
