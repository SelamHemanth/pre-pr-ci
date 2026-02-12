# Patch Pre-Check CI Tool Documentation

## Overview

The Patch Pre-Check CI Tool is an automated testing framework designed to validate Linux kernel patches across different distributions before submission. It streamlines patch validation by automating patch application, kernel builds, distribution-specific testing, and provides both command-line and web-based interfaces for enhanced usability.

## Features

- **Automated Patch Processing:** Generates patches from git commits and applies them sequentially
- **Incremental Build Testing:** Tests each patch individually to identify build-breaking changes early
- **Comprehensive Test Suite:** Configuration validation, multiple build configurations, distribution-specific testing
- **Smart Configuration:** Interactive wizard with sensible defaults
- **Password Management:** Secure storage of host and VM credentials for unattended testing
- **Clean Workflow:** Automatic git state management with rollback
- **VM Boot Verification:** Automated kernel installation, boot, and version verification on remote VMs
- **Web Interface:** Modern, responsive dashboard for monitoring and controlling all operations

## Supported Distributions

| Distribution | Target Kernel         | Kernel Versions     | Architectures  |
|--------------|-----------------------|---------------------|----------------|
| OpenAnolis   | cloud-kernel          | Multiple LTS        | x86_64         |
| openEuler    | Kernel                | Multiple LTS        | x86_64         |
| OpenCloud    | OpenCloudOS-Kernel    | Multiple LTS        | x86_64         |

## Installation & Setup

### Prerequisites

```bash
sudo yum install -y git make gcc flex bison elfutils-libelf-devel openssl-devel ncurses-devel bc rpm-build
```
OpenAnolis extra requirements
```bash
sudo yum install -y audit-libs-devel binutils-devel libbpf-devel libcap-ng-devel libnl3-devel newt-devel pciutils-devel xmlto yum-utils
```
openEuler extra requirements
```bash
sudo yum install -y python3
```

For VM boot testing:
```bash
sudo yum install -y sshpass
```

For Web Interface:
```bash
pip3 install Flask Flask-CORS Werkzeug --user
```

### Getting Started

#### Command Line Interface

```bash
git clone https://github.com/SelamHemanth/patch-precheck-ci.git
cd patch-precheck-ci

make config # Run config wizard
make build # Build/test patches
make test # Execute all tests
```

#### Web Interface

```bash
cd patch-precheck-ci/web
./start.sh
# Access at: http://your-server-ip:5000
```

### Configuration Steps

1. **Distribution Selection**

    ```
    ╔════════════════════════╗
    ║ Distribution Selection ║
    ╚════════════════════════╝
    Detected Distribution: anolis
    Available distributions:
      1) OpenAnolis
      2) openEuler
    Enter choice [1-2]:
    ```

2. **Distribution-Specific Configuration**

   **OpenAnolis:**
     - Linux source code path
     - Signed-off-by name/email
     - Anolis Bugzilla ID (ANBZ)
     - Number of patches to apply (from HEAD)
     - Build threads (default: CPU cores)

     **Test Options:**

     | Test                        | Description                  | Purpose                                    |
     |-----------------------------|------------------------------|--------------------------------------------|
     | check_dependency            | Verify required dependencies | Ensures all bug-fix commits are backported |
     | check_Kconfig               | Validate Kconfig settings    | Ensures config validity                    |
     | build_allyes_config         | Build with allyesconfig      | Compile w/ all enabled options             |
     | build_allno_config          | Build with allnoconfig       | Minimal kernel build                       |
     | build_anolis_defconfig      | Build with anolis_defconfig  | Production default config                  |
     | build_anolis_debug_defconfig| Build with debug config      | Enable debugging features                  |
     | anck_rpm_build              | Build ANCK RPM packages      | RPMs for installation                      |
     | check_kapi                  | Check KAPI compatibility     | ABI compatibility checks                   |
     | boot_kernel_rpm             | Automated VM boot test       | Install, boot, and verify kernel on VM     |

     Enable: individual (e.g. 1,3,5), all, or none.

   **Password Configuration** (when RPM build or boot test enabled)

   **Host Configuration:**
   - Host sudo password (for installing build dependencies)
   - Stored securely for unattended testing
   - Used for: package installation, yum-builddep

   **VM Configuration** (when boot test enabled):
   - VM IP address (bridge or local network)
   - VM root password (for SSH/SCP access)
   - Used for: RPM transfer, installation, reboot, verification

   **openEuler:**
     - Linux source code path
     - Signed-off-by name/email
     - Euler Bugzilla ID
     - Patch category (default: feature)
     - Number of patches to apply (from HEAD)
     - Build threads (default: CPU cores)

     **Test Options:**

     | Test              | Description                       | Purpose                                    |
     |-------------------|-----------------------------------|--------------------------------------------|
     | check_dependency  | Verify required dependencies      | Ensures all bug-fix commits are backported |
     | build_allmod      | Build with allmodconfig           | Compile with all modules enabled           |
     | check_patch       | Run checkpatch.pl validation      | Verify coding style and patch format       |
     | check_format      | Check code formatting             | Ensures code style consistency             |
     | rpm_build         | Build openEuler RPM packages      | RPMs for installation                      |
     | boot_kernel       | Boot test (requires remote setup) | Install, boot, and verify kernel on VM     |

     Enable: individual (e.g. 1,3,5), all, or none.

   **Password Configuration** (when RPM build or boot test enabled)

   **Host Configuration:**
   - Host sudo password (for installing build dependencies)
   - Stored securely for unattended testing
   - Used for: package installation, yum-builddep

   **VM Configuration** (when boot test enabled):
   - VM IP address (bridge or local network)
   - VM root password (for SSH/SCP access)
   - Used for: RPM transfer, installation, reboot, verification

