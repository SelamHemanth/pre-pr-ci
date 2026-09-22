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
TestDef = namedtuple('TestDef', 'name description log config_key')

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
    'euler': (
        TestDef('check_dependency', 'Check patch dependencies',
                'check_dependency.log', 'TEST_CHECK_DEPENDENCY'),
        TestDef('build_allmod', 'Build with allmodconfig',
                'build_allmod.log', 'TEST_BUILD_ALLMOD'),
        TestDef('check_kabi', 'Check KABI whitelist against Module.symvers',
                'check_kabi.log', 'TEST_CHECK_KABI'),
        TestDef('check_patch', 'Run checkpatch.pl validation',
                'check_patch.log', 'TEST_CHECK_PATCH'),
        TestDef('check_format', 'Validate commit message format',
                'check_format.log', 'TEST_CHECK_FORMAT'),
        TestDef('rpm_build', 'Build openEuler RPM packages',
                'rpm_build.log', 'TEST_RPM_BUILD'),
        TestDef('boot_kernel', 'Boot VM with built kernel',
                'boot_kernel.log', 'TEST_BOOT_KERNEL'),
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
    'euler': {
        'general': _COMMON_FIELDS['general'] + (
            FieldDef('BUGZILLA_ID', 'Bugzilla ID', 'text'),
            FieldDef('PATCH_CATEGORY', 'Patch category', 'select',
                     default='bugfix',
                     options=('feature', 'bugfix', 'performance', 'security')),
            FieldDef('NUM_PATCHES', 'Number of patches', 'number', default=5),
        ),
        'build': _COMMON_FIELDS['build'],
        'vm': _COMMON_FIELDS['vm'],
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
