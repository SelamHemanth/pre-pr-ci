# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - web/prci/system.py
# Host facts shown in the dashboard header
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#

"""Enough about the build host to answer "will this build even fit?".

Kernel builds fail late and confusingly when a filesystem fills up, so free
space on the kernel tree is surfaced next to the button that starts one.
"""

import os
import platform
import shutil


def snapshot(workspace_root, linux_src=None):
    info = {
        'hostname': platform.node(),
        'os': _os_pretty_name(),
        'kernel': platform.release(),
        'arch': platform.machine(),
        'cpus': os.cpu_count(),
        'load': _load(),
        'memory': _memory(),
        'disks': [],
    }

    seen = set()
    for label, path in (('workspace', workspace_root),
                        ('kernel tree', linux_src)):
        usage = _disk(path)
        if usage and usage['mount'] not in seen:
            seen.add(usage['mount'])
            usage['label'] = label
            info['disks'].append(usage)

    return info


def _os_pretty_name():
    try:
        with open('/etc/os-release', 'r') as handle:
            for line in handle:
                if line.startswith('PRETTY_NAME='):
                    return line.partition('=')[2].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def _load():
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        return None
    return {'1m': round(one, 2), '5m': round(five, 2), '15m': round(fifteen, 2)}


def _memory():
    wanted = {'MemTotal': 'total', 'MemAvailable': 'available'}
    out = {}
    try:
        with open('/proc/meminfo', 'r') as handle:
            for line in handle:
                key, _, rest = line.partition(':')
                if key in wanted:
                    out[wanted[key]] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return out or None


def _disk(path):
    if not path or not os.path.isdir(path):
        return None
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return {
        'path': path,
        'mount': _mount_point(path),
        'total': usage.total,
        'free': usage.free,
    }


def _mount_point(path):
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path
