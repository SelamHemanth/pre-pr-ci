#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/oe_header.py
# Write the openEuler metadata header onto a formatted patch
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
"""Add openEuler's commit-message header to a patch, or refuse to.

openEuler's ``format.py`` decides whether a backport's commit message is
acceptable.  It is the authority, and everything here exists to satisfy
it: the templates in ``commit_format``, the Signed-off-by rule in
``check_employee_id``, the Fixes rule for bugfix patches, the
cherry-pick rule for openEuler-24.03, and the comparison of the subject
line against the upstream commit.

The rule this file follows is that a patch which will be rejected should
never be written in the first place.  Where the header cannot be
completed truthfully -- an upstream SHA that resolves nowhere, a commit
in no released tag -- it refuses and says what is missing, rather than
emitting something shaped like a header and leaving the discovery to
CI.  A warning that scrolls past is not a safeguard.

Exit status: 0 written, 1 refused with a reason on stderr, 2 misuse.
"""

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oe_conflict

#: The separator between openEuler's header and the original commit
#: message.  Their templates accept 16 to 80 dashes.
SEPARATOR = '-' * 32

#: Inclusion lines their first two templates recognise by name.  A patch
#: that already carries any "<something> inclusion" line has been
#: through this once, or was written by hand, and is left alone.
_INCLUSION_RE = re.compile(r'^\w[\w ]* inclusion\s*$', re.MULTILINE)

#: How a backport says where it came from.  The stable trees use the
#: bracketed form; a hand-written backport usually uses the first.
_UPSTREAM_RES = (
    re.compile(r'^commit ([0-9a-f]{7,40}) upstream\.?\s*$', re.MULTILINE),
    re.compile(r'^\[ Upstream commit ([0-9a-f]{7,40}) \]\s*$', re.MULTILINE),
    re.compile(r'^commit ([0-9a-f]{7,40})\s*$', re.MULTILINE),
)

#: A Fixes: tag in any of the widths people write it in.  format.py
#: accepts exactly twelve hex characters and nothing else, so whatever
#: is here has to be rewritten to that width.
_FIXES_RE = re.compile(r'^(\s*Fixes:\s+)([0-9a-f]{6,40})(\s*\(.*)$',
                       re.MULTILINE)

#: KABI padding and similar have no upstream equivalent; openEuler files
#: them under "virt inclusion".  Deliberately not bounded as a word:
#: these turn up as kabi_reserve, check_kabi and KABI:, and all of them
#: mean the same thing.
_KABI_RE = re.compile(r'kabi|kapi', re.IGNORECASE)

#: openEuler-24.03 backports must name the OLK-6.6 commit they came from.
_CHERRY_PICK_RE = re.compile(
    r'\(cherry-pick from OLK-6\.6 commit [0-9a-f]{12,40}\)')

#: Releases their "from mainline-" line accepts.  Anything else, such as
#: the literal word "mainline", fails the template.
_TAG_RE = re.compile(r'^v?\d+\.\d+(-rc\d+)?(-dontuse)?$')

# ---------------------------------------------------------------- category
#
# What a patch is for is not something a diff can tell you, but it is
# something the commit message usually says outright, and the author has
# already said it once.  Asking again produced a single answer applied to
# every patch in a series, which is wrong the moment a series mixes a fix
# with a cleanup.
#
# The signals below are read in order of how much they mean.  A CVE
# number is unambiguous.  "Cc: stable" is nearly so: the stable rules
# only accept fixes, so a maintainer sending a patch there has already
# classified it.  A Fixes: tag says the same thing more weakly.  Only
# after those do the wording tests get a turn, and they read the subject
# before the body, because the subject is what the author chose to say.

#: An author who wrote the category themselves has the final word.
_EXPLICIT_RE = re.compile(r'^\s*category:\s*(\w+)\s*$', re.MULTILINE)

_CVE_RE = re.compile(r'\bCVE-\d{4}-\d{4,7}\b')
_SECURITY_RE = re.compile(
    r'\b(vulnerabilit\w+|exploitable|privilege escalation|infoleak'
    r'|information leak|arbitrary (code|write|read))\b', re.IGNORECASE)
