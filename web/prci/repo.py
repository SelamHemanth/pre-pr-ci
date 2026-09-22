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

log = logging.getLogger(__name__)

CLONE_URL = 'https://github.com/torvalds/linux.git'

#: A full clone of mainline takes a long time on a cold cache; without a cap a
#: wedged network leaves the job appearing to run forever.
CLONE_TIMEOUT = 60 * 60
FETCH_TIMEOUT = 15 * 60


def sync(path, emit=None):
    """Clone or update the mirror at ``path``.

    ``emit`` receives progress lines; it is how this ends up in a job log.
    Returns True when the mirror is usable afterwards.  A failure here is not
    fatal for every caller, so the outcome is reported rather than raised.
    """
    say = emit or (lambda message: log.info('%s', message))

    if not os.path.isdir(path):
        return _clone(path, say)

    say('Updating mirror of mainline in %s' % path)
    result = _git(['fetch', '--all', '--tags'], cwd=path, timeout=FETCH_TIMEOUT)
    for line in result.output.splitlines():
        say('  %s' % line)
    if result.ok:
        say('Mirror is up to date.')
        return True

    say('Fetch failed (exit %s); the mirror looks unusable.' % result.code)

    if not _discard(path, say):
        return False
    return _clone(path, say)


def _clone(path, say):
    say('Cloning mainline into %s (this takes a while on first run)' % path)
    result = _git(['clone', '--bare', CLONE_URL, path], timeout=CLONE_TIMEOUT)
    for line in result.output.splitlines():
        say('  %s' % line)
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


def _git(args, cwd=None, timeout=FETCH_TIMEOUT):
    """Run git, capturing both streams, and never raise on a non-zero exit.

    The exit status is what callers branch on; the previous implementation
    piped fetch into grep and read grep's status instead, so a clean fetch that
    printed nothing looked like a failure and got the mirror deleted.
    """
    try:
        completed = subprocess.run(
            ['git'] + args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
            check=False,
        )
        return _Result(completed.returncode, completed.stdout or '')
    except subprocess.TimeoutExpired:
        return _Result(124, 'git %s timed out' % ' '.join(args))
    except OSError as exc:
        return _Result(127, 'could not run git: %s' % exc)
