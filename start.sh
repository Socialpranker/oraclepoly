#!/bin/bash

# Oracle Poly - Quick Start Script
# Usage: ./start.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}   ORACLE POLY - Starting Services     ${NC}"
echo -e "${BLUE}========================================${NC}"

# Kill existing processes
echo -e "${YELLOW}Stopping existing processes...${NC}"
pkill -f "uvicorn app.main" 2>/dev/null || true
pkill -f "oracle_worker" 2>/dev/null || true
pkill -f "vite" 2>/dev/null || true
sleep 2

# 1. Start Backend (FastAPI)
echo -e "${GREEN}[1/3] Starting Backend (FastAPI)...${NC}"
source .venv/bin/activate
python -m uvicorn app.main:app --reload --port 8001 &
BACKEND_PID=$!
sleep 3

# 2. Start Oracle Worker (Redis queue consumer)
echo -e "${GREEN}[2/3] Starting Oracle Worker...${NC}"
python scripts/oracle_worker.py &
WORKER_PID=$!
sleep 3

# 3. Start Frontend (Vite)
echo -e "${GREEN}[3/3] Starting Frontend (Vite)...${NC}"
cd client
npm run dev &
FRONTEND_PID=$!
cd ..
sleep 3

echo ""
echo -e "${BLUE}========================================${NC}"
echo -e "${GREEN}All services started!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "  Frontend:  ${GREEN}http://localhost:5173${NC}"
echo -e "  Backend:   ${GREEN}http://localhost:8001${NC}"
echo -e "  Worker:    ${GREEN}oracle_worker (Redis queue)${NC}"
echo ""

echo -e "${YELLOW}Press Ctrl+C to stop all services${NC}"
echo ""

# Handle shutdown
cleanup() {
    echo ""
    echo -e "${YELLOW}Stopping all services...${NC}"
    kill $BACKEND_PID 2>/dev/null || true
    kill $WORKER_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    pkill -f "uvicorn app.main" 2>/dev/null || true
    pkill -f "oracle_worker" 2>/dev/null || true
    pkill -f "vite" 2>/dev/null || true
    echo -e "${GREEN}All services stopped.${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

# Wait for all processes
wait