## Make Targets

| Target             | Description                            |
|--------------------|----------------------------------------|
| config             | Interactive configuration wizard       |
| build              | Generate/apply patches & build         |
| test               | Run distribution-specific test suite   |
| list-tests         | List available tests for configured    |
| anolis-test=<name> | Run specific OpenAnolis test           |
| euler-test=<name>  | Run specific openEuler test            |
| clean              | Remove logs/build outputs              |
| reset              | Reset git to saved HEAD                |
| distclean          | Remove all artifacts/config            |
| update-tests       | Update test configuration only         |
| help               | Display usage info                     |

## Web Interface

### Overview

The web interface provides a modern, graphical dashboard for interacting with the Patch Pre-Check CI Tool without using the command line.

### Features

#### Dashboard
- **Real-time Status Display:** Shows current configuration, distribution, and system state
- **Configuration Badge:** Visual indicator of system readiness
- **Distribution Selector:** Quick switching between OpenAnolis and openEuler

#### Configuration Management
- **Interactive Form:** Multi-section wizard with validation
  - General: Kernel source, author info, bugzilla ID, patch settings
  - Build: Thread configuration
  - VM: Network and authentication settings
  - Host: Local authentication
- **View Configuration:** Display current settings with one click
- **Validation:** Client-side checks for required fields

#### Build Operations
- **Progress Tracking:** Real-time progress bars showing build status
- **Status Indicators:** Running, completed, and failed states
- **Elapsed Time:** Live tracking of operation duration
- **Log Access:** Immediate access to build logs via popup modal

#### Test Management
- **Test Grid:** Visual display of all available tests
- **Individual Execution:** Run any test with one click
- **Batch Execution:** "Run All Tests" option
- **Test Status Icons:**
  - 🔵 Spinning: Test in progress
  - ✅ Green: Test passed
  - ❌ Red: Test failed
- **Progress Tracking:** Per-test status and timing

#### Log Viewer
- **Popup Modal:** Full-screen terminal-style log display
- **Dark Theme:** Easy-to-read console output
- **Automatic Mapping:** Direct access to distribution-specific logs
- **Scrollable:** Handle large log files efficiently

#### Maintenance Operations
- **Clean:** Remove logs and build artifacts (with confirmation)
- **Reset:** Restore git repository state (double confirmation)
- **Safety Checks:** Prevent accidental data loss

### Architecture

#### Backend (Flask)
- **RESTful API:** JSON-based communication
- **Job Management:** Background task execution with threading
- **Log Mapping:** Automatic detection of distribution-specific log files
- **Configuration Storage:** Direct integration with .configure files
- **Make Integration:** Executes make commands in project root

