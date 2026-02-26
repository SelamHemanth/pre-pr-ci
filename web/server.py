#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/server.py
# Flask web server — REST API backend for the Pre-PR CI web interface
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""
Web Server for Pre-PR CI
Place this file in: pre-pr-ci/web/server.py
"""

import os
import pty
import shutil
import stat
import subprocess
import sys
import threading
import uuid
import signal
import json
import re
import select
import termios
import struct
import fcntl
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# flask-sock provides a simple WebSocket API
try:
    from flask_sock import Sock
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False
    print("[WARN] flask-sock not installed. Terminal WebSocket support disabled.")
    print("[WARN] Install with: pip install flask-sock")

app = Flask(__name__)
CORS(app)

if HAS_WEBSOCKET:
    sock = Sock(app)

# ── Project paths ────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

print(f"[INFO] Server running from: {SCRIPT_DIR}")
print(f"[INFO] Project root: {PROJECT_ROOT}")

LOGS_DIR = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

TORVALDS_REPO = os.path.join(PROJECT_ROOT, '.torvalds-linux')
JOBS_FILE     = os.path.join(LOGS_DIR, '.jobs.json')

job_lock      = threading.Lock()
job_processes = {}   # pid handles — never persisted


# ════════════════════════════════════════════════════════════════════════════
#  PERSISTENT TERMINAL SESSION
# ════════════════════════════════════════════════════════════════════════════

class TerminalSession:
    """
    Holds a single persistent PTY/shell session.
    Multiple WebSocket connections can attach/detach freely; the underlying
    shell process keeps running across page refreshes.
    """

    def __init__(self):
        self.pid       = None      # shell PID
        self.master_fd = None      # master side of the PTY
        self._lock     = threading.Lock()
        self._clients  = []        # list of (ws, thread) tuples currently connected
        self._reader   = None      # background thread that reads PTY output
        self._alive    = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Spawn the shell in a PTY if not already running."""
        with self._lock:
            if self._alive and self._check_alive():
                return  # already running

            shell = os.environ.get('SHELL', '/bin/bash')
            cols, rows = 220, 50

            # Fork a new PTY + shell
            self.pid, self.master_fd = pty.fork()

            if self.pid == 0:
                # ── child: set up environment and exec shell ──────────────
                os.environ['TERM']     = 'xterm-256color'
                os.environ['COLUMNS']  = str(cols)
                os.environ['LINES']    = str(rows)
                os.chdir(PROJECT_ROOT)
                os.execvp(shell, [shell, '--login'])
                sys.exit(1)

            # ── parent: set initial terminal size ────────────────────────
            self._set_winsize(self.master_fd, rows, cols)
            self._alive = True

            # Start the background reader thread
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()

            print(f"[terminal] Shell started (pid={self.pid}, fd={self.master_fd})")

    def _check_alive(self):
        """Return True if the shell process is still running."""
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def _set_winsize(self, fd, rows, cols):
        """Push a TIOCSWINSZ ioctl to resize the PTY."""
        try:
            winsize = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    # ── I/O ───────────────────────────────────────────────────────────────

    def _read_loop(self):
        """
        Continuously read PTY output and broadcast to all connected clients.
        Runs in a daemon thread for the lifetime of the shell process.
        """
        while True:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.05)
                if r:
                    data = os.read(self.master_fd, 4096)
                    if data:
                        self._broadcast(data)
            except OSError:
                # PTY closed — shell exited
                self._alive = False
                print("[terminal] Shell process ended.")
                break
            except Exception as exc:
                print(f"[terminal] read_loop error: {exc}")
                break

    def _broadcast(self, data: bytes):
        """Send raw PTY bytes to every connected WebSocket client."""
        dead = []
        with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                ws.send(data)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                self._clients = [c for c in self._clients if c not in dead]

    def write(self, data: bytes):
        """Write keyboard input from a client into the PTY master."""
        if self.master_fd is not None:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int):
        """Resize the PTY (called when the xterm.js viewport changes)."""
        if self.master_fd is not None:
            self._set_winsize(self.master_fd, rows, cols)

    def attach(self, ws):
        """Register a new WebSocket client."""
        with self._lock:
            self._clients.append(ws)
        print(f"[terminal] Client attached (total={len(self._clients)})")

    def detach(self, ws):
        """Unregister a WebSocket client."""
        with self._lock:
            self._clients = [c for c in self._clients if c is not ws]
        print(f"[terminal] Client detached (total={len(self._clients)})")


