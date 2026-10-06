# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/hostcheck.py
# Ask whether this host can test the configured kernel
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
"""Whether this host is fit to test the tree it has been pointed at.

The answer is lib/hostcheck.sh's, run rather than reimplemented, for
the same reason readiness.py runs ready.sh: the test scripts consult it
too, and a page that disagreed with them would offer a button the script
then refuses.

The answer has three parts, because the page shows them differently: a
headline, the reason behind it, and the evidence for that -- the
tool table, which sits behind "view more" because it is long and is
only wanted once the reason has raised the question.
"""

import os
import re
import subprocess
import time

#: Longer than readiness' TTL on purpose.  Nothing about this changes
#: when the series does; it changes when the host's packages do, which
#: is not something that happens while a page is open.
_TTL = 900.0

_cache = {}


def _stamp(kernel):
    # The tree's version is read out of its Makefile, so its mtime
    # covers the half of the question the tree owns.
    try:
        return os.path.getmtime(os.path.join(kernel, 'Makefile'))
    except OSError:
        return 0


def check(root, kernel=None, force=False):
    """Return a dict describing whether this host can test ``kernel``.

    ``ok`` is True whenever the host is fit *and* whenever the question
    cannot be answered.  An unanswerable probe is not evidence of a bad
    host, and must not be the reason the whole suite is refused.
    """
    blank = {'ok': True, 'headline': '', 'summary': '', 'reason': '',
             'note': '', 'table': []}
    if not kernel or not os.path.isdir(kernel):
        return blank

    script = os.path.join(root, 'lib', 'hostcheck.sh')
    if not os.path.exists(script):
        return blank

    stamp = (kernel, _stamp(kernel))
    hit = _cache.get(kernel)
    if not force and hit and hit[0] == stamp and time.time() - hit[1] < _TTL:
        return hit[2]

    try:
        done = subprocess.run(['bash', script, kernel], cwd=root,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return blank

    answer = _split(done.stdout.decode('utf-8', 'replace'),
                    done.returncode == 0)
    _cache[kernel] = (stamp, time.time(), answer)
    return answer


def _split(text, ok):
    """The script's three parts, in the three shapes the page needs.

    It prints the answer, the evidence and the context for it, in that
    order, separated by a rule.  Only the first is shown until somebody
    asks: a compiler error in front of a person who has not asked for
    one is noise standing in front of the thing they do need, which is
    that nothing is going to run.
    """
    blank = {'ok': True, 'headline': '', 'summary': '', 'reason': '',
             'note': '', 'table': []}
    if ok:
        return blank

    parts = text.split('\n---\n')
    lines = parts[0].strip('\n').split('\n')
    note, table = _table(parts[2].strip('\n') if len(parts) > 2 else '')
    return {
        'ok': False,
        'headline': lines[0].strip() if lines else 'This host cannot test '
                                                   'this kernel',
        'summary': ' '.join(l.strip() for l in lines[1:] if l.strip()),
        'reason': parts[1].strip('\n') if len(parts) > 1 else '',
        'note': note,
        'table': table,
    }


def _table(text):
    """The prose and the rows of the evidence, told apart by shape.

    The script prints a column-aligned table, which the page would
    rather draw itself than show as preformatted text -- a table it
    draws can be read on a narrow window, and the row that matters can
    be marked.  Two or more spaces is the column separator, which is
    what alignment means.
    """
    prose, rows = [], []
    for line in text.split('\n'):
        cells = [c.strip() for c in re.split(r'\s{2,}', line.strip())]
        if len(cells) == 3 and cells[0]:
            # The header row names the columns the page already names.
            if cells[0] == 'Tool':
                continue
            rows.append({'tool': cells[0], 'have': cells[1],
                         'want': cells[2]})
        elif line.strip():
            prose.append(line.strip())
    return ' '.join(prose), rows


def forget(kernel=None):
    """Drop the cached answer, after something that could change it."""
    if kernel is None:
        _cache.clear()
    else:
        _cache.pop(kernel, None)