#: Only stable@ addresses.  A Cc: to a person says nothing.
_STABLE_CC_RE = re.compile(
    r'^\s*Cc:\s*<?stable@(vger\.)?kernel\.org', re.MULTILINE | re.IGNORECASE)
_FIXES_TAG_RE = re.compile(r'^\s*Fixes:\s+[0-9a-f]{6,40}', re.MULTILINE)
_PERF_RE = re.compile(
    r'\b(optimi[sz]\w*|speed ?up|speedup|faster|performance|throughput'
    r'|latency|overhead|fast ?path|hot ?path)\b', re.IGNORECASE)
#: Deliberately narrow.  Words like "warning" or "check" appear in
#: plenty of patches that add something rather than repair it.
_FIX_RE = re.compile(
    r'\b(fix|fixes|fixed|fixing|correct|corrects|corrected|avoid|avoids'
    r'|prevent|prevents|resolve|resolves|regression|leak|overflow'
    r'|underflow|deadlock|race|use-after-free|uaf|double free|oops|panic'
    r'|crash|corruption|null (pointer )?deref\w*)\b', re.IGNORECASE)
_REVERT_RE = re.compile(r'^Revert\b', re.IGNORECASE)


def decide_category(subject, message, backport=True):
    """Work out what this patch is for, the way its author described it.

    Returns ``(category, why)``.  The reason is printed, because a
    category nobody chose should at least say what it was read from.
    """
    explicit = _EXPLICIT_RE.search(message)
    if explicit:
        return explicit.group(1), 'the commit message says so'

    # A patch with no upstream commit behind it that touches the kernel
    # ABI is repairing something.  The only reason openEuler tracks KABI
    # at all is that a module built against the released kernel has to
    # keep loading, so a change there is answering a break, whatever the
    # subject line calls it.  That outranks the wording tests below,
    # which read "reserve padding" as a feature.
    if not backport and _KABI_RE.search(subject + '\n' + message):
        return 'bugfix', 'it changes the kernel ABI'

    if _CVE_RE.search(message):
        return 'security', 'the message cites a CVE'
    if _SECURITY_RE.search(subject) or _SECURITY_RE.search(message):
        return 'security', 'the message describes a vulnerability'

    if _STABLE_CC_RE.search(message):
        return 'bugfix', 'it was copied to stable, which only takes fixes'
    if _FIXES_TAG_RE.search(message):
        return 'bugfix', 'it carries a Fixes: tag'
    if _REVERT_RE.search(subject):
        return 'bugfix', 'it is a revert'

    # The subject is the author's own summary, so it outranks the body.
    if _PERF_RE.search(subject):
        return 'performance', 'the subject is about performance'
    if _FIX_RE.search(subject):
        return 'bugfix', 'the subject describes a fix'
    if _PERF_RE.search(message):
        return 'performance', 'the message is about performance'

    return 'feature', 'nothing marks it as a fix'


class Refused(Exception):
    """The header cannot be written truthfully.  Says why."""


