#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/boot_test.sh
# Shared boot test: install a freshly built kernel RPM on a VM and reboot into it
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# Source this file; it defines functions and runs nothing on its own.
#
# Expects lib/vm.sh to be sourced too, and VM_IP, VM_ROOT_PWD and
# HOST_USER_PWD to be set.  It reports no verdict of its own: the
# distro's own suite does that, by looking at the machine this leaves
# running.
#
#   boot_install_and_reboot <rpms_dir> <boot_log>
#

BOOT_WAIT_SECONDS="${BOOT_WAIT_SECONDS:-300}"

_boot_log() {
	echo "  → $*" >> "${_BOOT_LOG}"
}

# Ask rpm what the package calls itself rather than parsing its file name.
# For a kernel package "<version>-<release>.<arch>" is exactly what uname -r
# reports once it is running, which is what the test compares against.
_boot_kernel_release() {
	local rpm_file="$1"

	rpm -qp --queryformat '%{VERSION}-%{RELEASE}.%{ARCH}' "${rpm_file}" 2>/dev/null
}

# The series' kernel package in a directory of RPMs -- the kernel
# itself, not one of the packages built beside it.
boot_kernel_rpm_file() {
	find "$1" -name 'kernel-*.rpm' \
		! -name '*debuginfo*' ! -name '*devel*' ! -name '*headers*' \
		-type f 2>/dev/null | head -n 1
}

# What that package will report as uname -r once it is running.
#
# Their acceptance suite reads it as EXPECT_KERNEL_VERSION, and the
# two cases that are not the boot test need it just as much: all three
# examine the running kernel, so all three have to know which kernel
# was supposed to come up.
boot_expected_kver() {
	local rpm_file

	rpm_file=$(boot_kernel_rpm_file "$1")
	[ -n "${rpm_file}" ] || return 1
	_boot_kernel_release "${rpm_file}"
}

_boot_ensure_sshpass() {
	if command -v sshpass >/dev/null 2>&1; then
		return 0
	fi

	_boot_log "Installing sshpass..."
	if echo "${HOST_USER_PWD}" | sudo -S yum install -y sshpass >> "${_BOOT_LOG}" 2>&1; then
		return 0
	fi

	_boot_log "yum install failed; building sshpass from source..."
	(
		cd /tmp || exit 1
		wget -q https://sourceforge.net/projects/sshpass/files/latest/download \
			-O sshpass.tar.gz >> "${_BOOT_LOG}" 2>&1 || exit 1
		tar -xzf sshpass.tar.gz >> "${_BOOT_LOG}" 2>&1 || exit 1
		cd sshpass-* || exit 1
		./configure >> "${_BOOT_LOG}" 2>&1 || exit 1
		make >> "${_BOOT_LOG}" 2>&1 || exit 1
		echo "${HOST_USER_PWD}" | sudo -S make install >> "${_BOOT_LOG}" 2>&1 || exit 1
	) >> "${_BOOT_LOG}" 2>&1
}

