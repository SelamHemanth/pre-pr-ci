#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/oe_ready.py
# Decide whether a series has already been prepared for openEuler
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
"""Tell prepare.sh whether there is anything left to do.

Preparing a series rewrites history: it formats the commits out, edits
every message, rewinds the branch and applies them back.  Doing that to
a series that is already prepared is not harmless.  It costs a rebuild
of everything downstream, and if it is interrupted the branch is left
short of the commits it started with.

So the test has to be exact.  "Has a header" is not enough: a commit
whose Conflicts: section sits in the wrong place looks finished and is
still rejected by the gate, and only another pass will move it.
Report that as work remaining, not as prepared.
"""

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oe_conflict

_INCLUSION_RE = re.compile(r'^[A-Za-z0-9 ]+ inclusion\s*$', re.MULTILINE)


def commits(kernel, count):
    out = subprocess.run(
        ['git', 'log', '--format=%H', '-n', str(count), 'HEAD'],
        cwd=kernel, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return out.stdout.decode().split()


def message(kernel, sha):
    out = subprocess.run(['git', 'log', '-1', '--format=%B', sha],
                         cwd=kernel, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    return out.stdout.decode('utf-8', 'replace')


def unprepared(kernel, sha, msg, signer, mirror):
    """Why this commit still needs a pass, or None if it is ready."""
    if not _INCLUSION_RE.search(msg):
        return 'no inclusion header'
    if signer not in msg:
        return 'no %s' % signer.split(':')[0]

    upstream = oe_conflict.mainline_commit(msg)
    if not upstream:
        return None

    if oe_conflict.already_declared(msg):
        # Present is not the same as acceptable.  Their format check is
        # strict about where the section sits -- the sign-offs have to
        # follow the closing bracket with nothing in between -- and a
        # section in the wrong place reads as done while still being
        # rejected by the gate.
        ok, why = oe_conflict.format_ok(msg)
        return None if ok else why

    # A divergence with no [Backport Changes] note is deliberately left
    # alone by the prepare pass, so it is not work remaining: running
    # again would report the same warning and change nothing.  Whether
    # it needs a Conflicts: section is a judgement about the diff, and
    # the prepare log shows the diff so it can be made.
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', required=True)
    parser.add_argument('--mirror', required=True)
    parser.add_argument('--count', type=int, required=True)
    parser.add_argument('--signer', required=True)
    args = parser.parse_args()

    found = commits(args.kernel, args.count)
    if len(found) < args.count:
        print('only %d commit(s) on the branch, expected %d'
              % (len(found), args.count))
        return 1

    for sha in found:
        msg = message(args.kernel, sha)
        why = unprepared(args.kernel, sha, msg, args.signer, args.mirror)
        if why:
            subject = msg.split('\n', 1)[0]
            print('%s %s: %s' % (sha[:12], subject[:60], why))
            return 1

    print('all %d commit(s) are ready to test' % args.count)
    return 0


if __name__ == '__main__':
    sys.exit(main())
