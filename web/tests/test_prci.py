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
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
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
                if test.name.startswith('oe_build_'):
                    # These are dispatched by pattern, from the same
                    # matrix the registry reads; the two lists are
                    # compared against each other below.
                    self.assertRegex(
                        script, r'(?m)^\s*oe_build_\*\)\s*$',
                        '%s/test.sh has no pattern label for the builds'
                        % distro)
                    continue
                # The dispatcher is a case statement: "  <name>)"
                self.assertRegex(
                    script, r'(?m)^\s*%s\)\s*$' % re.escape(test.name),
                    '%s/test.sh has no case label for %r'
                    % (distro, test.name))

    def test_both_sides_read_the_same_architecture_matrix(self):
        """registry.py and oe_build.sh must not hold two copies of it.

        They did, and the copies disagreed, which is the only way a
        list read off one file can come out two different lengths.
        """
        out = subprocess.run(
            ['bash', '-c',
             '. "%s/euler/oe_build.sh"; _oe_arches_they_build' % PROJECT_ROOT],
            env=dict(os.environ,
                     SCRIPT_DIR=os.path.join(PROJECT_ROOT, 'euler')),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.assertEqual(out.stdout.decode().split(),
                         list(registry.architectures_they_build()))

    def test_the_architectures_come_from_their_file(self):
        """Every name we offer is a name their matrix names.

        Whether a branch has it on is a separate question, answered per
        run by the build itself.  Reading only the true rows dropped
        loongarch, and a missing row reads as a check that does not
        exist rather than one this branch turns off.
        """
        theirs = read_file('euler', 'hulk_robot_test', 'openEuler', 'conf',
                           'check_build.yaml')
        ours = registry.architectures_they_build()
        for arch in ours:
            self.assertRegex(theirs, r'(?m)^\s+%s:\s*(true|false)\s*$' % arch)
        self.assertIn('loongarch', ours)

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
                    # run_oe_build is now called once per architecture
                    # in their matrix rather than once per literal
                    # name, so the name to look for is the variable.
                    wanted = 'arch'
                    named = named or 'run_oe_build "${arch}"' in script
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
                if test.name.startswith('oe_build_'):
                    # Built from the architecture rather than written
                    # out; checked by the test below, which runs the
                    # script's own line to see what name it arrives at.
                    continue
                self.assertIn(
                    test.config_key, script,
                    '%s/test.sh ignores %s, so disabling %r in the UI would '
                    'have no effect' % (distro, test.config_key, test.name))

    def test_the_build_switches_reach_the_names_the_ui_writes(self):
        """The UI's TEST_* key and the script's variable must be one name.

        The script derives it now instead of spelling each one out, so
        a mismatch would not be one typo in one line: every build would
        silently ignore its switch and run, or not run, regardless.

        So run the script's own line rather than a copy of it.
        """
        script = read_script('euler')
        line = next(l.strip() for l in script.split('\n')
                    if l.strip().startswith('flag='))
        for arch in registry.architectures_they_build():
            out = subprocess.run(
                ['bash', '-c', 'arch=%s; %s; printf %%s "$flag"'
                 % (arch, line)], stdout=subprocess.PIPE)
            self.assertEqual(
                out.stdout.decode(),
                registry.find_test('euler', 'oe_build_%s' % arch).config_key,
                'the switch for %s does not reach the key the UI writes'
                % arch)

    def test_the_registry_lists_tests_in_the_order_they_run(self):
        """The page works out which test is running from this order.

        During a full run the only thing the output says is which tests
        have finished, so "the first enabled one with no verdict yet" is
        how the running row is found.  That is only true while the
        registry and the script agree on the order, and nothing else
        would notice if they stopped agreeing -- the page would just
        point at the wrong row.
        """
        for distro in registry.DISTROS:
            script = read_script(distro)
            keys = [t.config_key for t in registry.tests_for(distro)]
            # Keyed on the flag, not the function it calls: anolis runs
            # build_anolis_debug through test_build_anolis_debug_defconfig,
            # and the flag is the only name both sides agree on.
            ran = re.findall(r'(?m)^\s*\[\s*"\$\{(TEST_\w+):-\w+\}"\s*==\s*'
                             r'"yes"\s*\]\s*&&\s*test_\w+\s*$', script)
            self.assertTrue(ran, '%s/test.sh has no run-all block' % distro)
            self.assertEqual(
                ran, [k for k in keys if k in set(ran)],
                '%s/test.sh runs its tests in a different order than '
                'registry.py lists them' % distro)

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

    def test_one_run_can_be_forgotten_without_the_rest(self):
        # Clearing everything to be rid of one failed attempt takes the
        # runs worth keeping with it, which is why people stop clearing.
        doomed = self.store.submit('test', ['true'], 'true')
        keeper = self.store.submit('test', ['true'], 'true')
        self.wait_for(doomed['id'])
        self.wait_for(keeper['id'])

        ok, _ = self.store.forget(doomed['id'])
        self.assertTrue(ok)
        self.assertIsNone(self.store.get(doomed['id']))
        self.assertIsNotNone(self.store.get(keeper['id']))

    def test_a_run_still_going_cannot_be_forgotten(self):
        running = self.store.submit('test', ['sleep', '30'], 'sleep')
        ok, why = self.store.forget(running['id'])
        self.assertFalse(ok)
        self.assertIn('not finished', why)
        self.assertIsNotNone(self.store.get(running['id']))
        self.store.kill(running['id'])

    def test_forgetting_a_job_that_is_not_there_says_so(self):
        ok, why = self.store.forget('no-such-job')
        self.assertFalse(ok)
        self.assertIn('No such job', why)

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

    def test_a_kabi_keyword_is_reported_and_not_rejected(self):
        """openEuler warns on this one; it does not turn the patch away.

        Their checkcustom.sh log_warns every result alike and exits
        zero regardless, so the wording that means anything is in
        pr_comment_api.py, where checkformat, checkdepend and
        checkbinary say FAILED and this one says WARNING -- and the
        "failed" flag computed next to it goes to an underscore at its
        only call site.

        So the script really does print "failed:1" for a kabi keyword
        and the series really is accepted.  Reading that as a
        rejection makes us stricter than the gate we exist to predict,
        which is the disagreement people stop believing us over.
        """
        import oe_checks
        # The line their script prints, and ours read, unchanged.
        lines = ['---- checkkabi result ----',
                 'total:100 failed:1 success:99']
        self.assertEqual(oe_checks.verdict(lines)[0], 'fail')
        self.assertIn('checkkabi', oe_checks.ONLY_WARNS)

    def test_no_other_check_is_downgraded(self):
        # Everything else in their comment says FAILED, and quietly
        # forgiving one of those would hide a real rejection.
        import oe_checks
        self.assertEqual(sorted(oe_checks.ONLY_WARNS), ['checkkabi'])
        for check in oe_checks.CHECKS:
            if check != 'checkkabi':
                self.assertNotIn(check, oe_checks.ONLY_WARNS)

    def test_their_own_wording_is_what_we_followed(self):
        """Pinned to their source, so an update that changes it shows up."""
        api = read_file('euler', 'hulk_robot_test', 'openEuler', 'lib',
                        'pr_comment_api.py')
        self.assertIn('checkkabi WARNING', api)
        for other in ('checkformat', 'checkdepend', 'checkbinary'):
            self.assertIn('%s FAILED' % other, api)
        # And the flag it sets is discarded by the only caller.
        self.assertIn('_, comment_str = _custom_result(', api)

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
        oe_build_arch "%(arch)s" %(quiet)s
    '''

    def build(self, arch, branch, stub=':', kabi=':', broken_before=False):
        return self._run(arch, branch, stub, kabi, broken_before,
                         quiet='>/dev/null 2>&1')[0]

    def build_output(self, arch, branch, stub=':', kabi=':',
                     broken_before=False):
        return self._run(arch, branch, stub, kabi, broken_before,
                         quiet='2>&1')[1]

    def _run(self, arch, branch, stub, kabi, broken_before, quiet):
        script = self.HARNESS % {
            'root': PROJECT_ROOT, 'stub': stub, 'kabi': kabi, 'arch': arch,
            'prior': '0' if broken_before else '1', 'quiet': quiet,
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
        done = subprocess.run(['bash', '-c', script], env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode('utf-8', 'replace')

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

    def test_checks_that_did_run_and_pass_are_not_reported_as_skipped(self):
        # Their job passes every row, because they build a base that
        # compiles. Ours can be pointed at a tree that does not, and
        # calling the whole arch skipped on the strength of one row
        # nobody can be blamed for buries five checks that genuinely
        # ran. A verdict that cannot be reconciled with theirs is one
        # people stop reading.
        self.assertEqual(
            self.build('x86_64', 'OLK-6.6',
                       kabi=r'printf "| x86_64 allmodconfig build '
                            r'| broken already, not your series |\n'
                            r'| x86_64 openeuler_defconfig | pass |\n'
                            r'| x86_64 checkkabi | pass |\n" >> $8',
                       broken_before=True),
            0)

    def test_the_pass_still_says_which_check_went_unjudged(self):
        out = self.build_output(
            'x86_64', 'OLK-6.6',
            kabi=r'printf "| x86_64 allmodconfig build '
                 r'| broken already, not your series |\n'
                 r'| x86_64 checkkabi | pass |\n" >> $8',
            broken_before=True)
        self.assertIn('allmodconfig', out)
        self.assertIn('not ones the series touches', out)

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

    def shell(self, body):
        script = '. "%s/euler/oe_build.sh"\n%s' % (PROJECT_ROOT, body)
        done = subprocess.run(['bash', '-c', script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return done.stdout.decode()

    def test_the_config_directory_is_not_the_arch_name(self):
        # x86_64 is the one architecture here whose configs are not in
        # arch/<ARCH>. Looking for arch/x86_64/configs found nothing, so
        # openeuler_defconfig was reported "not in this tree" on the one
        # architecture everybody builds, and the defconfig build, the
        # kabi check and the defconfig consistency check went with it.
        self.assertEqual(self.shell('_oe_srcarch x86_64').strip(), 'x86')
        for same in ('arm64', 'arm', 'powerpc', 'riscv', 'loongarch'):
            self.assertEqual(self.shell('_oe_srcarch %s' % same).strip(),
                             same)

    def test_every_arch_we_build_has_its_config_directory(self):
        # The mapping is only right if it names a directory the kernel
        # actually has, so check it against a real tree rather than
        # against itself.
        kernel = os.environ.get('PRCI_TEST_KERNEL')
        if not kernel or not os.path.isdir(os.path.join(kernel, 'arch')):
            self.skipTest('no kernel tree to check the mapping against')
        for arch in ('x86_64', 'aarch64', 'arm', 'ppc', 'ppc64', 'riscv64'):
            spec = self.shell('_oe_arch_spec %s' % arch).split()
            src = self.shell('_oe_srcarch %s' % spec[0]).strip()
            self.assertTrue(
                os.path.isdir(os.path.join(kernel, 'arch', src)),
                'arch/%s does not exist, for %s' % (src, arch))

    def test_a_failed_build_says_which_file_broke(self):
        # A row saying "broken already, not your series" with nothing
        # behind it reads as the tool excusing itself. The file name is
        # usually the whole explanation.
        errors = tempfile.NamedTemporaryFile('w', suffix='.log',
                                             delete=False)
        errors.write(
            'drivers/net/first/one.c:92:55: error: first complaint\n'
            'drivers/net/first/one.c:93:1: error: same file again\n'
            'drivers/net/second/deep/../two.c:443:10: error: another file\n')
        errors.close()
        self.addCleanup(os.unlink, errors.name)

        out = self.shell('_oe_report_errors %s' % errors.name)
        self.assertIn('drivers/net/first/one.c', out)
        self.assertIn('two.c', out)
        # One line per file: a single bad struct produces a dozen errors
        # and would otherwise crowd out the other drivers.
        self.assertEqual(out.count('drivers/net/first/one.c'), 1)

    def reported(self, *errors):
        log = tempfile.NamedTemporaryFile('w', suffix='.log', delete=False)
        log.write(''.join(e + '\n' for e in errors))
        log.close()
        self.addCleanup(os.unlink, log.name)
        return self.shell('_oe_report_errors %s' % log.name)

    def test_the_error_survives_however_long_the_path_is(self):
        # A driver that includes across directories carries enough ..
        # to fill the line on its own, and the message -- the half
        # worth reading -- was what got cut. Nobody can act on a line
        # that stops at "error:".
        deep = ('drivers/net/ethernet/vendor/product/src/library/host/'
                'service/nic/linux/../../../sdk/knldk/lld/../cqm/'
                'cqm_bitmap_table.c')
        out = self.reported('%s:443:10: error: positional initialization '
                            'of field in a struct declared with the '
                            'designated_init attribute '
                            '[-Werror=designated-init]' % deep)
        self.assertIn('positional initialization', out)
        # And the .. are resolved, or the path alone is unreadable.
        self.assertNotIn('..', out)

    def test_the_flag_that_classifies_the_error_is_never_cut_off(self):
        # gcc puts it last, so a plain truncation drops exactly the
        # word that says what kind of failure this is.
        out = self.reported('drivers/x/y.c:1:1: error: %s '
                            '[-Werror=incompatible-pointer-types]'
                            % ('a very wordy diagnostic ' * 8))
        self.assertIn('[-Werror=incompatible-pointer-types]', out)
        self.assertIn('...', out)
        self.assertEqual(out.count('-Werror'), 1)

    def test_a_short_message_is_left_alone(self):
        out = self.reported('drivers/x/y.c:1:1: error: short and sweet')
        self.assertIn('error: short and sweet', out)
        self.assertNotIn('...', out)

    def a_repo_on_a_branch(self):
        repo = tempfile.mkdtemp(prefix='prci-head-')
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        run = lambda *a: subprocess.check_call(
            a, cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run('git', 'init', '-q', '-b', 'mywork', '.')
        run('git', 'config', 'user.email', 't@t')
        run('git', 'config', 'user.name', 't')
        for text in ('one', 'two'):
            with open(os.path.join(repo, 'f'), 'w') as handle:
                handle.write(text)
            run('git', 'add', 'f')
            run('git', 'commit', '-qm', text)
        return repo

    def branch_of(self, repo):
        out = subprocess.run(['git', 'symbolic-ref', '--quiet', '--short',
                              'HEAD'], cwd=repo, stdout=subprocess.PIPE)
        return out.stdout.decode().strip() or '(detached)'

    def test_a_baseline_build_leaves_you_on_your_branch(self):
        # "git checkout <sha>" detaches, so coming back to what
        # "git rev-parse HEAD" returned leaves the tree off its branch
        # even when nothing went wrong. Every commit is still there and
        # git says "HEAD detached at ...", so it reads as damage, and
        # the next commit the user makes lands nowhere.
        repo = self.a_repo_on_a_branch()
        self.shell('''
            cd %s
            head=$(_oe_where_we_are)
            git checkout -q HEAD~1
            git checkout -q "${head}"
        ''' % repo)
        self.assertEqual(self.branch_of(repo), 'mywork')

    def test_an_interrupted_baseline_build_puts_the_branch_back(self):
        # The window is a build, so it is minutes long and Ctrl-C lands
        # inside it far more often than not.
        repo = self.a_repo_on_a_branch()
        script = '''
            . %(root)s/euler/oe_build.sh
            cd %(repo)s
            _oe_hold_head %(repo)s "$(_oe_where_we_are)"
            git checkout -q HEAD~1
            sleep 30
        ''' % {'root': PROJECT_ROOT, 'repo': repo}
        child = subprocess.Popen(['bash', '-c', script],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        child.send_signal(signal.SIGINT)
        child.wait(timeout=30)
        self.assertEqual(self.branch_of(repo), 'mywork')

    def test_a_tree_with_no_branch_is_left_where_it_was(self):
        repo = self.a_repo_on_a_branch()
        subprocess.check_call(['git', 'checkout', '-q', '--detach', 'HEAD'],
                              cwd=repo)
        was = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                      cwd=repo).decode().strip()
        self.shell('''
            cd %s
            head=$(_oe_where_we_are)
            git checkout -q HEAD~1
            git checkout -q "${head}"
        ''' % repo)
        now = subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                      cwd=repo).decode().strip()
        self.assertEqual(now, was)
        self.assertEqual(self.branch_of(repo), '(detached)')

    def attribution(self, errored, touched):
        """_oe_broke_its_own_files, with git answering for the series."""
        log = tempfile.NamedTemporaryFile('w', suffix='.log', delete=False)
        log.write(''.join('%s:1:1: error: broke\n' % f for f in errored))
        log.close()
        self.addCleanup(os.unlink, log.name)

        return self.shell('''
            git() { printf '%%s\\n' %(touched)s; }
            _oe_broke_its_own_files %(log)s 1
        ''' % {'touched': ' '.join("'%s'" % t for t in touched) or "''",
               'log': log.name})

    def test_breaking_a_file_of_its_own_is_the_series_fault(self):
        # The tree being broken already is decided by the baseline build
        # failing too, and that cannot tell breaking it further from
        # leaving it as found: both end with make exiting non-zero. A
        # gate that passes because it never really looked is the worst
        # kind, and this is where it would happen.
        out = self.attribution(
            errored=['drivers/other/theirs.c', 'drivers/mine/ours.c'],
            touched=['drivers/mine/ours.c', 'include/linux/ours.h'])
        self.assertEqual(out.split(), ['drivers/mine/ours.c'])

    def test_breakage_in_files_the_series_never_touched_is_not_its_fault(self):
        out = self.attribution(errored=['drivers/other/theirs.c'],
                               touched=['drivers/mine/ours.c'])
        self.assertEqual(out.strip(), '')

    def test_a_path_with_dot_dot_in_it_still_matches(self):
        # gcc prints paths as the build saw them, and a driver that
        # includes across directories produces several .. in the middle.
        # Compared unnormalised, those never match what git reports and
        # every such breakage is filed as somebody else's.
        out = self.attribution(
            errored=['drivers/mine/deep/../ours.c'],
            touched=['drivers/mine/ours.c'])
        self.assertEqual(out.split(), ['drivers/mine/ours.c'])

    def test_nothing_is_printed_when_there_are_no_errors(self):
        quiet = tempfile.NamedTemporaryFile('w', suffix='.log', delete=False)
        quiet.write('fs/foo.c:12: warning: unused variable\n')
        quiet.close()
        self.addCleanup(os.unlink, quiet.name)
        self.assertEqual(self.shell('_oe_report_errors %s' % quiet.name), '')

    def defconfig_check(self, mine, before, back='1'):
        """_oe_check_defconfig with listnewconfig answering to order."""
        kernel = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, kernel, True)
        os.makedirs(os.path.join(kernel, 'arch', 'x86', 'configs'))

        def put(name, text):
            path = os.path.join(kernel, name)
            with open(path, 'w') as f:
                f.write(text)
            return path

        put(os.path.join('arch', 'x86', 'configs', 'openeuler_defconfig'),
            'CONFIG_HAVE_GCC_PLUGINS=y\n')
        put('mine', mine)
        put('before', before)
        warnings = put('warnings', '')
        result = put('result', '')

        script = '''
            . "%(root)s/euler/oe_build.sh"
            # Stand in for the tree: the second call is the baseline.
            _oe_new_symbols() {
                if [ -f %(kernel)s/.asked ]; then cat %(kernel)s/before
                else touch %(kernel)s/.asked; cat %(kernel)s/mine; fi
            }
            git() { case "$1" in rev-parse) echo deadbeef ;; *) return 0 ;; esac; }
            _oe_check_defconfig %(kernel)s x86_64 x86_64 \\
                %(warnings)s %(result)s %(back)s
        ''' % {'root': PROJECT_ROOT, 'kernel': kernel,
               'warnings': warnings, 'result': result, 'back': back}
        done = subprocess.run(['bash', '-c', script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)

        def read(path):
            with open(path) as f:
                return f.read()
        return done.stdout.decode(), read(result), read(warnings)

    #: Unanswered before the series and after it, so the host's doing.
    #: What makes a symbol behave this way is that Kconfig only offers
    #: it on some machines -- gcc plugin symbols appear wherever the
    #: compiler's plugin headers are installed -- but nothing here
    #: depends on which symbol it is, so neither does the test.
    FROM_THE_HOST = 'CONFIG_ONE=y\nCONFIG_TWO=n\n'
    #: Unanswered only after the series, so the series added it.
    FROM_THE_SERIES = 'CONFIG_THREE=y\n'

    def test_symbols_the_host_offers_are_not_the_series_fault(self):
        log, result, warnings = self.defconfig_check(
            mine=self.FROM_THE_HOST, before=self.FROM_THE_HOST)
        self.assertIn('checkdefconfig | pass', result)
        # Said on the log, never in the warnings file, which is a gate.
        self.assertEqual(warnings, '')
        # Counted, not listed: the only symbols worth naming are the
        # ones a patch could do something about.
        self.assertIn('2 symbol(s) are unanswered', log)
        self.assertNotIn('CONFIG_ONE', log)
        self.assertNotIn('CONFIG_TWO', log)

    def test_a_symbol_the_series_really_added_still_fails(self):
        log, result, warnings = self.defconfig_check(
            mine=self.FROM_THE_HOST + self.FROM_THE_SERIES,
            before=self.FROM_THE_HOST)
        self.assertIn('checkdefconfig | fail', result)
        self.assertIn('CONFIG_THREE=y', warnings)
        # Only the ones the series is answerable for, nowhere else.
        self.assertNotIn('CONFIG_ONE', warnings)
        self.assertNotIn('CONFIG_TWO', warnings)
        self.assertNotIn('CONFIG_ONE', log)
        self.assertIn('update_oedefconfig', warnings)

    def test_a_defconfig_that_answers_everything_passes_quietly(self):
        log, result, warnings = self.defconfig_check(mine='', before='')
        self.assertIn('checkdefconfig | pass', result)
        self.assertEqual(warnings, '')

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


class TestConflictSection(unittest.TestCase):
    """Where the Conflicts: section goes, and what goes in the brackets."""

    def setUp(self):
        euler = os.path.join(PROJECT_ROOT, 'euler')
        if euler not in sys.path:
            sys.path.insert(0, euler)
        global oe_conflict, oe_header
        import oe_conflict
        import oe_header

    #: The shape a real backport's trailers have: several sign-offs
    #: carried down from upstream with a review in the middle of them,
    #: which is what makes where the section goes a question at all.
    UPSTREAM_TRAILERS = (
        'Signed-off-by: First Author <first@example.com>\n'
        'Signed-off-by: Second Author <second@example.com>\n'
        'Reviewed-by: A Reviewer <reviewer@example.com>\n'
        'Signed-off-by: A Maintainer <maintainer@example.com>\n'
    )

    NOTE = '[Backport Changes]\nBecause the tree already had part of it.\n\n'

    def build(self, message, files=('arch/x86/include/asm/cpufeatures.h',)):
        note = oe_conflict.existing_note(message)
        message = oe_conflict.strip_note(message)
        message = oe_header.add_signed_off_by(
            message, 'Signed-off-by: Someone <s@example.com>')
        before, sign_offs = oe_header.split_sign_offs(message)
        section = oe_conflict.section(list(files), note)
        return '\n'.join(before + section.split('\n') + sign_offs) + '\n'

    def test_the_section_sits_at_the_top_of_the_trailers(self):
        # Not wedged between the upstream sign-offs and ours: it is
        # replacing the author's own note, and belongs where that was.
        out = self.build('subject\n\nBody text.\n\n' + self.NOTE
                         + self.UPSTREAM_TRAILERS)
        lines = out.split('\n')
        self.assertEqual(lines[lines.index('Conflicts:') - 1], '')
        after = lines[lines.index('Conflicts:'):]
        closing = next(i for i, l in enumerate(after) if l.endswith(']'))
        self.assertTrue(after[closing + 1].startswith('Signed-off-by: First Author'),
                        'nothing may come between "]" and the sign-offs')

    def test_the_authors_own_note_becomes_the_description(self):
        out = self.build(
            'subject\n\nBody text.\n\n'
            '[Backport Changes]\n'
            'The target tree already uses that bit for something else.\n'
            'Every reference is by macro name.\n\n'
            + self.UPSTREAM_TRAILERS)
        self.assertIn('[The target tree already uses that bit for '
                      'something else.\nEvery reference is by macro name.]',
                      out)
        self.assertNotIn('[Backport Changes]', out)
        # Taking the block out must not leave a hole behind it.
        self.assertNotIn('\n\n\n', out)

    def test_openeuler_accepts_what_we_produce(self):
        for message in (
            'subject\n\nBody.\n\n' + self.NOTE + self.UPSTREAM_TRAILERS,
            'subject\n\nBody.\n\n[Backport Changes]\nBecause.\n\n'
            + self.UPSTREAM_TRAILERS,
            # A trailer group that does not open with a sign-off: the
            # section has to drop to the first one that follows.
            'subject\n\nBody.\n\n' + self.NOTE + 'Reviewed-by: R <r@e.com>\n'
            'Signed-off-by: S <s@e.com>\n',
        ):
            out = self.build(message)
            ok, why = oe_conflict.format_ok(
                out, ['arch/x86/include/asm/cpufeatures.h'])
            self.assertTrue(ok, '%s\n\nfor:\n%s' % (why, out))

    def test_a_blank_line_before_the_sign_offs_is_rejected(self):
        # The layout that reads best is the one their regex refuses, so
        # this records why the section butts up against the sign-offs.
        good = self.build('subject\n\nBody.\n\n' + self.NOTE
                          + self.UPSTREAM_TRAILERS)
        spaced = good.replace(']\nSigned-off-by: First Author',
                              ']\n\nSigned-off-by: First Author')
        self.assertTrue(oe_conflict.format_ok(good)[0])
        self.assertFalse(oe_conflict.format_ok(spaced)[0])


class TestUndescribedDivergence(unittest.TestCase):
    """What happens to a commit that diverges and says nothing about it.

    A byte-for-byte comparison calls a hunk at a different offset a
    difference, so most of these are false positives.  Nothing here
    can tell which, so nothing here edits the commit: it warns, shows
    the difference, and leaves the message as the author wrote it.
    """

    def setUp(self):
        euler = os.path.join(PROJECT_ROOT, 'euler')
        if euler not in sys.path:
            sys.path.insert(0, euler)
        global oe_conflict, oe_header
        import oe_conflict
        import oe_header

    class Args(object):
        kernel = '/k'
        mirror = '/m'
        commit = 'local1234'

    def declare(self, message, monkey):
        saved = {name: getattr(oe_conflict, name) for name in monkey}
        for name, value in monkey.items():
            setattr(oe_conflict, name, value)
        self.addCleanup(lambda: [setattr(oe_conflict, n, v)
                                 for n, v in saved.items()])
        return oe_header.declare_conflicts(message, 'abcdef1234567890',
                                           self.Args())

    DIVERGES = {
        'deviates': lambda *a: True,
        'differing_files': lambda *a: ['drivers/somewhere/a_file.c'],
        'difference': lambda *a: '--- a\n+++ b\n@@\n-old line\n+new line',
    }

    def test_a_commit_with_no_note_is_left_exactly_as_it_was(self):
        message = 'subject\n\nBody.\n\nSigned-off-by: S <s@e.com>\n'
        out, note, warning = self.declare(message, self.DIVERGES)
        self.assertEqual(out, message)
        self.assertIsNone(note)
        self.assertTrue(warning)

    def test_the_warning_names_the_files_and_shows_the_difference(self):
        _, _, warning = self.declare(
            'subject\n\nBody.\n\nSigned-off-by: S <s@e.com>\n', self.DIVERGES)
        self.assertIn('drivers/somewhere/a_file.c', warning)
        self.assertIn('-old line', warning)
        self.assertIn('+new line', warning)
        self.assertIn('false positive', warning)

    def test_a_long_difference_is_cut_short(self):
        long_diff = dict(self.DIVERGES,
                         difference=lambda *a: '\n'.join(
                             'line %d' % i for i in range(500)))
        _, _, warning = self.declare(
            'subject\n\nBody.\n\nSigned-off-by: S <s@e.com>\n', long_diff)
        self.assertIn('more line(s)', warning)
        self.assertLess(len(warning.split('\n')), 60)

    def test_a_commit_that_matches_upstream_gets_no_warning(self):
        out, note, warning = self.declare(
            'subject\n\nBody.\n\nSigned-off-by: S <s@e.com>\n',
            dict(self.DIVERGES, deviates=lambda *a: False))
        self.assertIsNone(note)
        self.assertIsNone(warning)

    def test_readiness_does_not_hold_the_series_back_for_one(self):
        # Nothing another pass can do about it, so reporting it as work
        # remaining would block testing on a warning for good.
        import oe_ready
        saved = oe_conflict.deviates
        oe_conflict.deviates = lambda *a: True
        self.addCleanup(lambda: setattr(oe_conflict, 'deviates', saved))
        why = oe_ready.unprepared(
            '/k', 'sha', 'subject\n\nmainline inclusion\ncommit abcdef123456\n'
            '\nSigned-off-by: S <s@e.com>\n',
            'Signed-off-by: S <s@e.com>', '/m')
        self.assertIsNone(why)


class TestWhoSignsWhat(unittest.TestCase):
    """Whose Signed-off-by goes on which patch.

    A backport is somebody else's work being carried across, and the
    sign-off the prepare pass adds says that much: this is who carried
    it.  A patch with nothing upstream behind it is original work, and
    the sign-off on it certifies the DCO for something the author
    wrote.  Nobody can make that certification on their behalf.

    It still has to be there.  openEuler's format.py rejects a patch
    with no Signed-off-by at all, whoever signed, so leaving one off
    would just move the failure to their gate.
    """

    SIGNER = 'Signed-off-by: Carrier <c@example.com>'

    def rewrite(self, message, subject='a subject'):
        patch = types.SimpleNamespace(
            message=message, subject=subject,
            set_message=lambda m: setattr(patch, 'message', m))
        args = types.SimpleNamespace(
            signer=self.SIGNER, mirror='/m', kernel='/k',
            bugzilla='12345', branch='OLK-6.6')
        oe_header.rewrite(patch, args)
        return patch.message

    def test_original_work_is_not_signed_for_its_author(self):
        out = self.rewrite(
            'virt inclusion\ncategory: bugfix\n\n'
            'Body.\n\nSigned-off-by: Author <a@example.com>\n')
        self.assertIn('Signed-off-by: Author', out)
        self.assertNotIn(self.SIGNER, out)

    def test_original_work_nobody_signed_is_refused(self):
        # Writing it out unsigned would only move the failure to their
        # gate, which is the one thing this tool exists to prevent.
        with self.assertRaises(oe_header.Refused) as caught:
            self.rewrite('virt inclusion\ncategory: bugfix\n\nBody.\n')
        self.assertIn('author', str(caught.exception).lower())

    def test_a_backport_still_gets_the_carriers_sign_off(self):
        out = self.rewrite(
            'mainline inclusion\ncommit abcdef123456\ncategory: bugfix\n\n'
            'commit abcdef1234567890abcdef1234567890abcdef12 upstream.\n\n'
            'Body.\n\nSigned-off-by: Author <a@example.com>\n')
        self.assertIn(self.SIGNER, out)

    def test_readiness_does_not_want_our_name_on_original_work(self):
        # The prepare pass will never add it, so looking for it here
        # would report the commit as unprepared for good and block
        # every run behind it.
        import oe_ready
        why = oe_ready.unprepared(
            '/k', 'sha',
            'subject\n\nvirt inclusion\ncategory: bugfix\n\n'
            'Signed-off-by: Author <a@example.com>\n',
            self.SIGNER, '/m')
        self.assertIsNone(why)

    def test_readiness_still_wants_somebody_to_have_signed(self):
        import oe_ready
        why = oe_ready.unprepared(
            '/k', 'sha', 'subject\n\nvirt inclusion\ncategory: bugfix\n',
            self.SIGNER, '/m')
        self.assertIsNotNone(why)
        self.assertIn('Signed-off-by', why)


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