# Global singleton terminal session — persists for the server lifetime
terminal_session = TerminalSession()


if HAS_WEBSOCKET:
    @sock.route('/ws/terminal')
    def terminal_ws(ws):
        """
        WebSocket endpoint for the xterm.js client.

        Protocol (JSON messages from client → server):
            {"type": "input",  "data": "<base64-or-raw text>"}
            {"type": "resize", "rows": N, "cols": N}

        Server → client: raw PTY bytes (binary or text frames).
        """
        # Ensure the shell is running
        terminal_session.start()
        terminal_session.attach(ws)

        try:
            while True:
                message = ws.receive()
                if message is None:
                    break  # client disconnected

                if isinstance(message, str):
                    try:
                        msg = json.loads(message)
                        if msg.get('type') == 'input':
                            terminal_session.write(msg['data'].encode('utf-8', errors='replace'))
                        elif msg.get('type') == 'resize':
                            rows = int(msg.get('rows', 24))
                            cols = int(msg.get('cols', 80))
                            terminal_session.resize(rows, cols)
                    except (json.JSONDecodeError, KeyError):
                        # Treat as raw input
                        terminal_session.write(message.encode('utf-8', errors='replace'))
                elif isinstance(message, bytes):
                    terminal_session.write(message)

        except Exception as exc:
            print(f"[terminal] WebSocket error: {exc}")
        finally:
            terminal_session.detach(ws)


@app.route('/api/terminal/status')
def terminal_status():
    """Return whether the terminal shell is currently alive."""
    alive = terminal_session._alive and terminal_session._check_alive()
    return jsonify({'alive': alive, 'pid': terminal_session.pid})


# ════════════════════════════════════════════════════════════════════════════
#  JOB MANAGEMENT  (unchanged from original)
# ════════════════════════════════════════════════════════════════════════════

def _load_jobs():
    try:
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_jobs(jobs_dict):
    try:
        with open(JOBS_FILE, 'w') as f:
            json.dump(jobs_dict, f, indent=2)
    except Exception as exc:
        print(f"[WARN] Could not persist jobs: {exc}")

jobs = _load_jobs()

_changed = False
for _jid, _job in jobs.items():
    if _job.get('status') in ('running', 'queued'):
        _job['status']   = 'failed'
        _job['error']    = 'Server restarted while job was in progress'
        _job['end_time'] = datetime.now().isoformat()
        _changed = True
if _changed:
    _save_jobs(jobs)


