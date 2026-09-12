#!/usr/bin/env bash
# Convenience script: runs the API (which also serves the dashboard at
# http://localhost:8000) for local dev.
#
# NOTE: no --reload on purpose. api/state.py keeps the estate, the graph
# and every case file in memory, so a reload triggered by saving a file
# wipes a running demo. Use --reload only while editing code, never
# while demoing.
set -e

uvicorn api.main:app --port 8000 &
API_PID=$!
trap "kill $API_PID" EXIT

echo "Dashboard:  http://localhost:8000/"
echo "API docs:   http://localhost:8000/docs"

# The Streamlit UI is the older frontend and is optional -- pass
# --streamlit to start it too.
if [ "$1" = "--streamlit" ]; then
  streamlit run ui/app.py
else
  wait $API_PID
fi
