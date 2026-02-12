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

# Torvalds repository path
TORVALDS_REPO = os.path.join(PROJECT_ROOT, '.torvalds-linux')

# Job storage with process tracking
jobs = {}
job_lock = threading.Lock()
job_processes = {}

# Add static file route
@app.route('/static/<path:filename>')
def static_files(filename):
    """Serve static files"""
    return send_from_directory(
        os.path.join(SCRIPT_DIR, 'static'),
        filename
    )

def clean_ansi_codes(text):
    """Remove ANSI color codes and escape sequences from text"""
    # Remove ANSI escape sequences
    ansi_escape = re.compile(r'\x1b\[[0-9;]*[mGKHfJ]|\x1b\[[\d;]*[A-Za-z]|\x1b\].*?\x07|\x1b\[.*?[@-~]')
    text = ansi_escape.sub('', text)

    # Remove common color codes like [0;34m, [0m, etc.
    text = re.sub(r'\[\d+(?:;\d+)*m', '', text)

    # Remove other escape sequences
    text = re.sub(r'\x1b[@-_][0-?]*[ -/]*[@-~]', '', text)

    return text

def clone_torvalds_repo_silent():
    """Clone Torvalds repo silently if not exists"""
    try:
        if not os.path.exists(TORVALDS_REPO):
            subprocess.run(
                ['git', 'clone', '--bare', 'https://github.com/torvalds/linux.git', TORVALDS_REPO],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False
            )
    except Exception:
        pass

def get_distro_config():
    """Read current distribution configuration"""
    config_file = os.path.join(PROJECT_ROOT, '.distro_config')
    if not os.path.exists(config_file):
        return None
    config = {}
    with open(config_file, 'r') as f:
        for line in f:
            if '=' in line:
                key, value = line.strip().split('=', 1)
                config[key] = value
    return config

def get_test_log_file(test_name, distro):
    """Map test name to actual log file"""
    if distro == 'anolis':
        log_map = {
            'check_dependency' : 'check_dependency.log',
            'check_kconfig': 'check_Kconfig.log',
            'build_allyes_config': 'build_allyes_config.log',
            'build_allno_config': 'build_allno_config.log',
            'build_anolis_defconfig': 'build_anolis_defconfig.log',
            'build_anolis_debug': 'build_anolis_debug_defconfig.log',
            'anck_rpm_build': 'anck_rpm_build.log',
            'check_kapi': 'kapi_test.log',
            'boot_kernel_rpm': 'boot_kernel_rpm.log'
        }
    else:  # euler
        log_map = {
            'check_dependency': 'check_dependency.log',
            'build_allmod': 'build_allmod.log',
            'check_patch': 'check_patch.log',
            'check_format': 'check_format.log',
            'rpm_build': 'rpm_build.log',
            'boot_kernel': 'boot_kernel.log'
        }

    log_filename = log_map.get(test_name, f'{test_name}.log')
    return os.path.join(LOGS_DIR, log_filename)

def run_make_command(command, job_id):
    """Run make command in background"""
    with job_lock:
        jobs[job_id]['status'] = 'running'
        jobs[job_id]['start_time'] = datetime.now().isoformat()

    log_file = os.path.join(LOGS_DIR, f'{job_id}_command.log')

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

        output_lines = []
        for line in process.stdout:
            output_lines.append(line)
            with open(log_file, 'a') as f:
                f.write(line)

        process.wait()
        exit_code = process.returncode

        with job_lock:
            if job_id in job_processes:
                del job_processes[job_id]

        actual_log = log_file
        config = get_distro_config()
        if config and ('test' in command):
            distro = config.get('DISTRO')
            test_name = None
            if 'anolis-test=' in command:
                test_name = command.split('anolis-test=')[1].strip()
            elif 'euler-test=' in command:
                test_name = command.split('euler-test=')[1].strip()

            if test_name and distro:
                actual_log = get_test_log_file(test_name, distro)

        with job_lock:
            if exit_code == -9 or exit_code == -15:
                jobs[job_id]['status'] = 'killed'
            else:
                jobs[job_id]['status'] = 'completed' if exit_code == 0 else 'failed'
            jobs[job_id]['exit_code'] = exit_code
            jobs[job_id]['output'] = ''.join(output_lines)
            jobs[job_id]['end_time'] = datetime.now().isoformat()
            jobs[job_id]['log_file'] = actual_log

    except Exception as e:
        with job_lock:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['error'] = str(e)
            jobs[job_id]['end_time'] = datetime.now().isoformat()
            if job_id in job_processes:
                del job_processes[job_id]