# Install the series' kernel RPM on the VM and reboot into it.
#
# This is not a test and reports no verdict.  On Anolis's CI these two
# steps are not part of a test either: their anck-ci-test Readme says
# the platform does them -- "安装RPM" with the artefact link and
# "重启机器" set to yes, the step chain install_rpm -> reboot ->
# run_case -- and their suite runs afterwards and only inspects what it
# finds running.  So this does the platform's two steps, and their
# check_kernel_version decides whether the right kernel came up.
#
#   boot_install_and_reboot <rpms_dir> <boot_log>
#
# Returns 0 when the VM answered ssh again after rebooting, 3 when
# there is no VM to do any of this on, and 1 otherwise with the reason
# in BOOT_REASON.  Sets BOOT_EXPECT_KVER to the version the installed
# RPM will report as uname -r, which is their EXPECT_KERNEL_VERSION.
boot_install_and_reboot() {
	local rpms_dir="$1"
	local boot_log="$2"

	_BOOT_LOG="${boot_log}"
	: > "${boot_log}"
	BOOT_REASON=''
	BOOT_EXPECT_KVER=''

	if [ -z "${VM_IP:-}" ]; then
		BOOT_REASON='No VM_IP configured'
		return 3
	fi

	if [ ! -d "${rpms_dir}" ]; then
		BOOT_REASON="RPM directory not found: ${rpms_dir}"
		return 1
	fi

	local kernel_rpm
	kernel_rpm=$(boot_kernel_rpm_file "${rpms_dir}")

	if [ -z "${kernel_rpm}" ]; then
		BOOT_REASON="No kernel RPM found in ${rpms_dir}"
		return 1
	fi

	local rpm_name
	rpm_name=$(basename "${kernel_rpm}")
	_boot_log "Using ${rpm_name}"

	local kernel_version
	kernel_version=$(_boot_kernel_release "${kernel_rpm}")
	if [ -z "${kernel_version}" ]; then
		BOOT_REASON="Could not read the kernel version out of ${rpm_name}"
		return 1
	fi
	BOOT_EXPECT_KVER="${kernel_version}"

	local vmlinuz_path="/boot/vmlinuz-${kernel_version}"
	_boot_log "Expecting kernel ${kernel_version} at ${vmlinuz_path}"

	_boot_log "Checking that ${VM_IP} answers..."
	if ! ping -c 2 -W 2 "${VM_IP}" >> "${boot_log}" 2>&1; then
		BOOT_REASON="VM ${VM_IP} is not reachable"
		return 1
	fi

	if ! _boot_ensure_sshpass; then
		BOOT_REASON='Could not install sshpass'
		return 1
	fi

	_boot_log "Copying the RPM to the VM..."
	if ! vm_scp "${kernel_rpm}" /tmp/ >> "${boot_log}" 2>&1; then
		BOOT_REASON="Could not copy ${rpm_name} to the VM"
		return 1
	fi

	_boot_log "Installing the RPM on the VM..."
	if ! vm_ssh "rpm -ivh --force /tmp/${rpm_name}" >> "${boot_log}" 2>&1; then
		BOOT_REASON="Installing ${rpm_name} on the VM failed"
		return 1
	fi

	if ! vm_ssh "test -f ${vmlinuz_path}" >> "${boot_log}" 2>&1; then
		BOOT_REASON="No kernel image at ${vmlinuz_path} after install"
		return 1
	fi

	_boot_log "Kernels known to the bootloader before the change:"
	vm_ssh "grubby --info ALL | grep -E '^kernel='" >> "${boot_log}" 2>&1

	_boot_log "Making ${vmlinuz_path} the default..."
	if ! vm_ssh "grubby --set-default=${vmlinuz_path}" >> "${boot_log}" 2>&1; then
		BOOT_REASON='grubby could not set the default kernel'
		return 1
	fi

	local default_kernel
	default_kernel=$(vm_ssh "grubby --default-kernel" 2>> "${boot_log}")
	default_kernel="${default_kernel%$'\r'}"
	_boot_log "Default kernel is now ${default_kernel}"

	if [ "${default_kernel}" != "${vmlinuz_path}" ]; then
		BOOT_REASON="Default kernel is ${default_kernel}, expected ${vmlinuz_path}"
		return 1
	fi

	_boot_log "Rebooting the VM..."
	# The connection dies with the machine, so a failure here means nothing.
	vm_ssh "reboot" >> "${boot_log}" 2>&1 || true

	sleep 10
	_boot_log "Waiting up to ${BOOT_WAIT_SECONDS}s for the VM to come back..."
	if ! vm_wait_ssh "${BOOT_WAIT_SECONDS}"; then
		BOOT_REASON="VM did not answer ssh within ${BOOT_WAIT_SECONDS}s of rebooting"
		return 1
	fi

	_boot_log "VM is back up running $(vm_ssh "uname -r" 2>> "${boot_log}")"
	return 0
}
