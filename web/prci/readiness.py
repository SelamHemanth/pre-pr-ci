# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/readiness.py
# Ask the distro whether its series is prepared
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
"""Whether the series has been prepared, as the UI needs to know it.

The answer itself belongs to the distro -- ``<distro>/ready.sh`` is what
``make prepare`` and ``make test`` both consult, and the UI has to agree
with them or it will offer a button that the script then refuses.  So
this runs that same script rather than reimplementing the rules.

The only thing added here is a cache.  The page polls for test status
every couple of seconds, and on openEuler the script renders a diff per
commit to check the Conflicts: sections, which is not something to do on
a timer.

What the cache is keyed on does most of the work.  Every input to the
answer is part of the commits themselves, and editing a commit message
gives the commit a new SHA, so the SHA at HEAD changes whenever the
answer could.  Together with the configuration's mtime that covers
everything except an updated upstream mirror, which is what the
expiry is for.
"""

import os
import subprocess
import time

#: A backstop, not the main mechanism: the key catches every change to
#: the commits, so this only has to catch a mirror that has been
#: fetched since.  Work in another terminal shows up immediately
#: regardless, because it moves HEAD.
_TTL = 300.0

_cache = {}


def _head(kernel):
    try:
        out = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=kernel,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.stdout.decode().strip()


def _stamp(root, distro, kernel):
    config = os.path.join(root, distro, '.configure')
    # Same file ready.sh reads, so a config change invalidates the
    # cached answer at the moment it could change it.
    try:
        mtime = os.path.getmtime(config)
    except OSError:
        mtime = 0
    return (_head(kernel) if kernel else '', mtime)


def check(root, distro, kernel=None, force=False):
    """Return (ready, explanation) for one distro.

    ``ready`` is False whenever the question cannot be answered, not
    just when the answer is no: a missing or broken ready.sh must not
    quietly let an unprepared series through to the tests.
    """
    script = os.path.join(root, distro, 'ready.sh')
    if not os.path.exists(script):
        return False, 'no readiness check for %s' % distro

    stamp = _stamp(root, distro, kernel)
    hit = _cache.get(distro)
    if not force and hit and hit[0] == stamp and time.time() - hit[1] < _TTL:
        return hit[2]

    try:
        done = subprocess.run(['bash', script], cwd=root,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=180)
        answer = (done.returncode == 0,
                  done.stdout.decode('utf-8', 'replace').strip())
    except subprocess.TimeoutExpired:
        answer = (False, 'the readiness check did not finish in time')
    except OSError as exc:
        answer = (False, 'could not run the readiness check: %s' % exc)

    _cache[distro] = (stamp, time.time(), answer)
    return answer


def forget(distro=None):
    """Drop the cached answer, after something that could change it."""
    if distro is None:
        _cache.clear()
    else:
        _cache.pop(distro, None)
