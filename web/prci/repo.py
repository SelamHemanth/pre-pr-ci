# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/repo.py
# Keeping the local bare mirror of Linus' tree up to date
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Maintenance of ``.torvalds-linux``, the bare mirror the dependency check
resolves upstream commits against.
"""

import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

CLONE_URL = 'https://github.com/torvalds/linux.git'

#: A full clone of mainline takes a long time on a cold cache; without a cap a
#: wedged network leaves the job appearing to run forever.
CLONE_TIMEOUT = 60 * 60
FETCH_TIMEOUT = 15 * 60


def to_stdout(message):
    """Default progress sink: the job log is this process's stdout.

    Not a logger.  These functions run as a subprocess whose output *is* the
    log the interface shows, and log.info() with no configured handler threw
    every line away, so both the mirror and sub-repository jobs produced a
    log containing nothing but the command line and the exit code.
    """
    print(message, flush=True)


def sync(path, emit=None):
    """Clone or update the mirror at ``path``.

    ``emit`` receives progress lines; it is how this ends up in a job log.
    Returns True when the mirror is usable afterwards.  A failure here is not
    fatal for every caller, so the outcome is reported rather than raised.
    """
    say = emit or to_stdout

    if not os.path.isdir(path):
        return _clone(path, say)

    say('Updating mirror of mainline in %s' % path)
    result = _git(['fetch', '--all', '--tags', '--progress'],
                  cwd=path, timeout=FETCH_TIMEOUT, say=say)
    if result.ok:
        say('Mirror is up to date.')
        return True

    say('Fetch failed (exit %s); the mirror looks unusable.' % result.code)

    if not _discard(path, say):
        return False
    return _clone(path, say)


def _clone(path, say):
    say('Cloning mainline into %s (this takes a while on first run)' % path)
    result = _git(['clone', '--bare', '--progress', CLONE_URL, path],
                  timeout=CLONE_TIMEOUT, say=say)
    if not result.ok:
        say('Clone failed (exit %s). Dependency checks will be skipped.'
            % result.code)
        return False

    # Without this, git refuses to read the mirror when the web server runs as
    # a different user than the one that created it.
    _git(['config', '--global', '--add', 'safe.directory', path])
    say('Clone complete.')
    return True


def _discard(path, say):
    """Remove a broken mirror, but only if we own it.

    A root-owned mirror is left alone on purpose: escalating with sudo from a
    web request is not something this server should be able to do.
    """
    try:
        owner = os.stat(path).st_uid
    except OSError as exc:
        say('Cannot stat %s: %s' % (path, exc))
        return False

    if owner != os.geteuid():
        say('%s is owned by uid %d, not by this server. Remove it by hand:'
            % (path, owner))
        say('    sudo rm -rf %s' % path)
        return False

    try:
        shutil.rmtree(path)
    except OSError as exc:
        say('Could not remove %s: %s' % (path, exc))
        return False

    say('Removed the stale mirror.')
    return True


class _Result:
    def __init__(self, code, output):
        self.code = code
        self.output = output

    @property
    def ok(self):
        return self.code == 0


def _git(args, cwd=None, timeout=FETCH_TIMEOUT, say=None):
    """Run git, never raising on a non-zero exit, streaming output to ``say``.

    The exit status is what callers branch on; the previous implementation
    piped fetch into grep and read grep's status instead, so a clean fetch that
    printed nothing looked like a failure and got the mirror deleted.

    Output is relayed line by line as git produces it rather than collected
    and printed at the end.  A bare clone of mainline runs for many minutes,
    and a log that stays empty for all of them is indistinguishable from one
    that has hung.  git writes progress with carriage returns, so those are
    split too, otherwise the counter arrives as one enormous line.
    """
    deadline = time.monotonic() + timeout
    lines = []
    try:
        process = subprocess.Popen(
            ['git'] + args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
    except OSError as exc:
        return _Result(127, 'could not run git: %s' % exc)

    try:
        for raw in process.stdout:
            if time.monotonic() > deadline:
                process.kill()
                return _Result(124, 'git %s timed out' % ' '.join(args))
            for line in raw.replace('\r', '\n').splitlines():
                line = line.rstrip()
                if not line:
                    continue
                lines.append(line)
                if say:
                    say('  %s' % line)
        process.wait(timeout=max(1, int(deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        process.kill()
        return _Result(124, 'git %s timed out' % ' '.join(args))
    finally:
        if process.stdout:
            process.stdout.close()

    return _Result(process.returncode, '\n'.join(lines))
