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

The only thing added here is a cache, and the care not to make anyone
wait for it.  The page polls for test status every couple of seconds;
this script takes some seconds per commit -- openEuler renders a diff
to check the Conflicts: sections, Anolis asks the mirror about every
backport -- so it is answered off the request, one run at a time.  See
background.py for what that is protecting against.

What the cache is keyed on does most of the work.  Every input to the
answer is part of the commits themselves, and editing a commit message
gives the commit a new SHA, so the SHA at HEAD changes whenever the
answer could.  The configuration's mtime covers the rest of what the
script reads, and the mirror's FETCH_HEAD covers the one input that is
neither: a commit the mirror had not heard of an hour ago is one it can
confirm now.  With all three in the key there is nothing left for an
expiry to catch, so it is long.
"""

import os
import subprocess

from . import background

#: With the three stamps below in the key, this is only still here
#: because an answer held for a whole day of a page left open would be
#: a surprise to somebody who had changed something this file cannot
#: see.
_TTL = 3600.0

_answers = background.Answer(_TTL)


def _head(kernel):
    try:
        out = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=kernel,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ''
    return out.stdout.decode().strip()


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _stamp(root, distro, kernel, mirror):
    # The config is the same file ready.sh reads, so a change to it
    # invalidates the answer at the moment it could change it, and
    # FETCH_HEAD is written by every fetch of the mirror.
    return (_head(kernel) if kernel else '',
            _mtime(os.path.join(root, distro, '.configure')),
            _mtime(os.path.join(mirror, 'FETCH_HEAD')) if mirror else 0)


#: What a caller is told while the first run is still going.  False,
#: because an unanswered question must not open the gate -- but with a
#: reason that says so rather than one that blames the series.
_UNKNOWN = (False, 'still working out whether the series is prepared')


def check(root, distro, kernel=None, mirror=None, force=False, wait=True):
    """Return (ready, explanation) for one distro.

    ``ready`` is False whenever the question cannot be answered, not
    just when the answer is no: a missing or broken ready.sh must not
    quietly let an unprepared series through to the tests.

    With ``wait`` false this returns immediately -- the answer from the
    last run, or _UNKNOWN before there has been one -- and the run
    happens behind the caller.  That is what the polled endpoints use;
    anything standing in front of a job asks to wait.
    """
    script = os.path.join(root, distro, 'ready.sh')
    if not os.path.exists(script):
        return False, 'no readiness check for %s' % distro

    def run():
        try:
            done = subprocess.run(['bash', script], cwd=root,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=180)
            return (done.returncode == 0,
                    done.stdout.decode('utf-8', 'replace').strip())
        except subprocess.TimeoutExpired:
            return False, 'the readiness check did not finish in time'
        except OSError as exc:
            return False, 'could not run the readiness check: %s' % exc

    return _answers.get(distro, _stamp(root, distro, kernel, mirror), run,
                        wait=wait, force=force, unknown=_UNKNOWN)


def forget(distro=None):
    """Drop the cached answer, after something that could change it."""
    _answers.forget(distro)
