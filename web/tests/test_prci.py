#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/tests/test_prci.py
# Unit tests for the web backend
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Run with:  python3 -m unittest discover -s web/tests

The registry tests read the shell scripts on purpose.  The web UI resolves
test names and log paths from registry.py, so when a name or a log file is
renamed in test.sh and not here, a test becomes unstartable or its output
becomes invisible.  That had already happened to euler's check_kabi.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest

WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(WEB_DIR)
if WEB_DIR not in sys.path:
    sys.path.insert(0, WEB_DIR)

from prci import jobs                                       # noqa: E402
from prci import readiness                                  # noqa: E402
from prci import repo                                       # noqa: E402
from prci import registry                                   # noqa: E402
from prci.distro import ConfigError, Workspace, redact       # noqa: E402
from prci.jobs import JobStore, strip_ansi                   # noqa: E402


def read_script(distro):
    with open(os.path.join(PROJECT_ROOT, distro, 'test.sh'), errors='replace') as f:
        return f.read()


def read_file(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), errors='replace') as f:
        return f.read()


class TestRegistryMatchesScripts(unittest.TestCase):
    """registry.py has to agree with the scripts it drives."""

    def test_every_test_is_dispatchable(self):
        for distro in registry.DISTROS:
            script = read_script(distro)
            for test in registry.tests_for(distro):
                # The dispatcher is a case statement: "  <name>)"
                self.assertRegex(
                    script, r'(?m)^\s*%s\)\s*$' % re.escape(test.name),
                    '%s/test.sh has no case label for %r'
                    % (distro, test.name))

    def test_verdict_names_are_registry_names(self):
        """A PASS:/FAIL: line has to name a test the interface knows.

        The job log parser keys results off these names, so a verdict for
        "check_Kconfig" when the registry says "check_kconfig" showed up as a
        result belonging to no test, and the test itself never got a verdict.
        """
        for distro in registry.DISTROS:
            script = read_script(distro)
            known = {test.name for test in registry.tests_for(distro)}
            reported = set(re.findall(r'(?m)^\s*(?:pass|fail|skip)\s+"([^"$]+)"',
                                      script))
            unknown = reported - known
            self.assertFalse(
                unknown,
                '%s/test.sh reports verdicts for %s, which registry.py does '
                'not list' % (distro, ', '.join(sorted(unknown))))

    def test_every_log_file_is_written(self):
        for distro in registry.DISTROS:
            script = read_script(distro)
            for test in registry.tests_for(distro):
                stem = test.log[:-len('.log')]
                # Either named outright, or produced by a helper that builds
                # the path from its argument: run_oe_check writes
                # "${LOGS_DIR}/${test_name}.log" from the name it is given,
                # and run_oe_build writes "${LOGS_DIR}/oe_build_${arch}.log"
                # from just the architecture.  The stem can be in any
                # argument position, since a later one may override it.
                named = test.log in script
                joined = re.sub(r'\\\n\s*', ' ', script)
                wanted = stem
                if stem.startswith('oe_build_'):
                    wanted = stem[len('oe_build_'):]
                built = re.search(
                    r'run_(?:kernel_build|oe_check|oe_build)'
                    r'(?:\s+"[^"]*")*\s+"%s"' % re.escape(wanted), joined)
                self.assertTrue(
                    named or built,
                    '%s/test.sh never writes %s (for test %r)'
                    % (distro, test.log, test.name))

    def test_every_config_key_is_honoured(self):
        for distro in registry.DISTROS:
            script = read_script(distro)
            for test in registry.tests_for(distro):
                self.assertIn(
                    test.config_key, script,
                    '%s/test.sh ignores %s, so disabling %r in the UI would '
                    'have no effect' % (distro, test.config_key, test.name))

    def test_no_duplicate_test_names(self):
        for distro in registry.DISTROS:
            names = [t.name for t in registry.tests_for(distro)]
            self.assertEqual(len(names), len(set(names)), distro)

    def test_lookup_rejects_unknown_names(self):
        # This is what keeps a URL path out of a make invocation.
        for bogus in ('bogus', 'oe_build_ppc; id', '../../etc/passwd',
                      'oe_build_x86_64 ', ''):
            self.assertIsNone(registry.find_test('euler', bogus), bogus)
        self.assertIsNotNone(registry.find_test('euler', 'oe_build_x86_64'))

    def test_secrets_are_marked(self):
        self.assertEqual(registry.SECRET_KEYS,
                         {'VM_ROOT_PWD', 'HOST_USER_PWD'})

    def test_form_is_a_list_so_the_order_survives_json(self):
        """Flask sorts object keys, so a dict here reordered the form."""
        for distro in registry.DISTROS:
            sections = registry.fields_as_json(distro)
            self.assertIsInstance(sections, list)
            # Not every distro has every section -- euler has no VM because
            # it has no boot test -- but the ones it does have must stay in
            # the declared order.
            keys = [s['key'] for s in sections]
            declared = [key for key, _ in registry.SECTION_LABELS]
            self.assertEqual(keys, [k for k in declared if k in keys])
            first = sections[0]['fields'][0]
            self.assertEqual(first['name'], 'LINUX_SRC_PATH')

    def test_form_never_carries_a_secret_value(self):
        for distro in registry.DISTROS:
            for section in registry.fields_as_json(distro):
                for field in section['fields']:
                    self.assertNotIn('value', field)
                    if field['name'] in registry.SECRET_KEYS:
                        self.assertTrue(field['secret'], field['name'])
                        self.assertIsNone(field['default'])


    def test_detection_folds_case(self):
        """openEuler writes ID="openEuler"; matching "openeuler" missed it."""
        import tempfile
        from unittest import mock
        cases = {
            'ID="openEuler"\n': 'euler',
            'ID=openeuler\n': 'euler',
            'ID="anolis"\n': 'anolis',
            'ID="Anolis"\n': 'anolis',
            'ID="ubuntu"\n': None,
            'NAME="nothing"\n': None,
        }
        for content, expected in cases.items():
            with tempfile.NamedTemporaryFile('w', suffix='.os', delete=False) as f:
                f.write('NAME="x"\n' + content)
                name = f.name
            try:
                real_open = open

                def fake_open(path, *a, **kw):
                    if path == '/etc/os-release':
                        return real_open(name, *a, **kw)
                    return real_open(path, *a, **kw)

                with mock.patch('prci.registry.open', fake_open, create=True):
                    self.assertEqual(registry.detect_distro(), expected,
                                     'os-release %r' % content)
            finally:
                os.unlink(name)

    def test_shell_writes_secrets_with_config_set(self):
        """A single-quoted password broke the whole .configure file."""
        for distro in registry.DISTROS:
            script = read_file(distro, 'configure.sh')
            for key in sorted(registry.SECRET_KEYS):
                # "KEY='${KEY}'" inside a heredoc cannot survive an
                # apostrophe: bash then refuses to source the file at all.
                # assertNotIn would print the whole script on failure.
                self.assertFalse(
                    "%s='${%s}'" % (key, key) in script,
                    '%s/configure.sh quotes %s by hand; use config_set'
                    % (distro, key))
                self.assertRegex(
                    script, r'config_set[^\n]*\b%s\b' % re.escape(key),
                    '%s/configure.sh must write %s with config_set'
                    % (distro, key))

    def test_pipeline_dispatches_real_test_names(self):
        """A typo here silently skips the test instead of failing."""
        pipeline = read_file('jenkins', 'jenkins_pipeline.groovy')
        # "make anolis-test=check_kconfig" / "make euler-test=..."
        for distro in registry.DISTROS:
            dispatched = set(re.findall(
                r'make %s-test=([A-Za-z0-9_]+)' % re.escape(distro),
                pipeline))
            known = {t.name for t in registry.tests_for(distro)}
            self.assertFalse(
                dispatched - known,
                'jenkins_pipeline.groovy runs %s tests that do not exist: %s'
                % (distro, ', '.join(sorted(dispatched - known))))

    def test_documented_test_names_exist(self):
        """The docs are what people copy into a command line."""
        doc = read_file('DOCUMENT.md')
        # The two per-distro tables list one test per row: "| name | ... |"
        rows = re.findall(r'^\s*\|\s*([a-z][a-z0-9_]{4,})\s*\|', doc, re.M)
        every = set()
        for distro in registry.DISTROS:
            every |= {t.name for t in registry.tests_for(distro)}
        # Only judge rows that look like test names, not the API table.
        suspect = {r for r in rows
                   if (r.startswith(('check_', 'build_', 'boot_'))
                       or r.endswith('_build'))}
        self.assertFalse(
            suspect - every,
            'DOCUMENT.md documents tests that do not exist: %s'
            % ', '.join(sorted(suspect - every)))


class TestConfigFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for distro in registry.DISTROS:
            os.makedirs(os.path.join(self.root, distro))
        self.workspace = Workspace(self.root)

        # A plausible kernel tree, not just a directory with a .git in it:
        # the path check looks for a top-level Makefile carrying VERSION and
        # PATCHLEVEL, because pointing this at the wrong repository used to
        # be accepted here and fail much later during the build.
        self.src = os.path.join(self.root, 'linux')
        os.makedirs(os.path.join(self.src, '.git'))
        with open(os.path.join(self.src, 'Makefile'), 'w') as fh:
            fh.write('VERSION = 6\nPATCHLEVEL = 12\nSUBLEVEL = 0\n')

    def tearDown(self):
        self.tmp.cleanup()

    def values(self, **overrides):
        base = {
            'LINUX_SRC_PATH': self.src,
            'SIGNER_NAME': 'Hemanth Selam',
            'SIGNER_EMAIL': 'Hemanth.Selam@amd.com',
            'BUGZILLA_ID': '12345',
            'NUM_PATCHES': '5',
            'BUILD_THREADS': '256',
            'VM_IP': '10.0.0.5',
            'VM_ROOT_PWD': 'secret',
            'HOST_USER_PWD': 'hostsecret',
        }
        base.update(overrides)
        return base

    def flags(self, **overrides):
        out = {k: 'yes' for k in registry.test_config_keys('euler')}
        out.update(overrides)
        return out

    def write(self, **overrides):
        self.workspace.write_config(
            'euler', self.values(**overrides), self.flags(), '/tmp/mirror')

    def test_round_trip(self):
        self.write()
        config = self.workspace.read_config('euler')
        self.assertEqual(config['LINUX_SRC_PATH'], self.src)
        self.assertEqual(config['HOST_USER_PWD'], 'hostsecret')
        self.assertEqual(config['TORVALDS_REPO'], '/tmp/mirror')
        self.assertEqual(self.workspace.selected_distro(), 'euler')
        self.assertTrue(self.workspace.is_configured())

    def test_file_is_not_world_readable(self):
        self.write()
        mode = os.stat(self.workspace.configure_path('euler')).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_hostile_password_survives_a_round_trip(self):
        """A quote in a password used to break every later source of the file."""
        nasty = 'p|a&s\'s"w0rd $(id) `whoami` \\ #x'
        self.write(HOST_USER_PWD=nasty)
        config = self.workspace.read_config('euler')
        self.assertEqual(config['HOST_USER_PWD'], nasty)
        # And the following key must still be intact.
        self.assertEqual(config['TORVALDS_REPO'], '/tmp/mirror')

    def test_bad_source_path_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self.write(LINUX_SRC_PATH='/nonexistent/linux')
        self.assertIn('LINUX_SRC_PATH', caught.exception.errors)

        not_git = os.path.join(self.root, 'plain')
        os.makedirs(not_git)
        with self.assertRaises(ConfigError) as caught:
            self.write(LINUX_SRC_PATH=not_git)
        self.assertIn('.git', caught.exception.errors['LINUX_SRC_PATH'])

    def test_relative_source_path_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.write(LINUX_SRC_PATH='linux')

    def test_bad_email_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            self.write(SIGNER_EMAIL='not-an-email')
        self.assertIn('SIGNER_EMAIL', caught.exception.errors)

    def test_bad_numbers_are_rejected(self):
        for field, value in (('BUILD_THREADS', '0'),
                             ('BUILD_THREADS', 'many'),
                             ('NUM_PATCHES', '-1')):
            with self.assertRaises(ConfigError, msg='%s=%s' % (field, value)):
                self.write(**{field: value})

    def test_a_select_field_rejects_a_value_not_on_the_list(self):
        with self.assertRaises(ConfigError):
            self.write(OE_TARGET_BRANCH='not-a-branch')

    def test_vm_settings_only_required_when_boot_test_is_on(self):
        # anolis, because euler no longer boots anything and has no VM
        # section at all.
        flags = {k: 'yes' for k in registry.test_config_keys('anolis')}
        values = dict(self.values(VM_IP='', VM_ROOT_PWD=''), ANBZ_ID='123')

        with self.assertRaises(ConfigError):
            self.workspace.write_config('anolis', values, flags, '/tmp/mirror')

        # Same input, boot test disabled: accepted.
        self.workspace.write_config(
            'anolis', values, dict(flags, TEST_BOOT_KERNEL='no'),
            '/tmp/mirror')
        self.assertEqual(self.workspace.read_config('anolis')['VM_IP'], '')

    def test_euler_asks_for_no_vm_details(self):
        # Nothing in openEuler's CI boots a kernel, so the form must not
        # demand an address and password for a machine that is never used.
        names = {f.name for f in registry.all_fields('euler')}
        self.assertNotIn('VM_IP', names)
        self.assertNotIn('VM_ROOT_PWD', names)

    def test_enabled_tests_reflects_flags(self):
        self.workspace.write_config(
            'euler', self.values(),
            self.flags(TEST_OE_CHECKPATCH='no'), '/tmp/mirror')
        enabled = self.workspace.enabled_tests('euler')
        self.assertFalse(enabled['oe_checkpatch'])
        self.assertTrue(enabled['oe_build_x86_64'])

    def test_redact_hides_secrets(self):
        self.write()
        shown = redact(self.workspace.read_config('euler'))
        self.assertNotIn('hostsecret', shown.values())
        self.assertNotEqual(shown['HOST_USER_PWD'], 'hostsecret')
        # Non-secret values still come back as they are.
        self.assertEqual(shown['BUGZILLA_ID'], '12345')

    def test_unconfigured_workspace(self):
        fresh = Workspace(tempfile.mkdtemp())
        self.assertIsNone(fresh.selected_distro())
        self.assertFalse(fresh.is_configured())
        self.assertIsNone(fresh.read_config())


