#!/usr/bin/env bash
# Convenience script: runs the API and UI together for local dev.
set -e
uvicorn api.main:app --reload --port 8000 &
API_PID=$!
trap "kill $API_PID" EXIT
sleep 2
streamlit run ui/app.py
