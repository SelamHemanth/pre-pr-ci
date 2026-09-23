# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/distro.py
# Reading and writing the distro selection and the per-distro .configure file
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Access to ``.distro_config`` and ``<distro>/.configure``.

``.configure`` is sourced by bash, so every value written here goes through
:func:`shlex.quote`; an unescaped quote in a password used to truncate the file
and take the following settings with it.  The file holds credentials, so it is
created 0600 and never leaves this module with its secrets intact -- see
:func:`redact`.
"""

import logging
import os
import re
import shlex
import subprocess
import tempfile
from datetime import datetime

from . import registry

log = logging.getLogger(__name__)

MASK = '••••••••'

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_HOST_RE = re.compile(r'^[A-Za-z0-9._:-]+$')


class ConfigError(Exception):
    """Validation failed.  ``errors`` maps field name -> human readable reason."""

    def __init__(self, errors):
        super().__init__('invalid configuration')
        self.errors = errors


def _atomic_write(path, text, mode=0o600):
    """Write ``text`` to ``path`` without ever leaving a half-written file
    behind, and without a window where it is world readable."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.tmp-')
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


def _parse_shell_assignments(path):
    """Read a ``key=value`` file the way bash would, minus the execution."""
    values = {}
    with open(path, 'r', errors='replace') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            if not key:
                continue
            try:
                parts = shlex.split(value)
            except ValueError:
                parts = [value.strip('\'"')]
            values[key] = parts[0] if parts else ''
    return values


class Workspace:
    """The pre-pr-ci checkout that the web server drives."""

    def __init__(self, root):
        self.root = root
        self.logs_dir = os.path.join(root, 'logs')
        self.distro_config = os.path.join(root, '.distro_config')

    # ── distro selection ──────────────────────────────────────────────────

    def selected_distro(self):
        """Return the configured distro id, or None if ``make config`` has not
        been run (or wrote something we do not recognise)."""
        if not os.path.exists(self.distro_config):
            return None
        distro = _parse_shell_assignments(self.distro_config).get('DISTRO')
        return distro if registry.is_distro(distro) else None

    def configure_path(self, distro):
        return os.path.join(self.root, distro, '.configure')

    def is_configured(self):
        distro = self.selected_distro()
        return bool(distro) and os.path.exists(self.configure_path(distro))

    # ── reading ───────────────────────────────────────────────────────────

    def read_config(self, distro=None):
        distro = distro or self.selected_distro()
        if not distro:
            return None
        path = self.configure_path(distro)
        if not os.path.exists(path):
            return None
        return _parse_shell_assignments(path)

    def enabled_tests(self, distro=None):
        """Map test name -> bool, from the ``TEST_*`` flags.

        A missing flag counts as enabled because that is how ``test.sh``
        defaults it (``${TEST_FOO:-yes}``).
        """
        distro = distro or self.selected_distro()
        config = self.read_config(distro) or {}
        return {
            test.name: config.get(test.config_key, 'yes').lower() != 'no'
            for test in registry.tests_for(distro)
        }

    # ── writing ───────────────────────────────────────────────────────────

    def write_config(self, distro, values, test_flags, torvalds_repo):
        """Validate and persist a configuration, raising ConfigError on bad
        input rather than writing a file that fails 20 minutes into a build.

        Returns advisory notes about the saved configuration: things that are
        legal but probably not intended.
        """
        if not registry.is_distro(distro):
            raise ConfigError({'distro': 'unknown distribution'})

        cleaned = self._validate(distro, values, test_flags)

        lines = [
            '# %s configuration' % registry.DISTROS[distro],
            '# Written by the Pre-PR CI web interface on %s'
            % datetime.now().strftime('%c'),
            '# Contains credentials: keep mode 0600.',
            '',
            '# General',
        ]

        def emit(key):
            lines.append('%s=%s' % (key, shlex.quote(cleaned.get(key, ''))))

        for field in registry.CONFIG_FIELDS[distro]['general']:
            emit(field.name)

        lines += ['', '# Build']
        for field in registry.CONFIG_FIELDS[distro]['build']:
            emit(field.name)

        lines += ['', '# Test selection', 'RUN_TESTS=yes']
        for key in registry.test_config_keys(distro):
            lines.append('%s=%s' % (key, test_flags.get(key, 'yes')))

        lines += ['', '# Host']
        for field in registry.CONFIG_FIELDS[distro]['host']:
            emit(field.name)

        # Not every distro has one.  openEuler's CI never boots a kernel, so
        # that section was dropped and writing an empty "# VM" heading with
        # nothing under it would only invite someone to fill it back in.
        if registry.CONFIG_FIELDS[distro].get('vm'):
            lines += ['', '# VM']
            for field in registry.CONFIG_FIELDS[distro]['vm']:
                emit(field.name)

        lines += ['', '# Repository',
                  'TORVALDS_REPO=%s' % shlex.quote(torvalds_repo), '']

        path = self.configure_path(distro)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _atomic_write(path, '\n'.join(lines))

        _atomic_write(
            self.distro_config,
            'DISTRO=%s\nDISTRO_DIR=%s\n' % (distro, distro),
            mode=0o644,
        )
        log.info('wrote configuration for %s', distro)
        return advisories(cleaned)

    def _validate(self, distro, values, test_flags):
        errors = {}
        cleaned = {}

        for field in registry.all_fields(distro):
            raw = values.get(field.name)
            value = '' if raw is None else str(raw).strip()

            if not value and field.default is not None:
                value = str(field.default)

            if not value:
                # The boot test is the only consumer of the VM settings, so
                # only insist on them when the user actually enabled it.
                if field.name in ('VM_IP', 'VM_ROOT_PWD') and \
                        not self._boot_test_enabled(distro, test_flags):
                    cleaned[field.name] = ''
                    continue
                if field.required:
                    errors[field.name] = '%s is required' % field.label
                cleaned[field.name] = ''
                continue

            problem = self._check_field(field, value)
            if problem:
                errors[field.name] = problem
            cleaned[field.name] = value

        if errors:
            raise ConfigError(errors)
        return cleaned

    @staticmethod
    def _boot_test_enabled(distro, test_flags):
        for test in registry.tests_for(distro):
            if test.name in ('boot_kernel', 'boot_kernel_rpm'):
                return test_flags.get(test.config_key, 'yes') != 'no'
        return False

    def _check_field(self, field, value):
        if field.name == 'LINUX_SRC_PATH':
            return self._check_source_tree(value)

        if field.type == 'email' and not _EMAIL_RE.match(value):
            return 'does not look like an email address'

        if field.name == 'VM_IP' and not _HOST_RE.match(value):
            return 'must be a hostname or IP address'

        if field.type == 'number':
            try:
                number = int(value)
            except ValueError:
                return 'must be a whole number'
            limit = 4096 if field.name == 'BUILD_THREADS' else 1000
            if not 1 <= number <= limit:
                return 'must be between 1 and %d' % limit

        if field.options and value not in field.options:
            return 'must be one of: %s' % ', '.join(field.options)

        return None

    def _check_source_tree(self, value):
        """Reject a kernel path that will only fail much later, or damage us.

        Everything here was reachable before and surfaced as a confusing
        failure some minutes into a run, or as a command operating on the
        wrong tree entirely.
        """
        if not os.path.isabs(value):
            return 'must be an absolute path'
        if not os.path.isdir(value):
            return 'no such directory on this host'
        if not os.path.exists(os.path.join(value, '.git')):
            # A worktree or submodule has .git as a file, not a directory.
            return 'not a git checkout (no .git)'

        source = os.path.realpath(value)
        project = os.path.realpath(self.root)

        # `make clean` runs `make clean` inside this path.  Pointed at our own
        # checkout that recurses into this Makefile and never terminates; the
        # ancestor case is worse, since the tool would be inside the tree the
        # clean and reset targets operate on.  A kernel tree *underneath* the
        # project is fine and expected -- euler/kernel is exactly that.
        if source == project:
            return 'this is the Pre-PR CI checkout, not a kernel tree'
        if project.startswith(source + os.sep):
            return 'contains the Pre-PR CI checkout; clean and reset would ' \
                   'operate on this tool'

        if not _looks_like_kernel_tree(source):
            return 'does not look like a Linux source tree (no Makefile ' \
                   'with VERSION and PATCHLEVEL)'

        # Patches are applied and the build runs in place.
        if not os.access(source, os.W_OK):
            return 'not writable by the user running this server'

        return None


