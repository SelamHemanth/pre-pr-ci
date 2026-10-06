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

import ast
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

from prci import hostcheck                                  # noqa: E402
from prci import jobs                                       # noqa: E402
from prci import readiness                                  # noqa: E402
from prci import repo                                       # noqa: E402
from prci import registry                                   # noqa: E402
from prci.distro import ConfigError, Workspace, redact       # noqa: E402
from prci.jobs import JobStore, strip_ansi                   # noqa: E402


def read_script(distro):
    with open(os.path.join(PROJECT_ROOT, distro, 'test.sh'), errors='replace') as f:
        return f.read()


def submodule_paths():
    """Every submodule path, from .gitmodules.

    Tests that copy or walk the tree have to leave these out: they are
    somebody else's repository, they are large, and tone-cli in
    particular has symlinks that shutil.copytree cannot reproduce.
    """
    text = read_file('.gitmodules')
    return re.findall(r'(?m)^\s*path\s*=\s*(\S+)\s*$', text)


def read_file(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), errors='replace') as f:
        return f.read()


class TestNoImportIsShadowedInsideAFunction(unittest.TestCase):
    """A second `import x` inside a function makes x local to all of it.

    Python decides a name is local by looking at the whole function body,
    not at the order the lines run in, so an `import threading` near the
    bottom of a function turns every earlier use of threading into a read
    of an unassigned local.  The function still compiles and still
    imports; it raises UnboundLocalError on the day the earlier branch is
    taken.

    It cost a service that would not start.  server.py imports threading
    at the top, main() started a background thread with it, and sixty
    lines further down a redundant `import threading` guarded by "only
    refresh an existing mirror" made the first use fail -- but only when
    a sub-repository was missing, which is the one morning in fifty that
    the earlier branch runs at all.  Systemd restarted it sixty-six
    times.

    Cheap to check for the whole tree, so it is checked for the whole
    tree rather than for the line that broke.
    """

    def offenders(self, path):
        tree = ast.parse(read_file(path))

        def names(node):
            out = set()
            for alias in node.names:
                out.add((alias.asname or alias.name).split('.')[0])
            return out

        at_module_scope = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                at_module_scope |= names(node)

        found = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                for name in names(node) & at_module_scope:
                    found.append('%s:%d: %s() re-imports %s'
                                 % (path, node.lineno, func.name, name))
        return found

    def test_nothing_in_the_web_interface_shadows_its_own_imports(self):
        bad = []
        for path in ['web/server.py'] + sorted(
                os.path.join('web', 'prci', f)
                for f in os.listdir(os.path.join(PROJECT_ROOT, 'web', 'prci'))
                if f.endswith('.py')):
            bad += self.offenders(path)
        self.assertEqual(bad, [], '\n'.join(bad))

    def test_the_check_would_have_caught_the_one_that_got_through(self):
        # Without this, a check that silently matches nothing looks the
        # same as a clean tree.
        source = ('import threading\n'
                  '\n'
                  'def main():\n'
                  '    threading.Thread()\n'
                  '    if cond:\n'
                  '        import threading\n'
                  '        threading.Thread()\n')
        probe = tempfile.NamedTemporaryFile('w', suffix='.py', dir=PROJECT_ROOT,
                                            delete=False)
        probe.write(source)
        probe.close()
        self.addCleanup(os.unlink, probe.name)
        found = self.offenders(os.path.basename(probe.name))
        self.assertEqual(len(found), 1, found)
        self.assertIn('main() re-imports threading', found[0])


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

    def test_the_checks_are_titled_the_way_their_ci_titles_them(self):
        """A row here is read against a row on their PR comment.

        Ours said "Coding style", "Commit message", "Kernel ABI"; theirs
        says checkpatch, checkformat, checkkabi.  Comparing the two
        meant translating every line, which is exactly the friction the
        architecture names had before they were changed to match.
        """
        titles = [t.title for t in registry.TESTS['euler']
                  if not t.name.startswith('oe_build')]
        self.assertEqual(titles, ['checkpatch', 'checkformat', 'checkdepend',
                                  'checkkabi', 'checkconflict', 'checkbinary'])
        # And each is the name their own script is called, so the title
        # is the thing to grep their source for.
        for title in titles:
            self.assertTrue(
                os.path.exists(os.path.join(
                    PROJECT_ROOT, 'euler', 'hulk_robot_test', 'openEuler',
                    '%s.sh' % title.replace('checkbinary', 'checkbinaryfile'))
                ) or title in read_file('euler', 'hulk_robot_test',
                                        'openEuler', 'checkcustom.sh'),
                '%s is not a name their CI uses' % title)

    def test_an_architecture_their_branch_has_off_reports_what_theirs_does(self):
        """Their job runs, compiles nothing, and is green.

        checkbuild.sh asks check_branch.py and exits 0 above the
        compile, so their PR comment shows SUCCESS for it -- their own
        console log reads "loongarch is set to false, exit" and then
        "Finished: SUCCESS".  Calling that a skip locally put a word in
        the column their green tick sits in, for the one row a reader
        is most likely to compare.
        """
        theirs = read_file('euler', 'hulk_robot_test', 'openEuler',
                           'checkbuild.sh')
        # Their exit 0 is above the build, which is why it is green.
        gate = theirs.index('check_branch.py')
        self.assertLess(gate, theirs.index('build_kernel\n'))
        self.assertRegex(theirs[gate:], r'IS_SKIP.*\n.*-ne 0.*\n\s*exit 0')
        # oe_build.sh turns that sentence into 3 for oe_hulk.sh, and test.sh
        # passes on 3.
        self.assertIn("*'is set to false'*) return 3 ;;",
                      read_file('euler', 'oe_build.sh'))
        self.assertRegex(read_script('euler'), r'(?m)^\s*3\)\s*pass ')

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
                built = False
                # run_their_build_case and run_their_vm_case default
                # each argument to the one before it, so whichever
                # argument comes last is the log's name.  Checking that
                # position rather than any position is the difference
                # between this noticing a wrong log name and not.
                for call in re.finditer(
                        r'run_their_(?:build_case|vm_case)([^\n;]*)', joined):
                    args = [a.strip('"\'') for a in call.group(1).split()]
                    if args and args[-1] == wanted:
                        built = True
                        break
                for call in re.finditer(
                        r'run_(?:kernel_build|oe_check|oe_build)([^\n;]*)',
                        joined):
                    args = [a.strip('"\'') for a in call.group(1).split()]
                    if wanted in args:
                        built = True
                        break
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
        at = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, at, True)
        fresh = Workspace(at)
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

    def test_a_conflict_is_reported_and_not_rejected(self):
        """Same row of their table, same reasoning as the kabi one.

        check_conflict.py counts a failure for every patch that will
        not apply to the branch as it stands, so a series rebased onto
        anything newer than their snapshot scores dozens of them --
        36 of 100 on the tree this was written against.  Their comment
        says WARNING and their gate takes the series.
        """
        import oe_checks
        lines = ['---- result ----', 'total: 100 failed: 36 success: 64']
        self.assertEqual(oe_checks.verdict(lines)[0], 'fail')
        self.assertIn('checkconflict', oe_checks.ONLY_WARNS)

    def test_no_other_check_is_downgraded(self):
        # Everything else in their comment says FAILED, and quietly
        # forgiving one of those would hide a real rejection.
        import oe_checks
        self.assertEqual(sorted(oe_checks.ONLY_WARNS),
                         ['checkconflict', 'checkkabi'])
        for check in oe_checks.CHECKS:
            if check not in ('checkkabi', 'checkconflict'):
                self.assertNotIn(check, oe_checks.ONLY_WARNS)

    def test_their_own_wording_is_what_we_followed(self):
        """Pinned to their source, so an update that changes it shows up.

        The wording that decides this is the cell in their status
        table, not the prose underneath it: for checkconflict the two
        disagree outright, the paragraph saying "checkconflict FAILED"
        while the table cell it sits under says WARNING.  The table is
        the row a reader sees against the check name, the "failed"
        flag beside it is thrown away by its only caller, and their
        gate does take a series with conflicts -- so the table is the
        one to follow.
        """
        api = read_file('euler', 'hulk_robot_test', 'openEuler', 'lib',
                        'pr_comment_api.py')

        def cell(check):
            found = re.search(
                r'<th>%s</th>.*?\.format\(\s*"([^"]+)"' % check,
                api, re.S)
            self.assertIsNotNone(found, 'no status row for %s' % check)
            return found.group(1)

        warning, failed = '&#9888; WARNING', '&#10060; FAILED'
        for warns in ('checkkabi', 'checkconflict'):
            self.assertEqual(cell(warns), warning)
        for rejects in ('checkformat', 'checkdepend', 'checkbinary'):
            self.assertEqual(cell(rejects), failed)
        # checkpatch is the one that is both, by its own two flags.
        self.assertIn('pres = "%s"' % failed, api)
        self.assertIn('pres = "%s"' % warning, api)
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


class TestAnolisUpstreamReference(unittest.TestCase):
    """A backport in Anolis says which commit it came from.

    Pinned to cloud-kernel !13995, which went in with their review
    check green, so the shape here is one Anolis actually accepted
    rather than one inferred from documentation.
    """

    MERGED = (
        'iommu/amd: Add SNP page mode 0 support\n'
        '\n'
        'ANBZ: #48382\n'
        '\n'
        'commit cb2860ad6c4ff7e15bb69c7e3a6842bbea743229 upstream.\n'
        '\n'
        'Newer AMD IOMMUs supports DTE[Mode]=0 for SNP-enabled system.\n'
        '\n'
        'Signed-off-by: Vasant Hegde <vasant.hegde@amd.com>\n'
        'Signed-off-by: mohanasv <mohanasv@amd.com>'
    )

    def setUp(self):
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'anolis'))
        import upstream_ref
        self.ref = upstream_ref

    def without_the_line(self):
        return self.MERGED.replace(
            'commit cb2860ad6c4ff7e15bb69c7e3a6842bbea743229 upstream.\n\n',
            '')

    def test_the_line_they_merged_is_recognised(self):
        self.assertEqual(self.ref.declared_sha(self.MERGED),
                         'cb2860ad6c4ff7e15bb69c7e3a6842bbea743229')
        self.assertIsNone(self.ref.declared_sha(self.without_the_line()))

    def test_inserting_it_reproduces_what_they_merged(self):
        """Byte for byte, or it is a different convention.

        The blank line either side is part of it.  An earlier pattern
        ended "\\s*$", and because \\s matches newlines under MULTILINE
        it ran past the blank line after the tag and put the insert one
        line too far down.
        """
        self.assertEqual(
            self.ref.insert_into(self.without_the_line(),
                                 'cb2860ad6c4ff7e15bb69c7e3a6842bbea743229'),
            self.MERGED)

    def test_preparing_twice_does_not_say_it_twice(self):
        self.assertEqual(
            self.ref.insert_into(self.MERGED,
                                 'cb2860ad6c4ff7e15bb69c7e3a6842bbea743229'),
            self.MERGED)

    def test_a_message_with_no_anbz_tag_is_refused_not_mangled(self):
        # Nowhere to put it means the message is not in their shape at
        # all, and guessing a position would produce something that
        # reads right and is not.
        with self.assertRaises(self.ref.Unresolved):
            self.ref.insert_into('a subject\n\nbody\n', 'a' * 40)

    def test_a_cherry_pick_records_the_sha_itself(self):
        # Cheaper than the mirror and right even when the subject was
        # reworded on the way down.
        self.assertEqual(
            self.ref.cherry_picked_sha(
                'subject\n\nbody\n\n(cherry picked from commit %s)\n' % ('b' * 40)),
            'b' * 40)

    def test_the_patch_subject_drops_the_patch_prefix(self):
        self.assertEqual(
            self.ref.patch_subject(
                'From: a <a@b.c>\nSubject: [PATCH v3 2/7] iommu/amd: a thing\n\n'),
            'iommu/amd: a thing')

    def test_a_folded_subject_is_put_back_together(self):
        # git wraps long subjects across lines, and half a subject
        # matches nothing in the mirror.
        self.assertEqual(
            self.ref.patch_subject(
                'Subject: [PATCH] iommu/amd: a subject long enough that git\n'
                ' folded it across two lines\n\nbody\n'),
            'iommu/amd: a subject long enough that git folded it across '
            'two lines')

    def test_readiness_asks_for_it(self):
        # The gate passed a series with the line missing from every
        # commit, which is the one thing a reader cannot reconstruct.
        ready = read_file('anolis', 'ready.sh')
        self.assertIn('upstream_ref.py', ready)

    def test_preparation_writes_it(self):
        self.assertIn('upstream_ref.py', read_file('anolis', 'prepare.sh'))


class TestWarnTravels(unittest.TestCase):
    """A warning has to survive the whole way to the screen.

    It starts as an exit status from oe_checks.py, becomes a WARN: line
    in test.sh, is read back out of the log by the job parser and is
    finally a badge in the interface.  Any one of those four links
    missing turns it back into the thing it must not be: a pass that
    nobody reads, or a failure that stops a series openEuler accepts.
    """

    def test_warn_has_an_exit_status_of_its_own(self):
        # Sharing pass's 0 is what made these read as passes.  Sharing
        # fail's 1 is what made them read as rejections.
        script = read_file('euler', 'oe_checks.py')
        found = re.search(r"\{'pass':\s*(\d+).*?'warn':\s*(\d+).*?"
                          r"'fail':\s*(\d+)", script, re.S)
        self.assertIsNotNone(found, 'oe_checks.py has no exit status map')
        passed, warned, failed = (int(n) for n in found.groups())
        self.assertNotEqual(warned, passed)
        self.assertNotEqual(warned, failed)

    def test_test_sh_reads_that_status(self):
        script = read_script('euler')
        self.assertRegex(script, r'(?m)^\s*5\)\s*warn ')

    def test_a_warn_is_not_counted_as_a_pass_or_a_failure(self):
        script = read_script('euler')
        self.assertRegex(script, r'(?m)^\s*warn\(\)\s*\{')
        self.assertIn('WARN:${test_name}', script)
        self.assertIn('((WARNED_TESTS++))', script)
        # And it must not be the thing that fails the run, because it
        # does not fail theirs.
        self.assertRegex(script, r'WARNED_TESTS\}"?\s*-gt 0')

    def test_the_log_parser_recognises_the_line_test_sh_writes(self):
        from prci.jobs import _RESULT_RE
        for line in ('WARN: oe_checkkabi',
                     '\u26a0 WARN: oe_checkconflict',
                     '  \u26a0 WARN : oe_build_x86_64 '):
            found = _RESULT_RE.match(line)
            self.assertIsNotNone(found, 'parser drops %r' % line)
            self.assertEqual(found.group(1), 'WARN')

    def test_the_interface_has_a_badge_for_it(self):
        page = read_file('web', 'templates', 'index.html')
        self.assertRegex(page, r"warn:\s*'badge-warn'")
        self.assertRegex(page, r'\.badge-warn\s*\{')
        # A pass badge and a warn badge that look alike would defeat
        # the point of separating them.
        self.assertNotRegex(page, r"pass:\s*'badge-warn'")

    def test_every_status_the_parser_knows_can_be_shown(self):
        """The parser and the interface must agree on the set of them."""
        from prci.jobs import _RESULT_RE
        page = read_file('web', 'templates', 'index.html')
        for status in re.search(r'\(([A-Z|]+)\)',
                                _RESULT_RE.pattern).group(1).split('|'):
            self.assertRegex(
                page, r"%s:\s*'badge-[a-z]+'" % status.lower(),
                'the interface cannot show a %s verdict' % status)


