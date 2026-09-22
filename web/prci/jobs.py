# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/jobs.py
# Queueing, running, following and killing make invocations
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""The job queue behind the web interface.

Three properties are worth knowing about:

*Jobs are serialised.*  Every job operates on the one kernel tree named by
``LINUX_SRC_PATH``, so running two at once corrupts both.  A single worker
thread drains the queue and the UI shows callers where they are in line.

*Each job gets its own log file.*  The test scripts write their own logs under
``logs/``; if we captured ``make`` stdout into the same path both writers would
interleave and truncate each other.  Job output goes to ``.prci/joblogs/`` and
the script's own log is offered alongside it.

*State lives outside ``logs/``*, because ``make clean`` deletes that directory
and would take the job history with it.
"""

import errno
import json
import logging
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime

from . import registry, repo

log = logging.getLogger(__name__)

#: Job history kept on disk.  Old entries are dropped oldest-first.
MAX_HISTORY = 200

#: How long a killed job gets to exit on SIGTERM before it gets SIGKILL.
KILL_GRACE_SECONDS = 10

#: Largest log slice returned in one response, and how much of an already-large
#: log the first request gets.  A kernel build log can reach hundreds of
#: megabytes and the browser only needs the end of it.
MAX_CHUNK_BYTES = 512 * 1024
INITIAL_TAIL_BYTES = 256 * 1024

ACTIVE_STATES = ('queued', 'running')
FINISHED_STATES = ('completed', 'failed', 'killed', 'cancelled', 'interrupted')

# Order matters: the OSC form must be tried before the single-character
# escapes, whose range covers the "]" that introduces it.
_ANSI_RE = re.compile(
    r'\x1b\].*?(?:\x07|\x1b\\)'   # OSC, e.g. a window title
    r'|\x1b\[[0-9;?]*[ -/]*[@-~]'  # CSI, e.g. colours
    r'|\x1b[@-Z\\-_]'              # two-character escapes
)

# Anchored on purpose: build output quotes strings like "PASS:" and an
# unanchored match turned compiler noise into test results.
_RESULT_RE = re.compile(
    r'^\s*(?:[\u2713\u2717\u2298]\s*)?(PASS|FAIL|SKIP)\s*:\s*(\S+)\s*$'
)


def strip_ansi(text):
    return _ANSI_RE.sub('', text)


class JobStore:
    def __init__(self, workspace, torvalds_repo, state_dir=None):
        self.workspace = workspace
        self.torvalds_repo = torvalds_repo

        self.state_dir = state_dir or os.path.join(workspace.root, '.prci')
        self.log_dir = os.path.join(self.state_dir, 'joblogs')
        self.index_path = os.path.join(self.state_dir, 'jobs.json')
        os.makedirs(self.log_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []          # job ids, oldest first
        self._processes = {}      # job id -> Popen, never persisted
        self._cancelled = set()   # queued jobs killed before they started
        self._queue = queue.Queue()

        self._load()
        self._worker = threading.Thread(
            target=self._run_queue, name='job-worker', daemon=True)
        self._worker.start()

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self):
        try:
            with open(self.index_path, 'r') as handle:
                stored = json.load(handle)
        except FileNotFoundError:
            return
        except (ValueError, OSError) as exc:
            log.warning('ignoring unreadable job index %s: %s',
                        self.index_path, exc)
            return

        jobs = stored.get('jobs', []) if isinstance(stored, dict) else stored
        for job in jobs:
            if not isinstance(job, dict) or 'id' not in job:
                continue
            # Nothing survived the restart, whatever the index claims.
            if job.get('status') in ACTIVE_STATES:
                job['status'] = 'interrupted'
                job['error'] = 'The server restarted while this job was running'
                job.setdefault('end_time', datetime.now().isoformat())
            self._jobs[job['id']] = job
            self._order.append(job['id'])
        self._save_locked()

    def _save_locked(self):
        payload = {'jobs': [self._jobs[i] for i in self._order if i in self._jobs]}
        try:
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix='.jobs-')
            with os.fdopen(fd, 'w') as handle:
                json.dump(payload, handle, indent=2)
            os.replace(tmp, self.index_path)
        except OSError as exc:
            log.warning('could not persist job index: %s', exc)

    def _prune_locked(self):
        while len(self._order) > MAX_HISTORY:
            for position, job_id in enumerate(self._order):
                job = self._jobs.get(job_id)
                if job and job.get('status') in ACTIVE_STATES:
                    continue
                self._order.pop(position)
                self._jobs.pop(job_id, None)
                self._drop_log(job)
                break
            else:
                return

    def _drop_log(self, job):
        path = (job or {}).get('log_file')
        if path and os.path.dirname(path) == self.log_dir:
            try:
                os.unlink(path)
            except OSError:
                pass

    # ── submission ────────────────────────────────────────────────────────

    def submit(self, kind, argv, display, test_name=None, distro=None,
               total_steps=None):
        """Queue a command.  ``argv`` is a list -- nothing here goes via a shell."""
        job_id = str(uuid.uuid4())
        job = {
            'id': job_id,
            'kind': kind,
            'command': display,
            'argv': list(argv),
            'test_name': test_name,
            'distro': distro,
            'status': 'queued',
            'created_time': datetime.now().isoformat(),
            'start_time': None,
            'end_time': None,
            'exit_code': None,
            'error': None,
            'log_file': os.path.join(self.log_dir, '%s.log' % job_id),
            'test_log_file': self._test_log_for(distro, test_name),
            'results': [],
            'total_steps': total_steps,
            'last_line': None,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._prune_locked()
            self._save_locked()
            # Same shape the other accessors return, so argv stays server-side.
            snapshot = self._decorate(job)
        self._queue.put(job_id)
        log.info('queued %s (%s)', display, job_id)
        return snapshot

    def _test_log_for(self, distro, test_name):
        if not distro or not test_name:
            return None
        test = registry.find_test(distro, test_name)
        if not test:
            return None
        return os.path.join(self.workspace.logs_dir, test.log)

    # ── worker ────────────────────────────────────────────────────────────

    def _run_queue(self):
        while True:
            job_id = self._queue.get()
            try:
                self._execute(job_id)
            except Exception:
                log.exception('job worker failed on %s', job_id)
            finally:
                self._queue.task_done()

    def _execute(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            # Must be tested under the same lock that publishes "running":
            # checking it before this point let a kill() land in between and
            # be overwritten, so the job ran after being cancelled.
            if job_id in self._cancelled or job['status'] == 'cancelled':
                self._cancelled.discard(job_id)
                return
            job['status'] = 'running'
            job['start_time'] = datetime.now().isoformat()
            argv = list(job['argv'])
            log_path = job['log_file']
            kind = job['kind']
            test_name = job.get('test_name')
            self._save_locked()

        needs_mirror = kind == 'build' or test_name == 'check_dependency'

        try:
            with open(log_path, 'w', buffering=1, errors='replace') as log_file:
                def say(message):
                    log_file.write('[repo-sync] %s\n' % message)

                if needs_mirror:
                    repo.sync(self.torvalds_repo, emit=say)

                log_file.write('$ %s\n\n' % ' '.join(argv))

                process = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=self.workspace.root,
                    universal_newlines=True,
                    bufsize=1,
                    errors='replace',
                    # Its own process group, so killing the job takes the whole
                    # make/gcc tree with it and not just make.
                    start_new_session=True,
                )
                with self._lock:
                    self._processes[job_id] = process

                # Popen does not close its pipes by itself, so without this
                # the server leaks a descriptor for every job it runs.
                with process:
                    for line in process.stdout:
                        log_file.write(line)
                        self._observe(job_id, line)
                    exit_code = process.wait()

                log_file.write('\n--- exited with code %d ---\n' % exit_code)
        except OSError as exc:
            self._finish(job_id, status='failed', error=str(exc))
            log.error('could not run %s: %s', argv, exc)
            return

        with self._lock:
            self._processes.pop(job_id, None)
            was_killed = job_id in self._cancelled
            self._cancelled.discard(job_id)

        if was_killed or exit_code in (-signal.SIGTERM, -signal.SIGKILL):
            status = 'killed'
        elif exit_code == 0:
            status = 'completed'
        else:
            status = 'failed'
        self._finish(job_id, status=status, exit_code=exit_code)

    def _observe(self, job_id, line):
        """Pick the test verdicts out of the stream so the UI can show real
        progress instead of an animation that means nothing."""
        clean = strip_ansi(line).rstrip('\n')
        if not clean.strip():
            return
        match = _RESULT_RE.match(clean)
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job['last_line'] = clean[-400:]
            if match:
                job['results'].append({
                    'verdict': match.group(1),
                    'test': match.group(2),
                })

    def _finish(self, job_id, status, exit_code=None, error=None):
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job['status'] = status
            job['end_time'] = datetime.now().isoformat()
            job['exit_code'] = exit_code
            if error:
                job['error'] = error
            self._processes.pop(job_id, None)
            self._save_locked()
        log.info('job %s %s (exit %s)', job_id, status, exit_code)

    # ── queries ───────────────────────────────────────────────────────────

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return self._decorate(job) if job else None

    def list(self, limit=None, kind=None):
        with self._lock:
            jobs = [self._jobs[i] for i in reversed(self._order)
                    if i in self._jobs]
            if kind:
                jobs = [j for j in jobs if j.get('kind') == kind]
            if limit:
                jobs = jobs[:limit]
            return [self._decorate(j) for j in jobs]

    def active(self):
        return [j for j in self.list() if j['status'] in ACTIVE_STATES]

    def _decorate(self, job):
        """Add the derived values the UI wants, without storing them."""
        out = dict(job)
        out.pop('argv', None)

        total = out.get('total_steps') or 0
        done = len(out.get('results') or [])
        if out['status'] in FINISHED_STATES:
            out['progress'] = 100
        elif total:
            out['progress'] = min(99, int(done * 100 / total))
        else:
            out['progress'] = None

        out['elapsed_seconds'] = self._elapsed(job)
        out['queue_position'] = self._queue_position(job)

        failed = [r['test'] for r in out.get('results') or []
                  if r['verdict'] == 'FAIL']
        out['failed_tests'] = failed
        out['log_size'] = self._size(out.get('log_file'))
        out['has_test_log'] = bool(
            out.get('test_log_file') and os.path.exists(out['test_log_file']))
        return out

    @staticmethod
    def _size(path):
        try:
            return os.path.getsize(path)
        except (OSError, TypeError):
            return 0

    @staticmethod
    def _elapsed(job):
        if not job.get('start_time'):
            return 0
        try:
            start = datetime.fromisoformat(job['start_time'])
            end = (datetime.fromisoformat(job['end_time'])
                   if job.get('end_time') else datetime.now())
        except (TypeError, ValueError):
            return 0
        return max(0, int((end - start).total_seconds()))

    def _queue_position(self, job):
        if job.get('status') != 'queued':
            return None
        waiting = [i for i in self._order
                   if self._jobs.get(i, {}).get('status') == 'queued']
        try:
            return waiting.index(job['id']) + 1
        except ValueError:
            return None

    # ── log following ─────────────────────────────────────────────────────

    def read_log(self, job_id, offset=None, which='job'):
        """Return the slice of a job's log starting at ``offset``.

        Passing the returned ``offset`` back on the next call is what makes
        following a build cheap; the previous implementation re-sent the whole
        file every 1.5 seconds.
        """
        job = self.get(job_id)
        if not job:
            return None

        path = job['test_log_file'] if which == 'test' else job['log_file']
        if not path:
            return {'text': '', 'offset': 0, 'size': 0, 'truncated': False,
                    'missing': True}

        try:
            size = os.path.getsize(path)
        except OSError:
            note = {
                'queued': 'Waiting for the job ahead of it to finish...',
                'running': 'Starting...',
            }.get(job['status'], 'No log file was produced.')
            return {'text': note, 'offset': 0, 'size': 0,
                    'truncated': False, 'missing': True}

        truncated = False
        if offset is None:
            offset = max(0, size - INITIAL_TAIL_BYTES)
            truncated = offset > 0
        elif offset > size:
            # The file was rotated or recreated underneath us.
            offset = 0
            truncated = True

        want = min(MAX_CHUNK_BYTES, size - offset)
        if want <= 0:
            return {'text': '', 'offset': offset, 'size': size,
                    'truncated': truncated, 'missing': False}

        try:
            with open(path, 'rb') as handle:
                handle.seek(offset)
                raw = handle.read(want)
        except OSError as exc:
            return {'text': '', 'offset': offset, 'size': size,
                    'truncated': truncated, 'missing': True,
                    'error': str(exc)}

        # Stop at the last newline so a chunk boundary can never split a
        # multi-byte character or an escape sequence the browser has to colour.
        if offset + len(raw) < size:
            cut = raw.rfind(b'\n')
            if cut != -1:
                raw = raw[:cut + 1]

        # The scripts colour their output for a terminal. Left in, the escape
        # sequences show up as literal "[0;34m" litter in the browser, so they
        # are removed here; the interface colours whole lines itself. The
        # offset stays a byte position in the file, which is all the caller
        # echoes back, so stripping cannot desynchronise the next read.
        return {
            'text': strip_ansi(raw.decode('utf-8', errors='replace')),
            'offset': offset + len(raw),
            'size': size,
            'truncated': truncated,
            'missing': False,
        }

    def log_file_for(self, job_id, which='job'):
        job = self.get(job_id)
        if not job:
            return None
        return job['test_log_file'] if which == 'test' else job['log_file']

    # ── control ───────────────────────────────────────────────────────────

    def kill(self, job_id):
        """Stop a job, whether it is running or still waiting its turn."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False, 'No such job'
            if job['status'] not in ACTIVE_STATES:
                return False, 'Job is not running'

            if job['status'] == 'queued':
                self._cancelled.add(job_id)
                job['status'] = 'cancelled'
                job['end_time'] = datetime.now().isoformat()
                self._save_locked()
                return True, 'Removed from the queue'

            process = self._processes.get(job_id)
            if not process:
                return False, 'The process is no longer tracked'
            self._cancelled.add(job_id)

        threading.Thread(
            target=self._terminate, args=(process,), daemon=True).start()
        return True, 'Stopping'

    @staticmethod
    def _terminate(process):
        try:
            group = os.getpgid(process.pid)
        except OSError:
            return

        for sig, wait in ((signal.SIGTERM, KILL_GRACE_SECONDS),
                          (signal.SIGKILL, 5)):
            try:
                os.killpg(group, sig)
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return
                log.warning('could not signal process group %d: %s', group, exc)
                return
            deadline = time.time() + wait
            while time.time() < deadline:
                if process.poll() is not None:
                    return
                time.sleep(0.2)

    def clear_history(self):
        """Forget finished jobs.  Running and queued ones stay."""
        removed = 0
        with self._lock:
            keep = []
            for job_id in self._order:
                job = self._jobs.get(job_id)
                if job and job.get('status') in ACTIVE_STATES:
                    keep.append(job_id)
                    continue
                self._drop_log(job)
                self._jobs.pop(job_id, None)
                removed += 1
            self._order = keep
            self._save_locked()
        return removed
