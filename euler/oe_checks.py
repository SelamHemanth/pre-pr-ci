#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/oe_checks.py
# Running openEuler's own static checks from their repository
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Run the checks openEuler's CI runs, using openEuler's own code.

The six scripts under ``euler/hulk_robot_test`` are what the real gate
executes, so reimplementing them here would only create a second opinion
that drifts from the one that decides whether a patch is accepted.  This
adapter runs them unmodified and translates their result into a verdict.

Their own wrappers cannot be reused.  checkcustom.sh expects Jenkins: a
BUILD_ID, a ``pr-<BUILD_ID>`` branch, a kernel cached under /mnt, gitcode
credentials, and a second repository (openeuler-jenkins) cloned alongside.
What it does underneath is much simpler, and that is what this reproduces:
point the scripts at a kernel tree, hand them a list of commits, and read
the counts they print.

Two details are worked around rather than patched, so the submodule stays
exactly as upstream published it:

  * The scripts read a ``config`` file that sits inside their own tree and
    is tracked by git.  Writing to it would leave the submodule permanently
    dirty, so the tree is copied into the work directory first.

  * check_conflict.py uploads an HTML diff to a Huawei bucket and needs the
    ``obs`` SDK to import at all.  A stub stands in for it and keeps the
    diff on disk, where it is more use to somebody running this locally.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys

#: Script and the stream its verdict comes out on.  The scripts do not
#: agree, and checkcustom.sh reads each from the one it happens to use.
CHECKS = {
    'checkpatch': ('pr_checkpatch.py', 'stderr'),
    'checkformat': ('format.py', 'stderr'),
    'checkdepend': ('depend.py', 'stderr'),
    'checkkabi': ('kabi_keyword_check.py', 'stdout'),
    'checkconflict': ('check_conflict.py', 'stderr'),
    'checkbinary': ('check_binary.py', 'stdout'),
}

#: Checks that read linux_path and the branch names out of the config file.
NEEDS_CONFIG = ('checkformat', 'checkdepend', 'checkconflict')

#: The four shapes the scripts report in.  They do not agree, and reading
#: the wrong one turns a failure into a pass, so all four are handled:
#:
#:   pr_checkpatch  total:100 failed:8 warning:0 success:92
#:   check_conflict total: 100 failed: 31 success: 69   (spaces, and the
#:                  failed key is absent entirely when nothing failed)
#:   format, kabi   check 100 patch(es) success
#:   depend         no counts at all, just "check failed: <commit>" lines
_COUNT_RE = re.compile(r'(failed|warning):\s*(\d+)')
_ALL_CLEAR_RE = re.compile(r'check \d+ (?:patch\(es\)|file\(s\)) success')
_TOTAL_OK_RE = re.compile(r'total:\s*\d+\s+success:\s*\d+')
_DEPEND_FAIL_RE = re.compile(r'^\s*check failed:')
_BANNER_RE = re.compile(r'----\s*results?\s*----')

OBS_STUB = '''"""Stand-in for the Huawei OBS SDK.

check_conflict.py renders a side-by-side diff as HTML and uploads it so the
PR comment can link to it.  Running locally there is nobody to link for, and
requiring cloud credentials to find out whether a backport conflicts would
be absurd, so the upload is replaced by writing the file where the caller
asked and reporting back the path it went to.
"""

import os


class _Body(object):
    def __init__(self, url):
        self.objectUrl = url


class _Response(object):
    def __init__(self, url):
        self.status = 200
        self.body = _Body(url)


class ObsClient(object):
    def __init__(self, **kwargs):
        self.out = os.environ.get('OE_CONFLICT_DIR', '.')

    def putContent(self, bucket, name, content=None):
        os.makedirs(self.out, exist_ok=True)
        path = os.path.join(self.out, os.path.basename(name))
        with open(path, 'w', errors='replace') as handle:
            handle.write(content or '')
        return _Response(path)

    def close(self):
        pass
'''


def _say(message):
    print(message, flush=True)


