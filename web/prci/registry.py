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
TestDef = namedtuple('TestDef', 'name description log config_key default_on')
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

TESTS = {
    'anolis': (
        TestDef('check_dependency', 'Check patch dependencies',
                'check_dependency.log', 'TEST_CHECK_DEPENDENCY'),
        TestDef('check_kconfig', 'Validate kernel configuration',
                'check_Kconfig.log', 'TEST_CHECK_KCONFIG'),
        TestDef('build_allyes_config', 'Build with allyesconfig',
                'build_allyes_config.log', 'TEST_BUILD_ALLYES'),
        TestDef('build_allno_config', 'Build with allnoconfig',
                'build_allno_config.log', 'TEST_BUILD_ALLNO'),
        TestDef('build_anolis_defconfig', 'Build with anolis_defconfig',
                'build_anolis_defconfig.log', 'TEST_BUILD_DEFCONFIG'),
        TestDef('build_anolis_debug', 'Build with anolis-debug_defconfig',
                'build_anolis_debug_defconfig.log', 'TEST_BUILD_DEBUG'),
        TestDef('anck_rpm_build', 'Build ANCK RPM packages',
                'anck_rpm_build.log', 'TEST_RPM_BUILD'),
        TestDef('check_kapi', 'Check kernel ABI compatibility',
                'kapi_test.log', 'TEST_CHECK_KAPI'),
        TestDef('boot_kernel_rpm', 'Boot VM with built kernel RPM',
                'boot_kernel_rpm.log', 'TEST_BOOT_KERNEL'),
        TestDef('build_perf', 'Build perf tool',
                'build_perf.log', 'TEST_BUILD_PERF'),
    ),
    # The six oe_ tests are openEuler's own gate, run from their code in the
    # hulk_robot_test submodule rather than reimplemented.  Our own
    # checkpatch, commit-format and dependency tests used to sit here and
    # were removed: a second opinion that drifts from the gate deciding
    # whether a patch is accepted is worse than no opinion at all.
    'euler': (
        TestDef('oe_checkpatch', 'openEuler checkpatch',
                'oe_checkpatch.log', 'TEST_OE_CHECKPATCH'),
        TestDef('oe_checkformat', 'openEuler commit message format',
                'oe_checkformat.log', 'TEST_OE_CHECKFORMAT'),
        TestDef('oe_checkdepend', 'openEuler upstream dependency closure',
                'oe_checkdepend.log', 'TEST_OE_CHECKDEPEND'),
        TestDef('oe_checkkabi', 'openEuler KABI keyword scan',
                'oe_checkkabi.log', 'TEST_OE_CHECKKABI'),
        TestDef('oe_checkconflict', 'openEuler backport conflict declaration',
                'oe_checkconflict.log', 'TEST_OE_CHECKCONFLICT'),
        TestDef('oe_checkbinary', 'openEuler binary file audit',
                'oe_checkbinary.log', 'TEST_OE_CHECKBINARY'),

        # One test per architecture, because that is one job per
        # architecture in their CI, and because a local run wants to say
        # "powerpc only" without editing anything.  Which of these their
        # gate would actually run depends on the target branch; each test
        # asks their conf/check_build.yaml and skips if the answer is no.
        #
        # x86_64 and aarch64 additionally compare the ABI and the
        # defconfig, exactly as their checkkabi.sh does -- those two
        # architectures are the ones openEuler ships, so they are the ones
        # whose ABI is promised.
        TestDef('oe_build_x86_64', 'openEuler build + KABI, x86_64',
                'oe_build_x86_64.log', 'TEST_OE_BUILD_X86_64'),
        TestDef('oe_build_aarch64', 'openEuler build + KABI, aarch64',
                'oe_build_aarch64.log', 'TEST_OE_BUILD_AARCH64', False),
        TestDef('oe_build_arm', 'openEuler cross build, arm',
                'oe_build_arm.log', 'TEST_OE_BUILD_ARM', False),
        TestDef('oe_build_ppc', 'openEuler cross build, powerpc',
                'oe_build_ppc.log', 'TEST_OE_BUILD_PPC', False),
        TestDef('oe_build_ppc64', 'openEuler cross build, powerpc64',
                'oe_build_ppc64.log', 'TEST_OE_BUILD_PPC64', False),
        TestDef('oe_build_riscv64', 'openEuler cross build, riscv64',
                'oe_build_riscv64.log', 'TEST_OE_BUILD_RISCV64', False),
        TestDef('oe_build_loongarch', 'openEuler cross build, loongarch',
                'oe_build_loongarch.log', 'TEST_OE_BUILD_LOONGARCH', False),
    ),
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