#### Frontend (Vue.js)
- **Single Page Application:** No page reloads
- **Reactive Updates:** Auto-refresh every 2 seconds
- **Modern UI:** Purple gradient design with Font Awesome icons
- **Responsive:** Works on desktop and mobile devices
- **Modal System:** Popups for configuration, logs, and confirmations

### API Endpoints

| Endpoint                | Method | Description                    |
|-------------------------|--------|--------------------------------|
| `/api/status`           | GET    | System configuration status    |
| `/api/config/fields`    | GET    | Get form fields for distro     |
| `/api/config`           | GET    | Retrieve current configuration |
| `/api/config`           | POST   | Save configuration             |
| `/api/tests`            | GET    | List available tests           |
| `/api/build`            | POST   | Run build operation            |
| `/api/test/all`         | POST   | Run all tests                  |
| `/api/test/<name>`      | POST   | Run specific test              |
| `/api/clean`            | POST   | Clean artifacts                |
| `/api/reset`            | POST   | Reset git repository           |
| `/api/jobs`             | GET    | List all jobs                  |
| `/api/jobs/<id>`        | GET    | Get job details                |
| `/api/jobs/<id>/log`    | GET    | Get job log output             |

### Installation

```bash
# Create web directory structure
mkdir -p web/templates

# Copy files
cp server.py web/
cp start.sh web/
cp requirements.txt web/
cp index.html web/templates/

# Make launcher executable
chmod +x web/start.sh

# Install dependencies
pip3 install -r web/requirements.txt --user

# Start server
cd web
./start.sh
```

### Usage

1. **Start Server:**
   ```bash
   cd web
   ./start.sh
   ```

2. **Access Dashboard:**
   - Open browser: `http://server-ip:5000`

3. **Configure System:**
   - Select distribution (OpenAnolis or openEuler)
   - Click "Configure System"
   - Fill in all required fields
   - Save configuration

4. **Run Operations:**
   - Click "Run Build" to build patches
   - Select individual tests or "Run All Tests"
   - Monitor progress in real-time
   - View logs with one click

5. **Maintenance:**
   - Use "Clean" to remove temporary files
   - Use "Reset" to restore git state

### Security Considerations

#### Network Access
- Server binds to `0.0.0.0:5000` by default
- Configure firewall: `sudo firewall-cmd --add-port=5000/tcp --permanent`
- Consider using reverse proxy (Nginx) for HTTPS
- Restrict access to trusted networks

#### Authentication
- Currently no authentication implemented
- Suitable for isolated lab networks
- For production: implement API key or OAuth

#### Password Storage
- Passwords stored in `.configure` files
- Same security concerns as CLI interface
- Consider using SSH keys for VM access

### Troubleshooting

**Frontend Not Loading:**
```bash
# Check if index.html exists
ls -la web/templates/index.html

# Check server logs for path errors
```

**Logs Not Displaying:**
```bash
# Verify logs directory exists
ls -la logs/

# Run a test to generate logs
make anolis-test=check_kconfig

# Check log file was created
ls -la logs/check_Kconfig.log
```

**Cannot Access from Network:**
```bash
# Open firewall
sudo firewall-cmd --add-port=5000/tcp --permanent
sudo firewall-cmd --reload

# Check port is listening
sudo netstat -tulpn | grep 5000
```

**Module Not Found:**
```bash
# Install dependencies
pip3 install -r web/requirements.txt --user --break-system-packages
```

## Service Management

### Overview

The web interface can be deployed as a systemd service for production environments. This provides automatic startup, monitoring, crash recovery, and integration with system logging.

### Benefits

| Feature | Manual Mode | Service Mode |
|---------|-------------|--------------|
| Auto-start on boot | ❌ No | ✅ Yes |
| Auto-restart on crash | ❌ No | ✅ Yes (5s delay) |
| Background execution | ❌ No | ✅ Yes |
| Systemd logging | ❌ No | ✅ Yes |
| Survives terminal close | ❌ No | ✅ Yes |
| Best for | Development | Production |

### Installation

#### Prerequisites

- Systemd-based Linux distribution
- Root/sudo access
- Web interface files in place

#### Install Service

```bash
# Navigate to project root
cd /path/to/patch-precheck-ci

# Install systemd service
sudo ./service.sh install
```

**What it does:**
1. Creates systemd service file at `/etc/systemd/system/patch-precheck-ci-web.service`
2. Detects current user and configures service to run as that user
3. Sets working directory to project root
4. Configures auto-restart on failure
5. Enables service to start on boot
6. Integrates with systemd journal logging

