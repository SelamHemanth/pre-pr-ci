#!/bin/bash
# Launcher for Patch Pre-Check CI Web Server

WEB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$WEB_DIR")"

echo "╔════════════════════════════════════════════════╗"
echo "║   Starting Patch Pre-Check CI Web Server      ║"
echo "╚════════════════════════════════════════════════╝"
echo ""
echo "Web Directory: $WEB_DIR"
echo "Project Root: $PROJECT_ROOT"
echo ""

# Change to project root
cd "$PROJECT_ROOT"

# Start server
echo "Starting web server..."
echo "Access at: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Press Ctrl+C to stop"
echo ""

python3 "$WEB_DIR/server.py"
