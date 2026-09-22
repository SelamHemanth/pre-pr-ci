# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/terminal.py
# The shared PTY behind the embedded terminal
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""One login shell in a PTY, shared by every attached browser.

The shell outlives individual WebSocket connections so that reloading the page
does not lose a long-running command.  Every viewer sees and types into the
same session -- it is one shell, not one per tab.
"""

import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios
import threading

log = logging.getLogger(__name__)

DEFAULT_ROWS, DEFAULT_COLS = 50, 220

#: Guards against a client asking for a size the kernel will refuse.
MAX_ROWS, MAX_COLS = 300, 1000

READ_SIZE = 65536


class TerminalSession:
    def __init__(self, cwd):
        self.cwd = cwd
        self.pid = None
        self.master_fd = None
        self._lock = threading.Lock()
        self._clients = []
        self._reader = None

    # ── lifecycle ─────────────────────────────────────────────────────────

    @property
    def alive(self):
        with self._lock:
            return self._alive_locked()

    def _alive_locked(self):
        if self.pid is None:
            return False
        try:
            # WNOHANG also reaps the child, so an exited shell does not linger
            # as a zombie reporting itself alive.
            reaped, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return False
        except OSError:
            return False
        if reaped == self.pid:
            self._teardown_locked()
            return False
        return True

    def start(self):
        """Spawn the shell if it is not already running."""
        with self._lock:
            if self._alive_locked():
                return

            self._teardown_locked()
            shell = os.environ.get('SHELL') or '/bin/bash'

            pid, master_fd = pty.fork()
            if pid == 0:
                os.environ['TERM'] = 'xterm-256color'
                os.environ['COLUMNS'] = str(DEFAULT_COLS)
                os.environ['LINES'] = str(DEFAULT_ROWS)
                try:
                    os.chdir(self.cwd)
                except OSError:
                    pass
                try:
                    os.execvp(shell, [shell, '--login'])
                finally:
                    os._exit(1)

            self.pid = pid
            self.master_fd = master_fd
            _set_winsize(master_fd, DEFAULT_ROWS, DEFAULT_COLS)

            self._reader = threading.Thread(
                target=self._read_loop, args=(master_fd,),
                name='pty-reader', daemon=True)
            self._reader.start()
            log.info('terminal shell started (pid=%d)', pid)

    def stop(self):
        with self._lock:
            pid = self.pid
            self._teardown_locked()
        if pid:
            try:
                os.killpg(os.getpgid(pid), signal.SIGHUP)
            except OSError:
                pass

    def _teardown_locked(self):
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.master_fd = None
        self.pid = None

    # ── I/O ───────────────────────────────────────────────────────────────

    def _read_loop(self, fd):
        """Fan PTY output out to every viewer until the shell exits.

        Bound to the fd it was started with so that a shell which exits and is
        later restarted cannot have two readers fighting over one descriptor.
        """
        while True:
            try:
                readable, _, _ = select.select([fd], [], [], 0.2)
                if not readable:
                    continue
                data = os.read(fd, READ_SIZE)
            except (OSError, ValueError):
                break
            if not data:
                break
            self._broadcast(data)

        log.info('terminal shell ended')
        with self._lock:
            if self.master_fd == fd:
                self._teardown_locked()

    def _broadcast(self, data):
        with self._lock:
            clients = list(self._clients)

        dead = []
        for client in clients:
            try:
                client.send(data)
            except Exception:
                dead.append(client)

        if dead:
            with self._lock:
                self._clients = [c for c in self._clients if c not in dead]

    def write(self, data):
        fd = self.master_fd
        if fd is None:
            return
        try:
            os.write(fd, data)
        except OSError:
            pass

    def resize(self, rows, cols):
        fd = self.master_fd
        if fd is None:
            return
        rows = max(1, min(int(rows), MAX_ROWS))
        cols = max(1, min(int(cols), MAX_COLS))
        _set_winsize(fd, rows, cols)

    # ── viewers ───────────────────────────────────────────────────────────

    def attach(self, client):
        with self._lock:
            self._clients.append(client)
            count = len(self._clients)
        log.debug('terminal client attached (%d total)', count)

    def detach(self, client):
        with self._lock:
            self._clients = [c for c in self._clients if c is not client]
            count = len(self._clients)
        log.debug('terminal client detached (%d total)', count)

    @property
    def viewers(self):
        with self._lock:
            return len(self._clients)


def _set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack('HHHH', rows, cols, 0, 0))
    except OSError:
        pass