**Output:**
```
╔══════════════════════════════════════════╗
║  Patch Pre-Check CI Web Service Manager  ║
╚══════════════════════════════════════════╝

ℹ Installing Patch Pre-Check CI Web Service...

✓ Service file created: /etc/systemd/system/patch-precheck-ci-web.service
✓ Systemd reloaded
✓ Service enabled (will start on boot)

✓ Service installed successfully!

ℹ Next steps:
  Start service:   sudo ./service.sh start
  Check status:    sudo ./service.sh status
  View logs:       sudo ./service.sh logs
```

### Basic Operations

#### Start Service

```bash
sudo ./service.sh start
```

Starts the web server in the background. The server will:
- Bind to `0.0.0.0:5000`
- Accept connections from all network interfaces
- Continue running after terminal closes
- Auto-restart if it crashes

#### Stop Service

```bash
sudo ./service.sh stop
```

Gracefully stops the web server. All active connections are closed.

#### Restart Service

```bash
sudo ./service.sh restart
```

Stops and starts the service. Useful after:
- Updating code (server.py, index.html)
- Changing configuration
- Installing dependencies

#### Check Status

```bash
sudo ./service.sh status
```

Shows detailed service information:
- Current state (running/stopped/failed)
- PID and resource usage (CPU, memory)
- Recent log entries
- Enabled/disabled state
- Uptime

**Example Output:**
```
╔══════════════════════════════════════════╗
║  Patch Pre-Check CI Web Service Manager  ║
╚══════════════════════════════════════════╝

● patch-precheck-ci-web.service - Patch Pre-Check CI Web Interface
     Loaded: loaded (/etc/systemd/system/patch-precheck-ci-web.service; enabled)
     Active: active (running) since Sun 2025-12-29 10:00:00 UTC; 2h 15min ago
   Main PID: 12345 (python3)
      Tasks: 3 (limit: 4915)
     Memory: 45.2M
        CPU: 1.234s
     CGroup: /system.slice/patch-precheck-ci-web.service
             └─12345 /usr/bin/python3 /path/to/patch-precheck-ci/web/server.py

════════════════════════════════════════════════

✓ Service is running
✓ Service is enabled (starts on boot)

ℹ Useful commands:
  View logs:    sudo ./service.sh logs
  Stop:         sudo ./service.sh stop
  Restart:      sudo ./service.sh restart
```

### Log Management

#### Follow Logs (Real-time)

```bash
sudo ./service.sh logs
```

Shows live log output. Press `Ctrl+C` to exit. Useful for:
- Monitoring requests
- Debugging issues
- Watching build/test progress

#### View Recent Logs

```bash
sudo ./service.sh logs-tail
```

Shows last 50 lines of logs. Quick check without following.

#### Advanced Log Queries

Using `journalctl` directly:

```bash
# Last 100 lines
sudo journalctl -u patch-precheck-ci-web -n 100

# All logs from today
sudo journalctl -u patch-precheck-ci-web --since today

# Last hour
sudo journalctl -u patch-precheck-ci-web --since "1 hour ago"

# Between specific times
sudo journalctl -u patch-precheck-ci-web --since "2025-12-29 10:00:00" --until "2025-12-29 12:00:00"

# Follow with filters
sudo journalctl -u patch-precheck-ci-web -f | grep ERROR

# Export to file
sudo journalctl -u patch-precheck-ci-web > service_logs.txt
```

### Uninstallation

```bash
sudo ./service.sh uninstall
```

Removes the service:
1. Stops the service if running
2. Disables auto-start on boot
3. Removes service file
4. Reloads systemd daemon

**Note:** Does not delete web interface files (server.py, index.html, etc.)

### Service Configuration

#### Service File Location

```
/etc/systemd/system/patch-precheck-ci-web.service
```

#### Default Configuration

