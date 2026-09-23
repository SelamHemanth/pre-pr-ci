# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/submodules.py
# Cloning and updating the helper repositories the tests need
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""The KABI tooling and the openEuler spec files live in git submodules.

A plain ``git clone`` of this project leaves those directories empty, and the
tests that need them then fail for reasons that look nothing like the real
cause: kabi-dw is simply not there.  This module reports which of them are
populated and fills in the ones that are not, so the interface can offer it
as a button in the same way it offers a mainline mirror update.
"""

import logging
import os
import subprocess

from . import repo

log = logging.getLogger(__name__)

#: Cloning three repositories over a slow link is not quick, but it is bounded.
SYNC_TIMEOUT = 30 * 60

#: Which tests stop working when a given submodule is missing.  Used to
#: explain the consequence rather than just naming a path.
NEEDED_BY = {
    'anolis/kabi-dw': ('check_kapi',),
    'anolis/kabi-whitelist': ('check_kapi',),
    # euler/kernel is src-openeuler/kernel, which carries check-kabi and
    # the ABI whitelists.  Only the two architectures openEuler ships
    # compare against them.
    'euler/kernel': ('oe_build_x86_64', 'oe_build_aarch64'),
    # hulk_robot_test carries the six checks, the build matrix and the
    # cross toolchains, so every euler test needs it.
    'euler/hulk_robot_test': ('oe_checkpatch', 'oe_checkformat',
                              'oe_checkdepend', 'oe_checkkabi',
                              'oe_checkconflict', 'oe_checkbinary',
                              'oe_build_x86_64', 'oe_build_aarch64',
                              'oe_build_arm', 'oe_build_ppc',
                              'oe_build_ppc64', 'oe_build_riscv64',
                              'oe_build_loongarch'),
}


def declared(root):
    """Submodule paths listed in .gitmodules, in file order.

    Read from .gitmodules rather than hardcoded, so adding a submodule to the
    project does not need a matching edit here.
    """
    path = os.path.join(root, '.gitmodules')
    found = []
    try:
        with open(path, 'r', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if line.startswith('path'):
                    _, _, value = line.partition('=')
                    value = value.strip()
                    if value:
                        found.append(value)
    except OSError:
        return []
    return found


def is_populated(root, sub):
    """True when ``sub`` has actually been checked out.

    An uninitialised submodule is present as an empty directory, so the
    directory existing is not enough to go on.
    """
    target = os.path.join(root, sub)
    if not os.path.isdir(target):
        return False
    try:
        return any(os.scandir(target))
    except OSError:
        return False


def status(root):
    """Per-submodule state for the interface.

    Returns a list of dicts in .gitmodules order, each with the path, whether
    it is populated, and which tests depend on it.
    """
    return [
        {
            'path': sub,
            'name': os.path.basename(sub),
            'present': is_populated(root, sub),
            'needed_by': list(NEEDED_BY.get(sub, ())),
        }
        for sub in declared(root)
    ]


def missing(root):
    """Paths of the submodules that are declared but not checked out."""
    return [s['path'] for s in status(root) if not s['present']]


def sync(root, emit=None):
    """Check out or update every submodule.

    Returns True when all of them are populated afterwards.  As with the
    mainline mirror, the outcome is reported rather than raised: a missing
    helper repository only breaks some of the tests, and the rest of the tool
    should stay usable.
    """
    say = emit or repo.to_stdout

    subs = status(root)
    if not subs:
        say('No submodules are declared in .gitmodules; nothing to do.')
        return True

    absent = [s for s in subs if not s['present']]
    if absent:
        say('Fetching %d sub-repository/ies: %s'
            % (len(absent), ', '.join(s['path'] for s in absent)))
        say('The first fetch pulls the full history of each and is not quick.')
    else:
        say('All %d sub-repositories are present; checking for updates.'
            % len(subs))

    # --init creates the ones never checked out, --recursive covers nested
    # submodules, and --remote is deliberately absent: the pinned commit is
    # what the tests were written against, so tracking the upstream branch
    # would silently change what check_kapi compares.
    result = repo._git(
        ['submodule', 'update', '--init', '--recursive', '--progress'],
        cwd=root, timeout=SYNC_TIMEOUT, say=say)

    if not result.ok:
        say('git submodule update failed (exit %s).' % result.code)
        _explain(root, say)
        return False

    still_missing = missing(root)
    if still_missing:
        say('These are still empty: %s' % ', '.join(still_missing))
        _explain(root, say)
        return False

    say('All sub-repositories are ready.')
    return True


#: The submodule carrying openEuler's own checks.
OE_CHECKS = 'euler/hulk_robot_test'


def revision(root, sub=OE_CHECKS):
    """Short commit and subject of a submodule, for showing in the UI."""
    result = repo._git(['log', '-1', '--format=%h  %s'],
                       cwd=os.path.join(root, sub), timeout=30)
    return result.output.strip() if result.ok else ''


def update_remote(root, sub=OE_CHECKS, emit=None):
    """Move one submodule to the tip of the branch its upstream publishes.

    Only this one.  The others stay pinned on purpose: check_kapi compares
    against the kabi-dw and whitelist versions it was written for, so
    tracking those would quietly change what the test means.  This one is
    the opposite case -- it holds the checks the real gate runs, and a gate
    pinned a year ago is not the gate -- so keeping up with it is the point.
    """
    say = emit or repo.to_stdout

    before = revision(root, sub)
    if before:
        say('Currently at %s' % before)

    result = repo._git(['submodule', 'update', '--remote', '--init',
                        '--progress', '--', sub],
                       cwd=root, timeout=SYNC_TIMEOUT, say=say)
    if not result.ok:
        say('Could not update %s (exit %s).' % (sub, result.code))
        return False

    after = revision(root, sub)
    if after and after == before:
        say('Already up to date.')
    else:
        say('Now at %s' % after)
        say('The checked-out commit changed, so commit the new pointer to')
        say('keep everyone on the same checks: git add %s' % sub)
    return True


def _explain(root, say):
    """Say what the failure costs, so the log is actionable."""
    for sub in status(root):
        if sub['present'] or not sub['needed_by']:
            continue
        say('  %s is missing, so these cannot run: %s'
            % (sub['path'], ', '.join(sub['needed_by'])))
    say('These repositories are on gitee.com and atomgit.com; a build machine')
    say('behind a proxy may need that configured for git before this works.')
