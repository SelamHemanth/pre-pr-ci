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
        input rather than writing a file that fails 20 minutes into a build."""
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

    @staticmethod
    def _check_field(field, value):
        if field.name == 'LINUX_SRC_PATH':
            if not os.path.isabs(value):
                return 'must be an absolute path'
            if not os.path.isdir(value):
                return 'no such directory on this host'
            if not os.path.isdir(os.path.join(value, '.git')):
                return 'not a git checkout (no .git directory)'
            return None

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


def redact(config):
    """Replace credentials with a mask so a configuration can be displayed.

    The UI shows this; the real values stay in the 0600 file.
    """
    return {
        key: (MASK if key in registry.SECRET_KEYS and value else value)
        for key, value in (config or {}).items()
    }