```systemd
[Unit]
Description=Patch Pre-Check CI Web Interface
After=network.target

[Service]
Type=simple
User=amd
WorkingDirectory=/home/amd/patch-precheck-ci
ExecStart=/usr/bin/python3 /home/amd/patch-precheck-ci/web/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

Environment="PATH=/home/amd/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/home/amd/patch-precheck-ci"

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

#### Key Settings

| Setting | Value | Purpose |
|---------|-------|---------|
| `Type` | `simple` | Foreground process |
| `User` | Auto-detected | Run as your user |
| `WorkingDirectory` | Project root | Access logs, configs |
| `Restart` | `on-failure` | Auto-restart on crash |
| `RestartSec` | `5` | Wait 5s before restart |
| `StandardOutput` | `journal` | Log to systemd |
| `NoNewPrivileges` | `true` | Security hardening |
| `PrivateTmp` | `true` | Isolated /tmp |

### Troubleshooting

#### Service Won't Start

**Check Logs:**
```bash
sudo ./service.sh logs-tail
```

**Common Issues:**

1. **Port Already in Use (Most Common)**

   **Problem:**
   ```
   Address already in use
   Port 5000 is in use by another program
   ```

   **Solution:**
   ```bash
   # Find what's using port 5000
   sudo lsof -i :5000

   # Stop conflicting process
   sudo pkill -f "server.py"
   sudo pkill -f "start.sh"

   # Or kill specific PID
   sudo kill <PID>

   # Start service
   sudo ./service.sh start
   ```

2. **Missing Dependencies**

   **Problem:**
   ```
   ModuleNotFoundError: No module named 'flask'
   ```

   **Solution:**
   ```bash
   # Install dependencies
   pip3 install -r web/requirements.txt --user

   # Restart service
   sudo ./service.sh restart
   ```

3. **File Not Found**

   **Problem:**
   ```
   FileNotFoundError: [Errno 2] No such file or directory: 'web/server.py'
   ```

   **Solution:**
   ```bash
   # Verify files exist
   ls -la web/server.py web/templates/index.html

   # Reinstall if needed
   sudo ./service.sh uninstall
   # Fix file locations
   sudo ./service.sh install
   ```

4. **Permission Denied**

   **Problem:**
   ```
   PermissionError: [Errno 13] Permission denied
   ```

   **Solution:**
   ```bash
   # Fix ownership
   sudo chown -R $USER:$USER /path/to/patch-precheck-ci

   # Reinstall service
   sudo ./service.sh uninstall
   sudo ./service.sh install
   ```

#### Service Keeps Restarting

**Check Logs:**
```bash
sudo ./service.sh logs
```

**Common Causes:**
- Syntax error in server.py
- Missing Python dependencies
- Port conflict
- Configuration file errors

**Solution:**
1. Fix the underlying issue
2. Test manually: `cd web && ./start.sh`
3. Once working, restart service: `sudo ./service.sh restart`

#### Can't Access from Network

**Problem:** Service running but can't access from browser

**Solutions:**

1. **Check Firewall:**
   ```bash
   # Open port
   sudo firewall-cmd --add-port=5000/tcp --permanent
   sudo firewall-cmd --reload

   # Verify
   sudo firewall-cmd --list-ports
   ```

2. **Check Binding:**
   ```bash
   # Verify server is listening on all interfaces
   sudo netstat -tulpn | grep 5000
   # Should show: 0.0.0.0:5000 (not 127.0.0.1:5000)
   ```

3. **Check SELinux:**
   ```bash
   # Temporarily disable for testing
   sudo setenforce 0

   # If that fixes it, add permanent exception
   sudo semanage port -a -t http_port_t -p tcp 5000
   ```

### Advanced Usage

#### Using systemctl Directly

The service integrates with standard systemctl commands:

```bash
# Start/stop/restart
sudo systemctl start patch-precheck-ci-web
sudo systemctl stop patch-precheck-ci-web
sudo systemctl restart patch-precheck-ci-web

# Status
sudo systemctl status patch-precheck-ci-web

# Enable/disable auto-start
sudo systemctl enable patch-precheck-ci-web
sudo systemctl disable patch-precheck-ci-web

# Reload configuration
sudo systemctl daemon-reload

# View service file
sudo systemctl cat patch-precheck-ci-web
```

#### Monitoring with systemd

```bash
# Show all properties
systemctl show patch-precheck-ci-web

# Specific properties
systemctl show patch-precheck-ci-web -p ActiveState
systemctl show patch-precheck-ci-web -p MainPID
systemctl show patch-precheck-ci-web -p MemoryCurrent
```

#### Boot-time Behavior

```bash
# Check if enabled
systemctl is-enabled patch-precheck-ci-web

