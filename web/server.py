#!/usr/bin/env python3
"""
Web Server for Patch Pre-Check CI Tool
Place this file in: patch-precheck-ci/web/server.py
"""

import os
import subprocess
import threading
import uuid
import signal
import re
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Auto-detect project root (parent of web directory)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

print(f"[INFO] Server running from: {SCRIPT_DIR}")
print(f"[INFO] Project root: {PROJECT_ROOT}")

LOGS_DIR = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(LOGS_DIR, exist_ok=True)

TORVALDS_REPO = os.path.join(PROJECT_ROOT, '.torvalds-linux')

jobs = {}
job_lock = threading.Lock()
job_processes = {}


def clean_ansi_codes(text):
    """Remove ANSI color codes and escape sequences from text."""
    text = re.sub(r'\x1b\[[0-9;]*[mGKHfJ]', '', text)
    text = re.sub(r'\x1b\[[\d;]*[A-Za-z]', '', text)
    text = re.sub(r'\x1b\].*?\x07', '', text)
    text = re.sub(r'\x1b[@-_][0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'\[\d+(?:;\d+)*m', '', text)
    return text


def clone_torvalds_repo_silent():
    try:
        if not os.path.exists(TORVALDS_REPO):
            subprocess.run(
                ['git', 'clone', '--bare',
                 'https://github.com/torvalds/linux.git', TORVALDS_REPO],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            )
    except Exception:
        pass


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
    """Map a test name to its expected log file path inside LOGS_DIR."""
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
    """
    Determine the log file path that will be written during the job.

    For individual tests the Makefile writes to a named file in LOGS_DIR.
    For build / test-all / clean / reset we capture all stdout ourselves
    into <job_id>_command.log in the same directory.

    This is called BEFORE the process starts so the path can be stored on
    the job record immediately, allowing the /log endpoint to serve live
    content while the process is still running.
    """
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
    """Run a make command in a background thread, writing output live to disk."""

    # Resolve log path before the process starts so the /log endpoint
    # can serve it immediately while the job is running.
    live_log = resolve_live_log_file(job_id, command)

    with job_lock:
        jobs[job_id]['status']        = 'running'
        jobs[job_id]['start_time']    = datetime.now().isoformat()
        jobs[job_id]['live_log_file'] = live_log   # key fix: available while running

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT,
            universal_newlines=True,
            bufsize=1,
            preexec_fn=os.setsid
        )

        with job_lock:
            job_processes[job_id] = process

        # Write every line to the log file immediately (line-buffered).
        with open(live_log, 'w') as lf:
            for line in process.stdout:
                lf.write(line)
                lf.flush()

        process.wait()
        exit_code = process.returncode

        with job_lock:
            job_processes.pop(job_id, None)

        with job_lock:
            if exit_code in (-9, -15):
                jobs[job_id]['status'] = 'killed'
            else:
                jobs[job_id]['status'] = 'completed' if exit_code == 0 else 'failed'
            jobs[job_id]['exit_code'] = exit_code
            jobs[job_id]['end_time']  = datetime.now().isoformat()
            jobs[job_id]['log_file']  = live_log

    except Exception as e:
        with job_lock:
            jobs[job_id]['status']   = 'failed'
            jobs[job_id]['error']    = str(e)
            jobs[job_id]['end_time'] = datetime.now().isoformat()
            job_processes.pop(job_id, None)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
    """Save configuration including per-test enable/disable flags."""
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
    clone_torvalds_repo_silent()
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id, 'command': 'make build',
            'status': 'queued', 'created_time': datetime.now().isoformat()
        }
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
    """
    Serve job log whether queued, running, or finished.

    While running  → reads live_log_file which is flushed line-by-line.
    After finish   → reads the same file (stored as log_file too).
    While queued   → returns a placeholder message.
    """
    with job_lock:
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404
        job = dict(jobs[job_id])  # snapshot; release lock fast

    # Prefer live_log_file (set at start), fall back to log_file (set at end)
    log_path = job.get('live_log_file') or job.get('log_file')

    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, 'r', errors='replace') as f:
                content = f.read()
            return jsonify({'log': clean_ansi_codes(content)})
        except Exception as e:
            return jsonify({'error': f'Read error: {str(e)}'}), 500

    # Graceful placeholders so the frontend always gets a 200
    if job.get('status') == 'queued':
        return jsonify({'log': 'Job is queued, waiting to start...'})

    if job.get('status') == 'running':
        return jsonify({'log': 'Process is starting...'})

    return jsonify({'error': 'No log available'}), 404


if __name__ == '__main__':
    print("\n╔═══════════════════════════════════╗")
    print("║   Patch Pre-Check CI Web Server   ║")
    print("╚═══════════════════════════════════╝\n")
    print(f"Access at: http://$(hostname -I | awk '{{print $1}}'):5000\n")
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
