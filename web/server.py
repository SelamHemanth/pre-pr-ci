#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/server.py
# HTTP and WebSocket layer for the web interface
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Routing only: the work lives in the prci package.

    prci.registry   what tests and settings exist
    prci.distro     reading and writing <distro>/.configure
    prci.jobs       the queue, the logs and the processes
    prci.repo       the mainline mirror
    prci.terminal   the shared shell
    prci.system     host facts for the dashboard

This server runs unauthenticated commands as the account that started it, so
it belongs on a trusted network only. See web/README.md.
"""

import argparse
import logging
import os
import sys

from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_sock import Sock

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(WEB_DIR)
if WEB_DIR not in sys.path:
    sys.path.insert(0, WEB_DIR)

from prci import registry                                    # noqa: E402
from prci import repo                                        # noqa: E402
from prci import system                                      # noqa: E402
from prci.distro import ConfigError, Workspace, redact        # noqa: E402
from prci.jobs import JobStore                                # noqa: E402
from prci.terminal import TerminalSession                     # noqa: E402

log = logging.getLogger('prci.server')

TORVALDS_REPO = os.path.join(PROJECT_ROOT, '.torvalds-linux')

workspace = Workspace(PROJECT_ROOT)
store = JobStore(workspace, TORVALDS_REPO)
terminal = TerminalSession(PROJECT_ROOT)

app = Flask(__name__, template_folder=os.path.join(WEB_DIR, 'templates'),
            static_folder=os.path.join(WEB_DIR, 'static'))
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}
sock = Sock(app)


# ── helpers ───────────────────────────────────────────────────────────────

def configured_distro():
    """Return (distro, error_response). Exactly one is set."""
    distro = workspace.selected_distro()
    if not distro:
        return None, (jsonify({
            'success': False,
            'error': 'No distribution selected. Save a configuration first.',
        }), 409)
    if not os.path.exists(workspace.configure_path(distro)):
        return None, (jsonify({
            'success': False,
            'error': '%s is selected but not configured yet.'
                     % registry.DISTROS[distro],
        }), 409)
    return distro, None


def latest_result_per_test():
    """Most recent verdict for each test name, for the test list in the UI."""
    latest = {}
    for job in reversed(store.list()):
        for result in job.get('results') or []:
            latest[result['test']] = {
                'verdict': result['verdict'],
                'job_id': job['id'],
                'when': job.get('end_time') or job.get('start_time'),
            }
    return latest


# ── pages and assets ──────────────────────────────────────────────────────

INDEX_HTML = os.path.join(WEB_DIR, 'templates', 'index.html')


@app.route('/')
def index():
    # Sent as-is rather than rendered: the page carries no server-side values,
    # and Jinja would try to evaluate Vue's {{ ... }} expressions.
    return send_file(INDEX_HTML)


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.svg',
                               mimetype='image/svg+xml')


# ── status ────────────────────────────────────────────────────────────────

@app.route('/api/status')
def api_status():
    distro = workspace.selected_distro()
    return jsonify({
        'configured': workspace.is_configured(),
        'distro': distro,
        'distro_label': registry.DISTROS.get(distro),
        'distros': [{'id': k, 'label': v} for k, v in registry.DISTROS.items()],
        # Lets a first-time visitor's distro picker default to this host
        # instead of guessing, the way the make wizard already does.
        'detected_distro': registry.detect_distro(),
        'active_jobs': store.active(),
        'terminal_alive': terminal.alive,
        'mirror_present': os.path.isdir(TORVALDS_REPO),
    })


@app.route('/api/system')
def api_system():
    config = workspace.read_config() or {}
    return jsonify(system.snapshot(PROJECT_ROOT, config.get('LINUX_SRC_PATH')))


# ── configuration ─────────────────────────────────────────────────────────

@app.route('/api/config/fields')
def api_config_fields():
    distro = request.args.get('distro') or workspace.selected_distro()
    if not registry.is_distro(distro):
        return jsonify({'success': False,
                        'error': 'Unknown distribution'}), 400
    return jsonify({
        'distro': distro,
        'sections': registry.fields_as_json(distro),
        'tests': [t._asdict() for t in registry.tests_for(distro)],
    })


@app.route('/api/config', methods=['GET'])
def api_config_get():
    distro = request.args.get('distro') or workspace.selected_distro()
    if not distro:
        return jsonify({'configured': False, 'distro': None, 'config': None})

    config = workspace.read_config(distro)
    return jsonify({
        'configured': config is not None,
        'distro': distro,
        # Passwords are replaced with a placeholder: this response reaches the
        # browser, and the stored value is not needed to render the form.
        'config': redact(config) if config else None,
        'enabled_tests': workspace.enabled_tests(distro) if config else {},
    })


@app.route('/api/config', methods=['POST'])
def api_config_post():
    payload = request.get_json(silent=True) or {}
    distro = payload.get('distro')
    if not registry.is_distro(distro):
        return jsonify({'success': False,
                        'errors': {'distro': 'Unknown distribution'}}), 400

    values = dict(payload.get('values') or {})
    existing = workspace.read_config(distro) or {}

    # An unchanged password field comes back as the mask, which must not be
    # written over the real value.
    from prci.distro import MASK
    for key in registry.SECRET_KEYS:
        if values.get(key) == MASK or (key in values and values[key] == ''):
            if existing.get(key):
                values[key] = existing[key]

    flags = {}
    selected = payload.get('tests')
    for key in registry.test_config_keys(distro):
        if selected is None:
            flags[key] = existing.get(key, 'yes')
        else:
            flags[key] = 'yes' if key in selected else 'no'

    try:
        workspace.write_config(distro, values, flags, TORVALDS_REPO)
    except ConfigError as exc:
        return jsonify({'success': False, 'errors': exc.errors}), 400

    return jsonify({'success': True, 'distro': distro})


# ── tests ─────────────────────────────────────────────────────────────────

@app.route('/api/tests')
def api_tests():
    distro = request.args.get('distro') or workspace.selected_distro()
    if not registry.is_distro(distro):
        return jsonify({'success': False,
                        'error': 'Unknown distribution'}), 400

    enabled = workspace.enabled_tests(distro)
    latest = latest_result_per_test()
    return jsonify({
        'distro': distro,
        'tests': [
            {
                'name': t.name,
                'description': t.description,
                'log': t.log,
                'config_key': t.config_key,
                'enabled': enabled.get(t.name, True),
                'last_result': latest.get(t.name),
            }
            for t in registry.tests_for(distro)
        ],
    })


# ── work ──────────────────────────────────────────────────────────────────

@app.route('/api/build', methods=['POST'])
def api_build():
    distro, error = configured_distro()
    if error:
        return error
    job = store.submit('build', ['make', 'build'], 'make build', distro=distro)
    return jsonify({'success': True, 'job': job})


@app.route('/api/test/<test_name>', methods=['POST'])
def api_test_one(test_name):
    distro, error = configured_distro()
    if error:
        return error

    # The name arrives in a URL and ends up in a make argument, so it is only
    # ever accepted when the registry recognises it.
    test = registry.find_test(distro, test_name)
    if not test:
        return jsonify({
            'success': False,
            'error': 'Unknown test for %s: %s' % (distro, test_name),
            'known': [t.name for t in registry.tests_for(distro)],
        }), 400

    target = '%s-test=%s' % (distro, test.name)
    job = store.submit('test', ['make', target], 'make %s' % target,
                       test_name=test.name, distro=distro, total_steps=1)
    return jsonify({'success': True, 'job': job})


@app.route('/api/test', methods=['POST'])
def api_test_all():
    distro, error = configured_distro()
    if error:
        return error

    enabled = [t for t in registry.tests_for(distro)
               if workspace.enabled_tests(distro).get(t.name, True)]
    job = store.submit('test_all', ['make', 'test'], 'make test',
                       distro=distro, total_steps=len(enabled) or None)
    return jsonify({'success': True, 'job': job, 'expected_tests': len(enabled)})


@app.route('/api/clean', methods=['POST'])
def api_clean():
    job = store.submit('clean', ['make', 'clean'], 'make clean')
    return jsonify({'success': True, 'job': job})


@app.route('/api/reset', methods=['POST'])
def api_reset():
    job = store.submit('reset', ['make', 'reset'], 'make reset')
    return jsonify({'success': True, 'job': job})


@app.route('/api/mirror/sync', methods=['POST'])
def api_mirror_sync():
    job = store.submit(
        'mirror',
        [sys.executable, '-c',
         'import sys; sys.path.insert(0, %r); '
         'from prci import repo; '
         'sys.exit(0 if repo.sync(%r) else 1)' % (WEB_DIR, TORVALDS_REPO)],
        'sync mainline mirror')
    return jsonify({'success': True, 'job': job})


# ── jobs ──────────────────────────────────────────────────────────────────

@app.route('/api/jobs')
def api_jobs():
    limit = request.args.get('limit', type=int, default=50)
    return jsonify({
        'jobs': store.list(limit=max(1, min(limit, 200)),
                           kind=request.args.get('kind')),
    })


@app.route('/api/jobs/<job_id>')
def api_job(job_id):
    job = store.get(job_id)
    if not job:
        return jsonify({'success': False, 'error': 'No such job'}), 404
    return jsonify({'job': job})


@app.route('/api/jobs/<job_id>/log')
def api_job_log(job_id):
    if not store.get(job_id):
        return jsonify({'success': False, 'error': 'No such job'}), 404
    which = 'test' if request.args.get('which') == 'test' else 'job'
    offset = request.args.get('offset', type=int)
    chunk = store.read_log(job_id, offset=offset, which=which)
    if chunk is None:
        return jsonify({'success': False, 'error': 'No log for this job'}), 404
    return jsonify(chunk)


@app.route('/api/jobs/<job_id>/log/download')
def api_job_log_download(job_id):
    if not store.get(job_id):
        return jsonify({'success': False, 'error': 'No such job'}), 404
    which = 'test' if request.args.get('which') == 'test' else 'job'
    path = store.log_file_for(job_id, which=which)
    if not path or not os.path.exists(path):
        return jsonify({'success': False, 'error': 'No log for this job'}), 404
    return send_file(path, as_attachment=True,
                     download_name=os.path.basename(path),
                     mimetype='text/plain')


@app.route('/api/jobs/<job_id>/kill', methods=['POST'])
def api_job_kill(job_id):
    ok, message = store.kill(job_id)
    status = 200 if ok else (404 if message == 'No such job' else 409)
    return jsonify({'success': ok, 'message': message}), status


@app.route('/api/jobs/clear', methods=['POST'])
def api_jobs_clear():
    removed = store.clear_history()
    return jsonify({'success': True, 'removed': removed})


# ── terminal ──────────────────────────────────────────────────────────────

@app.route('/api/terminal/status')
def api_terminal_status():
    return jsonify({'alive': terminal.alive, 'viewers': terminal.viewers})


@sock.route('/ws/terminal')
def ws_terminal(ws):
    """Attach a browser to the shared shell.

    Anyone who can reach this port gets a shell as the account running the
    server. That is the tool's existing trust model, not something this
    endpoint adds, but it is the reason the port must not be exposed.
    """
    import json

    if not terminal.alive:
        try:
            terminal.start()
        except OSError as exc:
            ws.send(json.dumps({'type': 'error', 'data': str(exc)}))
            return

    terminal.attach(ws)
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                message = json.loads(raw)
            except ValueError:
                continue

            kind = message.get('type')
            if kind == 'input':
                terminal.write(message.get('data', ''))
            elif kind == 'resize':
                terminal.resize(message.get('rows'), message.get('cols'))
    except Exception:
        log.debug('terminal client went away', exc_info=True)
    finally:
        terminal.detach(ws)


# ── errors ────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'error': 'No such endpoint'}), 404
    return send_file(INDEX_HTML)


@app.errorhandler(500)
def server_error(error):
    log.exception('unhandled error on %s', request.path)
    return jsonify({'success': False, 'error': str(error)}), 500


# ── entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Pre-PR CI web interface')
    parser.add_argument('--host', default='0.0.0.0',
                        help='address to bind (default: all interfaces)')
    parser.add_argument('--port', type=int, default=5000)
    parser.add_argument('--no-mirror-sync', action='store_true',
                        help='do not update the mainline mirror on startup')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s',
    )

    distro = workspace.selected_distro()
    print('Pre-PR CI web interface')
    print('  project    %s' % PROJECT_ROOT)
    print('  distro     %s' % (registry.DISTROS.get(distro) or 'not configured'))
    print('  listening  http://%s:%d'
          % ('localhost' if args.host in ('0.0.0.0', '') else args.host,
             args.port))
    if args.host == '0.0.0.0':
        print('  note       bound to every interface, and every visitor gets a')
        print('             shell as %s -- keep this on a trusted network'
              % (os.environ.get('USER') or 'this account'))
    print()

    if not args.no_mirror_sync and os.path.isdir(TORVALDS_REPO):
        # Only refresh an existing mirror; cloning several GB is not something
        # to start behind a server that has not finished booting.
        import threading
        threading.Thread(target=repo.sync, args=(TORVALDS_REPO,),
                         daemon=True).start()

    app.run(host=args.host, port=args.port, threaded=True,
            debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
