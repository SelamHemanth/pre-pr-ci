# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/registry.py
# Canonical definitions of distributions, tests and configuration fields
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Single source of truth for what each distribution can do.

Every ``name`` below must match a ``case`` label in ``<distro>/test.sh``, and
every ``log`` must match the file that test actually writes.  Both the UI and
the job runner resolve those from this one table, so a name that only exists
here is a test the user can start but never see the output of.
"""

import os
import re
from collections import namedtuple

#: A single test case.
#:
#: name        -- argument accepted by ``make <distro>-test=<name>``
#: description -- one-line summary shown in the UI
#: log         -- file under ``logs/`` that the test writes
#: config_key  -- ``TEST_*`` flag in ``<distro>/.configure`` that enables it
#: default_on  -- whether a fresh configuration enables it
#:
#: Almost everything is on by default.  The exception is the cross builds:
#: a full allmodconfig for one architecture is an hour of somebody's
#: machine, openEuler runs six of them in parallel across a fleet and we
#: have one host, so they are offered rather than imposed.
#:
#: name        -- the id, which is also the make target; never shown as
#:                a heading, because "oe_checkkabi" tells a reader less
#:                than "Kernel ABI" does and is not what they are picking
#: title       -- what it is called on screen
#: description -- what it actually checks, in a sentence
TestDef = namedtuple(
    'TestDef', 'name title description log config_key default_on')
TestDef.__new__.__defaults__ = (True,)

#: Field in the configuration form.
#:
#: secret -- true for values that must never be echoed back to the browser
FieldDef = namedtuple(
    'FieldDef',
    'name label type required default options hint secret',
)
FieldDef.__new__.__defaults__ = (True, None, None, None, False)

DISTROS = {
    'anolis': 'OpenAnolis',
    'euler': 'openEuler',
}

#: Their architecture matrix, read rather than copied.  An architecture
#: listed here that we leave out is a failure their gate finds and ours
#: never looks for; one we invent is a test with no counterpart in the
#: gate this exists to predict.
_CHECK_BUILD_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'euler', 'hulk_robot_test', 'openEuler', 'conf', 'check_build.yaml')

#: Only where their CI's name for it differs from the key in that file.
_THEIR_NAME = {'ppc': 'PPC'}

#: The two openEuler ships, and so the two whose ABI it promises: their
#: checkkabi.sh compares the ABI and the shipping config on these, and
#: their checkbuild.sh only compiles the rest.
_KABI_ARCHES = ('x86_64', 'aarch64')

#: Used only when the hulk_robot_test submodule is not checked out, in
#: which case every build test skips anyway -- but dropping them from
#: the table entirely would take their TEST_* keys out of .configure
#: and silently forget which ones the user had turned on.
_ARCHES_IF_UNREADABLE = ('aarch64', 'arm', 'x86_64', 'ppc', 'ppc64',
                         'riscv64')

_MATRIX_LINE_RE = re.compile(r'^\s+([A-Za-z0-9_]+):\s*(true|false)\s*$')


def architectures_they_build():
    """Every architecture true on at least one branch of their matrix.

    loongarch is in their file and false on every branch in it, so it
    is a job openEuler's CI runs nowhere.  Offering it put a seventh
    build in a list their gate only ever shows six of, which reads as
    a check they skipped rather than one that does not exist.

    Kept branch-independent on purpose.  Which of these a given branch
    builds is decided per run, by oe_build.sh asking this same file,
    and an architecture that drops out there reports as skipped --
    which is true, and different from not existing.
    """
    order, built = [], set()
    try:
        with open(_CHECK_BUILD_YAML) as handle:
            for line in handle:
                found = _MATRIX_LINE_RE.match(line)
                if not found:
                    continue
                arch = found.group(1)
                if arch not in order:
                    order.append(arch)
                if found.group(2) == 'true':
                    built.add(arch)
    except OSError:
        return _ARCHES_IF_UNREADABLE
    if not built:
        return _ARCHES_IF_UNREADABLE
    return tuple(arch for arch in order if arch in built)


def _build_tests():
    """One test per architecture, named the way their CI names it.

    "Build for arm64" and "Build for powerpc" were ours, and reading a
    result against their job list meant translating every row back.
    """
    out = []
    for arch in architectures_they_build():
        if arch in _KABI_ARCHES:
            what = ('Full build, then compares the ABI and the shipping '
                    'config against openEuler')
        else:
            what = 'Cross-compiles the tree, as their check_build job does'
        out.append(TestDef(
            'oe_build_%s' % arch,
            _THEIR_NAME.get(arch, arch),
            what,
            'oe_build_%s.log' % arch,
            'TEST_OE_BUILD_%s' % arch.upper(),
            arch == 'x86_64'))
    return tuple(out)

TESTS = {
    'anolis': (
        TestDef('check_dependency', 'Missing dependencies',
                'Looks for upstream commits your patches need that are '
                'not in the series',
                'check_dependency.log', 'TEST_CHECK_DEPENDENCY'),
        TestDef('check_kconfig', 'Kconfig',
                'Checks that new config symbols are declared and reachable',
                'check_Kconfig.log', 'TEST_CHECK_KCONFIG'),
        TestDef('build_allyes_config', 'Build, everything on',
                'Compiles with allyesconfig, which reaches code no normal '
                'config builds',
                'build_allyes_config.log', 'TEST_BUILD_ALLYES'),
        TestDef('build_allno_config', 'Build, everything off',
                'Compiles with allnoconfig, which catches code that only '
                'builds because something else was enabled',
                'build_allno_config.log', 'TEST_BUILD_ALLNO'),
        TestDef('build_anolis_defconfig', 'Build, shipping config',
                'Compiles with anolis_defconfig, the configuration '
                'OpenAnolis actually ships',
                'build_anolis_defconfig.log', 'TEST_BUILD_DEFCONFIG'),
        TestDef('build_anolis_debug', 'Build, debug config',
                'Compiles with anolis-debug_defconfig, which turns on the '
                'debugging checks',
                'build_anolis_debug_defconfig.log', 'TEST_BUILD_DEBUG'),
        TestDef('anck_rpm_build', 'Kernel packages',
                'Builds the ANCK RPMs, the form the kernel is delivered in',
                'anck_rpm_build.log', 'TEST_RPM_BUILD'),
        TestDef('check_kapi', 'Kernel ABI',
                'Checks the series does not break the ABI that modules '
                'built against this kernel rely on',
                'kapi_test.log', 'TEST_CHECK_KAPI'),
        TestDef('boot_kernel_rpm', 'Boot test',
                'Installs the built kernel in a VM and checks it comes up',
                'boot_kernel_rpm.log', 'TEST_BOOT_KERNEL'),
        TestDef('build_perf', 'Build perf',
                'Builds the perf tool, which breaks on kernel header '
                'changes that the kernel build itself does not notice',
                'build_perf.log', 'TEST_BUILD_PERF'),
    ),
    # The six oe_ tests are openEuler's own gate, run from their code in the
    # hulk_robot_test submodule rather than reimplemented.  Our own
    # checkpatch, commit-format and dependency tests used to sit here and
    # were removed: a second opinion that drifts from the gate deciding
    # whether a patch is accepted is worse than no opinion at all.
    'euler': (
        TestDef('oe_checkpatch', 'Coding style',
                'Runs checkpatch.pl the way openEuler does, skipping '
                'backports that match upstream exactly',
                'oe_checkpatch.log', 'TEST_OE_CHECKPATCH'),
        TestDef('oe_checkformat', 'Commit message',
                'Checks the inclusion header, category, bugzilla link and '
                'sign-off on every commit',
                'oe_checkformat.log', 'TEST_OE_CHECKFORMAT'),
        TestDef('oe_checkdepend', 'Missing fixes',
                'Looks for upstream commits that fix yours and are not in '
                'the series',
                'oe_checkdepend.log', 'TEST_OE_CHECKDEPEND'),
        TestDef('oe_checkkabi', 'Kernel ABI',
                'Flags changes to the structures and symbols that modules '
                'built against this kernel rely on',
                'oe_checkkabi.log', 'TEST_OE_CHECKKABI'),
        TestDef('oe_checkconflict', 'Backport differences',
                'Checks that every commit differing from upstream says '
                'which files differ and why',
                'oe_checkconflict.log', 'TEST_OE_CHECKCONFLICT'),
        TestDef('oe_checkbinary', 'Binary files',
                'Rejects binary files added or changed by the series',
                'oe_checkbinary.log', 'TEST_OE_CHECKBINARY'),

        # One test per architecture, because that is one job per
        # architecture in their CI, and because a local run wants to say
        # "powerpc only" without editing anything.  The list comes from
        # their own conf/check_build.yaml; see architectures_they_build.
    ) + _build_tests(),
}

_COMMON_FIELDS = {
    'general': (
        FieldDef('LINUX_SRC_PATH', 'Linux source path', 'text',
                 hint='Absolute path to the kernel git tree under test'),
        FieldDef('SIGNER_NAME', 'Signed-off-by name', 'text'),
        FieldDef('SIGNER_EMAIL', 'Signed-off-by email', 'email'),
    ),
    'build': (
        FieldDef('BUILD_THREADS', 'Build threads', 'number', default=256,
                 hint='Parallel make jobs'),
    ),
    'vm': (
        FieldDef('VM_IP', 'VM IP address', 'text',
                 hint='Target used by the boot test'),
        FieldDef('VM_ROOT_PWD', 'VM root password', 'password', secret=True),
    ),
    'host': (
        FieldDef('HOST_USER_PWD', 'Host sudo password', 'password',
                 secret=True, hint='Needed to install build dependencies'),
    ),
}

CONFIG_FIELDS = {
    'anolis': {
        'general': _COMMON_FIELDS['general'] + (
            FieldDef('ANBZ_ID', 'Anolis Bugzilla ID', 'text'),
            FieldDef('NUM_PATCHES', 'Number of patches', 'number', default=10),
        ),
        'build': _COMMON_FIELDS['build'],
        'vm': _COMMON_FIELDS['vm'],
        'host': _COMMON_FIELDS['host'],
    },
    # No 'vm' section: openEuler's CI never boots a kernel, so neither do we,
    # and with the boot test gone there is nothing to ask a VM address for.
    'euler': {
        'general': _COMMON_FIELDS['general'] + (
            FieldDef('BUGZILLA_ID', 'Bugzilla ID', 'text'),
            # No PATCH_CATEGORY.  It used to be asked once and stamped on
            # every patch in the series, which is wrong as soon as a series
            # mixes a fix with a cleanup.  oe_header.py reads it from each
            # commit instead; see decide_category there.
            FieldDef('NUM_PATCHES', 'Number of patches', 'number', default=5),
            # The branch the series is aimed at, which decides more than it
            # looks like it should: openEuler's conf/check_build.yaml keys
            # the architecture matrix by it, and the ABI whitelist for a
            # branch lives on a differently named branch of another repo.
            # It used to be readable only from the environment, with one
            # default in the checks and another in the builds.
            FieldDef('OE_TARGET_BRANCH', 'Target branch', 'select',
                     default='OLK-6.6',
                     options=('OLK-6.6', 'OLK-5.10', 'openEuler-1.0-LTS',
                              'openEuler-22.03-LTS', 'openEuler-25.03',
                              'master'),
                     hint='Decides which architectures are built and which '
                          'KABI whitelist applies'),
        ),
        'build': _COMMON_FIELDS['build'],
        'host': _COMMON_FIELDS['host'],
    },
}

#: Config keys whose values must stay on the server.
SECRET_KEYS = frozenset(
    f.name
    for distro in CONFIG_FIELDS.values()
    for section in distro.values()
    for f in section
    if f.secret
)


# /etc/os-release ID values, lowercased, mapped to our directory names.
# openEuler writes ID="openEuler" with a capital E, so always fold the case
# before looking it up here.
_OS_RELEASE_IDS = {
    'anolis': 'anolis',
    'openeuler': 'euler',
}


def detect_distro():
    """Which distro this host looks like, or None if we do not support it."""
    try:
        with open('/etc/os-release', 'r') as handle:
            for line in handle:
                if line.startswith('ID='):
                    ident = line.partition('=')[2].strip().strip('"\'')
                    return _OS_RELEASE_IDS.get(ident.lower())
    except OSError:
        pass
    return None


def is_distro(distro):
    return distro in DISTROS


def tests_for(distro):
    return TESTS.get(distro, ())


def find_test(distro, name):
    """Return the TestDef for ``name``, or None if this distro has no such test.

    Callers must treat None as "reject the request" -- the name reaches us from
    a URL and ends up in a make invocation.
    """
    for test in TESTS.get(distro, ()):
        if test.name == name:
            return test
    return None


def test_config_keys(distro):
    return tuple(t.config_key for t in tests_for(distro))


def default_flag(distro, config_key):
    """``'yes'`` or ``'no'`` for a test nobody has expressed an opinion on.

    An unknown key answers ``'yes'``: a flag left over from an older
    configuration should not silently disable something.
    """
    for test in tests_for(distro):
        if test.config_key == config_key:
            return 'yes' if test.default_on else 'no'
    return 'yes'


#: Order the form is presented in, and the heading for each group.
SECTION_LABELS = (
    ('general', 'General'),
    ('build', 'Build'),
    ('vm', 'Boot test VM'),
    ('host', 'Host'),
)


def fields_as_json(distro):
    """Render the form definition for the browser.

    A list, not a dict: Flask sorts JSON object keys, which presented the
    sections as Build, General, Host, VM regardless of the order here.
    """
    sections = CONFIG_FIELDS.get(distro, {})
    out = []
    for key, label in SECTION_LABELS:
        fields = sections.get(key)
        if not fields:
            continue
        out.append({
            'key': key,
            'label': label,
            'fields': [
                {
                    'name': f.name,
                    'label': f.label,
                    'type': f.type,
                    'required': f.required,
                    'default': f.default,
                    'options': list(f.options) if f.options else None,
                    'hint': f.hint,
                    # The flag tells the UI to render a password box and to
                    # leave it empty; the value itself never leaves the server.
                    'secret': f.secret,
                }
                for f in fields
            ],
        })
    return out


def all_fields(distro):
    for section in CONFIG_FIELDS.get(distro, {}).values():
        for field in section:
            yield field
