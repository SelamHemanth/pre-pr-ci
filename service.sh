#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - service.sh
# Systemd service helper — install/start/stop the Pre-PR CI web server as a system service
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"
WEB_DIR="$PROJECT_ROOT/web"
TORVALDS_REPO="${PROJECT_ROOT}/.torvalds-linux"
SERVICE_NAME="pre-pr-ci-web"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_header() {
    echo ""
    echo -e "${BLUE}╔═════════════════════════════════╗${NC}"
    echo -e "${BLUE}║  Pre-PR CI Web Service Manager  ║${NC}"
    echo -e "${BLUE}╚═════════════════════════════════╝${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

print_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        print_error "This operation requires root privileges"
        echo "Please run: sudo $0 $1"
        exit 1
    fi
}

configure_firewall() {
    print_info "Configuring firewall..."

    # Check if firewall-cmd is available
    if ! command -v firewall-cmd &> /dev/null; then
        print_warning "firewall-cmd not found, skipping"
        return 0
    fi

    # Check if port 5000 is open
    if firewall-cmd --list-ports | grep -q "5000/tcp"; then
        print_success "Port 5000 is already open in firewall"
        return 0
    fi

    # Open port 5000
    if firewall-cmd --add-port=5000/tcp --permanent; then
        firewall-cmd --reload
        print_success "Port 5000 opened in firewall"
    else
        print_warning "Could not open port automatically"
    fi
}

install_dependencies() {
    print_info "Checking Python dependencies..."

    # Install dependencies as the actual user
    print_info "Installing Python dependencies for user $ACTUAL_USER..."

    # Try normal install
    if su - "$ACTUAL_USER" -c "pip3 install flask flask_cors werkzeug flask-sock --user"; then
        print_success "Dependencies installed successfully"
    else
        # Try with --break-system-packages
        if su - "$ACTUAL_USER" -c "pip3 install flask flask_cors werkzeug --user --break-system-packages"; then
            print_success "Dependencies installed successfully"
        else
            print_error "Failed to install dependencies"
            exit 1
        fi
    fi
}

detect_user() {
    # Detect the actual user (not root when using sudo)
    if [ -n "$SUDO_USER" ]; then
        ACTUAL_USER="$SUDO_USER"
    else
        ACTUAL_USER="$(whoami)"
    fi
    
    # Get user's home directory
    USER_HOME=$(getent passwd "$ACTUAL_USER" | cut -d: -f6)
}

check_web_files() {
    # Check if web directory and files exist
    if [ ! -d "$WEB_DIR" ]; then
        print_error "Web directory not found: $WEB_DIR"
        echo "Please ensure the web interface is set up correctly"
        exit 1
    fi
    
    if [ ! -f "$WEB_DIR/server.py" ]; then
        print_error "server.py not found in $WEB_DIR"
        echo "Please ensure the web interface is properly set up"
        exit 1
    fi
}

create_service_file() {
    check_root "install"
    detect_user
    check_web_files
    
    print_info "Creating systemd service file..."
    
    # Find Python path
    PYTHON_PATH=$(which python3)
    
    # Create service file
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Pre-PR CI Web Interface
After=network.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PYTHON_PATH $WEB_DIR/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

# Environment
Environment="PATH=$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=$PROJECT_ROOT"

# Security
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    print_success "Service file created: $SERVICE_FILE"
    echo ""
    print_info "Configuration:"
    echo "  Service Name: $SERVICE_NAME"
    echo "  User:         $ACTUAL_USER"
    echo "  Project Root: $PROJECT_ROOT"
    echo "  Web Server:   $WEB_DIR/server.py"
    echo "  Python:       $PYTHON_PATH"
}

clone_torvalds() {
	# Clone Torvalds repo if not exists
	if [ ! -d "$TORVALDS_REPO" ]; then
		echo -e "${BLUE}Cloning Torvalds Linux repository...${NC}"
		git clone --bare https://github.com/torvalds/linux.git "$TORVALDS_REPO" 2>&1 | \
			stdbuf -oL tr '\r' '\n' | \
			grep -oP '\d+(?=%)' | \
			awk '{printf "\rProgress: %d%%", $1; fflush()}' || \
		git config --global --add safe.directory $TORVALDS_REPO
		echo -e "\r${GREEN}Repository cloned successfully${NC}"
		echo ""
	else
		echo -e "${GREEN}Torvalds repository already exists${NC}"
		echo -e "${BLUE}Updating repository...${NC}"
		(cd "$TORVALDS_REPO" && git fetch --all --tags 2>&1 | grep -v "^From" || true)
		echo -e "${GREEN}Repository updated${NC}"
	fi
}

install_service() {
    check_root "install"
    
    print_header
    print_info "Installing Pre-PR CI Web Service..."
    echo ""
    
    # Check files
    check_web_files

    # Configure firewall
    configure_firewall

    # Install dependencies
    detect_user
    install_dependencies
    
    # Create service file
    create_service_file
    
    # Reload systemd
    print_info "Reloading systemd daemon..."
    systemctl daemon-reload
    print_success "Systemd reloaded"
    
    # Enable service
    print_info "Enabling service..."
    systemctl enable "$SERVICE_NAME"
    print_success "Service enabled (will start on boot)"
    
    echo ""
    print_success "Service installed successfully!"
    echo ""
    print_info "Next steps:"
    echo "  Start service:   sudo ./service.sh start"
    echo "  Check status:    sudo ./service.sh status"
    echo "  View logs:       sudo ./service.sh logs"
    echo ""
}

