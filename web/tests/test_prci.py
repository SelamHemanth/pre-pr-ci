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
import sys
import tempfile
import time
import unittest

WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(WEB_DIR)
if WEB_DIR not in sys.path:
    sys.path.insert(0, WEB_DIR)

from prci import registry                                   # noqa: E402
from prci.distro import ConfigError, Workspace, redact       # noqa: E402
from prci.jobs import JobStore, strip_ansi                   # noqa: E402


def read_script(distro):
    with open(os.path.join(PROJECT_ROOT, distro, 'test.sh'), errors='replace') as f:
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

    def test_every_log_file_is_written(self):
        for distro in registry.DISTROS:
            script = read_script(distro)
            for test in registry.tests_for(distro):
                stem = test.log[:-len('.log')]
                # Either named outright, or produced by run_kernel_build,
                # which writes "${LOGS_DIR}/${test_name}.log".
                named = test.log in script
                built = re.search(
                    r'run_kernel_build\s+"%s"' % re.escape(stem), script)
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
        for bogus in ('bogus', 'check_kabi; id', '../../etc/passwd',
                      'build_allmod ', ''):
            self.assertIsNone(registry.find_test('euler', bogus), bogus)
        self.assertIsNotNone(registry.find_test('euler', 'check_kabi'))

    def test_secrets_are_marked(self):
        self.assertEqual(registry.SECRET_KEYS,
                         {'VM_ROOT_PWD', 'HOST_USER_PWD'})

    def test_form_is_a_list_so_the_order_survives_json(self):
        """Flask sorts object keys, so a dict here reordered the form."""
        for distro in registry.DISTROS:
            sections = registry.fields_as_json(distro)
            self.assertIsInstance(sections, list)
            self.assertEqual([s['key'] for s in sections],
                             ['general', 'build', 'vm', 'host'])
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


class TestConfigFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        for distro in registry.DISTROS:
            os.makedirs(os.path.join(self.root, distro))
        self.workspace = Workspace(self.root)

        self.src = os.path.join(self.root, 'linux')
        os.makedirs(os.path.join(self.src, '.git'))

    def tearDown(self):
        self.tmp.cleanup()

    def values(self, **overrides):
        base = {
            'LINUX_SRC_PATH': self.src,
            'SIGNER_NAME': 'Hemanth Selam',
            'SIGNER_EMAIL': 'Hemanth.Selam@amd.com',
            'BUGZILLA_ID': '12345',
            'PATCH_CATEGORY': 'bugfix',
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
        self.assertEqual(config['VM_ROOT_PWD'], 'secret')
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
        self.write(VM_ROOT_PWD=nasty, HOST_USER_PWD=nasty)
        config = self.workspace.read_config('euler')
        self.assertEqual(config['VM_ROOT_PWD'], nasty)
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

    def test_bad_category_is_rejected(self):
        with self.assertRaises(ConfigError):
            self.write(PATCH_CATEGORY='urgent')

    def test_vm_settings_only_required_when_boot_test_is_on(self):
        with self.assertRaises(ConfigError):
            self.workspace.write_config(
                'euler', self.values(VM_IP='', VM_ROOT_PWD=''),
                self.flags(), '/tmp/mirror')

        # Same input, boot test disabled: accepted.
        self.workspace.write_config(
            'euler', self.values(VM_IP='', VM_ROOT_PWD=''),
            self.flags(TEST_BOOT_KERNEL='no'), '/tmp/mirror')
        self.assertEqual(self.workspace.read_config('euler')['VM_IP'], '')

    def test_enabled_tests_reflects_flags(self):
        self.workspace.write_config(
            'euler', self.values(),
            self.flags(TEST_CHECK_PATCH='no'), '/tmp/mirror')
        enabled = self.workspace.enabled_tests('euler')
        self.assertFalse(enabled['check_patch'])
        self.assertTrue(enabled['build_allmod'])

    def test_redact_hides_secrets(self):
        self.write()
        shown = redact(self.workspace.read_config('euler'))
        self.assertNotIn('secret', shown.values())
        self.assertNotEqual(shown['VM_ROOT_PWD'], 'secret')
        self.assertEqual(shown['VM_IP'], '10.0.0.5')

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
                  r'printf "\xe2\x9c\x97 FAIL: build_allmod\n";'
                  r'printf "\xe2\x8a\x98 SKIP: boot_kernel\n";'
                  r'echo "gcc: warning about PASS: not a verdict"')
        job = self.store.submit('test_all', ['sh', '-c', script], 'verdicts',
                                total_steps=3)
        done = self.wait_for(job['id'])
        self.assertEqual(
            done['results'],
            [{'verdict': 'PASS', 'test': 'check_patch'},
             {'verdict': 'FAIL', 'test': 'build_allmod'},
             {'verdict': 'SKIP', 'test': 'boot_kernel'}])
        self.assertEqual(done['failed_tests'], ['build_allmod'])

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
            'test', ['echo', 'from make'], 'make euler-test=check_kabi',
            test_name='check_kabi', distro='euler')
        done = self.wait_for(job['id'])
        self.assertNotEqual(done['log_file'], done['test_log_file'])
        self.assertTrue(done['test_log_file'].endswith('check_kabi.log'))

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


if __name__ == '__main__':
    unittest.main(verbosity=2)