@app.route('/')
def index():
    """Serve frontend"""
    html_file = os.path.join(SCRIPT_DIR, 'templates', 'index.html')
    if os.path.exists(html_file):
        print(f"[INFO] Serving: {html_file}")
        with open(html_file, 'r') as f:
            return f.read()
    return jsonify({'error': 'Frontend not found', 'path': html_file}), 404

@app.route('/api/status')
def status():
    """System status"""
    config = get_distro_config()
    return jsonify({
        'configured': config is not None,
        'distro': config.get('DISTRO') if config else None,
        'workspace': PROJECT_ROOT,
        'timestamp': datetime.now().isoformat()
    })

@app.route('/api/config/fields')
def get_config_fields():
    """Get configuration fields"""
    distro = request.args.get('distro')
    if not distro or distro not in ['anolis', 'euler']:
        return jsonify({'error': 'Invalid distribution'}), 400

    if distro == 'anolis':
        fields = {
            'general': [
                {'name': 'LINUX_SRC_PATH', 'label': 'Linux source path', 'type': 'text', 'required': True},
                {'name': 'SIGNER_NAME', 'label': 'Signed-off-by name', 'type': 'text', 'required': True},
                {'name': 'SIGNER_EMAIL', 'label': 'Signed-off-by email', 'type': 'email', 'required': True},
                {'name': 'ANBZ_ID', 'label': 'Anolis Bugzilla ID', 'type': 'text', 'required': True},
                {'name': 'NUM_PATCHES', 'label': 'Number of patches', 'type': 'number', 'required': True, 'default': 10}
            ],
            'build': [
                {'name': 'BUILD_THREADS', 'label': 'Build threads', 'type': 'number', 'required': True, 'default': 256}
            ],
            'vm': [
                {'name': 'VM_IP', 'label': 'VM IP address', 'type': 'text', 'required': True},
                {'name': 'VM_ROOT_PWD', 'label': 'VM root password', 'type': 'password', 'required': True}
            ],
            'host': [
                {'name': 'HOST_USER_PWD', 'label': 'Host sudo password', 'type': 'password', 'required': True}
            ]
        }
    else:  # euler
        fields = {
            'general': [
                {'name': 'LINUX_SRC_PATH', 'label': 'Linux source path', 'type': 'text', 'required': True},
                {'name': 'SIGNER_NAME', 'label': 'Signed-off-by name', 'type': 'text', 'required': True},
                {'name': 'SIGNER_EMAIL', 'label': 'Signed-off-by email', 'type': 'email', 'required': True},
                {'name': 'BUGZILLA_ID', 'label': 'Bugzilla ID', 'type': 'text', 'required': True},
                {'name': 'PATCH_CATEGORY', 'label': 'Patch category', 'type': 'select', 'required': True,
                 'options': ['feature', 'bugfix', 'performance', 'security'], 'default': 'bugfix'},
                {'name': 'NUM_PATCHES', 'label': 'Number of patches', 'type': 'number', 'required': True, 'default': 5}
            ],
            'build': [
                {'name': 'BUILD_THREADS', 'label': 'Build threads', 'type': 'number', 'required': True, 'default': 256}
            ],
            'vm': [
                {'name': 'VM_IP', 'label': 'VM IP address', 'type': 'text', 'required': True},
                {'name': 'VM_ROOT_PWD', 'label': 'VM root password', 'type': 'password', 'required': True}
            ],
            'host': [
                {'name': 'HOST_USER_PWD', 'label': 'Host sudo password', 'type': 'password', 'required': True}
            ]
        }

    return jsonify({'distro': distro, 'fields': fields})