uninstall_service() {
    check_root "uninstall"
    
    print_header
    print_warning "Uninstalling Pre-PR CI Web Service..."
    echo ""
    
    # Stop service if running
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        print_info "Stopping service..."
        systemctl stop "$SERVICE_NAME"
        print_success "Service stopped"
    fi
    
    # Disable service
    if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        print_info "Disabling service..."
        systemctl disable "$SERVICE_NAME"
        print_success "Service disabled"
    fi
    
    # Remove service file
    if [ -f "$SERVICE_FILE" ]; then
        print_info "Removing service file..."
        rm -f "$SERVICE_FILE"
        print_success "Service file removed"
    fi
    
    # Reload systemd
    print_info "Reloading systemd daemon..."
    systemctl daemon-reload
    print_success "Systemd reloaded"
    
    echo ""
    print_success "Service uninstalled successfully!"
    echo ""
}

start_service() {
    check_root "start"
    
    print_header
    print_info "Starting Pre-PR CI Web Service..."
    echo ""
    
    if ! systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
        print_error "Service is not installed"
        echo "Please run: sudo ./service.sh install"
        exit 1
    fi

    # clone torvalds linux repo
    clone_torvalds

    systemctl start "$SERVICE_NAME"
    
    # Wait a moment for service to start
    sleep 2
    
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        print_success "Service started successfully!"
        echo ""
        print_info "Access the web interface at:"
        echo "  http://$(hostname -I | awk '{print $1}'):5000"
        echo ""
        print_info "Useful commands:"
        echo "  Check status: sudo ./service.sh status"
        echo "  View logs:    sudo ./service.sh logs"
        echo "  Stop service: sudo ./service.sh stop"
    else
        print_error "Failed to start service"
        echo ""
        print_info "Check logs for details:"
        echo "  sudo journalctl -u $SERVICE_NAME -n 50"
        exit 1
    fi
    echo ""
}

stop_service() {
    check_root "stop"
    
    print_header
    print_info "Stopping Pre-PR CI Web Service..."
    echo ""
    
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        print_warning "Service is not running"
        exit 0
    fi
    
    systemctl stop "$SERVICE_NAME"
    
    # Wait a moment
    sleep 1
    
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        print_success "Service stopped successfully!"
    else
        print_error "Failed to stop service"
        exit 1
    fi
    echo ""
}

restart_service() {
    check_root "restart"
    
    print_header
    print_info "Restarting Pre-PR CI Web Service..."
    echo ""
    
    systemctl restart "$SERVICE_NAME"
    
    # Wait a moment
    sleep 2
    
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        print_success "Service restarted successfully!"
        echo ""
        print_info "Access the web interface at:"
        echo "  http://$(hostname -I | awk '{print $1}'):5000"
    else
        print_error "Failed to restart service"
        echo ""
        print_info "Check logs for details:"
        echo "  sudo journalctl -u $SERVICE_NAME -n 50"
        exit 1
    fi
    echo ""
}

status_service() {
    print_header
    
    # Check if service file exists
    if [ ! -f "$SERVICE_FILE" ]; then
        print_warning "Service is not installed"
        echo ""
        print_info "To install: sudo ./service.sh install"
        exit 0
    fi
    
    # Show detailed status
    systemctl status "$SERVICE_NAME" --no-pager
    
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════${NC}"
    echo ""
    
    # Show service state
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        print_success "Service is running"
    else
        print_warning "Service is not running"
    fi
    
    if systemctl is-enabled --quiet "$SERVICE_NAME"; then
        print_success "Service is enabled (starts on boot)"
    else
        print_warning "Service is disabled"
    fi
    
    echo ""
    print_info "Useful commands:"
    echo "  View logs:    sudo ./service.sh logs"
    echo "  Start:        sudo ./service.sh start"
    echo "  Stop:         sudo ./service.sh stop"
    echo "  Restart:      sudo ./service.sh restart"
    echo ""
}

show_logs() {
    print_header
    print_info "Showing service logs (press Ctrl+C to exit)..."
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════${NC}"
    echo ""
    
    # Follow logs
    journalctl -u "$SERVICE_NAME" -f
}

show_logs_tail() {
    print_header
    print_info "Last 50 lines of service logs:"
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════${NC}"
    echo ""
    
    journalctl -u "$SERVICE_NAME" -n 50 --no-pager
    
    echo ""
    echo -e "${BLUE}════════════════════════════════════════════════${NC}"
    echo ""
    print_info "To follow logs in real-time: sudo ./service.sh logs"
    echo ""
}

show_help() {
    print_header
    
    cat << EOF
Usage: $0 <command>

Commands:
  install      Install and enable the systemd service
  uninstall    Stop, disable, and remove the service
  start        Start the web service
  stop         Stop the web service
  restart      Restart the web service
  status       Show service status
  logs         Follow service logs (real-time)
  logs-tail    Show last 50 lines of logs
  help         Show this help message

EOF
}

# Main logic
case "${1:-}" in
    install)
        install_service
        ;;
    uninstall)
        uninstall_service
        ;;
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        restart_service
        ;;
    status)
        status_service
        ;;
    logs)
        show_logs
        ;;
    logs-tail)
        show_logs_tail
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        print_error "Unknown command: ${1:-<none>}"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Available commands:"
        echo "  install, uninstall, start, stop, restart, status, logs, logs-tail, help"
        echo ""
        echo "Run '$0 help' for detailed usage information"
        exit 1
        ;;
esac
