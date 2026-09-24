#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - anolis/upstream_ref.py
# The backport provenance line Anolis puts in a commit message
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# A backported commit in Anolis says where it came from, on its own
# line between the ANBZ tag and the body.  From cloud-kernel !13995,
# merged with their review check green:
#
#     iommu/amd: Add SNP page mode 0 support
#
#     ANBZ: #48382
#
#     commit cb2860ad6c4ff7e15bb69c7e3a6842bbea743229 upstream.
#
#     Newer AMD IOMMUs supports DTE[Mode]=0 for SNP-enabled system.
#     ...
#     Signed-off-by: Joerg Roedel <joerg.roedel@amd.com>
#     Signed-off-by: mohanasv <mohanasv@amd.com>
#
# The full forty characters, and the trailing period is part of it.
#
# Original work has no such line, because there is no commit to point
# at.  So this never demands one of everything: it asks the mirror
# whether the patch exists upstream, and only says something when the
# answer is yes and the line is missing.

import argparse
import os
import re
import subprocess
import sys

#: The line itself.  Anolis writes the full sha; accepting a short one
#: on the way in means a message that says less than it could is
#: reported rather than silently passed.
UPSTREAM_RE = re.compile(r'^commit ([0-9a-f]{7,40}) upstream\.\s*$',
                         re.MULTILINE)

#: Where it goes: after the ANBZ tag, with a blank line either side.
#: The trailing class is spaces and tabs, not \s: \s matches newlines,
#: so a greedy \s*$ under MULTILINE runs past the blank line after the
#: tag and the insert lands one line too far down.
ANBZ_RE = re.compile(r'^ANBZ:[ \t]*#\d+[ \t]*$', re.MULTILINE)


class Unresolved(Exception):
    """No upstream commit could be identified for this patch."""


def _git(repo, *args):
    done = subprocess.run(('git', '-C', repo) + args,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return done.stdout.decode('utf-8', 'replace')


def declared_sha(message):
    """The sha the message already points at, if it points at one."""
    found = UPSTREAM_RE.search(message)
    return found.group(1) if found else None


def cherry_picked_sha(message):
    """What "git cherry-pick -x" recorded, if the carrier used it.

    Cheaper and more certain than matching subjects, and it is right
    even when the subject was reworded on the way down.
    """
    found = re.search(r'^\(cherry picked from commit ([0-9a-f]{40})\)\s*$',
                      message, re.MULTILINE)
    return found.group(1) if found else None


def upstream_sha_for(mirror, subject):
    """The upstream commit with this exact subject, or None.

    Subject is what there is to go on once a patch has been rebased and
    had its message edited; the diff will not match after a conflict
    resolution, but the subject of a backport is kept.  An ambiguous
    answer is no answer: two upstream commits with one subject means
    guessing which was meant, and a wrong sha in a commit message is
    worse than none.
    """
    if not subject.strip():
        return None
    out = _git(mirror, 'log', '--format=%H', '--fixed-strings',
               '--grep=%s' % subject, '--all', '-n', '20')
    matches = [line for line in out.split()
               if _git(mirror, 'log', '-1', '--format=%s', line).strip()
               == subject.strip()]
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def line_for(sha):
    """The line as Anolis writes it."""
    return 'commit %s upstream.' % sha


def insert_into(message, sha):
    """Put the line after the ANBZ tag, blank line either side.

    Anolis's order is subject, ANBZ, provenance, body, trailers, and
    putting it anywhere else would be a message that says the right
    thing in the wrong place.
    """
    if declared_sha(message):
        return message
    found = ANBZ_RE.search(message)
    if not found:
        raise Unresolved('no ANBZ tag to put the upstream line after')
    head = message[:found.end()]
    tail = message[found.end():].lstrip('\n')
    return '%s\n\n%s\n\n%s' % (head, line_for(sha), tail)


def _resolve(mirror, message, subject):
    """The sha this patch came from, by the cheapest route that works."""
    sha = cherry_picked_sha(message)
    if sha:
        return sha
    return upstream_sha_for(mirror, subject)


def patch_subject(text):
    """The subject of a format-patch file, unwrapped and unprefixed.

    "Subject:" can be folded across lines and carries a [PATCH n/m]
    prefix that is not part of the commit's subject at all, so neither
    survives into what gets matched against the mirror.
    """
    lines = text.split('\n')
    subject = None
    for index, line in enumerate(lines):
        if not line.startswith('Subject:'):
            continue
        parts = [line[len('Subject:'):].strip()]
        for folded in lines[index + 1:]:
            if not folded[:1] in (' ', '\t'):
                break
            parts.append(folded.strip())
        subject = ' '.join(parts)
        break
    if subject is None:
        return ''
    return re.sub(r'^\[[^\]]*\]\s*', '', subject).strip()


def rewrite_patch(path, mirror):
    """Put the line into a patch file, between its ANBZ tag and body.

    Returns the sha used, or None if the patch is not a backport or
    already says so.  Rewrites in place only when something changed.
    """
    with open(path, encoding='utf-8', errors='replace') as handle:
        text = handle.read()
    if declared_sha(text):
        return None
    sha = _resolve(mirror, text, patch_subject(text))
    if not sha:
        return None
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(insert_into(text, sha))
    return sha


def main():
    parser = argparse.ArgumentParser(
        description='The "commit <sha> upstream." line Anolis backports carry')
    parser.add_argument('--mirror', required=True,
                        help='a clone of mainline, to resolve the sha against')
    parser.add_argument('--kernel', help='the tree to read commits from')
    parser.add_argument('--count', type=int, default=0,
                        help='how many commits back to look')
    parser.add_argument('--subject', help='resolve this one subject and stop')
    parser.add_argument('--patch', help='rewrite this patch file in place')
    args = parser.parse_args()

    if not os.path.isdir(os.path.join(args.mirror, '.git')) \
            and not os.path.isdir(os.path.join(args.mirror, 'objects')):
        sys.stderr.write('%s is not a git mirror\n' % args.mirror)
        return 2

    # One patch file, for prepare.sh to fix as it goes.  Silence means
    # original work, which carries no such line and needs none.
    if args.patch:
        try:
            sha = rewrite_patch(args.patch, args.mirror)
        except Unresolved as why:
            sys.stderr.write('%s: %s\n' % (args.patch, why))
            return 2
        if sha:
            print(sha)
        return 0

    # One subject, for prepare.sh to ask about a single patch.
    if args.subject:
        sha = upstream_sha_for(args.mirror, args.subject)
        if not sha:
            return 1
        print(sha)
        return 0

    if not args.kernel or args.count < 1:
        sys.stderr.write('need --kernel and --count, or --subject\n')
        return 2

    # Every commit in the range, for ready.sh to gate on.
    missing = 0
    shas = _git(args.kernel, 'log', '--format=%H', '-n',
                str(args.count), 'HEAD').split()
    for sha in shas:
        message = _git(args.kernel, 'log', '-1', '--format=%B', sha)
        subject = _git(args.kernel, 'log', '-1', '--format=%s', sha).strip()
        if declared_sha(message):
            continue
        upstream = _resolve(args.mirror, message, subject)
        if upstream:
            print('%s %s: no "commit %s upstream." line'
                  % (sha[:12], subject[:52], upstream))
            missing += 1
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