class TestJobStore(unittest.TestCase):
    def setUp(self):
        # A killed job's worker may still be flushing its log when the test
        # ends, which would make a strict cleanup fail.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Workspace(self.tmp.name)
        os.makedirs(self.workspace.logs_dir, exist_ok=True)
        self.store = JobStore(self.workspace, '/tmp/no-mirror')

    def tearDown(self):
        # Leave no worker running into the next test.
        for job in self.store.active():
            self.store.kill(job['id'])
        deadline = time.time() + 10
        while self.store.active() and time.time() < deadline:
            time.sleep(0.05)
        self.tmp.cleanup()

    def wait_for(self, job_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.store.get(job_id)
            if job['status'] not in ('queued', 'running'):
                return job
            time.sleep(0.05)
        self.fail('job %s never finished (status %s)'
                  % (job_id, self.store.get(job_id)['status']))

    def test_successful_job(self):
        job = self.store.submit('test', ['true'], 'true')
        done = self.wait_for(job['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['exit_code'], 0)
        self.assertEqual(done['progress'], 100)

    def test_failing_job_is_reported_as_failed(self):
        job = self.store.submit('test', ['false'], 'false')
        done = self.wait_for(job['id'])
        self.assertEqual(done['status'], 'failed')
        self.assertNotEqual(done['exit_code'], 0)

    def test_missing_command_does_not_wedge_the_worker(self):
        job = self.store.submit('test', ['definitely-not-a-command'], 'nope')
        done = self.wait_for(job['id'])
        self.assertEqual(done['status'], 'failed')
        self.assertTrue(done['error'])

        # The worker must still pick up the next job.
        follow_up = self.store.submit('test', ['true'], 'true')
        self.assertEqual(self.wait_for(follow_up['id'])['status'], 'completed')

    def test_verdicts_are_collected(self):
        script = (r'printf "\033[0;32m\xe2\x9c\x93 PASS\033[0m: check_patch\n";'
                  r'printf "\xe2\x9c\x97 FAIL: oe_build_x86_64\n";'
                  r'printf "\xe2\x8a\x98 SKIP: boot_kernel\n";'
                  r'echo "gcc: warning about PASS: not a verdict"')
        job = self.store.submit('test_all', ['sh', '-c', script], 'verdicts',
                                total_steps=3)
        done = self.wait_for(job['id'])
        self.assertEqual(
            done['results'],
            [{'verdict': 'PASS', 'test': 'check_patch'},
             {'verdict': 'FAIL', 'test': 'oe_build_x86_64'},
             {'verdict': 'SKIP', 'test': 'boot_kernel'}])
        self.assertEqual(done['failed_tests'], ['oe_build_x86_64'])

    def test_log_is_read_incrementally(self):
        job = self.store.submit(
            'test', ['sh', '-c', 'for i in 1 2 3; do echo line$i; done'],
            'lines')
        self.wait_for(job['id'])

        first = self.store.read_log(job['id'], offset=0)
        self.assertIn('line1', first['text'])
        self.assertGreater(first['offset'], 0)

        # Reading from the returned offset yields nothing new.
        again = self.store.read_log(job['id'], offset=first['offset'])
        self.assertEqual(again['text'], '')
        self.assertEqual(again['offset'], first['offset'])

    def test_log_text_has_no_escape_sequences(self):
        """Left in, they render as literal "[0;34m" litter in the browser."""
        job = self.store.submit(
            'test', ['printf', '\033[0;32mgreen\033[0m and \033[1;33myellow\033[0m\n'],
            'colours')
        self.wait_for(job['id'])
        text = self.store.read_log(job['id'], offset=0)['text']
        self.assertIn('green and yellow', text)
        self.assertNotIn('\033', text)

    def test_log_chunks_end_on_a_line_boundary(self):
        job = self.store.submit(
            'test', ['sh', '-c', 'seq 1 500'], 'seq')
        self.wait_for(job['id'])
        chunk = self.store.read_log(job['id'], offset=0)
        self.assertTrue(chunk['text'].endswith('\n'))

    def test_offset_past_end_restarts(self):
        """A truncated or rotated log must not leave the viewer stuck."""
        job = self.store.submit('test', ['echo', 'hi'], 'hi')
        self.wait_for(job['id'])
        chunk = self.store.read_log(job['id'], offset=10 ** 9)
        self.assertTrue(chunk['truncated'])
        self.assertIn('hi', chunk['text'])
        self.assertEqual(chunk['offset'], chunk['size'])

    def test_job_output_does_not_land_in_the_test_script_log(self):
        """The runner used to capture make's stdout into the very file the
        test script writes, so both truncated each other."""
        job = self.store.submit(
            'test', ['echo', 'from make'], 'make euler-test=oe_build_ppc',
            test_name='oe_build_ppc', distro='euler')
        done = self.wait_for(job['id'])
        self.assertNotEqual(done['log_file'], done['test_log_file'])
        self.assertTrue(done['test_log_file'].endswith('oe_build_ppc.log'))

    def wait_until_running(self, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.store.get(job_id)['status'] == 'running':
                return
            time.sleep(0.05)
        self.fail('job %s never started' % job_id)

    def test_queued_job_can_be_cancelled(self):
        blocker = self.store.submit('test', ['sleep', '60'], 'sleep')
        self.wait_until_running(blocker['id'])

        queued = self.store.submit('test', ['true'], 'true')
        ok, _ = self.store.kill(queued['id'])
        self.assertTrue(ok)
        self.assertEqual(self.store.get(queued['id'])['status'], 'cancelled')

        self.store.kill(blocker['id'])
        self.wait_for(blocker['id'])

        # A cancelled job must stay cancelled once the worker reaches it.
        self.assertEqual(self.store.get(queued['id'])['status'], 'cancelled')

    def test_running_job_can_be_killed(self):
        job = self.store.submit('test', ['sleep', '60'], 'sleep')
        self.wait_until_running(job['id'])

        ok, _ = self.store.kill(job['id'])
        self.assertTrue(ok)
        self.assertEqual(self.wait_for(job['id'])['status'], 'killed')

    def test_killing_an_unknown_job(self):
        ok, message = self.store.kill('nope')
        self.assertFalse(ok)
        self.assertEqual(message, 'No such job')

    def test_jobs_are_serialised(self):
        """Two jobs must not run at once: they share one kernel tree."""
        marker = os.path.join(self.tmp.name, 'concurrent')
        script = ('set -e; test ! -e %(m)s; touch %(m)s; sleep 0.4; rm %(m)s'
                  % {'m': marker})
        jobs = [self.store.submit('test', ['sh', '-c', script], 'overlap')
                for _ in range(3)]
        for job in jobs:
            self.assertEqual(self.wait_for(job['id'])['status'], 'completed')

    def test_queue_position_is_reported(self):
        blocker = self.store.submit('test', ['sleep', '60'], 'sleep')
        self.wait_until_running(blocker['id'])

        first = self.store.submit('test', ['true'], 'true')
        second = self.store.submit('test', ['true'], 'true')
        # Only a waiting job has a position; the one already running has none.
        self.assertIsNone(self.store.get(blocker['id'])['queue_position'])
        self.assertEqual(self.store.get(first['id'])['queue_position'], 1)
        self.assertEqual(self.store.get(second['id'])['queue_position'], 2)

        for job in (first, second, blocker):
            self.store.kill(job['id'])

    def test_history_survives_a_restart_and_marks_interrupted_jobs(self):
        job = self.store.submit('test', ['true'], 'true')
        self.wait_for(job['id'])

        # A second store over the same state directory is what a restart
        # looks like to the job index.
        reopened = JobStore(self.workspace, '/tmp/no-mirror')
        self.assertEqual(reopened.get(job['id'])['status'], 'completed')

    def test_clear_history_keeps_active_jobs(self):
        finished = self.store.submit('test', ['true'], 'true')
        self.wait_for(finished['id'])
        running = self.store.submit('test', ['sleep', '30'], 'sleep')

        self.store.clear_history()
        self.assertIsNone(self.store.get(finished['id']))
        self.assertIsNotNone(self.store.get(running['id']))

        self.store.kill(running['id'])

    def test_argv_is_not_exposed_to_the_browser(self):
        job = self.store.submit('test', ['true'], 'true')
        self.assertNotIn('argv', job)


class TestAnsi(unittest.TestCase):
    def test_colour_codes_are_removed(self):
        self.assertEqual(strip_ansi('\033[0;32m\u2713 PASS\033[0m: x'),
                         '\u2713 PASS: x')

    def test_carriage_control_is_left_alone(self):
        self.assertEqual(strip_ansi('plain text'), 'plain text')

    def test_title_sequences_are_removed(self):
        self.assertEqual(strip_ansi('\033]0;title\007done'), 'done')


class TestMirrorFreshness(unittest.TestCase):
    """configure, check_dependency and the web build job each sync the
    mirror, so back-to-back runs used to refetch it for nothing.

    git is stubbed throughout.  sync() answers a failed fetch by deleting the
    mirror and cloning it again, so a test that let the real git run against
    this fixture -- which is a directory, not a repository -- pulled several
    gigabytes of mainline into /tmp before anyone noticed.
    """

    def setUp(self):
        from unittest import mock

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mirror = os.path.join(self.tmp.name, 'mirror')
        os.makedirs(self.mirror)

        self.git_calls = []
        patcher = mock.patch.object(repo, '_git', self.fake_git)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.addCleanup(setattr, repo, 'MAX_AGE', repo.MAX_AGE)

    def fake_git(self, args, cwd=None, timeout=None, say=None):
        self.git_calls.append(args[0])
        return repo._Result(0, '')

    def stamp(self, seconds_ago):
        path = os.path.join(self.mirror, 'FETCH_HEAD')
        open(path, 'w').close()
        when = time.time() - seconds_ago
        os.utime(path, (when, when))

    def test_age_is_none_before_the_first_fetch(self):
        self.assertIsNone(repo._age(self.mirror))

    def test_age_reads_the_fetch_head_stamp(self):
        self.stamp(600)
        self.assertAlmostEqual(repo._age(self.mirror), 600, delta=5)

    def test_a_fresh_mirror_is_not_fetched_again(self):
        repo.MAX_AGE = 1800
        self.stamp(60)
        said = []
        self.assertTrue(repo.sync(self.mirror, emit=said.append))
        self.assertEqual(self.git_calls, [])
        self.assertIn('not fetching again', ' '.join(said))

    def test_a_stale_mirror_is_fetched(self):
        repo.MAX_AGE = 1800
        self.stamp(3600)
        self.assertTrue(repo.sync(self.mirror, emit=lambda line: None))
        self.assertEqual(self.git_calls, ['fetch'])

    def test_a_mirror_that_never_fetched_is_fetched(self):
        # No FETCH_HEAD means no evidence of freshness, so the guard has to
        # stand aside rather than treat an unknown age as recent.
        repo.MAX_AGE = 1800
        self.assertTrue(repo.sync(self.mirror, emit=lambda line: None))
        self.assertEqual(self.git_calls, ['fetch'])

    def test_zero_disables_the_guard(self):
        repo.MAX_AGE = 0
        self.stamp(1)
        self.assertTrue(repo.sync(self.mirror, emit=lambda line: None))
        self.assertEqual(self.git_calls, ['fetch'])


class TestOpenEulerVerdicts(unittest.TestCase):
    """Reading openEuler's own checks correctly.

    Their six scripts report in four different shapes and none of them set
    a useful exit status, so the printed text is the only verdict there is.
    Misreading it turns a rejected patch into a passing one, which is worse
    than having no check at all.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'euler'))
        self.addCleanup(sys.path.remove,
                        os.path.join(PROJECT_ROOT, 'euler'))
        import oe_checks
        self.verdict = oe_checks.verdict

    def test_checkpatch_counts(self):
        self.assertEqual(
            self.verdict(['---- result ----',
                          'total:100 failed:8 warning:0 success:92'])[0],
            'fail')

    def test_checkpatch_warnings_are_not_failures(self):
        status, _ = self.verdict(
            ['---- result ----',
             'total:100 failed:0 warning:2 success:98'])
        self.assertEqual(status, 'warn')

    def test_conflict_spaces_after_the_colon(self):
        # check_conflict.py is the only one that writes "failed: 31".
        self.assertEqual(
            self.verdict(['---- result ----',
                          'total: 100 failed: 31 success: 69'])[0],
            'fail')

    def test_conflict_omits_the_failed_key_when_clean(self):
        # Nothing says "failed" here.  Reading that as unparseable would
        # report an error on every clean run.
        self.assertEqual(
            self.verdict(['---- result ----', 'total: 2 success: 2'])[0],
            'pass')

    def test_all_clear_sentences(self):
        for line in ('check 100 patch(es) success', 'check 43 file(s) success'):
            self.assertEqual(self.verdict(['---- result ----', line])[0],
                             'pass')

    def test_depend_names_failures_and_never_counts_them(self):
        status, detail = self.verdict(
            ['---- results ----',
             'check failed: abc123 net: a thing',
             'missing 6842427bf299',
             'check failed: def456 net: another thing'])
        self.assertEqual(status, 'fail')
        self.assertIn('2', detail)

    def test_depend_silence_under_the_banner_is_success(self):
        self.assertEqual(self.verdict(['---- results ----'])[0], 'pass')

    def test_no_result_at_all_is_an_error_not_a_pass(self):
        # A script that died before reporting has not approved anything.
        status, _ = self.verdict(['Traceback (most recent call last):',
                                  'UnicodeDecodeError: bad byte'])
        self.assertEqual(status, 'error')

    def test_per_commit_failed_lines_are_not_miscounted(self):
        # pr_checkpatch prints "check <sha> failed" per commit as well as a
        # total.  The total is what counts.
        status, detail = self.verdict(
            ['check abc123 failed',
             'check def456 failed',
             '---- result ----',
             'total:2 failed:2 warning:0 success:0'])
        self.assertEqual(status, 'fail')
        self.assertIn('2 failed', detail)


class TestBuildProgress(unittest.TestCase):
    """The build scripts' own output is what drives the build progress bar."""

    def feed(self, lines):
        """Replay log lines through the same matching _observe does."""
        state = {'step': 0, 'total': 0, 'phases': 0, 'phase': None}
        seen = []
        for raw in lines:
            clean = strip_ansi(raw).rstrip()
            patch = jobs._PATCH_RE.match(clean)
            phase = jobs._PHASE_RE.match(clean)
            if patch:
                state.update(step=int(patch.group(1)),
                             total=int(patch.group(2)),
                             phases=0, phase=None, label=patch.group(3))
            elif phase and state['step']:
                state['phase'] = phase.group(1)
                state['phases'] += 1
            if state['step'] and state['total']:
                within = min(0.95, state['phases'] / float(jobs._PHASES_PER_PATCH))
                seen.append(min(99, int((state['step'] - 1 + within)
                                        * 100 / state['total'])))
        return state, seen

    def anolis_lines(self, patches):
        out = []
        for i in range(1, patches + 1):
            out.append('\033[0;34m[%d/%d] Processing: %04d-fix.patch\033[0m'
                       % (i, patches, i))
            for label in ('Applying  ', 'Checkpatch', 'Building  '):
                out.append('  %s : \033[0;32m\u2713 PASS\033[0m' % label)
        return out

    def test_patch_header_sets_step_and_total(self):
        state, _ = self.feed(self.anolis_lines(3))
        self.assertEqual(state['step'], 3)
        self.assertEqual(state['total'], 3)
        self.assertEqual(state['label'], '0003-fix.patch')

    def test_progress_never_goes_backwards(self):
        _, seen = self.feed(self.anolis_lines(4))
        self.assertEqual(seen, sorted(seen))

    def test_progress_stays_below_complete_until_the_job_ends(self):
        _, seen = self.feed(self.anolis_lines(2))
        self.assertLess(max(seen), 100)

    def test_a_patch_never_claims_the_next_ones_share(self):
        # Finishing every phase of patch 1 of 4 must stay under 25%.
        _, seen = self.feed(self.anolis_lines(4)[:4])
        self.assertLess(max(seen), 25)

    def test_apply_only_output_still_advances(self):
        # openEuler applies patches without compiling them.
        lines = []
        for i in (1, 2):
            lines.append('[%d/2] Processing: %04d-fix.patch' % (i, i))
            lines.append('  Applying   : \u2713 PASS')
        _, seen = self.feed(lines)
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(seen[-1], seen[0])

    def test_a_failed_phase_is_still_a_phase(self):
        state, _ = self.feed(['[1/1] Processing: 0001-fix.patch',
                              '  Applying   : \u2717 FAIL'])
        self.assertEqual(state['phase'], 'Applying')

    def test_compiler_output_is_not_mistaken_for_a_patch_header(self):
        # A kernel build prints a great deal that looks vaguely like this.
        for noise in ('[1/3] Building modules',
                      'note: [2/5] Processing: not at the line start',
                      '  CC [M]  drivers/foo.o',
                      'make[2]: Entering directory'):
            self.assertIsNone(jobs._PATCH_RE.match(noise),
                              'patch header matched %r' % noise)

    def test_phase_line_needs_a_verdict(self):
        # "Building   : " with no verdict is the announcement, not the result.
        self.assertIsNone(jobs._PHASE_RE.match('  Building   : starting'))
        self.assertIsNotNone(jobs._PHASE_RE.match('  Building   : PASS'))


class TestPatchCategory(unittest.TestCase):
    """Reading the category out of the commit rather than asking for it.

    It used to be one answer from the configuration form, stamped on
    every patch in the series.  That is wrong the moment a series mixes
    a fix with a cleanup, and it is the submitter guessing at something
    the commit message already states.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'euler'))
        self.addCleanup(sys.path.remove, os.path.join(PROJECT_ROOT, 'euler'))
        import oe_header
        self.decide = oe_header.decide_category

    def category(self, subject, message=''):
        return self.decide(subject, message or subject)[0]

    def test_a_cve_is_a_security_patch(self):
        self.assertEqual(
            self.category('net: fix a thing', 'Fixes CVE-2023-12345 here.'),
            'security')

    def test_cve_outranks_the_fixes_tag(self):
        # Both are present on most CVE fixes; the CVE is the more
        # specific statement.
        self.assertEqual(
            self.category('net: fix a thing',
                          'CVE-2023-12345\nFixes: abcdef123456 ("x")'),
            'security')

    def test_copied_to_stable_means_bugfix(self):
        # The stable rules only accept fixes, so a maintainer sending it
        # there has already classified it.
        self.assertEqual(
            self.category('net: rework the thing',
                          'Cc: <stable@vger.kernel.org>'),
            'bugfix')

    def test_a_fixes_tag_means_bugfix(self):
        self.assertEqual(
            self.category('net: rework the thing',
                          'Fixes: abcdef123456 ("earlier commit")'),
            'bugfix')

    def test_a_revert_is_a_bugfix(self):
        self.assertEqual(self.category('Revert "net: add a thing"'), 'bugfix')

    def test_the_subject_decides_when_nothing_else_does(self):
        self.assertEqual(self.category('net: fix a null deref'), 'bugfix')
        self.assertEqual(self.category('net: avoid a race on close'),
                         'bugfix')
        self.assertEqual(self.category('net: optimise the hot path'),
                         'performance')

    def test_an_addition_is_a_feature(self):
        for subject in ('net: add support for the new chip',
                        'docs: describe the new sysfs knob',
                        'KABI: reserve padding in struct foo'):
            self.assertEqual(self.category(subject), 'feature', subject)

    def test_fix_inside_another_word_is_not_a_fix(self):
        # "prefix" and "suffix" end in the same three letters.
        self.assertEqual(self.category('net: add a prefix to the log line'),
                         'feature')

    def test_the_author_can_say_so_outright(self):
        self.assertEqual(
            self.category('net: add a thing', 'category: performance\n'),
            'performance')

    def test_the_subject_outranks_the_body(self):
        # A performance patch often explains which bug-shaped symptom it
        # relieves; what it is for is what the author put in the subject.
        self.assertEqual(
            self.category('net: speed up the lookup',
                          'The old code could stall under load.'),
            'performance')


class TestOpenEulerBuildVerdicts(unittest.TestCase):
    """What oe_build.sh concludes, with the compiler stubbed out.

    The build itself takes an hour and is not what goes wrong.  What goes
    wrong is the bookkeeping around it: openEuler forgives some warnings
    on some branches, and an architecture their matrix does not build has
    to report skipped rather than passed, or a run that compiled nothing
    reads as a run that found nothing.
    """

    #: Replaces the real build.  $6 is the warnings file for the cross
    #: path, and the function's exit status is make's.
    HARNESS = r'''
        . "%(root)s/euler/oe_build.sh"
        _oe_cross_build() { %(stub)s; }
        _oe_kabi_build()  { %(kabi)s; }
        _oe_prepare_whitelists() { return 0; }
        _oe_failed_before_the_series() { return %(prior)s; }
        oe_build_arch "%(arch)s" >/dev/null 2>&1
    '''

    def build(self, arch, branch, stub=':', kabi=':', broken_before=False):
        script = self.HARNESS % {
            'root': PROJECT_ROOT, 'stub': stub, 'kabi': kabi, 'arch': arch,
            'prior': '0' if broken_before else '1',
        }
        env = dict(
            os.environ,
            SCRIPT_DIR=os.path.join(PROJECT_ROOT, 'euler'),
            WORKDIR=PROJECT_ROOT,
            LINUX_SRC_PATH=tempfile.gettempdir(),
            OE_TARGET_BRANCH=branch,
            BUILD_THREADS='1',
            NUM_PATCHES='1',
        )
        return subprocess.call(['bash', '-c', script], env=env)

    #: A warning on stderr from the incremental build after the patches.
    WARNED = r'echo "fs/foo.c:12: warning: unused variable" > $6; true'
    #: make exited non-zero.
    BROKE = r'echo "error: no rule to make target" > $6; false'

    def test_clean_build_passes(self):
        self.assertEqual(self.build('ppc', 'OLK-6.6'), 0)

    def test_a_warning_the_patch_introduced_fails(self):
        self.assertEqual(self.build('ppc', 'OLK-6.6', self.WARNED), 1)

    def test_their_olk_5_10_powerpc_exemption_is_honoured(self):
        # openEuler tolerates powerpc warnings on OLK-5.10 and we cannot be
        # stricter than the gate we are predicting.
        self.assertEqual(self.build('ppc', 'OLK-5.10', self.WARNED), 0)

    def test_the_exemption_is_only_that_branch_and_that_arch(self):
        self.assertEqual(self.build('riscv64', 'OLK-5.10', self.WARNED), 1)
        self.assertEqual(self.build('ppc', 'OLK-6.6', self.WARNED), 1)

    def test_the_exemption_does_not_rescue_a_build_that_failed(self):
        self.assertEqual(self.build('ppc', 'OLK-5.10', self.BROKE), 1)

    def test_a_tree_that_was_already_broken_is_not_the_series_fault(self):
        # OLK-6.6 does not compile its own hinic drivers under gcc 12.3.
        # Calling that a rejected patch teaches people to ignore the
        # result, which costs more than the check is worth.
        self.assertEqual(
            self.build('ppc', 'OLK-6.6', self.BROKE, broken_before=True), 4)
        self.assertEqual(
            self.build('x86_64', 'OLK-6.6',
                       kabi=r'printf "| x86_64 allmodconfig build '
                            r'| broken already, not your series |\n" >> $8',
                       broken_before=True),
            4)

    def test_the_same_failure_on_a_clean_tree_is_the_series_fault(self):
        self.assertEqual(
            self.build('ppc', 'OLK-6.6', self.BROKE, broken_before=False), 1)

    def test_an_arch_they_do_not_build_is_skipped_not_passed(self):
        # loongarch is false on every branch in their check_build.yaml,
        # and 22.03 is aarch64 and x86_64 only.
        self.assertEqual(self.build('loongarch', 'OLK-6.6'), 3)
        self.assertEqual(self.build('riscv64', 'openEuler-22.03-LTS'), 3)
        self.assertEqual(self.build('x86_64', 'openEuler-22.03-LTS',
                                    kabi='true'), 0)

    def test_a_failed_kabi_row_fails_the_test(self):
        rows = (r'printf "| x86_64 allmodconfig build | pass |\n'
                r'| x86_64 checkkabi | %s |\n" >> $8')
        self.assertEqual(self.build('x86_64', 'OLK-6.6',
                                    kabi=rows % 'pass'), 0)
        self.assertEqual(self.build('x86_64', 'OLK-6.6',
                                    kabi=rows % 'fail'), 1)

    def test_a_broken_matrix_check_is_an_error_not_a_skip(self):
        # check_branch.py exits 1 both for "this arch is off" and for a
        # failed import.  Telling them apart is the difference between a
        # build gate and a build gate that never runs.
        script = (
            '. "%s/euler/oe_build.sh"\n'
            '_oe_arch_wanted x86_64 OLK-6.6 /nonexistent' % PROJECT_ROOT)
        self.assertEqual(subprocess.call(['bash', '-c', script],
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL), 2)


class TestCleanTree(unittest.TestCase):
    """require_clean_tree, which now runs before anything is rewritten.

    It used to run after the rewind, so a tree that was not clean cost
    the whole run: every header written, the branch rewound, and then a
    refusal that left it there.  And what made the tree unclean was a
    JSON file openEuler's own conflict check had written into it.
    """

    def setUp(self):
        self.kernel = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.kernel,
                                                            True))
        subprocess.check_call(['git', 'init', '-q', self.kernel])
        for key, value in (('user.name', 'T'), ('user.email', 't@e.com'),
                           ('commit.gpgsign', 'false')):
            subprocess.check_call(['git', '-C', self.kernel, 'config',
                                   key, value])
        self.write('tracked.c', 'int main(void);\n')
        subprocess.check_call(['git', '-C', self.kernel, 'add', '.'])
        subprocess.check_call(['git', '-C', self.kernel, 'commit', '-q',
                               '-m', 'first'])

    def write(self, name, text):
        with open(os.path.join(self.kernel, name), 'w') as f:
            f.write(text)

    def run_check(self):
        script = ('. "%s/lib/log.sh"\n. "%s/lib/worktree.sh"\n'
                  'require_clean_tree "%s"\n'
                  % (PROJECT_ROOT, PROJECT_ROOT, self.kernel))
        done = subprocess.run(['bash', '-c', script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode()

    def exists(self, name):
        return os.path.exists(os.path.join(self.kernel, name))

    def test_a_clean_tree_passes(self):
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)

    def test_leftovers_from_their_checks_are_swept_not_complained_about(self):
        # This is the exact file that cost a real run: check_conflict.py
        # writes it into the kernel tree for a comment-posting step we
        # do not have, and nothing under Jenkins ever cleans it up.
        self.write('checkconflict_diff_info.json', '{}')
        self.write('branch_0123456789ab.txt', 'diff')
        self.write('mainline_0123456789ab.txt', 'diff')
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)
        self.assertFalse(self.exists('checkconflict_diff_info.json'))
        self.assertFalse(self.exists('branch_0123456789ab.txt'))
        self.assertFalse(self.exists('mainline_0123456789ab.txt'))

    def test_a_tracked_file_is_never_swept(self):
        # A real source file that happens to match the pattern must
        # survive, so the sweep only ever touches what git does not know.
        self.write('branch_0123456789ab.txt', 'mine')
        subprocess.check_call(['git', '-C', self.kernel, 'add',
                               'branch_0123456789ab.txt'])
        subprocess.check_call(['git', '-C', self.kernel, 'commit', '-q',
                               '-m', 'keep me'])
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.exists('branch_0123456789ab.txt'))

    def test_uncommitted_work_stops_the_run(self):
        # This is what the check is for: the rewind would destroy it.
        self.write('tracked.c', 'int main(void) { return 1; }\n')
        rc, out = self.run_check()
        self.assertEqual(rc, 12)
        self.assertIn('tracked.c', out)

    def test_an_unrelated_untracked_file_is_not_a_reason_to_refuse(self):
        # It survives the rewind untouched, so refusing over one means
        # refusing over a stray editor backup.
        self.write('notes.txt~', 'scratch')
        rc, out = self.run_check()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.exists('notes.txt~'))


class TestReadiness(unittest.TestCase):
    """Whether the UI will let a test run.

    An unprepared series fails openEuler's checks for reasons that are
    the tool's omissions rather than the patch's faults, so the run
    buttons are gated on this.  A gate that fails open is worse than no
    gate: it looks like a verdict.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root,
                                                            True))
        os.makedirs(os.path.join(self.root, 'euler'))
        readiness.forget()

    def write_ready(self, body):
        path = os.path.join(self.root, 'euler', 'ready.sh')
        with open(path, 'w') as f:
            f.write('#!/usr/bin/env bash\n' + body + '\n')
        os.chmod(path, 0o755)

    def test_a_ready_series_is_ready(self):
        self.write_ready('echo "all 3 commit(s) are ready to test"; exit 0')
        ready, why = readiness.check(self.root, 'euler')
        self.assertTrue(ready)
        self.assertIn('ready to test', why)

    def test_an_unready_series_reports_why(self):
        self.write_ready('echo "abc123 subj: no inclusion header"; exit 1')
        ready, why = readiness.check(self.root, 'euler')
        self.assertFalse(ready)
        self.assertIn('no inclusion header', why)

    def test_a_missing_check_does_not_mean_ready(self):
        ready, why = readiness.check(self.root, 'euler')
        self.assertFalse(ready)
        self.assertIn('no readiness check', why)

    def test_a_broken_check_does_not_mean_ready(self):
        self.write_ready('exit 3')
        self.assertFalse(readiness.check(self.root, 'euler')[0])

    def test_the_answer_is_cached_between_polls(self):
        # The page polls every couple of seconds and the openEuler check
        # renders a diff per commit, so asking every time is not free.
        counter = os.path.join(self.root, 'runs')
        self.write_ready('echo x >> "%s"; exit 0' % counter)
        readiness.check(self.root, 'euler')
        readiness.check(self.root, 'euler')
        with open(counter) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_forget_asks_again(self):
        counter = os.path.join(self.root, 'runs')
        self.write_ready('echo x >> "%s"; exit 0' % counter)
        readiness.check(self.root, 'euler')
        readiness.forget('euler')
        readiness.check(self.root, 'euler')
        with open(counter) as f:
            self.assertEqual(len(f.readlines()), 2)


class TestReadyScripts(unittest.TestCase):
    """The shipped ready.sh scripts, against a throwaway tree."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.root,
                                                            True))
        self.kernel = os.path.join(self.root, 'kernel')
        subprocess.check_call(['git', 'init', '-q', self.kernel])
        for key, value in (('user.name', 'T'), ('user.email', 't@e.com'),
                           ('commit.gpgsign', 'false')):
            subprocess.check_call(['git', '-C', self.kernel, 'config',
                                   key, value])

    def commit(self, message):
        path = os.path.join(self.kernel, 'f')
        with open(path, 'a') as f:
            f.write('x\n')
        subprocess.check_call(['git', '-C', self.kernel, 'add', 'f'])
        subprocess.check_call(['git', '-C', self.kernel, 'commit', '-q',
                               '-m', message])

    def run_ready(self, distro, config):
        # A copy, so the developer's own .configure is never read or
        # written by the tests.
        import shutil
        target = os.path.join(self.root, distro)
        shutil.copytree(os.path.join(PROJECT_ROOT, distro), target,
                        symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(
                            'kernel', 'hulk_robot_test', '__pycache__'))
        with open(os.path.join(target, '.configure'), 'w') as f:
            f.write(config)
        done = subprocess.run(
            ['bash', os.path.join(target, 'ready.sh')],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode()

    ANOLIS = ('LINUX_SRC_PATH="%s"\nNUM_PATCHES=1\nANBZ_ID="1234"\n'
              'SIGNER_NAME="T"\nSIGNER_EMAIL="t@e.com"\n')

    def test_anolis_wants_the_anbz_tag_and_a_sign_off(self):
        self.commit('a patch\n\nno tags here')
        rc, out = self.run_ready('anolis', self.ANOLIS % self.kernel)
        self.assertEqual(rc, 1)
        self.assertIn('ANBZ: #1234', out)

        self.commit('a patch\n\nANBZ: #1234\n\nSigned-off-by: T <t@e.com>')
        rc, out = self.run_ready('anolis', self.ANOLIS % self.kernel)
        self.assertEqual(rc, 0, out)

    def test_a_short_branch_is_not_ready(self):
        self.commit('only one\n\nANBZ: #1234\n\nSigned-off-by: T <t@e.com>')
        config = self.ANOLIS.replace('NUM_PATCHES=1',
                                     'NUM_PATCHES=5') % self.kernel
        rc, out = self.run_ready('anolis', config)
        self.assertEqual(rc, 1)
        self.assertIn('expected 5', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