class TestNoDistroIsChosenForYou(unittest.TestCase):
    """The distro picker starts empty.

    The two gates run different checks and reject different things, so a
    preselected one quietly predicts the wrong CI: the run goes green
    against a gate the series was never going to face.  Nothing about this
    host says where the patches are headed either, so detection is shown
    as a note and never selected.
    """

    def setUp(self):
        self.page = read_file('web', 'templates', 'index.html')

    def test_the_form_starts_with_no_distribution(self):
        found = re.search(r'form:\s*\{\s*distro:\s*(.+?),', self.page)
        self.assertIsNotNone(found, 'cannot find the form initialiser')
        self.assertIn(found.group(1), ("''", '""'),
                      'the form is preloaded with %s' % found.group(1))

    def test_no_distribution_is_named_as_a_fallback(self):
        """Not 'the saved one, or else euler'."""
        found = re.search(r'this\.form\.distro\s*=\s*([^;]+);', self.page,
                          re.S)
        self.assertIsNotNone(found, 'cannot find where the picker is set')
        for distro in registry.DISTROS:
            self.assertNotIn("'%s'" % distro, found.group(1),
                             'the picker falls back to %s' % distro)
        self.assertNotIn('detected_distro', found.group(1),
                         'the picker is filled from host detection')

    def test_the_host_it_detected_is_shown_but_not_selected(self):
        self.assertIn('status.detected_distro', self.page,
                      'the detected distro is not surfaced at all')

    def test_saving_without_one_is_refused_rather_than_guessed(self):
        self.assertRegex(self.page, r':disabled="saving \|\| !form\.distro"')

        server = read_file('web', 'server.py')
        self.assertRegex(server, r"if not distro:\s*\n\s*return jsonify")

        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with self.assertRaises(ConfigError) as caught:
            Workspace(root).write_config('', {}, {}, '/tmp/mirror')
        self.assertIn('distro', caught.exception.errors)


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


class TestOpenEulerTheirScripts(unittest.TestCase):
    """That their checkkabi.sh and checkbuild.sh run, and run unaltered.

    There is nothing of ours left in the verdict: the rows, their order,
    the branch exemptions and the exit status all come out of the
    submodule.  So what is worth testing is the seam -- that their code
    loads without running, that the functions we stand in for are the
    ones that reach for Jenkins and not the ones that decide anything,
    and that the single guard we do add can only ever downgrade a
    failure their check already reached.
    """

    SUB = os.path.join(PROJECT_ROOT, 'euler', 'hulk_robot_test', 'openEuler')

    def shell(self, body, **env):
        """Run body with oe_hulk.sh sourced, and hand back what it said."""
        script = ('. "%s/lib/warnings.sh"\n. "%s/euler/oe_hulk.sh"\n%s'
                  % (PROJECT_ROOT, PROJECT_ROOT, body))
        done = subprocess.run(
            ['bash', '-c', script],
            env=dict(os.environ,
                     SCRIPT_DIR=os.path.join(PROJECT_ROOT, 'euler'),
                     WORKDIR=PROJECT_ROOT, **env),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode('utf-8', 'replace')

    # ---- loading their code without running it ----

    def test_their_functions_load_without_their_main_running(self):
        # Both scripts end in `main "$@"`, so sourcing one as it stands
        # would run the whole gate before a single shim was in place.
        # Dropping that one line leaves a file of definitions.
        rc, out = self.shell(
            'test_path="%s"\n'
            '_hulk_load "%s/checkkabi.sh"\n'
            'declare -f build_allmodconfig build_defconfig check_kabi '
            'check_defconfig main >/dev/null && echo all-defined'
            % (self.SUB, self.SUB))
        self.assertEqual(rc, 0, out)
        self.assertIn('all-defined', out)
        # Their main's first act is to ask check_branch.py about the
        # branch, which would have printed.  Nothing ran.
        self.assertNotIn('will perform build check by default', out)
        self.assertNotIn('Remove old directories', out)

    def test_the_checks_that_decide_a_verdict_are_theirs_verbatim(self):
        # The guard against the obvious regression: a shim that quietly
        # shadowed one of these would leave the tool reporting its own
        # opinion under openEuler's name.
        rc, out = self.shell(
            'test_path="%s"\n'
            '_hulk_load "%s/checkkabi.sh"\n'
            '_hulk_shim_jenkins; _hulk_shim_whitelists; _hulk_shim_kernel\n'
            '_hulk_shim_layout; _hulk_shim_gcc; _hulk_shim_defconfig\n'
            'for f in build_allmodconfig build_defconfig check_kabi \\\n'
            '         print_kabi_check_script get_kabi_whitelist_branch; do\n'
            '  declare -f "$f" | md5sum | cut -d" " -f1\n'
            'done' % (self.SUB, self.SUB))
        self.assertEqual(rc, 0, out)
        theirs = subprocess.run(
            ['bash', '-c',
             'test_path="%s"\n'
             '. <(grep -v \'^main "\\$@"[[:space:]]*$\' "%s/checkkabi.sh")\n'
             'for f in build_allmodconfig build_defconfig check_kabi '
             'print_kabi_check_script get_kabi_whitelist_branch; do '
             '  declare -f "$f" | md5sum | cut -d" " -f1; done'
             % (self.SUB, self.SUB)],
            stdout=subprocess.PIPE)
        self.assertEqual(out.split(), theirs.stdout.decode().split())

    def test_log_error_is_fatal_the_way_theirs_is(self):
        # Not stated anywhere we can read -- openeuler-jenkins is not on
        # this machine -- but their powerpc job ends on the line after
        # "[ERROR] build failed" with the build marked failed, having
        # never reached git_am_pr or the second build that follows it.
        rc, out = self.shell('log_error "build failed"; echo kept-going')
        self.assertNotEqual(rc, 0)
        self.assertNotIn('kept-going', out)
        self.assertIn('[ERROR] build failed', out)

    # ---- the shims, and what they are allowed to stand in for ----

    def test_the_overlay_stubs_the_comment_api_and_nothing_else(self):
        # pr_comment_api.py posts the verdict to a pull request that does
        # not exist yet, and wants a Jenkins token we do not have.
        overlay = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, overlay, True)
        rc, out = self.shell('_hulk_overlay "%s" "%s/t"'
                             % (self.SUB, overlay))
        self.assertEqual(rc, 0, out)
        here = os.path.join(overlay, 't', 'lib')
        self.assertEqual(
            0, subprocess.call(['python3', os.path.join(here, 'pr_comment_api.py'),
                                '-o', 'x']))
        # Their own checks have to still be there to be called.
        for f in ('check_branch.py', 'common.sh'):
            self.assertTrue(os.path.exists(os.path.join(here, f)), f)
        # check_branch.py resolves conf/ beside the lib/ it is run from,
        # with abspath rather than realpath, so conf/ has to come too.
        self.assertTrue(os.path.exists(
            os.path.join(overlay, 't', 'conf', 'check_build.yaml')))

    def test_the_overlay_leaves_their_checkout_alone(self):
        # The reason it is a copy.  A stub written over the original, or
        # the object file their kabi_guard Makefile drops, would show up
        # as a local modification of a submodule.
        theirs = os.path.join(self.SUB, 'lib', 'pr_comment_api.py')
        with open(theirs, 'rb') as f:
            before = f.read()
        overlay = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, overlay, True)
        self.shell('_hulk_overlay "%s" "%s/t"' % (self.SUB, overlay))
        with open(theirs, 'rb') as f:
            self.assertEqual(before, f.read())

    def a_tree(self):
        """A stand-in for the user's kernel, with something to lose."""
        kernel = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, kernel, True)
        for name in ('Makefile', 'Module.symvers', 'do-not-delete-me'):
            with open(os.path.join(kernel, name), 'w') as f:
                f.write('x\n')
        return kernel

    def test_the_build_directory_is_a_link_to_the_tree_we_were_given(self):
        # Their download_openeuler_kernel copies a reference clone and
        # merges the pull request into it.  We are handed that tree.
        kernel = self.a_tree()
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        rc, out = self.shell(
            'current_path="%s"; BUILD_ID=t; _hulk_shim_kernel\n'
            'download_openeuler_kernel\n'
            'readlink "%s/openeuler/kernel-t"' % (scratch, scratch),
            LINUX_SRC_PATH=kernel)
        self.assertEqual(rc, 0, out)
        self.assertIn(kernel, out)

    def test_their_cleanup_takes_the_link_and_not_the_tree(self):
        # Their main() finishes with `rm -rf .../kernel-$BUILD_ID`.  rm
        # unlinks a symbolic link rather than following it, which is the
        # whole reason a link is safe here -- but it is the user's kernel
        # on the other end of it, so it is worth asserting rather than
        # believing.
        kernel = self.a_tree()
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        self.shell(
            'current_path="%s"; BUILD_ID=t; _hulk_shim_kernel\n'
            'download_openeuler_kernel\n'
            'rm -rf "%s/openeuler/kernel-t"' % (scratch, scratch),
            LINUX_SRC_PATH=kernel)
        self.assertTrue(os.path.exists(
            os.path.join(kernel, 'do-not-delete-me')))

    def test_a_stale_symbol_list_does_not_survive_into_their_kabi_check(self):
        # Where their freshness comes from, and the reason the shim does
        # more than put a link down.  Their tree is new every run, so
        # check_kabi reading a Module.symvers means this build wrote it.
        # Ours has been built in before, for another architecture, and
        # the kernel keeps Module.symvers under mrproper rather than
        # clean -- so `make clean` would leave one behind for check-kabi
        # to compare against with no way of knowing what produced it.
        kernel = self.a_tree()
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        self.shell(
            'current_path="%s"; BUILD_ID=t; _hulk_shim_kernel\n'
            'download_openeuler_kernel' % scratch,
            LINUX_SRC_PATH=kernel)
        self.assertFalse(os.path.exists(os.path.join(kernel,
                                                     'Module.symvers')))

    def test_the_layout_check_does_not_touch_the_tree_it_builds_in(self):
        # Their build_defconfig_base gets its kABI layout baseline with
        # `git checkout origin/$tbranch` in the build directory.  That is
        # a scratch clone for them and the tree the series lives in for
        # us.  Their newer code is also not in the gate that produced the
        # logs we are read against: no checklayout row appears in any of
        # them.
        rc, out = self.shell(
            'test_path="%s"\n'
            '_hulk_load "%s/checkkabi.sh"\n'
            '_hulk_shim_layout\n'
            'declare -f build_defconfig_base check_layout_new report_layout'
            % (self.SUB, self.SUB))
        self.assertEqual(rc, 0, out)
        self.assertNotIn('git checkout', out)
        self.assertNotIn('check_layout.py', out)

    # ---- the one verdict we add, which can only subtract ----

    def defconfig_check(self, new, touched='drivers/net/foo.c',
                        shipped='arm64', label='aarch64'):
        """Their real check_defconfig, with our guard around it.

        Their four lines run: the shipped defconfig goes over .config,
        `make listnewconfig` is asked what is left unanswered, and the
        row is theirs to fail.  Only `make` and `git` are stubbed.
        """
        kernel = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, kernel, True)
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, scratch, True)
        if shipped:
            os.makedirs(os.path.join(kernel, 'arch', shipped, 'configs'))
            with open(os.path.join(kernel, 'arch', shipped, 'configs',
                                   'openeuler_defconfig'), 'w') as f:
                f.write('CONFIG_HAVE_GCC_PLUGINS=y\n')
        os.makedirs(os.path.join(scratch, 'openeuler'))
        os.symlink(kernel, os.path.join(scratch, 'openeuler', 'kernel-t'))
        with open(os.path.join(kernel, 'listnewconfig'), 'w') as f:
            f.write(new)

        rc, out = self.shell(
            'test_path="%(sub)s"\n'
            '_hulk_load "%(sub)s/checkkabi.sh"\n'
            # After loading, because their script opens with
            # current_path=$(pwd) and would clobber it.  oe_hulk_arch
            # gets the same effect by cd-ing there first.
            'current_path="%(scratch)s"; BUILD_ID=t; arch=%(label)s\n'
            'tbranch=OLK-6.6\n'
            '_hulk_shim_defconfig\n'
            'make() { cat %(kernel)s/listnewconfig; }\n'
            'git() { case "$*" in *rev-parse*) echo deadbeef ;;'
            '                     *diff*) echo "%(touched)s" ;; esac; }\n'
            'check_defconfig\n'
            % {'sub': self.SUB, 'scratch': scratch, 'kernel': kernel,
               'touched': touched, 'label': label},
            NUM_PATCHES='1')

        def read(name):
            try:
                with open(os.path.join(kernel, name)) as f:
                    return f.read()
            except FileNotFoundError:
                return ''
        return rc, out, read('result'), read('build_output.txt')

    def test_a_defconfig_that_answers_everything_passes(self):
        _, _, result, warnings = self.defconfig_check(new='')
        self.assertIn('| aarch64 checkdefconfig | pass |', result)
        self.assertEqual(warnings, '')

    def test_an_unanswered_symbol_fails_with_their_wording(self):
        _, _, result, warnings = self.defconfig_check(
            new='CONFIG_THREE=y\nnot a config line\n',
            touched='drivers/net/Kconfig')
        self.assertIn('| aarch64 checkdefconfig | fail |', result)
        self.assertIn('CONFIG_THREE=y', warnings)
        # Their grep is "^CONFIG_.*", so anything else listnewconfig
        # prints is not a symbol and does not decide the row.
        self.assertNotIn('not a config line', warnings)
        self.assertIn('openeuler_defconfig for aarch64 is not updated,',
                      warnings)
        self.assertIn("you can configure and run 'make update_oedefconfig'",
                      warnings)

    def test_a_series_with_no_kconfig_cannot_have_added_a_symbol(self):
        # scripts/gcc-plugins/Kconfig gates GCC_PLUGINS on whether the
        # compiler's plugin-version.h exists, and openEuler ships that
        # header in a package their build node does not install.  So a
        # developer machine that has it reports five symbols -- the
        # plugin ones and the two RANDSTRUCT choices -- where their own
        # aarch64 and x86_64 jobs report none on the same commit, and no
        # patch has ever been near any of them.
        #
        # A symbol is offered because some Kconfig file says so, so a
        # series that touches none cannot have added one.
        _, log, result, warnings = self.defconfig_check(
            new='CONFIG_GCC_PLUGINS=y\nCONFIG_RANDSTRUCT_FULL=n\n',
            touched='tools/perf/pmu-events/arch/x86/amdzen6/floating-point.json')
        self.assertIn('| aarch64 checkdefconfig | pass |', result)
        # Rolled back out of the warnings file, which their main() reads
        # for the "| build warning | fail |" row -- leaving the symbols
        # there would fail the run by another name.
        self.assertEqual(warnings, '')
        # Named on the log all the same: it is the first thing someone
        # will wonder about after reading their CI comment.
        self.assertIn('2 symbol(s) have no answer', log)
        self.assertIn('CONFIG_GCC_PLUGINS=y', log)
        self.assertIn('touches no Kconfig file', log)

    def test_the_guard_is_asked_only_after_their_check_has_failed(self):
        # It can subtract a failure and never add one.  A series that
        # touches no Kconfig and that their check passed gets a pass,
        # and gets it from them.
        _, log, result, _ = self.defconfig_check(
            new='', touched='drivers/net/foo.c')
        self.assertIn('| aarch64 checkdefconfig | pass |', result)
        self.assertNotIn('touches no Kconfig file', log)

    def test_the_guard_says_yes_when_it_cannot_tell(self):
        # It only ever excuses a patch, so an unanswerable question must
        # not be the thing that does the excusing.
        _, out = self.shell('_oe_series_touches_kconfig 0 && echo yes')
        self.assertIn('yes', out)

    def test_an_arch_with_no_shipped_defconfig_gets_no_row(self):
        # Their else branch logs that the check is not mandatory here and
        # records nothing, which is what an architecture openEuler does
        # not ship a defconfig for deserves.
        _, out, result, _ = self.defconfig_check(new='CONFIG_THREE=y\n',
                                                 shipped=None)
        self.assertEqual(result, '')
        self.assertIn('NOT mandatory', out)

    # ---- what Jenkins would have told their scripts ----

    def test_a_broken_matrix_check_is_an_error_not_a_skip(self):
        # check_branch.py exits 1 both for "this arch is off" and for a
        # failed import.  Telling them apart is the difference between a
        # build gate and a build gate that never runs.
        rc, _ = self.shell('_oe_arch_wanted x86_64 OLK-6.6 /nonexistent '
                           '>/dev/null 2>&1')
        self.assertEqual(rc, 2)

    def test_an_arch_their_branch_has_off_is_not_reported_as_checked(self):
        # Their job for one of these runs, prints the line below and
        # exits 0, so their PR comment shows it SUCCESS.  Both are a
        # pass; only one of them checked anything.
        rc, out = self.shell('oe_hulk_arch loongarch',
                             LINUX_SRC_PATH=tempfile.gettempdir(),
                             KABI_KERNEL_DIR=os.path.join(PROJECT_ROOT,
                                                          'euler', 'kernel'))
        self.assertEqual(rc, 3)
        self.assertIn('loongarch is set to false, exit', out)

    def test_a_named_werror_has_to_be_answered_by_name(self):
        # -Wno-error on its own undoes a blanket -Werror and leaves every
        # explicit -Werror=<name> standing.  OLK-6.6's hinic drivers trip
        # over -Werror=designated-init, which has to be answered by name.
        tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tree, True)
        with open(os.path.join(tree, 'Makefile'), 'w') as f:
            f.write('KBUILD_CFLAGS += -Werror=designated-init\n')
        _, out = self.shell(
            'cd "%s" && _oe_set_no_werror "" && printf "%%s\\n" '
            '"${_OE_NO_WERROR[0]}"' % tree)
        self.assertIn('-Wno-error=designated-init', out)
        self.assertIn('-Wno-error ', out)

    def a_tree_with_werror_configs(self):
        """A tree whose Kconfig offers the usual crop of WERROR symbols."""
        tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tree, True)
        with open(os.path.join(tree, 'Kconfig'), 'w') as f:
            f.write('config WERROR\n\tbool\n'
                    'config DRM_I915_WERROR\n\tbool\n')
        os.mkdir(os.path.join(tree, 'scripts'))
        # Stands in for scripts/config, recording what it was asked to
        # turn off.
        cfg = os.path.join(tree, 'scripts', 'config')
        with open(cfg, 'w') as f:
            f.write('#!/bin/sh\necho "$@" >> disabled\n')
        os.chmod(cfg, 0o755)
        return tree

    def config_hook(self, goal):
        """What the make hook does when their script asks for `goal`."""
        tree = self.a_tree_with_werror_configs()
        _, out = self.shell(
            'cd "%s"\n'
            '_hulk_shim_make\n'
            # The real make is not wanted; only what the hook does after
            # it, and whether it runs at all.
            'command() { shift; echo "real make: $*" >> ran; }\n'
            'make %s\n' % (tree, goal))

        def read(name):
            try:
                with open(os.path.join(tree, name)) as f:
                    return f.read()
            except FileNotFoundError:
                return ''
        return out, read('disabled'), read('ran')

    def test_the_config_bit_goes_off_for_allmodconfig(self):
        # allmodconfig turns CONFIG_WERROR on, and the command line
        # cannot answer a kernel that was configured to treat warnings as
        # errors.  Their build_allmodconfig configures and compiles in
        # one function, so `make` is the only seam between the two.
        _, disabled, _ = self.config_hook('allmodconfig')
        self.assertIn('--disable WERROR', disabled)
        # Read out of the tree, not listed: amdgpu, i915, kvm and powerpc
        # each have one of their own.
        self.assertIn('--disable DRM_I915_WERROR', disabled)
        # Disabling by hand can leave a dependent symbol unanswered.
        self.assertIn('olddefconfig', disabled + _)

    def test_oldconfig_is_hooked_too_because_theirs_runs_it_last(self):
        # Their build_allmodconfig is `make allmodconfig; make oldconfig;
        # make -j`, so oldconfig is the last thing to touch .config
        # before the compile reads it.
        _, disabled, _ = self.config_hook('oldconfig')
        self.assertIn('--disable WERROR', disabled)

    def test_their_defconfig_build_keeps_its_config_exactly(self):
        # openeuler_defconfig ships CONFIG_WERROR off, so there is
        # nothing to turn off -- and this is the build whose
        # Module.symvers the three kabi rows are read from, so the fewer
        # things touching it the better.
        _, disabled, ran = self.config_hook('openeuler_defconfig')
        self.assertEqual(disabled, '')
        self.assertIn('openeuler_defconfig', ran)

    def test_the_flags_reach_both_builds_and_all_four_variables(self):
        # Tidier to scope these to allmodconfig as well, and it does not
        # work: scripts/Makefile.extrawarn adds -Werror=designated-init
        # unconditionally, and openeuler_defconfig sets CONFIG_HINIC3=m
        # and CONFIG_HINIC5=m -- so the shipped defconfig compiles the
        # drivers that trip it, and without the flags their defconfig
        # build fails here and takes the ABI comparison with it.
        tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tree, True)
        with open(os.path.join(tree, 'Makefile'), 'w') as f:
            f.write('KBUILD_CFLAGS += -Werror=designated-init\n')
        _, out = self.shell(
            '_hulk_export_arch x86_64 "" "%s"\n'
            'for v in KCFLAGS KAFLAGS CFLAGS_KERNEL CFLAGS_MODULE; do\n'
            '  printf "%%s=%%s\\n" "$v" "${!v}"\n'
            'done' % tree)
        for var in ('KCFLAGS', 'KAFLAGS', 'CFLAGS_KERNEL', 'CFLAGS_MODULE'):
            self.assertIn('%s=-Wno-error -Wno-error=designated-init' % var,
                          out)

    #: Two diagnostics their builder does not emit and one the series
    #: owns, in the shape gcc actually prints them.
    WARNINGS = (
        "drivers/net/ethernet/huawei/hinic3/cqm/cqm_bitmap_table.c:415:10: "
        "warning: positional initialization [-Wdesignated-init]\n"
        "  415 |         {check_use_vram, cqm_buf_vram_kalloc},\n"
        "      |          ^~~~~~~~~~~~~~\n"
        "drivers/net/ethernet/huawei/hinic3/cqm/cqm_bitmap_table.c:415:10: "
        "note: (near initialization for 'g_malloc_funcs[0]')\n"
        "drivers/net/ethernet/huawei/hinic5/src/nic/linux/../../../sdk/"
        "knldk/lld/hinic5_lld.c:88:62: warning: positional initialization "
        "[-Wdesignated-init]\n"
        "fs/foo/bar.c: In function 'thing':\n"
        "fs/foo/bar.c:12:5: warning: unused variable 'x' "
        "[-Wunused-variable]\n"
    )

    def filtered(self, text, touched):
        """What survives warnings_keep_only_ours of a build's own output."""
        out = tempfile.NamedTemporaryFile('w', delete=False)
        out.write(text)
        out.close()
        self.addCleanup(os.unlink, out.name)
        self.shell(
            'git() { case "$*" in *rev-parse*) echo deadbeef ;;'
            '                     *diff*) printf "%%s\\n" %(touched)s ;;'
            ' esac; }\n'
            'warnings_keep_only_ours %(out)s 0'
            % {'touched': ' '.join("'%s'" % t for t in touched) or "''",
               'out': out.name},
            NUM_PATCHES='1')
        with open(out.name) as f:
            return f.read()

    def test_a_warning_the_series_did_not_cause_is_not_its_warning(self):
        # Their main() fails the run on any warning at all, which is a
        # sharp check on a builder whose clean tree compiles silently --
        # their aarch64 run of 2026-09-22 wrote nothing to
        # build_output.txt and finished SUCCESS.  Here the same tree is
        # never silent: OLK-6.6's hinic drivers warn under gcc 12.3 in
        # files no series has been near, so the row would fail every run
        # and say nothing about the patch.
        kept = self.filtered(self.WARNINGS, ['fs/foo/bar.c'])
        self.assertNotIn('cqm_bitmap_table.c', kept)
        # The source echo and the note belong to the group they explain
        # and go with it.
        self.assertNotIn('cqm_buf_vram_kalloc', kept)
        self.assertNotIn('near initialization', kept)

    def test_a_warning_in_a_file_the_series_touched_still_fails_the_row(self):
        # The row is kept, and kept for the thing it is for.  Dropping it
        # outright would pass a series that warns in its own new file and
        # fails their gate, which is the whole reason to run this first.
        kept = self.filtered(self.WARNINGS, ['fs/foo/bar.c'])
        self.assertIn("fs/foo/bar.c:12:5: warning: unused variable", kept)
        # Including the "In function" line that introduces it.
        self.assertIn("fs/foo/bar.c: In function 'thing'", kept)

    def test_the_kernels_own_dot_dot_paths_still_match(self):
        # hinic5 compiles through .../nic/linux/../../../sdk/knldk/lld/,
        # which is the same file git names without the dot-dots.  Without
        # normalising, every warning from it reads as somebody else's.
        kept = self.filtered(
            self.WARNINGS,
            ['drivers/net/ethernet/huawei/hinic5/sdk/knldk/lld/hinic5_lld.c'])
        self.assertIn('hinic5_lld.c:88:62', kept)

    def test_a_build_that_stopped_still_says_what_stopped_it(self):
        kept = self.filtered(
            self.WARNINGS +
            'make[4]: *** [scripts/Makefile.build:243: fs/nfsd.o] Error 1\n',
            ['fs/foo/bar.c'])
        self.assertIn('make[4]: *** [scripts/Makefile.build:243', kept)

    def test_nothing_is_dropped_when_the_series_cannot_be_determined(self):
        # It only ever excuses a patch, so an unanswerable question must
        # not be the thing that does the excusing.
        kept = self.filtered(self.WARNINGS, [])
        self.assertEqual(kept, self.WARNINGS)

    def test_what_their_check_kabi_writes_is_not_filtered(self):
        # check-kabi appends its own output to the same file, and that is
        # a verdict rather than a warning.  The filter is wrapped around
        # each build and judges only what that build appended.
        rc, out = self.shell(
            'test_path="%s"\n'
            '_hulk_load "%s/checkkabi.sh"\n'
            '_hulk_shim_builds\n'
            'declare -f check_kabi check_defconfig | grep -c '
            'warnings_keep_only_ours' % (self.SUB, self.SUB))
        self.assertIn('0', out)
        self.assertNotEqual(rc, 0)  # grep -c found none

    def test_a_flag_this_compiler_does_not_know_is_never_passed(self):
        # Several of the names in the tree are clang's, and handing gcc a
        # -Wno-error= for a warning it does not have is a hard error --
        # which would fail every file instead of the one it was aimed at.
        tree = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tree, True)
        with open(os.path.join(tree, 'Makefile'), 'w') as f:
            f.write('KBUILD_CFLAGS += -Werror=no-such-warning-exists\n')
        _, out = self.shell(
            'cd "%s" && _oe_set_no_werror "" && printf "%%s\\n" '
            '"${_OE_NO_WERROR[0]}"' % tree)
        self.assertNotIn('no-such-warning-exists', out)


