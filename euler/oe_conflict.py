#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/oe_conflict.py
# Find where a backport diverges from upstream, the way openEuler does
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
"""Decide whether a backport differs from the commit it claims to be.

openEuler's check_conflict.py takes the SHA out of the "mainline
inclusion" header, renders both that commit and yours as diffs, and
compares them byte for byte.  If they differ at all, the commit message
has to carry a Conflicts: section naming every file that differs and
explaining why, or the patch is rejected.

Everything here reproduces their rendering exactly, because "byte for
byte" means a comparison that is nearly the same is not the same at all:
five lines of context rather than three, index lines dropped, and the
text between @@ markers blanked.  Get any of that wrong and we either
invent conflicts that do not exist or miss the ones that do.
"""

import os
import re
import subprocess

#: Dropped before comparing: blob SHAs differ between any two trees and
#: say nothing about whether the change is the same.
_INDEX_RE = re.compile(r'index [a-f0-9]{12,40}\.\.[a-f0-9]{12,40}( \d{6})?')

#: The text after the line numbers in a hunk header is the enclosing
#: function, which moves around between trees for reasons that are not
#: the patch's doing.  They blank it; so do we.
_AT_RE = re.compile(r'@@.*@@')

#: How their get_mainline_commit reads the header: the first line
#: beginning with "commit" after an inclusion line.
_INCLUSION_RE = re.compile(r'^(mainline|stable) inclusion\s*$', re.MULTILINE)
_COMMIT_RE = re.compile(r'^commit\s+([0-9a-f]{8,40})', re.MULTILINE)


def _run(args, cwd):
    try:
        done = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
    except OSError:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode('utf-8', 'replace')


def mainline_commit(message):
    """The upstream SHA their checker will compare against, or None."""
    found = _INCLUSION_RE.search(message)
    if not found:
        return None
    after = message[found.end():]
    commit = _COMMIT_RE.search(after)
    return commit.group(1) if commit else None


def rendered_diff(repo, commit, path=None):
    """One commit as their checker renders it for comparison."""
    args = ['git', 'show', '-U5', commit, '--pretty=format:']
    if path:
        args += ['--', path]
    out = _run(args, repo)
    if not out:
        return None

    lines = []
    for line in out.strip('"').split('\n'):
        if _INDEX_RE.match(line):
            continue
        if line.startswith('@@ '):
            lines.append(_AT_RE.sub('', line))
        else:
            lines.append(line)
    return '\n'.join(lines) + '\n'


def _touched(repo, commit):
    out = _run(['git', 'show', '--name-only', commit, '--pretty=format:'],
               repo)
    return [f for f in (out or '').split('\n') if f.strip()]


def differing_files(kernel, commit, mirror, upstream):
    """Files whose change here is not the change made upstream.

    The union of what both commits touch, minus the ones that came
    across unchanged.  A file only one of them touches counts as
    differing, which is how a backport that drops a hunk gets caught.
    """
    files = set(_touched(kernel, commit)) | set(_touched(mirror, upstream))

    out = []
    for name in sorted(files):
        here = rendered_diff(kernel, commit, name)
        there = rendered_diff(mirror, upstream, name)
        if here is None or there is None or here != there:
            out.append(name)
    return out


def deviates(kernel, commit, mirror, upstream):
    """Whether their checker would call this a conflict at all."""
    here = rendered_diff(kernel, commit)
    there = rendered_diff(mirror, upstream)
    if here is None or there is None:
        return True
    return here != there


# ------------------------------------------------------------- the section

#: An explanation the author already wrote.  Backports in this tree
#: carry it under this heading; it is the same prose their Conflicts:
#: section wants, just under a name their checker does not look for.
_BACKPORT_NOTE_RE = re.compile(
    r'^\[Backport Changes\]\s*\n(.*?)(?=\n\s*\n|\Z)',
    re.MULTILINE | re.DOTALL)

#: An existing section, so a second run does not add another.
_CONFLICTS_RE = re.compile(r'^Conflicts:\s*$', re.MULTILINE)


def existing_note(message):
    """The author's own explanation, unindented, or None."""
    found = _BACKPORT_NOTE_RE.search(message)
    if not found:
        return None
    body = found.group(1)
    lines = [l.strip() for l in body.split('\n') if l.strip()]
    return '\n'.join(lines) if lines else None


def strip_note(message):
    """Remove the [Backport Changes] block once it has been moved.

    Taking the block out leaves the blank line above it against the
    blank line below, so close the gap rather than leaving a hole
    where the note used to be.
    """
    without = _BACKPORT_NOTE_RE.sub('', message)
    return re.sub(r'\n{3,}', '\n\n', without).rstrip() + '\n'


def section(files, note):
    """The Conflicts: block, in the only shape their regex accepts.

    Their check_conflict_format builds one cumulative regex: whitespace
    indented file lines, then a line opening with "[", then any number
    of lines, then a line closing with "]", then Signed-off-by.  Each
    step is anchored to the one before, so a blank line anywhere inside
    -- including between the closing bracket and the first trailer --
    fails the match.  The layout below is not a house style, it is the
    only arrangement that passes.
    """
    out = ['Conflicts:']
    out += ['\t%s' % f for f in files]
    if not note:
        note = ('The backport differs from the upstream commit in the '
                'file(s) above. Describe why here.')
    body = note.strip().split('\n')
    body[0] = '[' + body[0]
    body[-1] = body[-1] + ']'
    out += body
    return '\n'.join(out)


def already_declared(message):
    return bool(_CONFLICTS_RE.search(message))


#: Their check_conflict_format, built the same way: one regex grown a
#: piece at a time and re-matched from the start of the section, so
#: every piece is anchored to the one before it.  Reproduced rather
#: than approximated, because the interesting failures are the ones
#: where a section looks right and does not match -- a blank line
#: between the closing bracket and the sign-offs, or a Reviewed-by
#: sitting where a Signed-off-by has to be.
_FORMAT_STEPS = (
    r'(\s+.*\n)+',
    r'\[.*',
    r'(.*?\n)*',
    r'.*\]\n',
    r'(Signed-off-by: .*?\n?)+',
)


def format_ok(message, files=()):
    """Whether their checker would accept the section in this message.

    Returns (ok, why).  ``files`` are the ones that must be named; they
    check that too, after the shape.
    """
    lines = message.split('\n')
    for index, line in enumerate(lines):
        if line.strip() != 'Conflicts:':
            continue
        rest = '\n'.join(lines[index + 1:])

        pattern = ''
        for step in _FORMAT_STEPS:
            pattern += step
            if not re.match(pattern, rest):
                if step == _FORMAT_STEPS[0]:
                    return False, 'no files listed under Conflicts:'
                if step == _FORMAT_STEPS[-1]:
                    return False, ('the Conflicts: description is not '
                                   'followed directly by Signed-off-by')
                return False, 'the Conflicts: description is not in brackets'

        for name in files:
            if name not in rest:
                return False, 'the Conflicts: section does not name %s' % name
        return True, ''

    return False, 'no Conflicts: section'