def clean_ansi_codes(text):
    text = re.sub(r'\x1b\[[0-9;]*[mGKHfJ]', '', text)
    text = re.sub(r'\x1b\[[\d;]*[A-Za-z]', '', text)
    text = re.sub(r'\x1b\].*?\x07', '', text)
    text = re.sub(r'\x1b[@-_][0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'\[\d+(?:;\d+)*m', '', text)
    return text


def _get_dir_owner_uid(path):
    try:
        return os.stat(path).st_uid
    except Exception:
        return None


def _remove_torvalds_repo(repo_path):
    uid = _get_dir_owner_uid(repo_path)
    is_root_owned = (uid == 0)
    if is_root_owned:
        print(f"[INFO] '{repo_path}' is owned by root. Sudo privileges are required to remove it.")
        try:
            result = subprocess.run(['sudo', 'rm', '-rf', repo_path],
                                    stdin=sys.stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode != 0:
                print(f"[ERROR] 'sudo rm -rf' failed: {result.stderr.decode(errors='replace').strip()}")
                return False
            print(f"[INFO] '{repo_path}' removed successfully (via sudo).")
            return True
        except Exception as exc:
            print(f"[ERROR] Exception while removing with sudo: {exc}")
            return False
    else:
        try:
            shutil.rmtree(repo_path)
            print(f"[INFO] '{repo_path}' removed successfully.")
            return True
        except Exception as exc:
            print(f"[ERROR] Failed to remove '{repo_path}': {exc}")
            return False


def clone_torvalds_repo_silent():
    CLONE_URL = 'https://github.com/torvalds/linux.git'

    def _clone():
        print(f"[INFO] Cloning Torvalds Linux repo into '{TORVALDS_REPO}' ...")
        result = subprocess.run(['git', 'clone', '--bare', CLONE_URL, TORVALDS_REPO],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0:
            print("[WARN] Clone failed (possible network issue). Continuing without it.")
        else:
            print("[INFO] Clone completed successfully.")

    if not os.path.exists(TORVALDS_REPO):
        _clone()
        return

    print(f"[INFO] Torvalds repo found at '{TORVALDS_REPO}'. Fetching latest tags ...")
    fetch_result = subprocess.run(['git', 'fetch', '--all', '--tags'],
                                  cwd=TORVALDS_REPO, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, check=False)
    if fetch_result.returncode == 0:
        print("[INFO] Fetch completed successfully.")
        return

    err_msg = fetch_result.stderr.decode(errors='replace').strip()
    print(f"[WARN] 'git fetch --all --tags' failed: {err_msg}")
    print("[INFO] Removing stale repo and re-cloning ...")
    if _remove_torvalds_repo(TORVALDS_REPO):
        _clone()
    else:
        print("[ERROR] Could not remove stale repo. Skipping re-clone.")


def _sync_torvalds_repo_to_log(log_file):
    def _log(msg):
        log_file.write(msg + '\n')
        log_file.flush()

    CLONE_URL = 'https://github.com/torvalds/linux.git'

    def _clone():
        _log(f"[repo-sync] Cloning Torvalds Linux repo into '{TORVALDS_REPO}' ...")
        result = subprocess.run(['git', 'clone', '--bare', CLONE_URL, TORVALDS_REPO],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, check=False)
        for line in (result.stdout or '').splitlines():
            _log(f"[repo-sync] {line}")
        if result.returncode != 0:
            _log("[repo-sync] WARNING: Clone failed. Continuing anyway.")
        else:
            _log("[repo-sync] Clone completed successfully.")

    try:
        if not os.path.exists(TORVALDS_REPO):
            _clone()
            return

        _log("[repo-sync] Torvalds repo found. Fetching latest tags ...")
        fetch_result = subprocess.run(['git', 'fetch', '--all', '--tags'],
                                      cwd=TORVALDS_REPO, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, universal_newlines=True, check=False)
        for line in (fetch_result.stdout or '').splitlines():
            _log(f"[repo-sync] {line}")

        if fetch_result.returncode == 0:
            _log("[repo-sync] Fetch completed successfully.")
            return

        _log(f"[repo-sync] WARNING: fetch failed (exit {fetch_result.returncode}).")
        _log("[repo-sync] Removing stale repo and re-cloning ...")

        uid = _get_dir_owner_uid(TORVALDS_REPO)
        if uid == 0:
            _log("[repo-sync] Directory is root-owned — using sudo rm -rf ...")
            rm_result = subprocess.run(['sudo', 'rm', '-rf', TORVALDS_REPO],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       universal_newlines=True, check=False)
            if rm_result.returncode != 0:
                _log("[repo-sync] ERROR: sudo rm failed. Skipping re-clone.")
                return
            _log("[repo-sync] Removed successfully (via sudo).")
        else:
            try:
                shutil.rmtree(TORVALDS_REPO)
                _log("[repo-sync] Removed successfully.")
            except Exception as exc:
                _log(f"[repo-sync] ERROR: {exc}. Skipping re-clone.")
                return

        _clone()
    except Exception as exc:
        _log(f"[repo-sync] Unexpected error: {exc}. Skipping repo sync.")


def get_distro_config():
    config_file = os.path.join(PROJECT_ROOT, '.distro_config')
    if not os.path.exists(config_file):
        return None
    config = {}
    with open(config_file, 'r') as f:
        for line in f:
            line = line.strip()
            if '=' in line:
                key, value = line.split('=', 1)
                config[key] = value
    return config


def get_test_log_file(test_name, distro):
    if distro == 'anolis':
        log_map = {
            'check_dependency':       'check_dependency.log',
            'check_kconfig':          'check_Kconfig.log',
            'build_allyes_config':    'build_allyes_config.log',
            'build_allno_config':     'build_allno_config.log',
            'build_anolis_defconfig': 'build_anolis_defconfig.log',
            'build_anolis_debug':     'build_anolis_debug_defconfig.log',
            'anck_rpm_build':         'anck_rpm_build.log',
            'check_kapi':             'kapi_test.log',
            'boot_kernel_rpm':        'boot_kernel_rpm.log',
        }
    else:
        log_map = {
            'check_dependency': 'check_dependency.log',
            'build_allmod':     'build_allmod.log',
            'check_kabi':       'check_kabi.log',
            'check_patch':      'check_patch.log',
            'check_format':     'check_format.log',
            'rpm_build':        'rpm_build.log',
            'boot_kernel':      'boot_kernel.log',
        }
    return os.path.join(LOGS_DIR, log_map.get(test_name, f'{test_name}.log'))


def resolve_live_log_file(job_id, command):
    config = get_distro_config()
    distro = config.get('DISTRO') if config else None

    test_name = None
    if distro and 'anolis-test=' in command:
        test_name = command.split('anolis-test=')[1].strip()
    elif distro and 'euler-test=' in command:
        test_name = command.split('euler-test=')[1].strip()

    if test_name and distro:
        return get_test_log_file(test_name, distro)

    return os.path.join(LOGS_DIR, f'{job_id}_command.log')


def run_make_command(command, job_id):
    live_log = resolve_live_log_file(job_id, command)

    with job_lock:
        jobs[job_id]['status']        = 'running'
        jobs[job_id]['start_time']    = datetime.now().isoformat()
        jobs[job_id]['live_log_file'] = live_log
        _save_jobs(jobs)

    try:
        with open(live_log, 'w') as lf:
            is_check_dep = ('anolis-test=check_dependency' in command or
                            'euler-test=check_dependency'  in command)
            if command == 'make build' or is_check_dep:
                _sync_torvalds_repo_to_log(lf)

            process = subprocess.Popen(
                command, shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                cwd=PROJECT_ROOT, universal_newlines=True,
                bufsize=1, preexec_fn=os.setsid
            )

            with job_lock:
                job_processes[job_id] = process

            for line in process.stdout:
                lf.write(line)
                lf.flush()

            process.wait()
            exit_code = process.returncode
            lf.write(f'\n--- process exited with code {exit_code} ---\n')
            lf.flush()

            with job_lock:
                job_processes.pop(job_id, None)
                if exit_code in (-9, -15):
                    jobs[job_id]['status'] = 'killed'
                else:
                    jobs[job_id]['status'] = 'completed' if exit_code == 0 else 'failed'
                jobs[job_id]['exit_code'] = exit_code
                jobs[job_id]['end_time']  = datetime.now().isoformat()
                jobs[job_id]['log_file']  = live_log
                _save_jobs(jobs)

    except Exception as e:
        with job_lock:
            jobs[job_id]['status']   = 'failed'
            jobs[job_id]['error']    = str(e)
            jobs[job_id]['end_time'] = datetime.now().isoformat()
            jobs[job_id]['log_file'] = live_log
            job_processes.pop(job_id, None)
            _save_jobs(jobs)


# ════════════════════════════════════════════════════════════════════════════
#  HTTP ROUTES  (all unchanged from original)
# ════════════════════════════════════════════════════════════════════════════

@app.route('/static/<path:filename>')
def static_files(filename):
    return send_from_directory(os.path.join(SCRIPT_DIR, 'static'), filename)


@app.route('/')
def index():
    html_file = os.path.join(SCRIPT_DIR, 'templates', 'index.html')
    if os.path.exists(html_file):
        with open(html_file, 'r') as f:
            return f.read()
    return jsonify({'error': 'Frontend not found', 'path': html_file}), 404


@app.route('/api/status')
def status():
    config = get_distro_config()
    return jsonify({
        'configured': config is not None,
        'distro':     config.get('DISTRO') if config else None,
        'workspace':  PROJECT_ROOT,
        'timestamp':  datetime.now().isoformat()
    })


@app.route('/api/config/fields')
def get_config_fields():
    distro = request.args.get('distro')
    if not distro or distro not in ['anolis', 'euler']:
        return jsonify({'error': 'Invalid distribution'}), 400

    if distro == 'anolis':
        fields = {
            'general': [
                {'name': 'LINUX_SRC_PATH', 'label': 'Linux source path',   'type': 'text',     'required': True},
                {'name': 'SIGNER_NAME',    'label': 'Signed-off-by name',  'type': 'text',     'required': True},
                {'name': 'SIGNER_EMAIL',   'label': 'Signed-off-by email', 'type': 'email',    'required': True},
                {'name': 'ANBZ_ID',        'label': 'Anolis Bugzilla ID',  'type': 'text',     'required': True},
                {'name': 'NUM_PATCHES',    'label': 'Number of patches',   'type': 'number',   'required': True, 'default': 10},
            ],
            'build': [
                {'name': 'BUILD_THREADS',  'label': 'Build threads',       'type': 'number',   'required': True, 'default': 256},
            ],
            'vm': [
                {'name': 'VM_IP',          'label': 'VM IP address',       'type': 'text',     'required': True},
                {'name': 'VM_ROOT_PWD',    'label': 'VM root password',    'type': 'password', 'required': True},
            ],
            'host': [
                {'name': 'HOST_USER_PWD',  'label': 'Host sudo password',  'type': 'password', 'required': True},
            ],
        }
    else:
        fields = {
            'general': [
                {'name': 'LINUX_SRC_PATH',  'label': 'Linux source path',   'type': 'text',     'required': True},
                {'name': 'SIGNER_NAME',     'label': 'Signed-off-by name',  'type': 'text',     'required': True},
                {'name': 'SIGNER_EMAIL',    'label': 'Signed-off-by email', 'type': 'email',    'required': True},
                {'name': 'BUGZILLA_ID',     'label': 'Bugzilla ID',         'type': 'text',     'required': True},
                {'name': 'PATCH_CATEGORY',  'label': 'Patch category',      'type': 'select',   'required': True,
                 'options': ['feature', 'bugfix', 'performance', 'security'], 'default': 'bugfix'},
                {'name': 'NUM_PATCHES',     'label': 'Number of patches',   'type': 'number',   'required': True, 'default': 5},
            ],
            'build': [
                {'name': 'BUILD_THREADS',   'label': 'Build threads',       'type': 'number',   'required': True, 'default': 256},
            ],
            'vm': [
                {'name': 'VM_IP',           'label': 'VM IP address',       'type': 'text',     'required': True},
                {'name': 'VM_ROOT_PWD',     'label': 'VM root password',    'type': 'password', 'required': True},
            ],
            'host': [
                {'name': 'HOST_USER_PWD',   'label': 'Host sudo password',  'type': 'password', 'required': True},
            ],
        }

    return jsonify({'distro': distro, 'fields': fields})


@app.route('/api/config', methods=['GET'])
def get_current_config():
    config = get_distro_config()
    if not config:
        return jsonify({'error': 'Not configured'}), 404

    distro = config.get('DISTRO')
    config_file = os.path.join(PROJECT_ROOT, distro, '.configure')
    if not os.path.exists(config_file):
        return jsonify({'error': 'Config file not found'}), 404

    detailed = {}
    with open(config_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                detailed[key] = value.strip('"').strip("'")
    return jsonify(detailed)


@app.route('/api/config', methods=['POST'])
def set_config():
    data        = request.json
    distro      = data.get('distro')
    config_data = data.get('config', {})
    test_flags  = data.get('test_flags', {})

    if not distro or distro not in ['anolis', 'euler']:
        return jsonify({'error': 'Invalid distribution'}), 400

    def flag(key):
        return test_flags.get(key, 'yes')

    try:
        with open(os.path.join(PROJECT_ROOT, '.distro_config'), 'w') as f:
            f.write(f'DISTRO={distro}\n')
            f.write(f'DISTRO_DIR={distro}\n')

        config_file = os.path.join(PROJECT_ROOT, distro, '.configure')
        with open(config_file, 'w') as f:
            f.write(f'# {distro} Configuration\n')
            f.write(f'# Generated: {datetime.now().strftime("%c")}\n\n')
            f.write('# General\n')
            f.write(f'LINUX_SRC_PATH="{config_data.get("LINUX_SRC_PATH", "")}"\n')
            f.write(f'SIGNER_NAME="{config_data.get("SIGNER_NAME", "")}"\n')
            f.write(f'SIGNER_EMAIL="{config_data.get("SIGNER_EMAIL", "")}"\n')

            if distro == 'anolis':
                f.write(f'ANBZ_ID="{config_data.get("ANBZ_ID", "")}"\n')
            else:
                f.write(f'BUGZILLA_ID="{config_data.get("BUGZILLA_ID", "")}"\n')
                f.write(f'PATCH_CATEGORY="{config_data.get("PATCH_CATEGORY", "bugfix")}"\n')

            f.write(f'NUM_PATCHES="{config_data.get("NUM_PATCHES", "10")}"\n\n')
            f.write('# Build\n')
            f.write(f'BUILD_THREADS="{config_data.get("BUILD_THREADS", "512")}"\n\n')
            f.write('# Test Configuration\n')
            f.write('RUN_TESTS="yes"\n')

            test_keys = (
                ['CHECK_DEPENDENCY', 'CHECK_KCONFIG', 'BUILD_ALLYES', 'BUILD_ALLNO',
                 'BUILD_DEFCONFIG', 'BUILD_DEBUG', 'RPM_BUILD', 'CHECK_KAPI', 'BOOT_KERNEL']
                if distro == 'anolis' else
                ['CHECK_DEPENDENCY', 'BUILD_ALLMOD', 'CHECK_KABI',
                 'CHECK_PATCH', 'CHECK_FORMAT', 'RPM_BUILD', 'BOOT_KERNEL']
            )
            for key in test_keys:
                f.write(f'TEST_{key}="{flag(f"TEST_{key}")}"\n')

            f.write('\n# Host\n')
            f.write(f'HOST_USER_PWD=\'{config_data.get("HOST_USER_PWD", "")}\'\n\n')
            f.write('# VM\n')
            f.write(f'VM_IP="{config_data.get("VM_IP", "")}"\n')
            f.write(f'VM_ROOT_PWD=\'{config_data.get("VM_ROOT_PWD", "")}\'\n')
            f.write('\n# Repository\n')
            f.write(f'TORVALDS_REPO="{os.path.join(PROJECT_ROOT, ".torvalds-linux")}"\n')

        return jsonify({'message': 'Configuration saved', 'distro': distro})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/tests')
def list_tests():
    config = get_distro_config()
    if not config:
        return jsonify({'error': 'Not configured'}), 404

    distro = config.get('DISTRO')
    tests = {
        'anolis': [
            {'name': 'check_dependency',       'description': 'Check patch dependencies'},
            {'name': 'check_kconfig',          'description': 'Validate kernel configuration'},
            {'name': 'build_allyes_config',    'description': 'Build with allyesconfig'},
            {'name': 'build_allno_config',     'description': 'Build with allnoconfig'},
            {'name': 'build_anolis_defconfig', 'description': 'Build with anolis_defconfig'},
            {'name': 'build_anolis_debug',     'description': 'Build with anolis-debug_defconfig'},
            {'name': 'anck_rpm_build',         'description': 'Build ANCK RPM packages'},
            {'name': 'check_kapi',             'description': 'Check kernel ABI compatibility'},
            {'name': 'boot_kernel_rpm',        'description': 'Boot VM with built kernel RPM'},
        ],
        'euler': [
            {'name': 'check_dependency', 'description': 'Check patch dependencies'},
            {'name': 'build_allmod',     'description': 'Build with allmodconfig'},
            {'name': 'check_kabi',       'description': 'Check KABI whitelist against Module.symvers'},
            {'name': 'check_patch',      'description': 'Run checkpatch.pl validation'},
            {'name': 'check_format',     'description': 'Check code formatting'},
            {'name': 'rpm_build',        'description': 'Build openEuler RPM packages'},
            {'name': 'boot_kernel',      'description': 'Boot test (requires remote setup)'},
        ],
    }
    return jsonify({'distro': distro, 'tests': tests.get(distro, [])})


@app.route('/api/build', methods=['POST'])
def build():
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id, 'command': 'make build',
            'status': 'queued', 'created_time': datetime.now().isoformat()
        }
        _save_jobs(jobs)
    threading.Thread(target=run_make_command, args=('make build', job_id), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/test/all', methods=['POST'])
def test_all():
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id, 'command': 'make test',
            'status': 'queued', 'created_time': datetime.now().isoformat()
        }
        _save_jobs(jobs)
    threading.Thread(target=run_make_command, args=('make test', job_id), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/test/<test_name>', methods=['POST'])
def test_specific(test_name):
    config = get_distro_config()
    if not config:
        return jsonify({'error': 'Not configured'}), 400
    distro  = config.get('DISTRO')
    command = f'make {distro}-test={test_name}'
    job_id  = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id, 'command': command, 'test_name': test_name,
            'status': 'queued', 'created_time': datetime.now().isoformat()
        }
        _save_jobs(jobs)
    threading.Thread(target=run_make_command, args=(command, job_id), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/clean', methods=['POST'])
def clean():
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id, 'command': 'make clean',
            'status': 'queued', 'created_time': datetime.now().isoformat()
        }
        _save_jobs(jobs)
    threading.Thread(target=run_make_command, args=('make clean', job_id), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/reset', methods=['POST'])
def reset():
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id, 'command': 'make reset',
            'status': 'queued', 'created_time': datetime.now().isoformat()
        }
        _save_jobs(jobs)
    threading.Thread(target=run_make_command, args=('make reset', job_id), daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/jobs/<job_id>/kill', methods=['POST'])
def kill_job(job_id):
    with job_lock:
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404
        if jobs[job_id]['status'] != 'running':
            return jsonify({'error': 'Job is not running'}), 400
        if job_id not in job_processes:
            return jsonify({'error': 'Process not found'}), 404
        process = job_processes[job_id]

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        with job_lock:
            jobs[job_id]['status']   = 'killed'
            jobs[job_id]['end_time'] = datetime.now().isoformat()
            job_processes.pop(job_id, None)
            _save_jobs(jobs)
        return jsonify({'message': 'Job killed successfully', 'job_id': job_id})
    except Exception as e:
        return jsonify({'error': f'Failed to kill job: {str(e)}'}), 500


@app.route('/api/jobs')
def get_jobs():
    with job_lock:
        return jsonify(list(jobs.values()))


@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    with job_lock:
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404
        return jsonify(jobs[job_id])


@app.route('/api/jobs/<job_id>/log')
def get_job_log(job_id):
    with job_lock:
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404
        job = dict(jobs[job_id])

    log_path = job.get('live_log_file') or job.get('log_file')

    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, 'r', errors='replace') as f:
                content = f.read()
            return jsonify({'log': clean_ansi_codes(content)})
        except Exception as e:
            return jsonify({'error': f'Read error: {str(e)}'}), 500

    if job.get('status') == 'queued':
        return jsonify({'log': 'Job is queued, waiting to start...'})
    if job.get('status') == 'running':
        return jsonify({'log': 'Process is starting...'})

    return jsonify({'error': 'No log available'}), 404


# ════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n╔══════════════════════════╗")
    print("║   Pre-PR CI Web Server   ║")
    print("╚══════════════════════════╝\n")
    print(f"Access at: http://$(hostname -I | awk '{{print $1}}'):5000\n")
    if not HAS_WEBSOCKET:
        print("[WARN] Terminal WebSocket disabled. Run: pip install flask-sock\n")
    # NOTE: debug=True uses the Werkzeug reloader which forks the process;
    # use debug=False (or --no-reload) to keep the terminal PTY stable.
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