class TestAnolisTheirScripts(unittest.TestCase):
    """That Anolis's own anck-pack-and-boot runs, and runs unaltered.

    Their suite is three files: run.sh holds the caselist and the rule
    that turns a build log into a verdict, anck_build.py composes the
    command line and names the log, and anck_build.sh does the clone,
    the dependency installs and every make line.  None of that is
    reimplemented any more, so what is worth testing is the seam --
    that their code loads without running, that the three things this
    machine cannot give their scripts are stood in for without
    touching anything that decides a verdict, and that the two tables
    we do keep are read back out of their files rather than remembered.
    """

    SUITE = os.path.join(PROJECT_ROOT, 'anolis', 'tone-cli', 'tests',
                         'anck-pack-and-boot')

    def setUp(self):
        if not os.path.isdir(self.SUITE):
            self.skipTest('anolis/tone-cli is not checked out')

    def scratch(self):
        """A directory that goes away again.

        Several of these hold a kernel clone, and /tmp on a build host
        is as likely as not a tmpfs -- it is 756G of RAM on this one --
        so a suite that leaves its scratch behind is spending memory on
        every run.
        """
        at = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, at, True)
        return at

    def shell(self, body, **env):
        """Run body with an_tone.sh sourced, and hand back what it said.

        The passwords are cleared out of the inherited environment
        first.  An already-exported HOST_USER_PWD would stay exported
        through a plain assignment, which is the opposite of what the
        real flow does and would quietly make one of these tests pass
        for the wrong reason.
        """
        script = '. "%s/anolis/an_tone.sh"\n%s' % (PROJECT_ROOT, body)
        clean = dict(os.environ, **env)
        for secret in ('HOST_USER_PWD', 'VM_ROOT_PWD'):
            clean.pop(secret, None)
        done = subprocess.run(
            ['bash', '-c', script], env=clean,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode('utf-8', 'replace')

    def code_of(self, *parts):
        """A shell file with its comments and blank lines taken out.

        Several of these ask whether a make line of ours survives, and
        the comments explaining which of their make lines replaced it
        would otherwise answer yes.
        """
        lines = []
        for line in read_file(*parts).splitlines():
            bare = line.strip()
            if bare and not bare.startswith('#'):
                lines.append(line)
        return '\n'.join(lines)

    def their_run_sh(self):
        return read_file('anolis', 'tone-cli', 'tests', 'anck-pack-and-boot',
                         'run.sh')

    # ---- loading their code without running it ----

    def test_their_functions_load_and_nothing_of_theirs_runs(self):
        # run.sh is a pure function library -- unlike openEuler's, there
        # is no `main "$@"` at the end to strip -- so this checks the
        # other half of that claim: that sourcing it really does execute
        # none of their suite.
        rc, out = self.shell(
            'AN_TONE_LOGS=/tmp _an_tone_load\n'
            'declare -f run show_result anck_build anck_boot_test '
            'check_kapi check_dmesg pass fail skip warn >/dev/null '
            '&& echo all-defined')
        self.assertEqual(rc, 0, out)
        self.assertIn('all-defined', out)
        # Their run() builds a kernel and prints these on the way.
        self.assertNotIn('====', out)
        self.assertNotIn('Clone kernel repository', out)

    def test_what_decides_a_verdict_is_theirs_verbatim(self):
        # The guard against the obvious regression: a shim that quietly
        # shadowed one of these would leave the tool reporting its own
        # opinion under Anolis's name.  run() is the caselist and the
        # grep, show_result turns a status into a marker, and the four
        # markers are what their parse.awk reads.
        theirs = 'run show_result pass fail skip warn prepare_build_repo'
        rc, out = self.shell(
            'AN_TONE_LOGS=/tmp _an_tone_load\n'
            '_an_tone_shim_boot; _an_tone_shim_build\n'
            'for f in %s; do declare -f "$f" | md5sum | cut -d" " -f1; done'
            % theirs)
        self.assertEqual(rc, 0, out)
        unshimmed = subprocess.run(
            ['bash', '-c',
             '. "%s/run.sh"\n'
             'for f in %s; do declare -f "$f" | md5sum | cut -d" " -f1; done'
             % (self.SUITE, theirs)],
            stdout=subprocess.PIPE)
        self.assertEqual(out.split(), unshimmed.stdout.decode().split())

    def test_only_their_harness_and_their_hosts_are_shimmed(self):
        # The other direction: the functions we do replace should be the
        # ones that reach for their build hosts and their artefact
        # store, and no others.
        rc, out = self.shell(
            'AN_TONE_LOGS=/tmp _an_tone_load\n'
            '_an_tone_shim_boot; _an_tone_shim_build\n'
            'for f in anck_build anck_boot_test check_kapi; do\n'
            '  declare -f "$f" | md5sum | cut -d" " -f1\n'
            'done')
        self.assertEqual(rc, 0, out)
        unshimmed = subprocess.run(
            ['bash', '-c',
             '. "%s/run.sh"\n'
             'for f in anck_build anck_boot_test check_kapi; do '
             '  declare -f "$f" | md5sum | cut -d" " -f1; done' % self.SUITE],
            stdout=subprocess.PIPE)
        for ours, unchanged in zip(out.split(),
                                   unshimmed.stdout.decode().split()):
            self.assertNotEqual(ours, unchanged)

    # ---- the two tables, read back out of their files ----

    def test_the_caselist_is_their_caselist(self):
        # Their run() walks a caselist of its own.  If they add a case
        # and we do not, the tool silently stops running it, which is
        # the failure mode this whole approach exists to avoid.
        theirs = re.search(r'(?m)^\s*caselist="([^"]+)"', self.their_run_sh())
        self.assertIsNotNone(theirs, 'their caselist moved')
        rc, out = self.shell('an_tone_cases')
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(out.split()), sorted(theirs.group(1).split()))

    def test_every_case_maps_to_the_keyword_their_py_selects_it_by(self):
        # anck_build.py picks cases out of $testcases by substring.  Our
        # table has to agree with theirs or asking for one case would
        # build another, or none.
        py = read_file('anolis', 'tone-cli', 'tests', 'anck-pack-and-boot',
                       'anck_build.py')
        theirs = dict(
            (case, keyword) for keyword, case in
            re.findall(r'if "(\w+)" in cases:\s*\n\s*group\d\.append\("(\w+)"\)',
                       py))
        self.assertTrue(theirs, 'their keyword mapping moved')
        for case, keyword in theirs.items():
            rc, out = self.shell('_an_tone_keyword %s' % case)
            self.assertEqual(rc, 0, '%s: %s' % (case, out))
            self.assertEqual(out.strip(), keyword, case)

    def test_a_name_that_is_not_one_of_theirs_is_refused(self):
        rc, out = self.shell('_an_tone_keyword build_everything')
        self.assertNotEqual(rc, 0)
        rc, out = self.shell('an_tone_case build_everything',
                             LINUX_SRC_PATH=PROJECT_ROOT)
        self.assertEqual(rc, 2, out)
        self.assertIn('no case', out)

    # ---- the repository their clone line is pointed at ----

    def a_kernel(self, version='5.10.134'):
        """A git repository that answers `make -s kernelversion`."""
        tree = self.scratch()
        self.addCleanup(shutil.rmtree, tree, True)
        with open(os.path.join(tree, 'Makefile'), 'w') as f:
            f.write('kernelversion:\n\t@echo %s\n' % version)
        for cmd in (['git', 'init', '-q', '-b', 'pr_13653'],
                    ['git', 'add', '-A'],
                    ['git', '-c', 'user.email=t@t', '-c', 'user.name=t',
                     'commit', '-qm', 'base']):
            subprocess.run(cmd, cwd=tree, check=True,
                           stdout=subprocess.DEVNULL)
        return tree

    def test_the_repo_carries_their_branch_at_the_series(self):
        # Their clone line takes a branch named for one of their
        # releases, and that name is not decoration: the same string
        # picks the ck-build branch, the dependency list and which of
        # their two Kconfig checks runs.  The user's tree is on a pull
        # request branch instead, so it gets a repository that has both.
        tree = self.a_kernel()
        bare = self.scratch() + '/cloud-kernel.git'
        rc, out = self.shell('_an_tone_repo "%s" devel-5.10 "%s"'
                             % (tree, bare))
        self.assertEqual(rc, 0, out)
        head = subprocess.run(['git', '-C', tree, 'rev-parse', 'HEAD'],
                              stdout=subprocess.PIPE).stdout.decode().strip()
        there = subprocess.run(
            ['git', '-C', bare, 'rev-parse', 'refs/heads/devel-5.10'],
            stdout=subprocess.PIPE).stdout.decode().strip()
        self.assertEqual(there, head)

    def test_the_repo_borrows_the_objects_rather_than_copying_them(self):
        # A kernel is two gigabytes of history and this runs once per
        # case.  An alternates file lends it the user's object store.
        tree = self.a_kernel()
        bare = self.scratch() + '/cloud-kernel.git'
        rc, out = self.shell('_an_tone_repo "%s" devel-5.10 "%s"'
                             % (tree, bare))
        self.assertEqual(rc, 0, out)
        with open(os.path.join(bare, 'objects/info/alternates')) as f:
            self.assertEqual(f.read().strip(),
                             os.path.join(tree, '.git/objects'))
        packs = os.path.join(bare, 'objects', 'pack')
        self.assertEqual([] if not os.path.isdir(packs) else os.listdir(packs),
                         [])

    def test_the_users_tree_is_not_written_to(self):
        # It is the tree they are about to send upstream.
        tree = self.a_kernel()

        def state():
            return subprocess.run(
                ['git', '-C', tree, 'for-each-ref'],
                stdout=subprocess.PIPE).stdout.decode()

        before = state()
        bare = self.scratch() + '/cloud-kernel.git'
        rc, out = self.shell('_an_tone_repo "%s" devel-5.10 "%s"'
                             % (tree, bare))
        self.assertEqual(rc, 0, out)
        self.assertEqual(state(), before)
        # In particular, no branch of theirs was created in it.
        self.assertNotIn('devel-5.10', before + state())

    def test_the_repo_is_named_what_their_rpm_build_expects(self):
        # Their anck_rpm_build does `ln -sf ../$anck_repo cloud-kernel`,
        # and $anck_repo is the basename of the URL with .git removed.
        # Any other name and their ck-build harness links nothing.
        self.assertIn('ln -sf ../${anck_repo} cloud-kernel',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.sh'))
        rc, out = self.shell('echo "${AN_TONE_SCRATCH}"')
        self.assertEqual(rc, 0, out)
        source = read_file('anolis', 'an_tone.sh')
        self.assertIn('cloud-kernel.git', source)

    def test_their_branch_gate_refuses_a_kernel_they_do_not_build(self):
        # Their anck_build.sh takes 4.19, 5.10, 6.1, 6.6 and 7.0 and
        # calls anything else unsupported.  Better to say so up front
        # than to clone and build for an hour first.
        tree = self.a_kernel(version='6.12.0')
        rc, out = self.shell('an_tone_case check_Kconfig',
                             LINUX_SRC_PATH=tree)
        self.assertEqual(rc, 2, out)
        self.assertIn('not one of their CI branches', out)

    # ---- the overlay ----

    def test_the_overlay_replaces_only_their_entry_point(self):
        # anck_build.sh is the one file that has to change, because it
        # is the one that needs /anck_build.  Their run.sh and their
        # anck_build.py are what decide and what drives, and they are
        # copied byte for byte.
        at = self.scratch() + '/suite'
        rc, out = self.shell('_an_tone_overlay "%s"' % at)
        self.assertEqual(rc, 0, out)

        for name in ('run.sh', 'anck_build.py', 'parse.awk'):
            with open(os.path.join(at, name), 'rb') as ours, \
                 open(os.path.join(self.SUITE, name), 'rb') as theirs:
                self.assertEqual(ours.read(), theirs.read(), name)

        with open(os.path.join(at, 'anck_build.sh')) as f:
            stub = f.read()
        self.assertIn('--sandbox', stub)
        self.assertIn('an_tone.sh', stub)
        # And their real one is what the stub ends up running.
        self.assertNotIn('KERNEL_CI_REPO_URL=$2', stub)
        self.assertIn('KERNEL_CI_REPO_URL=$2',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.sh'))

    def test_the_overlay_does_not_write_into_their_checkout(self):
        # Theirs is a submodule; a modification there would show up as a
        # local change of theirs and would be carried into a commit.
        before = subprocess.run(
            ['git', '-C', os.path.join(PROJECT_ROOT, 'anolis', 'tone-cli'),
             'status', '--porcelain'],
            stdout=subprocess.PIPE).stdout.decode()
        at = self.scratch() + '/suite'
        rc, out = self.shell('_an_tone_overlay "%s"' % at)
        self.assertEqual(rc, 0, out)
        after = subprocess.run(
            ['git', '-C', os.path.join(PROJECT_ROOT, 'anolis', 'tone-cli'),
             'status', '--porcelain'],
            stdout=subprocess.PIPE).stdout.decode()
        self.assertEqual(before, after)

    # ---- root, and where the password does not go ----

    def test_the_password_does_not_reach_the_environment(self):
        # anolis/test.sh is explicit that the two passwords are
        # deliberately not exported "so they stay out of
        # /proc/<pid>/environ of every command a test runs", and a
        # kernel build runs a great many commands.  So it goes through
        # an askpass helper instead, and this is the test that says so.
        #
        # HOST_USER_PWD is set here as a plain shell variable and not
        # through the environment, because that is how it arrives: read
        # out of .configure, never exported.
        at = self.scratch()
        rc, out = self.shell(
            'unset HOST_USER_PWD; HOST_USER_PWD=hunter2\n'
            'AN_TONE_OVERLAY=/tmp; AN_TONE_BIN="%s"; AN_TONE_LOGS=/tmp\n'
            '_an_tone_askpass "%s" || exit 1\n'
            '_an_tone_env /tmp devel-5.10 /tmp/x.git\n'
            'env | grep -c hunter2 || true' % (at, at))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.strip().splitlines()[-1], '0',
                         'the password reached the environment')
        self.assertNotIn('hunter2', read_file('anolis', 'an_tone.sh'))

    def test_the_password_is_read_from_the_config_not_inherited(self):
        # test.sh runs an_tone.sh as a script, so anything it does not
        # export is gone.  It does not export this one on purpose, which
        # would have left their yum silently installing nothing.
        source = read_file('anolis', 'an_tone.sh')
        self.assertIn('.configure', source)
        self.assertNotIn('export HOST_USER_PWD', source)
        self.assertNotIn('export HOST_USER_PWD', read_file('anolis', 'test.sh'))

    def test_the_password_file_is_readable_only_by_its_owner(self):
        at = self.scratch()
        rc, out = self.shell('unset HOST_USER_PWD; HOST_USER_PWD=hunter2\n'
                             '_an_tone_askpass "%s"' % at)
        self.assertEqual(rc, 0, out)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(at, '.pw')).st_mode),
                         0o600)
        # And the helper hands it to sudo, which is the only reader.
        with open(os.path.join(at, 'askpass')) as f:
            self.assertIn('.pw', f.read())

    def test_the_password_file_does_not_outlive_the_run(self):
        at = self.scratch()
        rc, out = self.shell(
            'unset HOST_USER_PWD; HOST_USER_PWD=hunter2\n'
            '_an_tone_askpass "%s" || exit 1\n'
            'AN_TONE_BIN="%s"\n'
            'test -f "%s/.pw" || { echo never-written; exit 1; }\n'
            '_an_tone_cleanup\n'
            'test -f "%s/.pw" && echo still-there || echo gone'
            % (at, at, at, at))
        self.assertEqual(rc, 0, out)
        self.assertIn('gone', out)

    def test_yum_reports_their_real_exit_status(self):
        # Most of their yum lines go unchecked, but build_perf's does
        # not: it reports "Failed to install perf dependencies" and
        # stops.  Swallowing a failure there would turn their clear
        # message into a compile error hundreds of lines later.
        self.assertIn('show_result $1 1 "Failed to install perf dependencies"',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.sh'))
        at = self.scratch()
        # A sudo that fails, reached the way the shim reaches it.
        fake = os.path.join(at, 'bin')
        os.makedirs(fake)
        with open(os.path.join(fake, 'sudo'), 'w') as f:
            f.write('#!/bin/sh\nexit 7\n')
        os.chmod(os.path.join(fake, 'sudo'), 0o755)
        rc, out = self.shell(
            'unset HOST_USER_PWD; HOST_USER_PWD=hunter2\n'
            '_an_tone_bin "%s/shims" || exit 1\n'
            '_an_tone_askpass "%s/shims" || exit 1\n'
            'PATH="%s:$PATH" "%s/shims/yum" install -y anything\n'
            'echo "rc=$?"' % (at, at, fake, at))
        self.assertIn('rc=7', out)

    def test_yum_without_a_password_says_so_instead_of_failing(self):
        # A dependency that is already present must not fail the case,
        # and most of their yum lines are unchecked anyway.
        at = self.scratch()
        rc, out = self.shell(
            '_an_tone_bin "%s" || exit 1\n'
            'unset SUDO_ASKPASS\n'
            '"%s/yum" install -y anything\n'
            'echo "rc=$?"' % (at, at))
        self.assertIn('rc=0', out)
        self.assertIn('nothing was installed', out)

    # ---- the sandbox ----

    def test_the_build_does_not_run_as_root(self):
        # The mount namespace needs root to create; the build must not
        # have it.  setpriv drops back to the user between the two.
        source = read_file('anolis', 'an_tone.sh')
        self.assertIn('setpriv --reuid', source)
        sandbox = source[source.index('_an_tone_sandbox() {'):]
        sandbox = sandbox[:sandbox.index('\n}\n')]
        self.assertIn('unshare --mount', sandbox)
        self.assertIn('setpriv', sandbox)
        # Their anck_build.sh must be reached through setpriv, not
        # before it.
        self.assertLess(sandbox.index('setpriv'),
                        sandbox.index('anck_build.sh'))

    def test_path_and_home_survive_the_privilege_drop(self):
        # sudo keeps the rest of the environment with -E but overrides
        # PATH from secure_path, which is where the yum shims are, and
        # always_set_home makes HOME /root, which their rpmbuild would
        # then try to build under.
        source = read_file('anolis', 'an_tone.sh')
        sandbox = source[source.index('_an_tone_sandbox() {'):]
        sandbox = sandbox[:sandbox.index('\n}\n')]
        self.assertIn('PATH=', sandbox)
        self.assertIn('HOME=', sandbox)

    def sandbox_fallback(self):
        """The half of the sandbox that runs when sudo cannot."""
        source = read_file('anolis', 'an_tone.sh')
        sandbox = source[source.index('_an_tone_sandbox() {'):]
        sandbox = sandbox[:sandbox.index('\n}\n')]
        return sandbox[sandbox.index('\n  fi\n'):]

    def test_the_build_runs_where_sudo_cannot_become_root(self):
        # The web service's systemd unit sets NoNewPrivileges, which
        # forbids a setuid binary from gaining privilege -- so sudo
        # refuses outright there, before it even asks for a password.
        # A sandbox that could only be made with sudo therefore failed
        # all seven build cases under the service while passing from a
        # shell, which is as confusing a failure as this tool has had.
        fallback = self.sandbox_fallback()
        self.assertIn('unshare --user --map-root-user --mount', fallback)
        self.assertNotIn('sudo -A', fallback)
        self.assertNotIn('exec sudo', fallback)

    def test_an_unprivileged_namespace_can_mount_their_build_directory(self):
        # What that fallback rests on, asserted rather than assumed: a
        # host can switch unprivileged user namespaces off entirely
        # (user.max_user_namespaces=0), and if this one has, every
        # build case fails under the service with no hint as to why.
        if not os.path.isdir('/anck_build'):
            self.skipTest('/anck_build has not been created yet')
        at = self.scratch()
        self.addCleanup(shutil.rmtree, at, True)
        open(os.path.join(at, 'proof'), 'w').close()
        rc, out = self.shell(
            'setpriv --no-new-privs unshare --user --map-root-user '
            '--mount -- bash -c \'mount --bind "$1" /anck_build && '
            'ls /anck_build\' _ "%s"' % at)
        self.assertEqual(rc, 0, out)
        self.assertIn('proof', out)

    def test_what_the_namespace_writes_comes_back_out_as_ours(self):
        # In that namespace the build is root, and root's files would
        # be unreadable and unremovable afterwards if the mapping did
        # not undo itself on the way out.  A build tree nobody can
        # delete would wedge every later run.
        if not os.path.isdir('/anck_build'):
            self.skipTest('/anck_build has not been created yet')
        at = self.scratch()
        self.addCleanup(shutil.rmtree, at, True)
        rc, out = self.shell(
            'setpriv --no-new-privs unshare --user --map-root-user '
            '--mount -- bash -c \'mount --bind "$1" /anck_build && '
            'touch /anck_build/written\' _ "%s"' % at)
        self.assertEqual(rc, 0, out)
        written = os.path.join(at, 'written')
        self.assertTrue(os.path.exists(written))
        self.assertEqual(os.stat(written).st_uid, os.getuid())

    def test_their_installs_are_not_attempted_when_root_is_unreachable(self):
        # Their yum lines go through sudo, so with root out of reach
        # each one would fail -- and build_perf is the one of theirs
        # that checks, reporting "Failed to install perf dependencies"
        # and stopping.  Telling the shims up front turns seven hard
        # failures into seven builds with a line in the log.
        self.assertIn('unset SUDO_ASKPASS', self.sandbox_fallback())

    def test_each_case_gets_its_own_anck_build(self):
        # Their three groups run in parallel on three separate hosts and
        # do not share /anck_build.  Sharing one here would have them
        # deleting each other's kernel tree: their anck_build.sh starts
        # with `rm -rf $anck_repo`.
        self.assertIn('rm -rf $anck_repo',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.sh'))
        source = read_file('anolis', 'an_tone.sh')
        sandbox = source[source.index('_an_tone_sandbox() {'):]
        sandbox = sandbox[:sandbox.index('\n}\n')]
        self.assertIn('${AN_TONE_SCRATCH}/${case_name}', sandbox)

    def test_their_three_groups_all_build_here(self):
        # Their anck_build.py sends two of the three groups to other
        # hosts when it is told about them.  The VM has no resources to
        # build a kernel with, so all three stay here -- which is what
        # their third group already does.
        rc, out = self.shell(
            'AN_TONE_OVERLAY=/tmp AN_TONE_BIN=/tmp AN_TONE_LOGS=/tmp\n'
            '_an_tone_env /tmp devel-5.10 /tmp/x.git\n'
            'echo "yes=[${YES_BUILDER}] def=[${DEF_BUILDER}]'
            ' remote=[${REMOTE_HOST}]"')
        self.assertEqual(rc, 0, out)
        self.assertIn('yes=[] def=[] remote=[]', out)

    def test_no_pull_request_is_applied(self):
        # Their anck_build.sh applies KERNEL_CI_PR_ID with git am on top
        # of the branch it cloned.  There is no pull request yet -- that
        # is the point of running this before submission -- and the
        # clone is already the series, so theirs takes its own
        # "Skip apply patch" path.
        self.assertIn('Skip apply patch',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.sh'))
        rc, out = self.shell(
            'AN_TONE_OVERLAY=/tmp AN_TONE_BIN=/tmp AN_TONE_LOGS=/tmp\n'
            '_an_tone_env /tmp devel-5.10 /tmp/x.git\n'
            'echo "pr=[${KERNEL_CI_PR_ID}]"')
        self.assertIn('pr=[]', out)

    # ---- their four markers ----

    def test_their_four_markers_become_four_statuses(self):
        # parse.awk is where their markers are spelled, and Warning is
        # one of them: a case they print and still accept.  Folding it
        # into pass would hide something they flagged and into fail
        # would reject a series they let through.
        awk = read_file('anolis', 'tone-cli', 'tests', 'anck-pack-and-boot',
                        'parse.awk')
        for marker in ('====PASS:', '====FAIL:', '====SKIP:', '====WARN:'):
            self.assertIn(marker, awk, marker)

        for marker, want in (('====PASS: build_perf', 0),
                             ('====FAIL: build_perf', 1),
                             ('====SKIP: build_perf', 3),
                             ('====WARN: build_perf', 5),
                             ('nothing of theirs at all', 2)):
            rc, out = self.shell('_an_tone_verdict_rc "%s"' % marker)
            self.assertEqual(rc, want, '%s gave %d: %s' % (marker, rc, out))

    def test_a_failure_outranks_a_warning(self):
        rc, _ = self.shell(
            '_an_tone_verdict_rc "====WARN: a\n====FAIL: b"')
        self.assertEqual(rc, 1)

    # ---- the handoff to their anck-ci-test ----

    def rpms_at(self, layout):
        """A scratch with their RPMs laid out under outputs/<layout>."""
        at = self.scratch()
        self.addCleanup(shutil.rmtree, at, True)
        where = os.path.join(at, 'anck_rpm_build', 'ck-build', 'outputs',
                             layout)
        os.makedirs(where)
        version = '5.10.134-0.git.abcdef.an23.x86_64'
        for pkg in ('kernel-core', 'kernel-modules', 'kernel-headers'):
            open(os.path.join(where, '%s-%s.rpm' % (pkg, version)), 'w').close()
        return at, where

    def test_the_rpm_directory_is_found_in_either_of_their_layouts(self):
        # Their own two functions disagree: anck_boot_test reads
        # outputs/0, while the anck_build that produced them collects
        # with `find /anck_build/ck-build/outputs -name *.rpm`.  Their
        # an8 builders lay the outputs out under a build number and
        # their an23 ones under rpmbuild/RPMS/$arch, so following the
        # find is the only one of the two that is right for both.
        self.assertIn('anck_rpms_dir="/anck_build/ck-build/outputs/0"',
                      self.their_run_sh())
        self.assertIn('find /anck_build/ck-build/outputs -name *.rpm',
                      self.their_run_sh())

        for layout in ('0', 'rpmbuild/RPMS/x86_64'):
            at, where = self.rpms_at(layout)
            rc, out = self.shell('AN_TONE_SCRATCH="%s" an_tone_rpm_dir' % at)
            self.assertEqual(rc, 0, '%s: %s' % (layout, out))
            self.assertEqual(out.strip(), where, layout)

    def test_no_rpms_is_reported_rather_than_guessed_at(self):
        # The row that follows is a skip with a reason, not a failure:
        # nothing was rejected, their build simply has not run yet.
        rc, out = self.shell(
            'AN_TONE_SCRATCH=/tmp/nowhere-$$\n'
            'an_tone_rpm_dir && echo found || echo none')
        self.assertEqual(rc, 0, out)
        self.assertIn('none', out)
        self.assertIn('has not produced any RPMs yet',
                      read_file('anolis', 'test.sh'))

    def test_a_source_rpm_is_not_offered_to_their_boot_test(self):
        # Their anck_boot_test does `rpm -Uvh --force /anck_rpms/*.rpm`
        # over the whole directory it is given, which a src.rpm would
        # fail.
        self.assertIn('rpm -Uvh --force /anck_rpms/*.rpm',
                      self.their_run_sh())
        at, where = self.rpms_at('rpmbuild/SRPMS')
        os.rename(os.path.join(where,
                               'kernel-core-5.10.134-0.git.abcdef.an23.x86_64.rpm'),
                  os.path.join(where, 'kernel-5.10.134-0.git.abcdef.an23.src.rpm'))
        rc, out = self.shell('AN_TONE_SCRATCH="%s" an_tone_rpm_dir' % at)
        self.assertNotIn('SRPMS', out)

    def test_the_expected_version_is_read_their_way(self):
        # Their anck_boot_test asks the kernel-headers package, with
        # this exact query format, and their anck-ci-test compares
        # uname -r against it.  Left unset, theirs falls back to the
        # newest installed kernel-headers and warns -- which on the VM
        # is whatever was there before the series.
        self.assertIn('%{VERSION}-%{RELEASE}.%{ARCH}', self.their_run_sh())
        self.assertIn('%{VERSION}-%{RELEASE}.%{ARCH}',
                      read_file('anolis', 'an_tone.sh'))
        self.assertIn('EXPECT_KERNEL_VERSION', read_file('anolis', 'test.sh'))

    def test_installing_and_rebooting_reports_no_verdict(self):
        # On their CI those two steps are the platform's, not a test's:
        # their anck-ci-test Readme gives the chain as install_rpm ->
        # reboot -> run_case, and their suite only inspects what it
        # finds running.  So the helper that does them must not be
        # deciding a row.
        boot = read_file('lib', 'boot_test.sh')
        for verdict in ('pass "', 'fail "', 'skip "'):
            self.assertNotIn(verdict, boot, verdict)
        self.assertIn('boot_install_and_reboot', boot)
        self.assertIn('boot_install_and_reboot', read_file('anolis', 'test.sh'))

    # ---- the VM cases, and the password they need ----

    def configured(self, name):
        """The value .configure gives name, or None."""
        path = os.path.join(PROJECT_ROOT, 'anolis', '.configure')
        if not os.path.exists(path):
            return None
        with open(path, errors='replace') as f:
            for line in f:
                if line.startswith(name + '='):
                    return line.split('=', 1)[1].strip().strip('\'"')
        return None

    def in_a_case(self, body, **env):
        """Run body with cases/lib.sh sourced, as a case is run."""
        clean = {k: v for k, v in os.environ.items()
                 if k not in ('VM_ROOT_PWD', 'HOST_USER_PWD')}
        clean.update(env)
        done = subprocess.run(
            ['bash', '-c', '. "%s/anolis/cases/lib.sh"\n%s'
             % (PROJECT_ROOT, body)],
            env=clean, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode('utf-8', 'replace')

    def test_a_case_is_given_the_vm_password_it_asks_for(self):
        # test.sh exports VM_IP but deliberately not VM_ROOT_PWD, and a
        # case is a process of its own -- so it inherited an address
        # with no password, which their anck_ci_test.sh cannot tell
        # apart from no VM at all.  Both VM rows skipped with "no VM
        # configured" on a machine that had one configured.
        if not self.configured('VM_ROOT_PWD'):
            self.skipTest('no VM password in anolis/.configure')
        rc, out = self.in_a_case('echo "pwd=${VM_ROOT_PWD:+set}"')
        self.assertEqual(rc, 0, out)
        self.assertIn('pwd=set', out)

    def test_the_vm_password_still_goes_no_further_than_the_case(self):
        # Reading it must not amount to exporting it.  anolis/.configure
        # keeps both passwords unexported on purpose, so that neither
        # shows up in /proc/<pid>/environ of the commands a case runs --
        # and a case runs ssh, scp and a kernel build.
        if not self.configured('VM_ROOT_PWD'):
            self.skipTest('no VM password in anolis/.configure')
        rc, out = self.in_a_case(
            'bash -c \'echo "child=${VM_ROOT_PWD:-none}"\'')
        self.assertEqual(rc, 0, out)
        self.assertIn('child=none', out)

    def test_reading_the_config_does_not_overrule_the_caller(self):
        # Only the passwords may come across.  Sourcing the whole file
        # would overwrite everything the caller had set deliberately,
        # which is how a case is aimed at a downloaded debuginfo rpm or
        # at a kernel other than the configured one -- lib.sh says in
        # as many words that anything already exported wins.  Caught
        # the hard way: a test aiming at a 6.12 tree to check their
        # branch gate was handed the real tree and built it.
        rc, out = self.in_a_case('echo "src=${LINUX_SRC_PATH:-unset}"',
                                 LINUX_SRC_PATH='/nowhere/in/particular')
        self.assertEqual(rc, 0, out)
        self.assertIn('src=/nowhere/in/particular', out)

    def test_a_vm_case_that_skips_says_what_their_suite_said(self):
        # Their run.sh checks boot_kernel_rpm first and skips the other
        # two when it fails, because both read the running kernel and
        # there is nothing to say about the series if the machine is not
        # booted into it.  Their marker alone does not say that, and
        # "Reason: ====SKIP: check_kapi" tells a reader nothing.
        source = read_file('anolis', 'test.sh')
        fn = source[source.index('vm_skip_reason() {'):]
        fn = fn[:fn.index('\n}\n') + 3]
        log = tempfile.NamedTemporaryFile('w', suffix='.log', delete=False)
        self.addCleanup(os.unlink, log.name)
        log.write('expect kernel version: 5.10.134-1.an23.x86_64\n'
                  'Error: running kernel [6.6.0-1.an23.x86_64] != '
                  'expected [5.10.134-1.an23.x86_64]\n'
                  '====FAIL: boot_kernel_rpm\n'
                  '====SKIP: check_kapi\n'
                  '====SKIP: check_dmesg\n')
        log.close()
        done = subprocess.run(
            ['bash', '-c', fn + '\nvm_skip_reason "%s"' % log.name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = done.stdout.decode()
        self.assertIn('running kernel', out)
        self.assertIn('TEST_BOOT_KERNEL', out)

    def test_a_skip_with_nothing_to_explain_it_still_says_something(self):
        # Their suite skips for reasons of its own too -- no debuginfo
        # for check_kapi, say -- and falling back to their markers is
        # better than reporting an empty reason.
        source = read_file('anolis', 'test.sh')
        fn = source[source.index('vm_skip_reason() {'):]
        fn = fn[:fn.index('\n}\n') + 3]
        log = tempfile.NamedTemporaryFile('w', suffix='.log', delete=False)
        self.addCleanup(os.unlink, log.name)
        log.write('no kernel-debuginfo installed\n====SKIP: check_kapi\n')
        log.close()
        done = subprocess.run(
            ['bash', '-c', fn + '\nvm_skip_reason "%s"' % log.name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertIn('check_kapi', done.stdout.decode())

    # ---- nothing of ours left in the build ----

    def test_no_make_line_of_ours_survives_in_test_sh(self):
        # Every one of their build cases is a make line in their
        # anck_build.sh.  A copy of one here is the drift this replaced:
        # their build_allno_config does not run `make modules` and ours
        # did, and their two defconfig cases run `make olddefconfig`
        # after the defconfig and ours did not.
        ours = self.code_of('anolis', 'test.sh')
        for line in ('make clean', 'make allyesconfig', 'make allnoconfig',
                     'make anolis_defconfig', 'make anolis-debug_defconfig',
                     'make olddefconfig', 'make dist-configs-check',
                     'make dist-configs-update', 'make dist-genspec',
                     'make dist-rpms', 'make modules', 'make -j'):
            self.assertNotIn(line, ours, '%s is still run by test.sh' % line)
        # And every one of them is in theirs, so this is not passing
        # because the names changed.
        theirs = read_file('anolis', 'tone-cli', 'tests',
                           'anck-pack-and-boot', 'anck_build.sh')
        for line in ('make clean', 'make allyesconfig', 'make allnoconfig',
                     'make anolis_defconfig', 'make olddefconfig',
                     'make dist-genspec'):
            self.assertIn(line, theirs, line)

    # ---- their ck-build branch ----

    def anolis_release(self):
        try:
            with open('/etc/os-release') as f:
                fields = dict(
                    line.strip().split('=', 1) for line in f
                    if '=' in line)
        except OSError:
            return None
        if fields.get('ID', '').strip('"') != 'anolis':
            return None
        return fields.get('VERSION_ID', '').strip('"').split('.')[0]

    def a_ck_build(self, *branches):
        """A stand-in for their ck-build repository, with given branches."""
        at = self.scratch() + '/ck-build.git'
        subprocess.run(['git', 'init', '-q', '--bare', at], check=True)
        tree = self.a_kernel()
        head = subprocess.run(['git', '-C', tree, 'rev-parse', 'HEAD'],
                              stdout=subprocess.PIPE).stdout.decode().strip()
        with open(os.path.join(at, 'objects/info/alternates'), 'w') as f:
            f.write(os.path.join(tree, '.git/objects'))
        for branch in branches:
            subprocess.run(['git', '-C', at, 'update-ref',
                            'refs/heads/' + branch, head], check=True)
        return at

    def ck_branch(self, repo, branch, **env):
        rc, out = self.shell(
            'unset CK_BUILDER_BRANCH\n'
            '_AN_TONE_CK_REPO="%s"\n'
            '_an_tone_ck_branch %s\n'
            'echo "chose=[${CK_BUILDER_BRANCH:-}]"' % (repo, branch), **env)
        self.assertEqual(rc, 0, out)
        return out

    def test_their_builder_for_this_hosts_release_is_preferred(self):
        # Their anck_build.sh picks an8-5.10 for a 5.10 kernel, because
        # that is the machine they build 5.10 on.  rpm 4.18 on Anolis 23
        # rejects that branch's spec template outright -- "%rpmversion
        # is a built-in" and extra tokens after %endif are errors there,
        # warnings on their an23 branches -- so on an Anolis 23 host the
        # an23 builder is the one that gets as far as compiling.
        release = self.anolis_release()
        if not release:
            self.skipTest('not an Anolis host, so there is nothing to prefer')
        repo = self.a_ck_build('an8-5.10', 'an%s-5.10' % release)
        out = self.ck_branch(repo, 'devel-5.10')
        self.assertIn('chose=[an%s-5.10]' % release, out)
        self.assertIn('different Anolis release', out)

    def test_their_own_default_stands_when_they_publish_nothing_for_us(self):
        # Leaving it unset is what hands the choice back to their
        # script, and theirs is the only combination they test.
        repo = self.a_ck_build('an8-5.10')
        out = self.ck_branch(repo, 'devel-7.0')
        self.assertIn('chose=[]', out)

    def test_an_explicit_builder_branch_wins(self):
        # Their script's own override, which is how a user pins one
        # without either of us editing their code.
        rc, out = self.shell(
            'CK_BUILDER_BRANCH=an8-5.10\n'
            '_AN_TONE_CK_REPO="%s"\n'
            '_an_tone_ck_branch devel-5.10\n'
            'echo "chose=[${CK_BUILDER_BRANCH}]"'
            % self.a_ck_build('an8-5.10', 'an23-5.10'))
        self.assertEqual(rc, 0, out)
        self.assertIn('chose=[an8-5.10]', out)

    def test_no_builder_branch_is_written_down(self):
        # The release comes from /etc/os-release, the series from the
        # branch their code was given, and the result is used only if
        # their repository has it.  A branch name of ours in here would
        # be a guess with a shelf life.
        source = self.code_of('anolis', 'an_tone.sh')
        for guess in ('an8-5.10', 'an23-5.10', 'an23-6.6', 'an8-4.19',
                      'an23-6.1'):
            self.assertNotIn(guess, source, guess)
        self.assertIn('/etc/os-release', source)
        # And theirs does carry them, which is where they belong.
        self.assertIn('an8-5.10',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.sh'))

    def test_nothing_un_pins_a_submodule_behind_our_back(self):
        """`submodule update --remote` is not how we follow them.

        It moves a submodule to the tip of its branch without recording
        it, so the commit this repository pins and the code actually
        running stop being the same thing -- and the whole argument for
        running their scripts instead of copying them is that the pinned
        commit is what we ran.  A deliberate update is a commit that
        moves the pointer, not a side effect of running a test.

        This used to sit in the check_kapi reimplementation, which is
        gone.
        """
        for where in ('anolis', 'euler', 'lib'):
            for name in sorted(os.listdir(os.path.join(PROJECT_ROOT, where))):
                if not name.endswith('.sh'):
                    continue
                body = self.code_of(where, name)
                self.assertNotIn('--remote', body, '%s/%s' % (where, name))

    def test_the_build_job_count_is_theirs(self):
        # Their anck_build.sh sets its own, one less than the processor
        # count, so the tool's BUILD_THREADS no longer applies here.
        self.assertIn('job_num',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.sh'))
        self.assertNotIn('BUILD_THREADS=', read_file('anolis', 'test.sh'))


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
        # Every submodule, read from .gitmodules rather than listed
        # here.  A list went stale the moment tone-cli was added, and
        # the failure was a copytree error deep in someone else's
        # symlinks rather than anything to do with readiness.
        skip = ['__pycache__'] + [os.path.basename(path) for path in
                                  submodule_paths()]
        shutil.copytree(os.path.join(PROJECT_ROOT, distro), target,
                        symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(*skip))
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


class TestProgressBar(unittest.TestCase):
    """That a silent build still shows how far along it is.

    Their build cases end in ``make -j $job_num -s``, so between their
    configure step and their verdict nothing is printed at all -- for
    allyesconfig the best part of an hour.  The object files appearing
    on disk are what gets counted instead, and their own markers say
    which phase is producing them.
    """

    SCRIPT = os.path.join(PROJECT_ROOT, 'lib', 'progress.py')

    def setUp(self):
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'lib'))
        self.addCleanup(sys.path.remove,
                        os.path.join(PROJECT_ROOT, 'lib'))
        import progress
        self.progress = progress
        self.at = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.at, True)

    def a_tree(self, objects=5, others=3):
        tree = os.path.join(self.at, 'tree')
        deep = os.path.join(tree, 'drivers', 'net')
        os.makedirs(deep)
        for n in range(objects):
            open(os.path.join(deep, 'f%d.o' % n), 'w').close()
        for n in range(others):
            open(os.path.join(deep, 'f%d.c' % n), 'w').close()
        return tree

    def a_run(self, script, *extra):
        """Run the bar over a little shell script, without a terminal."""
        runner = os.path.join(self.at, 'runner.sh')
        with open(runner, 'w') as f:
            f.write('#!/bin/bash\n' + script)
        os.chmod(runner, 0o755)
        done = subprocess.run(
            ['python3', self.SCRIPT,
             '--watch', os.path.join(self.at, 'tree'),
             '--output', os.path.join(self.at, 'case.log'),
             '--totals', os.path.join(self.at, 'totals'),
             '--name', 'demo'] + list(extra) + ['--', runner],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode('utf-8', 'replace')

    def remember(self, total):
        """What a previous successful run of this case built."""
        os.makedirs(os.path.join(self.at, 'totals'), exist_ok=True)
        with open(os.path.join(self.at, 'totals', 'demo'), 'w') as f:
            f.write('%d\n' % total)

    def objects(self, how_many):
        """A build in progress, that far along."""
        tree = os.path.join(self.at, 'tree')
        os.makedirs(tree, exist_ok=True)
        return 'for i in $(seq 1 %d); do : > "%s/o$i.o"; done\nsleep 6\n' % (
            how_many, tree)

    # ---- what gets counted ----

    def test_only_object_files_are_counted(self):
        tree = self.a_tree(objects=5, others=3)
        self.assertEqual(self.progress.count_objects(tree), 5)

    def test_a_tree_that_does_not_exist_yet_counts_nothing(self):
        # Their script creates it by cloning into it, so the bar starts
        # before there is anything to look at.
        self.assertEqual(self.progress.count_objects(
            os.path.join(self.at, 'not-yet')), 0)
        self.assertEqual(self.progress.count_objects(''), 0)

    def test_a_linked_tree_is_not_counted_twice(self):
        # Their anck_rpm_build does `ln -sf ../${anck_repo} cloud-kernel`
        # to put the kernel where their build harness expects it, so a
        # walk that followed links would count every object twice and
        # the bar would sit at half of what it should be.
        tree = self.a_tree(objects=5, others=0)
        os.symlink(tree, os.path.join(self.at, 'tree', 'mirror'))
        self.assertEqual(self.progress.count_objects(tree), 5)

    # ---- the phase, in their words ----

    def a_phase(self, text):
        log = os.path.join(self.at, 'phases.log')
        with open(log, 'w') as f:
            f.write(text)
        return self.progress.Phase(log).poll()

    def test_their_markers_become_the_label(self):
        # The three shapes their anck_build.sh announces steps in.
        self.assertEqual(self.a_phase('===> Clone kernel repository...\n'),
                         'Clone kernel repository')
        self.assertEqual(
            self.a_phase('== Build Kernel with allyesconfig ==\n'),
            'Build Kernel with allyesconfig')
        self.assertEqual(self.a_phase('  -> running their suite on 1.2.3.4\n'),
                         'running their suite on 1.2.3.4')

    def test_the_latest_marker_wins(self):
        self.assertEqual(
            self.a_phase('===> Clone kernel repository...\n'
                         'Cloning into cloud-kernel...\n'
                         '===> Install related packages...\n'
                         '== Check kconfig ==\n'
                         'PASS\n'),
            'Check kconfig')

    def test_the_log_is_read_as_it_grows(self):
        # A kernel build's log is read every fifth of a second for as
        # long as the build runs, so it is read from where the last read
        # stopped rather than from the beginning each time.
        log = os.path.join(self.at, 'growing.log')
        with open(log, 'w') as f:
            f.write('===> Clone kernel repository...\n')
        phase = self.progress.Phase(log)
        self.assertEqual(phase.poll(), 'Clone kernel repository')
        with open(log, 'a') as f:
            f.write('== Build Kernel with allnoconfig ==\n')
        self.assertEqual(phase.poll(), 'Build Kernel with allnoconfig')
        # Nothing new, and the answer does not become blank.
        self.assertEqual(phase.poll(), 'Build Kernel with allnoconfig')

    def test_the_phase_is_read_from_where_their_script_writes_it(self):
        # Their anck_build.py runs their anck_build.sh with its output
        # redirected to /tmp/anck_<case>.log, so our own log gets
        # nothing until the case is over.  Reading the phase from ours
        # left the label blank for the whole of a run.
        theirs = os.path.join(self.at, 'theirs.log')
        rc, out = self.a_run(
            'echo "===> Clone kernel repository..." > "%s"\nsleep 6\n'
            % theirs, '--phases', theirs)
        self.assertEqual(rc, 0)
        self.assertIn('phase=Clone kernel repository', out)

    def test_their_live_log_is_named_the_way_their_script_names_it(self):
        # Two of their files agree on this path -- anck_build.py writes
        # it and run.sh reads the verdict out of it -- so it is theirs
        # to change, and when they do, this is where it shows up rather
        # than as a bar with no label.
        self.assertIn('"/tmp/anck_{}.log".format(config)',
                      read_file('anolis', 'tone-cli', 'tests',
                                'anck-pack-and-boot', 'anck_build.py'))
        self.assertIn('/tmp/anck_${2:-}.log',
                      read_file('anolis', 'an_tone.sh'))
        self.assertIn('--case-log', read_file('anolis', 'test.sh'))

    # ---- running the case ----

    def test_the_cases_status_is_the_bars_status(self):
        # The bar is in front of their verdict, so swallowing a failure
        # here would turn every failing case into a pass.
        self.assertEqual(self.a_run('exit 0')[0], 0)
        self.assertEqual(self.a_run('exit 1')[0], 1)
        self.assertEqual(self.a_run('exit 5')[0], 5)

    def test_the_cases_output_goes_to_its_log_and_not_the_bar(self):
        # Their "<case>: pass" is read back out of that file, and
        # anything of ours in it would be read along with it.
        rc, out = self.a_run('echo "check_Kconfig: pass"\n'
                             'echo "====PASS: check_Kconfig"\n')
        self.assertEqual(rc, 0)
        self.assertNotIn('====PASS', out)
        with open(os.path.join(self.at, 'case.log')) as f:
            log = f.read()
        self.assertIn('====PASS: check_Kconfig', log)
        self.assertNotIn('prci-progress', log)

    # ---- the denominator, which is nowhere written down ----

    def test_the_first_run_of_a_case_has_no_percentage(self):
        # Nothing to measure against yet.  Claiming one would be making
        # it up, and allyesconfig and allnoconfig differ by four orders
        # of magnitude.
        os.makedirs(os.path.join(self.at, 'tree'))
        rc, out = self.a_run('touch "%s"/tree/a.o\nsleep 6\n' % self.at)
        self.assertEqual(rc, 0)
        self.assertIn('pct=-', out)

    def test_a_case_measures_itself_against_its_last_run(self):
        self.remember(1000)
        rc, out = self.a_run(self.objects(500))
        self.assertEqual(rc, 0)
        self.assertIn('pct=50', out)

    def stale_objects(self, how_many):
        """A previous run's work, still in the tree and plainly older."""
        tree = self.a_tree(objects=how_many, others=0)
        old = time.time() - 7200
        for here, _, files in os.walk(tree):
            for name in files:
                os.utime(os.path.join(here, name), (old, old))
        return tree

    def test_the_previous_runs_objects_are_not_counted_as_this_ones(self):
        # Their anck_build.sh opens by removing the tree and cloning it
        # again, and openEuler's build runs `make distclean` -- but
        # neither has done so in the first half-minute, so what is on
        # disk until then is the last run's work.  Counted, it opened
        # the bar at 99% and dropped it to nothing once their clean-up
        # caught up.  An object older than the run did not come from it.
        self.remember(400)
        self.stale_objects(400)
        rc, out = self.a_run(self.objects(100))
        self.assertEqual(rc, 0)
        self.assertNotIn('pct=99', out)
        self.assertNotIn('pct=100', out)
        self.assertIn('pct=25', out)

    def test_an_older_object_is_not_this_runs(self):
        tree = self.stale_objects(6)
        self.assertEqual(self.progress.count_objects(tree), 6)
        self.assertEqual(
            self.progress.count_objects(tree, time.time() - 60), 0)

    def test_what_gets_remembered_is_this_runs_work_only(self):
        # Otherwise the first run after a tree was left dirty would
        # remember the two runs added together, and every bar after it
        # would stop half way.
        self.stale_objects(300)
        rc, _ = self.a_run(self.objects(50))
        self.assertEqual(rc, 0)
        with open(os.path.join(self.at, 'totals', 'demo')) as f:
            self.assertEqual(f.read().strip(), '50')

    def test_a_case_that_compiles_nothing_claims_no_percentage(self):
        # Their check_Kconfig runs their config tooling and no compiler,
        # leaving eight objects behind.  A bar measured against eight
        # reads 99% within seconds and stays there for the minute the
        # check really takes, which is worse than no bar.
        self.remember(8)
        rc, out = self.a_run(self.objects(8))
        self.assertEqual(rc, 0)
        self.assertIn('pct=-', out)
        self.assertNotIn('pct=9', out)

    def test_what_a_case_built_is_remembered_for_next_time(self):
        rc, _ = self.a_run(self.objects(7))
        self.assertEqual(rc, 0)
        with open(os.path.join(self.at, 'totals', 'demo')) as f:
            self.assertEqual(f.read().strip(), '7')

    def test_a_case_that_failed_is_not_remembered(self):
        # A build that stopped early left fewer objects behind than a
        # whole one; writing that down would make the next run's bar
        # reach 100% and sit there for the rest of the build.
        self.remember(9000)
        self.a_tree(objects=3)
        self.assertEqual(self.a_run('exit 1')[0], 1)
        with open(os.path.join(self.at, 'totals', 'demo')) as f:
            self.assertEqual(f.read().strip(), '9000')

    # ---- the estimate ----

    def test_no_estimate_is_offered_from_the_first_few_samples(self):
        # Those land while their configure step is still running, where
        # the rate is nothing like the compile's.  Extrapolated, they
        # read "eta 34m45s" for a build that had fifty seconds left.
        rate = self.progress.Rate()
        rate.add(100.0, 0)
        rate.add(105.0, 2)
        self.assertEqual(rate.per_second(), 0.0)

    def test_the_clone_and_the_configure_are_not_counted_as_compiling(self):
        # Their clone takes half a minute and their configure step a
        # while after it, all of it producing no objects at all.  Left
        # in the window those samples halve the rate, which is how a
        # build a minute from finishing read "eta 25m58s".
        rate = self.progress.Rate()
        for step in range(8):                  # cloning: nothing yet
            rate.add(100.0 + step * 5, 0)
        for step in range(8):                  # compiling, 10 a second
            rate.add(140.0 + step * 5, step * 50)
        self.assertAlmostEqual(rate.per_second(), 10.0, places=5)

    def test_the_estimate_comes_from_the_recent_past(self):
        # A whole-run average would be held down by the configure step
        # for the rest of the build.
        rate = self.progress.Rate()
        for step in range(40):
            rate.add(100.0 + step * 5, step * 50)
        self.assertAlmostEqual(rate.per_second(), 10.0, places=5)

    def test_a_build_that_has_stopped_producing_gets_no_estimate(self):
        # Linking, or a stall.  Dividing by a rate of zero is the
        # obvious hazard; claiming it will never finish is the other.
        rate = self.progress.Rate()
        for step in range(10):
            rate.add(100.0 + step * 5, 500)
        self.assertEqual(rate.per_second(), 0.0)

    # ---- what it draws ----

    def test_the_bar_never_fills_before_their_verdict(self):
        # The count can legitimately overshoot a remembered total -- a
        # series that adds a driver builds more than the run before it
        # -- and a bar reading 100% while the build carries on says the
        # tool has lost track.
        self.remember(300)
        rc, out = self.a_run(self.objects(900))
        self.assertEqual(rc, 0)
        self.assertIn('pct=99', out)
        self.assertNotIn('pct=100', out)

    def test_the_bar_advances_within_a_character(self):
        # Eighth-blocks at 28 columns give 224 positions rather than 28,
        # which is the difference between looking stalled during a long
        # phase and visibly creeping.
        width = 28
        seen = set()
        for permille in range(0, 1000):
            seen.add(self.progress.bar(permille / 1000.0, width, True))
        self.assertGreater(len(seen), width * 4)
        # And it stays one bar wide however full it is, or the line
        # would jump about as it fills.
        for fraction in (0.0, 0.015, 0.5, 0.999, 1.0):
            self.assertEqual(
                len(self.progress.bar(fraction, width, True)), width)

    def test_a_terminal_that_cannot_draw_blocks_gets_plain_ones(self):
        plain = self.progress.bar(0.5, 28, False)
        self.assertEqual(len(plain), 28)
        self.assertEqual(set(plain), set('= '))

    # ---- drawing alongside work this shell is doing itself ----

    def watch_only(self, prepare, seconds=7):
        """What openEuler's build does: start the bar, work, stop it."""
        os.makedirs(os.path.join(self.at, 'tree'), exist_ok=True)
        script = (
            '. "%s/lib/progress.sh"\n'
            'PROGRESS_TOTALS="%s/totals"\n'
            'progress_watch "%s/tree" "%s/log" demo\n'
            '%s'
            'sleep %d\n'
            'progress_unwatch 0\n'
            % (PROJECT_ROOT, self.at, self.at, self.at, prepare, seconds))
        done = subprocess.run(['bash', '-c', script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return done.returncode, done.stdout.decode('utf-8', 'replace')

    def test_a_build_this_shell_runs_itself_still_gets_a_bar(self):
        # openEuler's build is a shell function their runner sources,
        # not a script it can run, so there is no child to put the bar
        # in front of.
        self.remember(400)
        rc, out = self.watch_only(
            'for i in $(seq 1 200); do : > "%s/tree/o$i.o"; done\n'
            % self.at)
        self.assertEqual(rc, 0)
        self.assertIn('pct=50', out)

    def test_stopping_the_bar_remembers_what_was_built(self):
        rc, out = self.watch_only(
            'for i in $(seq 1 250); do : > "%s/tree/o$i.o"; done\n'
            % self.at, seconds=1)
        self.assertEqual(rc, 0, out)
        with open(os.path.join(self.at, 'totals', 'demo')) as f:
            self.assertEqual(f.read().strip(), '250')

    def test_the_bar_does_not_outlive_the_build_it_was_drawing(self):
        # It is a background process, so a missed kill would leave it
        # walking a kernel tree every five seconds until the machine
        # was rebooted.
        os.makedirs(os.path.join(self.at, 'tree'))
        before = subprocess.run(['pgrep', '-fc', 'progress.py'],
                                stdout=subprocess.PIPE).stdout.strip()
        self.watch_only('', seconds=1)
        after = subprocess.run(['pgrep', '-fc', 'progress.py'],
                               stdout=subprocess.PIPE).stdout.strip()
        self.assertEqual(before, after)

    # ---- openEuler's markers, which are not Anolis's ----

    def test_openeulers_stars_become_the_label(self):
        # Their log_info comes from openeuler-jenkins and wraps every
        # step in them.  Their build sends make's stdout to /dev/null
        # and its stderr to a file, so these lines are the only thing
        # that reaches the console for the whole of a compile.
        self.assertEqual(
            self.a_phase('[2026-10-06 11:20:00] [ INFO ] ***** Start to '
                         'download kernel of openeuler *****\n'),
            'Start to download kernel of openeuler')

    def test_a_bare_row_of_stars_is_not_a_label(self):
        # Their scripts print those as separators.
        self.assertEqual(self.a_phase('===> Clone kernel repository...\n'
                                      '*****\n'
                                      '**********\n'),
                         'Clone kernel repository')

    def test_both_runners_draw_the_same_bar(self):
        # The two call it differently because their gates are reached
        # differently, but neither keeps a copy of how it is started.
        for distro in ('anolis', 'euler'):
            source = read_file(distro, 'test.sh')
            self.assertIn('lib/progress.sh', source, distro)
            self.assertIn('PROGRESS_TOTALS', source, distro)
        self.assertIn('with_progress', read_file('anolis', 'test.sh'))
        self.assertIn('progress_watch', read_file('euler', 'test.sh'))
        self.assertIn('progress_unwatch', read_file('euler', 'test.sh'))

    def test_openeulers_build_status_survives_the_bar(self):
        # Their verdict is read from PIPESTATUS, and the bar is started
        # and stopped around it -- so the status has to be taken before
        # progress_unwatch runs anything of its own.
        source = read_file('euler', 'test.sh')
        build = source[source.index('run_oe_build() {'):]
        build = build[:build.index('\n}\n')]
        self.assertLess(build.index('PIPESTATUS'),
                        build.index('progress_unwatch'))

    # ---- and what the web interface makes of it ----

    def test_the_web_interface_reads_the_percentage(self):
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'web'))
        self.addCleanup(sys.path.remove, os.path.join(PROJECT_ROOT, 'web'))
        from prci.jobs import _PROGRESS_RE
        found = _PROGRESS_RE.match(
            '[prci-progress] pct=42 done=12431 total=29000 elapsed=900 '
            'phase=Build Kernel with allyesconfig')
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1), '42')
        self.assertEqual(found.group(2), '12431')
        self.assertEqual(found.group(5), 'Build Kernel with allyesconfig')
        # The first run of a case reports no percentage, and that has to
        # parse too rather than being mistaken for a build log line.
        self.assertIsNotNone(_PROGRESS_RE.match(
            '[prci-progress] pct=- done=0 total=0 elapsed=3 phase=Cloning'))

    def test_the_web_interface_shows_what_a_build_is_doing(self):
        # The data arrived and nothing drew it: a running allyesconfig
        # showed an indeterminate animation while the backend held the
        # phase and twenty thousand objects.
        page = read_file('web', 'templates', 'index.html')
        self.assertIn('buildStage', page)
        self.assertIn('build_phase', page)
        self.assertIn('build_objects', page)

    def test_a_single_test_is_not_announced_as_a_patch(self):
        # total_steps is 1 for one test and no step number is ever
        # reported for it, so the counter read "Patch undefined of 1".
        page = read_file('web', 'templates', 'index.html')
        counter = page[page.index('stageCounter()'):]
        counter = counter[:counter.index('},')]
        self.assertIn('!j.step', counter)

    def test_what_the_bar_writes_is_not_shown_as_the_build_log(self):
        # last_line is put in front of the user; "pct=42 done=12431" in
        # that spot would replace what the build is actually doing.
        source = read_file('web', 'prci', 'jobs.py')
        observe = source[source.index('def _observe('):]
        observe = observe[:observe.index('\n    def ')]
        self.assertIn('_PROGRESS_RE', observe)
        self.assertLess(observe.index('_PROGRESS_RE'),
                        observe.index("job['last_line']"))


class TestCountingWarnings(unittest.TestCase):
    """What a build's warnings amount to, for the distro that ignores them.

    A 5.10 tree under gcc 12.3 raises hundreds of warnings that predate
    any series: -Wdangling-pointer and -Warray-compare did not exist
    when the code was written.  openEuler's gate fails on any of them,
    so there they are filtered before their script reads the file.
    Anolis never looks, so there nothing is filtered and a count is
    appended instead -- read as a list, the one warning that matters is
    buried in the ones that never will.
    """

    LOG = (
        "arch/x86/kvm/mmu/mmu.c:4321:9: warning: unused variable 'x' "
        "[-Wunused-variable]\n"
        "drivers/pci/rom.c:107:13: warning: comparison of distinct "
        "pointer types lacks a cast\n"
        "arch/x86/boot/bioscall.S:35: Warning: found `movsd'\n"
        "samples/ftrace/ftrace-direct-multi.o: warning: objtool: "
        "my_tramp()+0x10: 'naked' return found in RETHUNK build\n"
    )

    def summarised(self, text, touched):
        """The lines warnings_summarise appends to a log."""
        log = tempfile.NamedTemporaryFile('w', delete=False, suffix='.log')
        log.write(text)
        log.close()
        self.addCleanup(os.unlink, log.name)
        script = (
            '. "%(root)s/lib/warnings.sh"\n'
            'git() { case "$*" in *rev-parse*) echo deadbeef ;;'
            '                     *diff*) printf "%%s\\n" %(touched)s ;;'
            ' esac; }\n'
            'warnings_summarise %(log)s'
            % {'root': PROJECT_ROOT, 'log': log.name,
               'touched': ' '.join("'%s'" % t for t in touched) or "''"})
        subprocess.run(['bash', '-c', script],
                       env=dict(os.environ, NUM_PATCHES='1'),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with open(log.name) as f:
            return [l for l in f.read().splitlines()
                    if l.startswith('[prci]')]

    def test_their_own_output_is_left_exactly_as_they_wrote_it(self):
        # Their verdict is read back out of this file.  The count goes
        # after it, never into it.
        log = tempfile.NamedTemporaryFile('w', delete=False, suffix='.log')
        log.write(self.LOG)
        log.close()
        self.addCleanup(os.unlink, log.name)
        subprocess.run(
            ['bash', '-c',
             '. "%s/lib/warnings.sh"\n'
             'git() { case "$*" in *rev-parse*) echo deadbeef ;;'
             '                     *diff*) echo fs/foo.c ;; esac; }\n'
             'warnings_summarise %s' % (PROJECT_ROOT, log.name)],
            env=dict(os.environ, NUM_PATCHES='1'),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with open(log.name) as f:
            kept = f.read()
        self.assertTrue(kept.startswith(self.LOG), 'their output was edited')

    def test_a_warning_the_series_did_not_cause_is_counted_not_blamed(self):
        said = '\n'.join(self.summarised(self.LOG, ['fs/foo.c']))
        self.assertIn('none of them in a file this series', said)
        self.assertNotIn('rom.c', said, 'a stranger\'s warning was listed')

    def test_a_warning_the_series_did_cause_is_named(self):
        said = '\n'.join(
            self.summarised(self.LOG, ['arch/x86/kvm/mmu/mmu.c']))
        self.assertIn('1 of them in a file this series', said)
        self.assertIn("mmu.c:4321:9: warning: unused variable 'x'", said)
        # And what it would mean elsewhere, since one distro fails on it.
        self.assertIn("openEuler's CI fails a build for any warning", said)

    def test_both_spellings_of_a_warning_are_counted(self):
        # gcc writes "file:line:col: warning:"; the assembler writes
        # "file:line: Warning:" with no column and a capital; objtool
        # reports against the object file.  All four lines above count.
        said = '\n'.join(self.summarised(self.LOG, ['fs/foo.c']))
        self.assertIn('4 compiler warnings', said)

    def test_an_error_is_counted_as_an_error(self):
        # build_perf is the one case that compiles with -Werror, so
        # there every diagnostic arrives as an error.  Whose files they
        # are in is the first thing worth knowing about a failure.
        said = '\n'.join(self.summarised(
            "util/scripting-engines/trace-event-python.c:1642:9: error: "
            "'PySys_SetArgv' is deprecated [-Werror=deprecated-declarations]"
            "\n", ['fs/foo.c']))
        self.assertIn('1 compiler errors', said)
        self.assertNotIn('warnings and', said)

    def test_a_diagnostic_outside_the_tree_is_placed_outside_it(self):
        # Nine of build_perf's eleven errors are in /usr/lib64/perl5,
        # which no patch to the kernel can do anything about.  Saying
        # only that they are "in files no series has been near" buries
        # the part that answers the question.
        said = '\n'.join(self.summarised(
            "/usr/lib64/perl5/CORE/handy.h:125:23: error: cast from "
            "function call [-Werror=bad-function-cast]\n"
            "tests/bpf.c:36:17: error: argument 2 null where non-null "
            "expected [-Werror=nonnull]\n", ['fs/foo.c']))
        self.assertIn('1 of them are not in the kernel tree at all', said)
        self.assertIn('2 compiler errors', said)

    def test_werror_is_read_from_their_own_words(self):
        # Only perf builds this way, and it says so itself, so there is
        # nothing to know about perf here.
        werror = ("util/pmu.c:1:1: error: nope [-Werror=unused]\n"
                  "cc1: all warnings being treated as errors\n")
        self.assertIn('treats warnings as errors',
                      '\n'.join(self.summarised(werror, ['fs/foo.c'])))
        self.assertNotIn('treats warnings as errors',
                         '\n'.join(self.summarised(self.LOG, ['fs/foo.c'])))

    def test_nothing_is_claimed_when_the_series_cannot_be_determined(self):
        said = '\n'.join(self.summarised(self.LOG, []))
        self.assertIn('cannot', said)
        self.assertNotIn('none of them in a file', said)

    def test_a_build_with_nothing_to_say_says_nothing(self):
        self.assertEqual(
            self.summarised('build_allno_config: pass\n', ['fs/foo.c']), [])

    def test_both_runners_share_one_implementation(self):
        # It was openEuler's alone, which is why Anolis's logs had no
        # answer for the same warnings on the same host.
        for distro in ('euler', 'anolis'):
            self.assertIn('lib/warnings.sh',
                          read_file(distro, 'test.sh'),
                          '%s does not load the shared one' % distro)
        hulk = read_file('euler', 'oe_hulk.sh')
        self.assertIn('warnings_keep_only_ours', hulk)
        self.assertNotIn('_HULK_ONLY_OURS', hulk, 'the old copy is still here')

    def test_anolis_counts_after_every_build_case(self):
        # One call in their shared runner rather than per case, so a
        # case added to their suite is covered by having been added.
        source = read_file('anolis', 'test.sh')
        runner = source[source.index('run_their_build_case()'):]
        runner = runner[:runner.index('\n}')]
        self.assertIn('warnings_summarise', runner)
        self.assertLess(runner.index('warnings_summarise'),
                        runner.index('case ${rc} in'),
                        'the count lands after the verdict is reported')


class TestCopyingALog(unittest.TestCase):
    """Reading a failure here usually ends in pasting it somewhere else."""

    def setUp(self):
        self.page = read_file('web', 'templates', 'index.html')
        body = self.page[self.page.index('async copyLog()'):]
        self.copy = body[:body.index('\n    scrollLog()')]

    def test_copy_sits_beside_download(self):
        actions = self.page[self.page.index('<div class="log-actions">'):]
        actions = actions[:actions.index('</div>')]
        self.assertIn('copyLog()', actions)
        self.assertLess(actions.index('copyLog()'),
                        actions.index('logDownloadUrl'),
                        'Copy is not beside Download')

    def test_it_copies_the_whole_log_and_not_the_part_on_screen(self):
        # The first read of a long log is tailed, so the text the viewer
        # holds is short of the file; copying that would quietly hand
        # over less than the button next to it does.
        self.assertIn('logDownloadUrl', self.copy)
        self.assertNotIn('this.logText', self.copy)
        self.assertGreater(jobs.INITIAL_TAIL_BYTES, 0,
                           'nothing tails the first read any more')

    def test_it_still_copies_where_there_is_no_secure_context(self):
        # Served over plain HTTP to anyone who is not on this host, and
        # navigator.clipboard does not exist there.
        clipboard = self.page[self.page.index('async toClipboard('):]
        clipboard = clipboard[:clipboard.index('\n    scrollLog()')]
        self.assertIn('window.isSecureContext', clipboard)
        self.assertIn("document.execCommand('copy')", clipboard)
        self.assertIn('removeChild', clipboard,
                      'the textarea it copies through is left in the page')

    def test_a_copy_that_did_not_happen_says_so(self):
        self.assertIn("toast('bad'", self.copy)
        self.assertNotIn("toast('ok'", self.copy,
                         'a toast for something the button already shows')
        self.assertIn('logCopied = true', self.copy)


class TestExplainingAMissingPackage(unittest.TestCase):
    """Why one of their BuildRequires cannot be satisfied here.

    Their kernel.spec asks for a name Red Hat publishes and Anolis does
    not, with no Provides linking the two, so yum says "Some packages
    could not be found" and names nothing.  Four unexplained lines at
    the top of every case log, about something that does not matter.
    """

    def explain(self, argv, rpm='', repoquery='', installed=''):
        """pkg_explain's output, with the host's tools stubbed.

        Their real answers are this host's, and this host is not the one
        the next person runs these on.
        """
        script = (
            '. "%(root)s/lib/pkgexplain.sh"\n'
            'rpm() { case "$*" in'
            '  *--whatprovides*) printf "%%s" %(rpm)s ;;'
            '  *-qa*) printf "%%s" %(installed)s ;;'
            ' esac; }\n'
            'dnf() { printf "%%s" %(repoquery)s; }\n'
            'pkg_explain %(argv)s\n'
            % {'root': PROJECT_ROOT, 'argv': argv,
               'rpm': "'%s'" % rpm, 'repoquery': "'%s'" % repoquery,
               'installed': "'%s'" % installed})
        done = subprocess.run(['bash', '-c', script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return done.stdout.decode()

    def test_a_name_that_resolves_is_not_mentioned(self):
        # Silence is the usual answer, and the whole point: a command
        # can fail for its own reasons and inventing a dependency
        # problem to explain a network timeout is worse than nothing.
        said = self.explain('install -y gcc bash')
        self.assertEqual(said.strip(), '')

    def wanted(self, argv):
        """The names a command was asking for, as _pkg_wanted sees them."""
        done = subprocess.run(
            ['bash', '-c', '. "%s/lib/pkgexplain.sh"\n_pkg_wanted %s'
             % (PROJECT_ROOT, argv)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return done.stdout.decode().split()

    def test_the_verb_is_not_taken_for_a_package(self):
        # "install" is not something to look up.  Recognised by
        # position, since the verbs are theirs to choose.
        self.assertEqual(self.wanted('install -y gcc bash'),
                         ['gcc', 'bash'])
        self.assertEqual(self.wanted('remove --setopt=x=1 tmux'), ['tmux'])

    def test_a_name_no_repository_offers_is_explained_away(self):
        said = self.explain('install -y vendor-rpm-config',
                            rpm='no package provides vendor-rpm-config\n',
                            installed='system-rpm-config\nbash\n')
        self.assertIn('vendor-rpm-config is not a package on this host',
                      said)
        self.assertIn('system-rpm-config is installed and named like it',
                      said)
        self.assertIn('nothing is actually missing', said)

    def test_nothing_is_suggested_when_nothing_matches(self):
        said = self.explain('install -y something-else',
                            rpm='no package provides something-else\n',
                            installed='bash\ncoreutils\n')
        self.assertIn('cannot be met by name here', said)
        self.assertNotIn('named like it', said,
                         'a lookalike was invented')

    def test_a_package_that_was_merely_not_reached_says_so(self):
        # Available and absent is a different story, and not one to
        # explain away: their command should have installed it.
        said = self.explain('install -y tmux',
                            rpm='no package provides tmux\n',
                            repoquery='tmux-3.3a-4.an23.x86_64\n')
        self.assertIn('is in a repository but was not installed', said)
        self.assertNotIn('nothing is actually missing', said)

    def test_no_package_name_is_written_down_anywhere(self):
        # The whole requirement: this has to be right on a host and a
        # tree it was not written for.
        with open(os.path.join(PROJECT_ROOT, 'lib', 'pkgexplain.sh')) as f:
            source = f.read()
        for name in ('redhat-rpm-config', 'system-rpm-config'):
            self.assertNotIn(name, source, 'a package name was hardcoded')
        # Their spec is asked what it wants, by rpm's own parser.
        self.assertIn('rpmspec -q --buildrequires', source)

    def test_it_is_asked_only_after_their_command_fails(self):
        with open(os.path.join(PROJECT_ROOT, 'anolis', 'an_tone.sh')) as f:
            shim = f.read()
        self.assertIn('pkg_explain', shim)
        guard = shim[:shim.index('pkg_explain')]
        self.assertIn('rc}" -ne 0', guard,
                      'it runs whether or not their command failed')


class TestHostFitness(unittest.TestCase):
    """Whether this host can test the tree, asked before anything runs.

    A 5.10 tree cannot be measured by version numbers: it states the
    tools it needs as minimums and Anolis 23 clears every one of them,
    which is exactly why their own CI never notices that perf will not
    compile here.  So the verdict is a probe and the versions are only
    the explanation -- and both come out of their tree, not out of a
    table of ours.
    """

    def tree(self, warnings='EXTRA_WARNINGS := -Wswitch-default\n',
             doc=None):
        """A kernel tree with as much of their layout as this needs."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        with open(os.path.join(root, 'Makefile'), 'w') as f:
            f.write('VERSION = 5\nPATCHLEVEL = 10\nSUBLEVEL = 134\n')
        if warnings is not None:
            os.makedirs(os.path.join(root, 'tools', 'scripts'))
            with open(os.path.join(root, 'tools', 'scripts',
                                   'Makefile.include'), 'w') as f:
                f.write(warnings)
        if doc is not None:
            os.makedirs(os.path.join(root, 'Documentation', 'process'))
            with open(os.path.join(root, 'Documentation', 'process',
                                   'changes.rst'), 'w') as f:
                f.write(doc)
        return root

    def report(self, tree, gcc='return 1'):
        """hostcheck_report's output and verdict, with gcc stubbed.

        The probe is the one thing a test cannot let run for real: on
        this host it fails, and on whatever host runs these tests next
        it might not.
        """
        script = (
            '. "%s/lib/hostcheck.sh"\n'
            'gcc() { if [ "$1" = -dumpfullversion ]; then echo 12.3.0;'
            '        else echo "/usr/include/x.h:1:1: error: switch'
            ' missing default case [-Werror=switch-default]" >&2; %s; fi; }\n'
            'hostcheck_report "%s"\n' % (PROJECT_ROOT, gcc, tree))
        done = subprocess.run(['bash', '-c', script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL)
        return done.stdout.decode(), done.returncode

    def test_the_verdict_is_a_probe_and_not_a_version_comparison(self):
        # The point of the whole exercise: a host that compiles what
        # their build compiles is fit, whatever its versions say.
        said, rc = self.report(self.tree(), gcc='return 0')
        self.assertEqual(rc, 0, 'a host that can compile was refused')
        self.assertEqual(said.strip(), '',
                         'a fit host was warned about anyway')

    def test_a_host_that_cannot_compile_their_build_is_refused(self):
        said, rc = self.report(self.tree())
        self.assertEqual(rc, 1)
        self.assertIn('5.10.134', said, 'the tree it is unfit for is unnamed')
        self.assertIn('error: switch missing default case', said,
                      'the compiler was not quoted on why')

    def test_the_warnings_come_from_their_file_not_from_us(self):
        # Their own flag list, read from their own makefile: a list of
        # ours would go stale the moment they changed theirs.
        with open(os.path.join(PROJECT_ROOT, 'lib', 'hostcheck.sh')) as f:
            source = f.read()
        self.assertIn('tools/scripts/Makefile.include', source)
        self.assertIn('EXTRA_WARNINGS', source)
        self.assertIn('ExtUtils::Embed', source,
                      'the include paths are not found their way')

    def test_a_probe_that_could_not_run_refuses_nothing(self):
        # No Makefile.include, so there is nothing to probe with.  An
        # unanswered question is not evidence of a bad host.
        said, rc = self.report(self.tree(warnings=None))
        self.assertEqual(rc, 0, 'the suite was refused on no evidence')
        self.assertEqual(said.strip(), '')

    def test_the_table_is_read_from_their_documentation(self):
        doc = (
            '====================== ===============  ==================\n'
            '        Program        Minimal version   Command to check\n'
            '====================== ===============  ==================\n'
            'GNU C                  4.9              gcc --version\n'
            '====================== ===============  ==================\n')
        said, rc = self.report(self.tree(doc=doc))
        self.assertEqual(rc, 1)
        self.assertIn('GNU C', said, 'their table was not read')
        self.assertIn('4.9', said, 'what the tree asks for is not shown')
        self.assertIn('12.3.0', said, 'what the host has is not shown')

    def test_a_tool_the_tree_never_mentions_is_said_to_be_unmentioned(self):
        # perl is the tool that breaks, and their table does not list
        # it at all.  A requirement never stated is a requirement
        # nothing can check, which is the most useful line in the
        # warning and must not be left blank.
        said, rc = self.report(self.tree(doc='no table here\n'))
        self.assertEqual(rc, 1)
        self.assertIn('not stated by the tree', said)

    def test_the_answer_is_separated_from_the_evidence(self):
        # The page shows the reason and keeps the table behind "view
        # more", and splits them on this rather than on our prose.
        said, _ = self.report(self.tree())
        self.assertIn('\n---\n', said)
        answer = hostcheck._split(said, False)
        self.assertTrue(answer['headline'])
        # The sentence is one line and carries no compiler output; the
        # compiler output is what sits behind it.
        self.assertEqual(answer['summary'].count('\n'), 0)
        self.assertNotIn('error:', answer['summary'])
        self.assertIn('error: switch missing default', answer['reason'])
        self.assertNotIn('---', answer['note'])

    def test_an_unanswerable_question_leaves_the_host_fit(self):
        self.assertTrue(hostcheck.check(PROJECT_ROOT, kernel=None)['ok'])
        self.assertTrue(hostcheck.check(PROJECT_ROOT,
                                        kernel='/nonexistent')['ok'])

    def test_nothing_is_run_while_the_host_is_unfit(self):
        with open(os.path.join(PROJECT_ROOT, 'anolis', 'test.sh')) as f:
            script = f.read()
        self.assertIn('hostcheck_report', script,
                      'the suite never asks whether it can run')
        self.assertLess(script.index('hostcheck_report'),
                        script.index('Running specific test'),
                        'the host is checked after tests have started')

    def test_the_interface_refuses_as_well_as_greys_out(self):
        # A grey button is a courtesy; the refusal has to be the
        # server's, or a stale page starts a run that cannot pass.
        with open(os.path.join(PROJECT_ROOT, 'web', 'server.py')) as f:
            server = f.read()
        self.assertIn('host_unfit', server)
        self.assertLess(server.index('host = host_fitness(distro)'),
                        server.index('ready, why = series_readiness(distro)'),
                        'readiness is answered before fitness')
        with open(os.path.join(PROJECT_ROOT, 'web', 'templates',
                               'index.html')) as f:
            page = f.read()
        # Everything that starts work on the tree: the three fetches, the
        # prepare, the whole run and a single test.  Configuration is
        # deliberately not among them, since repointing the kernel path
        # is the way out of an unfit host.
        self.assertEqual(page.count('|| !hostOk'), 5,
                         'a button that starts work is still live')
        self.assertIn('View more', page, 'the evidence cannot be opened')

    def test_the_compiler_output_waits_until_it_is_asked_for(self):
        # A person who has just been told nothing will run needs the
        # sentence, not fourteen errors from a header they have never
        # heard of.
        with open(os.path.join(PROJECT_ROOT, 'web', 'templates',
                               'index.html')) as f:
            page = f.read()
        panel = page[page.index('<div class="unfit"'):]
        panel = panel[:panel.index('<div class="wrap"')]
        shown = panel[:panel.index('unfit-more')]
        self.assertIn('{{ fitness.summary }}', shown,
                      'the one sentence that matters is not shown')
        # Printed, not merely mentioned: the toggle tests the same value
        # to decide whether it has anything to offer.
        self.assertNotIn('{{ fitness.reason }}', shown,
                         'compiler output is shown before it is asked for')
        self.assertIn('{{ fitness.reason }}', panel,
                      'it cannot be reached at all')


if __name__ == '__main__':
    unittest.main(verbosity=2)