@app.route('/api/config', methods=['GET'])
def get_current_config():
    """Get current configuration"""
    config = get_distro_config()
    if not config:
        return jsonify({'error': 'Not configured'}), 404

    distro = config.get('DISTRO')
    config_file = os.path.join(PROJECT_ROOT, distro, '.configure')
    if not os.path.exists(config_file):
        return jsonify({'error': 'Config file not found'}), 404

    detailed_config = {}
    with open(config_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip('"').strip("'")
                detailed_config[key] = value

    return jsonify(detailed_config)

@app.route('/api/config', methods=['POST'])
def set_config():
    """Save configuration"""
    data = request.json
    distro = data.get('distro')
    config_data = data.get('config', {})

    if not distro or distro not in ['anolis', 'euler']:
        return jsonify({'error': 'Invalid distribution'}), 400

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

            if distro == 'anolis':
                for test in ['CHECK_DEPENDENCY', 'CHECK_KCONFIG', 'BUILD_ALLYES', 'BUILD_ALLNO', 'BUILD_DEFCONFIG',
                            'BUILD_DEBUG', 'RPM_BUILD', 'CHECK_KAPI', 'BOOT_KERNEL']:
                    f.write(f'TEST_{test}="yes"\n')
            else:
                for test in ['CHECK_DEPENDENCY', 'BUILD_ALLMOD', 'CHECK_PATCH',
                            'CHECK_FORMAT', 'RPM_BUILD', 'BOOT_KERNEL']:
                    f.write(f'TEST_{test}="yes"\n')

            f.write('\n# Host\n')
            f.write(f'HOST_USER_PWD=\'{config_data.get("HOST_USER_PWD", "")}\'\n\n')
            f.write('# VM\n')
            f.write(f'VM_IP="{config_data.get("VM_IP", "")}"\n')
            f.write(f'VM_ROOT_PWD=\'{config_data.get("VM_ROOT_PWD", "")}\'\n')

            if distro == 'euler':
                f.write(f'\n# Repository\n')
                f.write(f'TORVALDS_REPO="{os.path.join(PROJECT_ROOT, ".torvalds-linux")}"\n')

        return jsonify({'message': 'Configuration saved', 'distro': distro})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/tests')
def list_tests():
    """List available tests"""
    config = get_distro_config()
    if not config:
        return jsonify({'error': 'Not configured'}), 404

    distro = config.get('DISTRO')
    tests = {
        'anolis': [
            {'name': 'check_dependency', 'description': 'Check patch dependencies'},
            {'name': 'check_kconfig', 'description': 'Validate kernel configuration'},
            {'name': 'build_allyes_config', 'description': 'Build with allyesconfig'},
            {'name': 'build_allno_config', 'description': 'Build with allnoconfig'},
            {'name': 'build_anolis_defconfig', 'description': 'Build with anolis_defconfig'},
            {'name': 'build_anolis_debug', 'description': 'Build with anolis-debug_defconfig'},
            {'name': 'anck_rpm_build', 'description': 'Build ANCK RPM packages'},
            {'name': 'check_kapi', 'description': 'Check kernel ABI compatibility'},
            {'name': 'boot_kernel_rpm', 'description': 'Boot VM with built kernel RPM'}
        ],
        'euler': [
            {'name': 'check_dependency', 'description': 'Check patch dependencies'},
            {'name': 'build_allmod', 'description': 'Build with allmodconfig'},
            {'name': 'check_patch', 'description': 'Run checkpatch.pl validation'},
            {'name': 'check_format', 'description': 'Check code formatting'},
            {'name': 'rpm_build', 'description': 'Build openEuler RPM packages'},
            {'name': 'boot_kernel', 'description': 'Boot test (requires remote setup)'}
        ]
    }
    return jsonify({'distro': distro, 'tests': tests.get(distro, [])})

@app.route('/api/build', methods=['POST'])
def build():
    """Run make build"""
    clone_torvalds_repo_silent()
    
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id,
            'command': 'make build',
            'status': 'queued',
            'created_time': datetime.now().isoformat()
        }
    thread = threading.Thread(target=run_make_command, args=('make build', job_id))
    thread.daemon = True
    thread.start()
    return jsonify({'job_id': job_id})