# Check boot timing
systemd-analyze blame | grep patch-precheck

# View startup order
systemd-analyze critical-chain patch-precheck-ci-web.service
```

### Deployment Workflows

#### Development Workflow

```bash
# Use manual mode for development
cd web
./start.sh

# See output directly in terminal
# Press Ctrl+C to stop
```

#### Testing Workflow

```bash
# Make changes to code
vim web/server.py
vim web/templates/index.html

# Test manually first
cd web
./start.sh
# Verify changes work
# Ctrl+C to stop

# Deploy to service
sudo ./service.sh restart
```

#### Production Workflow

```bash
# Initial deployment
sudo ./service.sh install
sudo ./service.sh start

# Updates
git pull
sudo ./service.sh restart

# Monitoring
sudo ./service.sh status
sudo ./service.sh logs-tail
```

#### Backup and Restore

```bash
# Backup service configuration
sudo cp /etc/systemd/system/patch-precheck-ci-web.service /path/to/backup/

# Restore
sudo cp /path/to/backup/patch-precheck-ci-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo ./service.sh start
```

### Performance Tuning

#### Resource Limits

Edit service file to add limits:

```bash
sudo systemctl edit patch-precheck-ci-web --full
```

Add under `[Service]`:
```systemd
# Limit memory to 512MB
MemoryLimit=512M

# Limit CPU usage to 50%
CPUQuota=50%

# Limit file descriptors
LimitNOFILE=4096
```

Reload and restart:
```bash
sudo systemctl daemon-reload
sudo ./service.sh restart
```

### Security Hardening

The service includes basic security settings:
- `NoNewPrivileges=true` - Prevents privilege escalation
- `PrivateTmp=true` - Isolated temporary directory
- Runs as non-root user

For additional hardening, edit service file:

```bash
sudo systemctl edit patch-precheck-ci-web --full
```

Add under `[Service]`:
```systemd
# Filesystem protection
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/path/to/patch-precheck-ci/logs

# Network restrictions (if only local access needed)
RestrictAddressFamilies=AF_INET AF_INET6

# System call filtering
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
```

### Migration Guide

#### From Manual to Service Mode

```bash
# 1. Stop manual server
pkill -f "server.py"

# 2. Install service
sudo ./service.sh install

# 3. Start service
sudo ./service.sh start

# 4. Verify
sudo ./service.sh status
```

#### From Service to Manual Mode

```bash
# 1. Stop service
sudo ./service.sh stop

# 2. Optionally disable auto-start
sudo systemctl disable patch-precheck-ci-web

# 3. Start manually
cd web
./start.sh
```

### Best Practices

1. **Choose One Mode:** Don't run manual and service simultaneously
2. **Test First:** Test changes in manual mode before deploying to service
3. **Monitor Logs:** Regularly check logs for errors: `sudo ./service.sh logs-tail`
4. **Plan Restarts:** Restart service during maintenance windows if users are active
5. **Backup Config:** Keep backup of service file after customization
6. **Use Firewall:** Always configure firewall when exposing to network
7. **Update Regularly:** Keep dependencies updated: `pip3 install -U -r web/requirements.txt`

## Security Considerations

### Password Storage
- Passwords are stored in plaintext in `.configure` file
- File permissions should be restricted: `chmod 600 anolis/.configure`
- **Do not commit `.configure` to version control**
- Consider using SSH keys for production environments

### SSH Configuration
- Currently uses `StrictHostKeyChecking=no` for automation
- For production, configure proper SSH key authentication
- Use known_hosts verification in secure environments

### Web Interface Security
- No authentication by default
- Suitable for trusted networks only
- Consider implementing authentication for production
- Use HTTPS with reverse proxy in production
- Restrict firewall access to known IPs

## Contributing

To add a new distribution:

- Create: `newdistro/` directory
- Add: `config.sh`, `build.sh`, `test.sh`, `Makefile`
- Update: main `Makefile` with new targets
- Implement boot testing if supported
- Thoroughly test all features
- Submit pull request

## License

GPL-3.0 licence

This tool is provided as-is for kernel development and testing purposes.

## Support

- [Repository](https://github.com/SelamHemanth/patch-precheck-ci)
- Issues via GitHub

---