def _looks_like_kernel_tree(path):
    """True when ``path`` has a Linux top-level Makefile.

    Every Linux Makefile since forever opens with VERSION/PATCHLEVEL, and
    they appear in the first few lines, so this reads only the head of the
    file rather than the whole thing.
    """
    try:
        with open(os.path.join(path, 'Makefile'), 'r', errors='replace') as fh:
            head = [next(fh, '') for _ in range(12)]
    except OSError:
        return False

    text = ''.join(head)
    return bool(re.search(r'^\s*VERSION\s*=', text, re.M)
                and re.search(r'^\s*PATCHLEVEL\s*=', text, re.M))


def _git_line(args, cwd, timeout=10):
    """One line of git output, or None if git is slow, missing or unhappy.

    Used only for advisory checks, so every failure means 'say nothing'
    rather than blocking a save on a sluggish filesystem.
    """
    try:
        done = subprocess.run(['git'] + args, cwd=cwd, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              universal_newlines=True, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def advisories(values):
    """Things worth saying about a valid configuration, but not worth
    refusing it for.

    These are judgement calls about the host and the tree rather than errors:
    the configuration is well formed, it just may not do what was intended.
    """
    notes = []
    source = values.get('LINUX_SRC_PATH')

    threads = values.get('BUILD_THREADS')
    if threads and threads.isdigit():
        cpus = os.cpu_count() or 1
        if int(threads) > cpus * 2:
            notes.append(
                'BUILD_THREADS is %s on a %d-CPU host; beyond about %d the '
                'build gets slower, not faster.' % (threads, cpus, cpus))

    if not (source and os.path.isdir(source)):
        return notes

    wanted = values.get('NUM_PATCHES')
    if wanted and wanted.isdigit():
        # Capped so this stays instant on a tree with a million commits.
        have = _git_line(['rev-list', '--count', '-n', str(int(wanted) + 1),
                          'HEAD'], cwd=source)
        if have and have.isdigit() and int(have) < int(wanted):
            notes.append(
                'NUM_PATCHES is %s but the current branch has only %s commit(s).'
                % (wanted, have))

    # --untracked-files=no keeps this off the slow path: enumerating untracked
    # files in an object directory takes seconds and tells us nothing here.
    dirty = _git_line(['status', '--porcelain', '--untracked-files=no'],
                      cwd=source)
    if dirty:
        notes.append(
            'The kernel tree has uncommitted changes; patches are generated '
            'from commits, so those edits will not be included.')

    branch = _git_line(['rev-parse', '--abbrev-ref', 'HEAD'], cwd=source)
    if branch == 'HEAD':
        notes.append(
            'The kernel tree has a detached HEAD; check out a branch before '
            'generating patches.')

    return notes


def redact(config):
    """Replace credentials with a mask so a configuration can be displayed.

    The UI shows this; the real values stay in the 0600 file.
    """
    return {
        key: (MASK if key in registry.SECRET_KEYS and value else value)
        for key, value in (config or {}).items()
    }