def git(args, cwd, check=False):
    """Run git and return stripped stdout, or '' if it failed."""
    try:
        out = subprocess.run(['git'] + args, cwd=cwd, check=check,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise Refused('could not run git: %s' % exc)
    if out.returncode != 0:
        return ''
    return out.stdout.decode('utf-8', 'replace').strip()


# --------------------------------------------------------------- patch file

class Patch(object):
    """A ``git format-patch`` file, split where we need to insert.

    Kept as lines rather than reparsed and reserialised, so that
    everything we do not touch survives byte for byte -- a patch is
    going through ``git am`` next, and a reflowed diff does not apply.
    """

    def __init__(self, path):
        self.path = path
        with open(path, encoding='utf-8', errors='surrogateescape') as handle:
            self.lines = handle.read().split('\n')

        # Mail headers, then a blank line, then the message, then a line
        # of exactly "---", then the diffstat and the diff.
        self.head_end = next(
            (i for i, l in enumerate(self.lines) if l == ''), 0)
        self.msg_end = next(
            (i for i, l in enumerate(self.lines)
             if l == '---' and i > self.head_end), len(self.lines))

    @property
    def message(self):
        return '\n'.join(self.lines[self.head_end + 1:self.msg_end])

    @property
    def subject(self):
        """The subject with the mail wrapping and the [PATCH] tag removed."""
        out = []
        for line in self.lines[:self.head_end]:
            if out and line[:1] in (' ', '\t'):
                out.append(line.strip())
                continue
            if out:
                break
            if line.startswith('Subject:'):
                out.append(line[len('Subject:'):].strip())
        subject = ' '.join(out)
        return re.sub(r'^\[[^\]]*\]\s*', '', subject)

    def set_message(self, message):
        self.lines[self.head_end + 1:self.msg_end] = message.split('\n')
        self.msg_end = next(
            (i for i, l in enumerate(self.lines)
             if l == '---' and i > self.head_end), len(self.lines))

    def save(self):
        with open(self.path, 'w', encoding='utf-8',
                  errors='surrogateescape') as handle:
            handle.write('\n'.join(self.lines))


# ------------------------------------------------------------------ lookups

def upstream_sha(message):
    for pattern in _UPSTREAM_RES:
        found = pattern.search(message)
        if found:
            return found.group(1)
    return None


def expand_sha(sha, mirror):
    """Widen an abbreviated SHA to the forty characters they require.

    Returns None when the mirror has never heard of it, which is the
    answer for a typo, for a commit that is not upstream at all, and for
    a mirror that needs fetching.  All three mean the same thing here:
    we cannot write a truthful header.
    """
    full = git(['rev-parse', '--verify', '--quiet', '%s^{commit}' % sha],
               cwd=mirror)
    return full if len(full) == 40 else None


def release_tag(sha, mirror):
    """The first release containing ``sha``, or None.

    ``git describe --contains`` answers this directly.  The tempting
    fallback, ``git describe --tags``, answers a different question --
    the last release *before* the commit -- and putting that in a "from
    mainline-" line states that the change shipped in a release that
    does not contain it.  It is not used.
    """
    described = git(['describe', '--contains', sha], cwd=mirror)
    tag = re.split(r'[~^]', described)[0] if described else ''
    if _TAG_RE.match(tag):
        return tag

    # Equivalent question, asked a different way, for the commits
    # describe declines to name.
    listed = git(['tag', '--contains', sha, '--sort=version:refname'],
                 cwd=mirror)
    for candidate in listed.split('\n'):
        candidate = candidate.strip()
        if _TAG_RE.match(candidate):
            return candidate
    return None


def upstream_subject(sha, mirror):
    return git(['log', '-1', '--format=%s', sha], cwd=mirror)


def upstream_message(sha, mirror):
    return git(['log', '-1', '--format=%B', sha], cwd=mirror)


# ------------------------------------------------------------------ rewrites

def normalise_fixes(message, mirror, kernel, strict=False):
    """Rewrite Fixes: tags to the shape format.py accepts.

    Their regex is ``Fixes: ([0-9a-f]{12}) \\(.*\\)`` -- exactly twelve,
    so a seven character tag fails and a full forty character one fails
    too.  The twelve characters have to name a commit in the openEuler
    tree rather than upstream, because that is where format.py resolves
    them, so an abbreviation is widened against the mirror and then cut
    rather than cut blindly.

    The subject in the brackets is taken from the commit rather than
    kept as written.  Their regex does not read it, but a person does,
    and a Fixes: tag whose subject belongs to a different commit than
    its SHA is a wrong answer that looks like a right one.

    ``strict`` is for the patches their format.py checks the tag on:
    no mainline or stable inclusion and category: bugfix, which is the
    KABI case.  There it runs ``git rev-parse`` in the openEuler tree,
    so the target has to be a commit in *that* tree -- an upstream SHA
    the fork predates resolves in the mirror and fails their check --
    and it must not be a merge.
    """
    problems = []

    def fix(match):
        prefix, sha, rest = match.groups()
        if strict:
            full = expand_sha(sha, kernel)
            if not full:
                problems.append('%s is not a commit in the openEuler tree'
                                % sha)
                return match.group(0)
            if len(git(['log', '-1', '--format=%P', full],
                       cwd=kernel).split()) > 1:
                problems.append('%s is a merge commit' % sha)
                return match.group(0)
        else:
            full = expand_sha(sha, mirror) or expand_sha(sha, kernel)
            if not full:
                return match.group(0)

        subject = (upstream_subject(full, kernel)
                   or upstream_subject(full, mirror))
        if not subject:
            return '%s%s%s' % (prefix, full[:12], rest)
        return '%s%s ("%s")' % (prefix, full[:12], subject)

    out = _FIXES_RE.sub(fix, message)
    if problems:
        raise Refused(
            'the Fixes: tag will not pass their format check: %s.\n'
            '  This patch has no upstream commit behind it, so openEuler '
            'resolves the tag against the openEuler tree itself, and it '
            'is the only thing saying what the patch is for.'
            % '; '.join(problems))
    return out


def header_lines(kind, tag, sha, category, bugzilla, cherry_pick):
    """The header, in the order their templates read it."""
    out = ['%s inclusion' % kind]
    if kind == 'mainline':
        out.append('from mainline-%s' % tag)
    elif kind == 'stable':
        out.append('from stable-%s' % tag)
    if sha:
        out.append('commit %s' % sha)
    out.append('category: %s' % category)
    out.append('bugzilla: https://atomgit.com/openeuler/kernel/issues/%s'
               % bugzilla)
    if kind in ('mainline', 'stable'):
        out.append('CVE: NA')
    out.append('')
    if sha:
        out.append('Reference: https://github.com/torvalds/linux/commit/%s'
                   % sha)
        out.append('')
    if cherry_pick:
        # Their check insists this sits before the separator.
        out.append(cherry_pick)
        out.append('')
    out.append(SEPARATOR)
    out.append('')
    return out


#: Roughly what git considers a trailer: "Word-Word: something".
_TRAILER_RE = re.compile(r'^[A-Z][A-Za-z-]*(-by)?:\s')


def add_signed_off_by(message, signer):
    """Append the Signed-off-by, as the last line of the trailer block.

    format.py wants one on every patch, including the KABI ones that
    used to be exempted here for a reason it does not recognise.

    The blank line matters: git only treats the final paragraph as
    trailers when every line in it is one, so putting a Signed-off-by
    straight after a sentence makes it part of the prose as far as
    interpret-trailers and every tool built on it is concerned.
    """
    if signer in message:
        return message

    body = message.rstrip('\n')
    last = body.rsplit('\n', 1)[-1] if body else ''
    joiner = '\n' if _TRAILER_RE.match(last) else '\n\n'
    return body + joiner + signer


# --------------------------------------------------------------------- main

def rewrite(patch, args):
    """Add the header to one patch, or raise Refused."""
    message = patch.message
    subject = patch.subject

    if _INCLUSION_RE.search(message):
        # Already carries a header, so only the Signed-off-by rule and
        # the Fixes width are still ours to enforce.
        message = normalise_fixes(message, args.mirror, args.kernel)
        patch.set_message(add_signed_off_by(message, args.signer))
        return 'already had a header', None

    sha = upstream_sha(message)
    cherry_pick = None
    strict_fixes = False

    if sha is None:
        if not _KABI_RE.search(subject + '\n' + message):
            raise Refused(
                'no upstream commit in the message and nothing marking it '
                'as a KABI change, so there is no way to tell openEuler '
                'where this came from.\n'
                '  Add "commit <40-char sha> upstream." for a backport, or '
                'write the inclusion header by hand for an original patch.')
        kind, tag = 'virt', None
        category, why = decide_category(subject, message, backport=False)

        # Their rule: a bugfix with no upstream commit behind it has to
        # say what it fixes.  We cannot invent that, and quietly filing
        # the patch under something else would be a lie told to get past
        # a check, so this one goes back to the author.
        if category == 'bugfix' and not _FIXES_TAG_RE.search(message):
            raise Refused(
                'this reads as a bugfix (%s) but has no upstream commit '
                'and no Fixes: tag.\n'
                '  openEuler requires one for a bugfix that is not a '
                'backport: Fixes: <12-char sha> ("subject of the bad '
                'commit").' % why)

        # And the tag has to name something.  A Fixes: line nobody can
        # resolve is worse than none: it reads as answered and points
        # nowhere, and for these patches it is the only thing saying
        # what the change is for.
        strict_fixes = True
    else:
        # Always resolve, even at full width.  A forty character SHA is
        # the right shape for their template and still names nothing,
        # and skipping the lookup for it would turn "this commit does
        # not exist" into the much more confusing "no release contains
        # this commit".
        full = expand_sha(sha, args.mirror)
        if not full:
            raise Refused(
                'upstream commit %s is not in the mirror.\n'
                '  Either the SHA is wrong, or the mirror needs fetching.'
                % sha)

        tag = release_tag(full, args.mirror)
        if not tag:
            raise Refused(
                'upstream commit %s is in the mirror but in no release, so '
                'there is no honest value for "from mainline-".\n'
                '  A commit that has not been tagged in an rc yet, or that '
                'is only in linux-next, is not a mainline backport.'
                % full[:12])

        upstream = upstream_subject(full, args.mirror)
        if upstream and upstream != subject:
            raise Refused(
                'the subject does not match the upstream commit, which '
                'openEuler compares directly.\n'
                '  upstream: %s\n'
                '  this one: %s' % (upstream, subject))

        kind, sha = 'mainline', full
        # Read the upstream commit's own message, not our copy of it.
        # A backport often drops the Cc: stable and Fixes: lines that
        # say most clearly what the change was for.
        category, why = decide_category(
            subject, upstream_message(full, args.mirror) or message)

        if args.branch.startswith('openEuler-24.03'):
            found = _CHERRY_PICK_RE.search(message)
            if not found:
                raise Refused(
                    'openEuler-24.03 backports must name the OLK-6.6 commit '
                    'they came from.\n'
                    '  Add "(cherry-pick from OLK-6.6 commit <sha>)" to the '
                    'commit message.')
            cherry_pick = found.group(0)
            message = _CHERRY_PICK_RE.sub('', message)

    message = normalise_fixes(message, args.mirror, args.kernel, strict_fixes)

    # sha is None on the virt path, which is what leaves out the
    # "commit" and "Reference" lines their third template does not have.
    header = header_lines(kind, tag, sha, category, args.bugzilla,
                          cherry_pick)
    # The trailing newline is the blank line their templates require
    # after the separator; joining the list alone does not produce it.
    message = '\n'.join(header) + '\n' + message.lstrip('\n')
    message = add_signed_off_by(message, args.signer)

    message, note, warning = declare_conflicts(message, sha, args)
    patch.set_message(message)

    described = '%s inclusion' % kind
    if tag:
        described += ' from %s' % tag
    described += ', category: %s (%s)' % (category, why)
    return described + (', %s' % note if note else ''), warning


def declare_conflicts(message, sha, args):
    """Add the Conflicts: section when the backport diverges upstream.

    openEuler renders your commit and the upstream one as diffs and
    compares them byte for byte.  Any difference at all -- a hunk
    dropped, a line renumbered, a neighbouring change already in the
    tree -- means the message has to name every file that differs and
    say why, or checkconflict rejects the patch.

    Which files differ is not something anybody can work out reliably
    by eye, and it is exactly what their comparison already computes.
    So compute it the same way and write the section.

    What this cannot do is explain why, so it does not write a section
    at all unless the author already has: the [Backport Changes] note
    is the explanation, in the author's words, and moving it into the
    brackets is the whole of the job.  Inventing a placeholder for a
    commit that has no note would satisfy the gate with a sentence
    nobody wrote and nobody means, and leave the commit worse than it
    was found.

    A commit that diverges with no note is therefore left exactly as
    it is, and reported as a warning with the diff attached.  Most of
    those are false positives -- a hunk landing at a different offset
    reads as a difference to a byte-for-byte comparison -- and the
    only way to tell is to look, so the report shows the difference
    rather than asserting there is a problem.
    """
    if not sha or not args.commit:
        return message, None, None
    if oe_conflict.already_declared(message):
        return message, None, None
    if not oe_conflict.deviates(args.kernel, args.commit, args.mirror, sha):
        return message, None, None

    files = oe_conflict.differing_files(
        args.kernel, args.commit, args.mirror, sha)
    if not files:
        return message, None, None

    note = oe_conflict.existing_note(message)
    if not note:
        return message, None, divergence_report(files, sha, args)

    message = oe_conflict.strip_note(message)
    before, sign_offs = split_sign_offs(message)
    section = oe_conflict.section(files, note)
    # Their regex runs straight from the closing bracket into
    # "Signed-off-by:", so the section goes immediately above the
    # sign-offs with nothing in between -- not above the whole trailer
    # block, which on a real backport starts with the Reviewed-by and
    # Tested-by lines carried down from upstream.
    message = '\n'.join(before + section.split('\n') + sign_offs) + '\n'

    return message, 'Conflicts: %d file(s)' % len(files), None


# Enough of the difference to recognise a renumbered hunk without
# turning the prepare log into the diff itself.
_REPORT_LINES = 40


def divergence_report(files, sha, args):
    """What to say about a divergence nobody has described."""
    head = ['differs from upstream %s, and has no [Backport Changes] note '
            'to turn into a Conflicts: section.' % sha[:12],
            'checkconflict will ask for one naming:']
    head += ['    %s' % f for f in files]

    body = oe_conflict.difference(
        args.kernel, args.commit, args.mirror, sha, files).split('\n')
    if len(body) > _REPORT_LINES:
        extra = len(body) - _REPORT_LINES
        body = body[:_REPORT_LINES] + ['... %d more line(s)' % extra]

    return '\n'.join(head + ['', 'How it differs:'] + body + [
        '', 'If that is only context or line numbers moving, it is a false '
        'positive and the commit is fine as it stands.'])


_SOB_RE = re.compile(r'^Signed-off-by:\s')


def split_sign_offs(message):
    """Split the message where the Conflicts: section has to go.

    Their regex runs straight from the closing bracket into
    "Signed-off-by:", so the section has to sit directly above one.
    Several places satisfy that; the right one is the highest, at the
    top of the trailer group, because that is where the author's own
    [Backport Changes] block was and the note is replacing it.  Below
    the upstream trailers it reads as something the backporter added
    to a chain they do not own.

    A trailer group that opens with something else -- a Reviewed-by
    carried down from upstream -- pushes the section down to the first
    sign-off after it, which is as high as the gate allows.
    """
    lines = message.rstrip('\n').split('\n')

    # The last paragraph, which is where the trailers are.
    start = len(lines)
    while start > 0 and lines[start - 1].strip():
        start -= 1

    for i in range(start, len(lines)):
        if _SOB_RE.match(lines[i]):
            return lines[:i], lines[i:]

    # No sign-off to sit above, which add_signed_off_by makes
    # impossible in practice.
    return lines + [''], []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('patch')
    parser.add_argument('--mirror', required=True,
                        help="clone of Linus's tree, for SHA and tag lookups")
    parser.add_argument('--kernel', required=True,
                        help='the openEuler tree the patch is destined for')
    parser.add_argument('--commit', default='',
                        help='the original commit this patch was formatted '
                             'from, for comparing against upstream')
    parser.add_argument('--bugzilla', required=True)
    parser.add_argument('--signer', required=True,
                        help='the full "Signed-off-by: Name <mail>" line')
    parser.add_argument('--branch', default='OLK-6.6')
    args = parser.parse_args()

    if not os.path.isfile(args.patch):
        print('no such patch: %s' % args.patch, file=sys.stderr)
        return 2

    patch = Patch(args.patch)
    try:
        summary, warning = rewrite(patch, args)
    except Refused as why:
        print('%s:\n  %s' % (os.path.basename(args.patch), why),
              file=sys.stderr)
        return 1

    patch.save()
    print(summary)
    if warning:
        # Second line onwards, which is what prepare.sh indents under
        # the patch it belongs to.
        print(warning)
    return 0


if __name__ == '__main__':
    sys.exit(main())
