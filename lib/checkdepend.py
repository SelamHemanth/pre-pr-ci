#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/checkdepend.py
# Dependency checker — verify upstream commit dependencies for backported patches
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Report upstream commits that a set of backported commits depends on.

For every commit being backported, mainline is searched for later commits that
mention it.  Those that reference it with a ``Fixes:`` trailer are real
dependencies and must be backported too; anything else that merely mentions
the hash is reported for information.

Exit status is the result, so callers do not have to grep the output:

    0  every real dependency is already present in the target tree
    1  at least one real dependency is missing
    2  the check could not be run

Used by both anolis/test.sh and euler/test.sh; the two distributions had
byte-identical copies of this file apart from one comment.
"""

import argparse
import os
import re
import subprocess
import sys

#: git's abbreviated hash length, as used by Fixes: trailers.
SHORT = 7

C_RED = '\033[31m'
C_GREEN = '\033[32m'
C_BLUE = '\033[34m'
C_YELLOW = '\033[1;33m'
C_BOLD = '\033[1m'
C_OFF = '\033[0m'

_HASH_RE = re.compile(r'^[0-9a-fA-F]{7,40}$')


class GitError(Exception):
    pass


def git(repo, *args):
    """Run git in ``repo`` and return its stdout.

    Arguments go straight to execve as a list.  The previous version built a
    shell command string out of values read from .commits.txt, which meant a
    crafted file could run arbitrary commands.
    """
    completed = subprocess.run(
        ['git', '-C', repo] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise GitError(
            completed.stderr.decode('utf-8', 'replace').strip()
            or 'git %s exited %d' % (args[0], completed.returncode))
    # Kernel history is not uniformly UTF-8.
    return completed.stdout.decode('utf-8', 'replace')


def resolve(repo, commitish):
    """Return (full_hash, subject) for a commit, or (None, reason)."""
    if commitish.startswith('-'):
        return None, 'not a commit name'
    try:
        # The trailing "--" tells git no pathspec follows, so a hash that also
        # happens to name a file is still read as a revision.
        out = git(repo, 'show', '--no-patch', '--pretty=format:%H%n%s',
                  commitish, '--')
    except GitError as exc:
        return None, str(exc)

    lines = out.splitlines()
    if not lines or not lines[0].strip():
        return None, 'no such commit'
    return lines[0].strip(), (lines[1].strip() if len(lines) > 1 else '')


def mentioning_commits(repo, short):
    """Commits anywhere in ``repo`` whose message mentions ``short``."""
    separator = '\x1e'
    try:
        out = git(repo, 'log', '--all', '-i', '--fixed-strings',
                  '--grep=%s' % short,
                  '--pretty=format:%%H%%x1f%%s%%x1f%%b%s' % separator)
    except GitError as exc:
        raise GitError('searching for %s: %s' % (short, exc))

    found = []
    for entry in out.split(separator):
        if not entry.strip():
            continue
        parts = entry.split('\x1f')
        if len(parts) < 3:
            continue
        found.append((parts[0].strip(), parts[1].strip(), parts[2]))
    return found


def has_fixes_trailer(short, message):
    """True when ``message`` carries a "Fixes: <short>" trailer."""
    return bool(re.search(r'^\s*Fixes:\s*%s' % re.escape(short),
                          message, re.IGNORECASE | re.MULTILINE))


def mentions_uncommented(short, message):
    """True when ``short`` appears on a line that is not commented out."""
    for line in message.splitlines():
        position = line.find(short)
        if position == -1:
            continue
        if '#' in line[:position]:
            continue
        return True
    return False


def already_backported(repo, full_hash, subject):
    """True when ``repo`` already contains the upstream commit.

    Checked by hash first: a backport quotes the upstream hash in its message
    ("commit <hash> upstream"), which is far more reliable than the old
    substring search for the subject, where a short or generic subject could
    match an unrelated commit and hide a missing dependency.
    """
    for length in (12, SHORT):
        try:
            if git(repo, 'log', '--all', '--fixed-strings',
                   '--grep=%s' % full_hash[:length], '--pretty=format:%H').strip():
                return True
        except GitError:
            pass

    if not subject:
        return False
    try:
        subjects = git(repo, 'log', '--all', '--pretty=format:%s')
    except GitError:
        return False
    return any(line.strip() == subject for line in subjects.splitlines())


def read_commits(path):
    with open(path, 'r', errors='replace') as handle:
        return [line.strip() for line in handle if line.strip()]


def prompt_for_input():
    user_repo = input('%sPath to your kernel source:%s ' % (C_BOLD, C_OFF)).strip()
    stable_repo = input('%sPath to the mainline mirror:%s ' % (C_BOLD, C_OFF)).strip()
    print('%sCommits, one per line. Type "done" when finished:%s' % (C_BOLD, C_OFF))
    commits = []
    while True:
        line = input().strip()
        if line.lower() == 'done':
            break
        if line:
            commits.append(line)
    return user_repo, stable_repo, commits


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('user_repo', nargs='?',
                        help='kernel tree the patches were applied to')
    parser.add_argument('stable_repo', nargs='?',
                        help='bare mirror of mainline to search')
    parser.add_argument('commits_file', nargs='?',
                        help='file of upstream commit hashes, one per line')
    parser.add_argument('--output-dir', default=None,
                        help='where to write .dep_log and .full_commits '
                             '(default: the current directory)')
    args = parser.parse_args()

    if args.user_repo and args.stable_repo and args.commits_file:
        user_repo, stable_repo = args.user_repo, args.stable_repo
        try:
            commits = read_commits(args.commits_file)
        except OSError as exc:
            print('Cannot read %s: %s' % (args.commits_file, exc),
                  file=sys.stderr)
            return 2
    elif not any((args.user_repo, args.stable_repo, args.commits_file)):
        user_repo, stable_repo, commits = prompt_for_input()
    else:
        parser.error('give all three of user_repo, stable_repo and commits_file')

    for label, path in (('kernel tree', user_repo), ('mainline mirror', stable_repo)):
        if not os.path.isdir(path):
            print('%s%s does not exist: %s%s' % (C_RED, label, path, C_OFF),
                  file=sys.stderr)
            return 2

    if not commits:
        print('No commits to check.')
        return 0

    out_dir = args.output_dir or os.getcwd()
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        print('Cannot use %s: %s' % (out_dir, exc), file=sys.stderr)
        return 2

    dep_log = os.path.join(out_dir, '.dep_log')
    full_commits = os.path.join(out_dir, '.full_commits')

    # Resolve every requested commit before reporting anything, so a typo in
    # the list is visible immediately.
    resolved = []
    with open(full_commits, 'w') as handle:
        for commitish in commits:
            if not _HASH_RE.match(commitish):
                print('%sSkipping %r: not a commit hash%s'
                      % (C_YELLOW, commitish, C_OFF))
                continue
            full_hash, subject = resolve(stable_repo, commitish)
            if not full_hash:
                print('%sSkipping %s: %s%s'
                      % (C_YELLOW, commitish, subject, C_OFF))
                continue
            handle.write('%s %s\n' % (full_hash, subject))
            resolved.append((full_hash, subject))

    if not resolved:
        print('%sNone of the given commits could be found in %s%s'
              % (C_RED, stable_repo, C_OFF))
        return 2

    print('\n%sChecking %d commit(s) for upstream dependencies%s\n'
          % (C_BOLD, len(resolved), C_OFF))

    missing_total = 0
    with open(dep_log, 'w') as log:
        for full_hash, subject in resolved:
            short = full_hash[:SHORT]
            print('%sCommit:%s %s %s' % (C_BLUE, C_OFF, full_hash[:14], subject))

            try:
                candidates = mentioning_commits(stable_repo, short)
            except GitError as exc:
                print('  %sCould not search mainline: %s%s'
                      % (C_RED, exc, C_OFF))
                return 2

            seen = set()
            real, informational = [], []
            for dep_hash, dep_subject, dep_body in candidates:
                if dep_hash.lower() == full_hash.lower() or dep_hash in seen:
                    continue
                seen.add(dep_hash)

                message = dep_subject + '\n' + dep_body
                if has_fixes_trailer(short, message):
                    real.append((dep_hash, dep_subject))
                elif mentions_uncommented(short, message):
                    informational.append((dep_hash, dep_subject))

            for dep_hash, dep_subject in real + informational:
                log.write('%s %s\n' % (dep_hash[:14], dep_subject))

            if not real and not informational:
                print('  %sPASS%s -> nothing upstream refers to it\n'
                      % (C_GREEN, C_OFF))
                continue

            missing = [(h, s) for h, s in real
                       if not already_backported(user_repo, h, s)]
            missing_total += len(missing)

            if missing:
                print('  %sFAIL%s -> %d fix(es) for this commit are not applied'
                      % (C_RED, C_OFF, len(missing)))
            else:
                print('  %sPASS%s -> every fix for this commit is applied'
                      % (C_GREEN, C_OFF))

            for dep_hash, dep_subject in real:
                state = ('%smissing%s' % (C_RED, C_OFF)
                         if (dep_hash, dep_subject) in missing
                         else '%sapplied%s' % (C_GREEN, C_OFF))
                print('    Fixes: %s%s %s%s [%s]'
                      % (C_YELLOW, dep_hash[:14], dep_subject, C_OFF, state))
            for dep_hash, dep_subject in informational:
                print('    mentions: %s %s (no Fixes: trailer)'
                      % (dep_hash[:14], dep_subject))
            print()

    if missing_total:
        print('%s%d dependency commit(s) still need backporting.%s'
              % (C_RED, missing_total, C_OFF))
        print('Details in %s' % dep_log)
        return 1

    print('%sAll upstream dependencies are satisfied.%s' % (C_GREEN, C_OFF))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