@app.route('/api/test/all', methods=['POST'])
def test_all():
    """Run all tests"""
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id,
            'command': 'make test',
            'status': 'queued',
            'created_time': datetime.now().isoformat()
        }
    thread = threading.Thread(target=run_make_command, args=('make test', job_id))
    thread.daemon = True
    thread.start()
    return jsonify({'job_id': job_id})

@app.route('/api/test/<test_name>', methods=['POST'])
def test_specific(test_name):
    """Run specific test"""
    config = get_distro_config()
    if not config:
        return jsonify({'error': 'Not configured'}), 400

    distro = config.get('DISTRO')
    command = f'make {distro}-test={test_name}'

    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id,
            'command': command,
            'test_name': test_name,
            'status': 'queued',
            'created_time': datetime.now().isoformat()
        }
    thread = threading.Thread(target=run_make_command, args=(command, job_id))
    thread.daemon = True
    thread.start()
    return jsonify({'job_id': job_id})

@app.route('/api/clean', methods=['POST'])
def clean():
    """Run make clean"""
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id,
            'command': 'make clean',
            'status': 'queued',
            'created_time': datetime.now().isoformat()
        }
    thread = threading.Thread(target=run_make_command, args=('make clean', job_id))
    thread.daemon = True
    thread.start()
    return jsonify({'job_id': job_id})

@app.route('/api/reset', methods=['POST'])
def reset():
    """Run make reset"""
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'id': job_id,
            'command': 'make reset',
            'status': 'queued',
            'created_time': datetime.now().isoformat()
        }
    thread = threading.Thread(target=run_make_command, args=('make reset', job_id))
    thread.daemon = True
    thread.start()
    return jsonify({'job_id': job_id})

@app.route('/api/jobs/<job_id>/kill', methods=['POST'])
def kill_job(job_id):
    """Kill a running job"""
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
            jobs[job_id]['status'] = 'killed'
            jobs[job_id]['end_time'] = datetime.now().isoformat()
            if job_id in job_processes:
                del job_processes[job_id]
        
        return jsonify({'message': 'Job killed successfully', 'job_id': job_id})
    except Exception as e:
        return jsonify({'error': f'Failed to kill job: {str(e)}'}), 500

@app.route('/api/jobs')
def get_jobs():
    """Get all jobs"""
    with job_lock:
        return jsonify(list(jobs.values()))

@app.route('/api/jobs/<job_id>')
def get_job(job_id):
    """Get specific job"""
    with job_lock:
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404
        return jsonify(jobs[job_id])

@app.route('/api/jobs/<job_id>/log')
def get_job_log(job_id):
    """Get job log with ANSI codes removed"""
    with job_lock:
        if job_id not in jobs:
            return jsonify({'error': 'Job not found'}), 404

        job = jobs[job_id]
        log_file = job.get('log_file')

        if not log_file or not os.path.exists(log_file):
            test_name = job.get('test_name')
            if test_name:
                config = get_distro_config()
                if config:
                    distro = config.get('DISTRO')
                    log_file = get_test_log_file(test_name, distro)

        # Read and clean log file
        if log_file and os.path.exists(log_file):
            try:
                with open(log_file, 'r') as f:
                    raw_log = f.read()
                    # Clean ANSI codes before returning
                    clean_log = clean_ansi_codes(raw_log)
                    return jsonify({'log': clean_log})
            except Exception as e:
                return jsonify({'error': f'Read error: {str(e)}'}), 500

        # Fallback to command output
        output = job.get('output', '')
        if output:
            # Clean ANSI codes from output too
            clean_output = clean_ansi_codes(output)
            return jsonify({'log': clean_output})

        return jsonify({'error': 'No log available'}), 404

if __name__ == '__main__':
    print("\n╔═══════════════════════════════════╗")
    print("║   Patch Pre-Check CI Web Server   ║")
    print("╚═══════════════════════════════════╝\n")
    print(f"Access at: http://$(hostname -I | awk '{{print $1}}'):5000\n")
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