def _run(args, cwd=None, env=None):
    """Run a command, relaying its output as it appears.

    These checks walk a hundred commits and call git for each one, so
    collecting the output and printing it at the end would leave the job log
    empty for minutes at a time.
    """
    process = subprocess.Popen(
        args, cwd=cwd, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
    lines = []
    for line in process.stdout:
        line = line.rstrip()
        lines.append(line)
        _say('  %s' % line)
    process.stdout.close()
    process.wait()
    return process.returncode, lines


def _git(args, cwd):
    out = subprocess.run(['git'] + args, cwd=cwd, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, universal_newlines=True)
    return out.stdout.strip()


def prepare(source, workdir, mirror, branch):
    """Copy their static_checking tree somewhere writable and configure it.

    Returns the directory the copy lives in, or None when the submodule has
    not been checked out.
    """
    tree = os.path.join(source, 'openEuler', 'lib', 'static_checking')
    if not os.path.isdir(os.path.join(tree, 'scripts')):
        return None

    target = os.path.join(workdir, 'oe-static-checking')
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(tree, target)

    # commons/__init__.py reads this at import time and raises if a key is
    # absent, so every key has to be present even when the check ignores it.
    with open(os.path.join(target, 'config'), 'w') as handle:
        handle.write('[checkdepend]\n')
        handle.write('linux_path = %s\n' % mirror)
        handle.write('target_branch = %s\n' % branch)
        handle.write('source_branch = %s\n' % branch)

    with open(os.path.join(target, 'obs.py'), 'w') as handle:
        handle.write(OBS_STUB)

    return target


def have_commits(kernel, count):
    out = _git(['rev-list', '--count', '-n', str(count), 'HEAD'], cwd=kernel)
    try:
        return int(out)
    except ValueError:
        return 0


class PrBranch(object):
    """A ``pr-<BUILD_ID>`` branch, because that is what their scripts read.

    The scripts accept a bare list of commits, and using it is tempting, but
    depend.py treats that as the single-patch case and then runs ``git log
    <branch>...`` across the whole tree, which on a real kernel produces
    megabytes of output and dies decoding a commit subject that is not
    UTF-8.  Under Jenkins it never takes that path, because -s is always
    passed.  Taking the same path they do avoids the whole area.

    Only a ref is created.  The working tree and the checked-out branch are
    left alone, and the ref is removed again afterwards.
    """

    def __init__(self, kernel):
        self.kernel = kernel
        self.build_id = 'prci%d' % os.getpid()
        self.name = 'pr-%s' % self.build_id

    def __enter__(self):
        subprocess.run(['git', 'branch', '-f', self.name, 'HEAD'],
                       cwd=self.kernel, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return self

    def __exit__(self, *exc):
        subprocess.run(['git', 'branch', '-D', self.name], cwd=self.kernel,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return False


def verdict(lines):
    """Turn their printed counts into (status, detail).

    Anything unrecognised is reported as an error rather than a pass: a
    check that produced no result line did not run, and calling that success
    is how a broken gate goes unnoticed.
    """
    failed = warned = None
    listed = 0
    banner = False

    for line in lines:
        if _BANNER_RE.search(line):
            banner = True
        if _DEPEND_FAIL_RE.search(line):
            listed += 1
        for kind, value in _COUNT_RE.findall(line):
            if kind == 'failed':
                failed = int(value)
            else:
                warned = int(value)
        if _ALL_CLEAR_RE.search(line) or _TOTAL_OK_RE.search(line):
            failed = failed or 0
            warned = warned or 0

    # depend.py names each failure and never counts them, so the names are
    # the count.  Its silence under the banner is what passing looks like.
    if failed is None and listed:
        failed = listed
    if failed is None and banner:
        failed = 0

    if failed is None:
        return 'error', 'no result line; the check did not finish'
    if failed:
        return 'fail', '%d failed' % failed
    if warned:
        return 'warn', '%d warning(s)' % warned
    return 'pass', 'all clear'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=sorted(CHECKS))
    parser.add_argument('--source', required=True,
                        help='the hulk_robot_test submodule')
    parser.add_argument('--kernel', required=True,
                        help='kernel tree holding the commits under test')
    parser.add_argument('--workdir', required=True)
    parser.add_argument('--mirror', default='',
                        help='bare mainline mirror, for the checks that '
                             'resolve upstream commits')
    parser.add_argument('--branch', default='master')
    parser.add_argument('--count', type=int, default=5)
    args = parser.parse_args()

    script, stream = CHECKS[args.check]

    if not os.path.isdir(os.path.join(args.kernel, '.git')):
        _say('%s is not a git tree.' % args.kernel)
        return 2

    if args.check in NEEDS_CONFIG and not args.mirror:
        _say('%s resolves commits against mainline and needs --mirror.'
             % args.check)
        return 2

    tree = prepare(args.source, args.workdir, args.mirror, args.branch)
    if tree is None:
        _say('The hulk_robot_test submodule is empty.')
        _say('Run: git submodule update --init euler/hulk_robot_test')
        return 2

    available = have_commits(args.kernel, args.count)
    if not available:
        _say('No commits to check.')
        return 3
    if available < args.count:
        _say('Only %d commit(s) available; checking those.' % available)
        args.count = available

    env = dict(os.environ)
    # The stub obs module and their commons package both have to be
    # importable, and the scripts add their own directory to sys.path only
    # after the imports at the top of the file have already run.
    env['PYTHONPATH'] = os.pathsep.join(
        [tree, os.path.join(tree, 'scripts'), env.get('PYTHONPATH', '')])
    env['OE_CONFLICT_DIR'] = os.path.join(args.workdir, 'logs', 'conflicts')

    _say('Running openEuler %s over %d commit(s) in %s'
         % (args.check, args.count, args.kernel))
    _say('(%s, reading results from %s)' % (script, stream))

    with PrBranch(args.kernel) as pr:
        env['BUILD_ID'] = pr.build_id
        code, lines = _run(
            [sys.executable, os.path.join(tree, 'scripts', script),
             '-s', 'HEAD~%d' % args.count],
            cwd=args.kernel, env=env)

    status, detail = verdict(lines)
    _say('')
    _say('openEuler %s: %s (%s, exit %d)' % (args.check, status, detail, code))

    return {'pass': 0, 'warn': 0, 'fail': 1, 'error': 2}[status]


if __name__ == '__main__':
    sys.exit(main())
