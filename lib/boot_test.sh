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
# Expects lib/vm.sh to be sourced too, and the caller to provide the pass/fail
# helpers, VM_IP, VM_ROOT_PWD and HOST_USER_PWD.
#
#   run_boot_test <test_name> <rpms_dir> <boot_log>
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

run_boot_test() {
	local test_name="$1"
	local rpms_dir="$2"
	local boot_log="$3"

	_BOOT_LOG="${boot_log}"
	: > "${boot_log}"

	if [ -z "${VM_IP:-}" ]; then
		skip "${test_name}" "No VM_IP configured"
		echo ""
		return
	fi

	if [ ! -d "${rpms_dir}" ]; then
		fail "${test_name}" "RPM directory not found: ${rpms_dir}"
		echo ""
		return
	fi

	local kernel_rpm
	kernel_rpm=$(find "${rpms_dir}" -name 'kernel-*.rpm' \
		! -name '*debuginfo*' ! -name '*devel*' ! -name '*headers*' \
		-type f | head -n 1)

	if [ -z "${kernel_rpm}" ]; then
		fail "${test_name}" "No kernel RPM found in ${rpms_dir}"
		echo ""
		return
	fi

	local rpm_name
	rpm_name=$(basename "${kernel_rpm}")
	_boot_log "Using ${rpm_name}"

	local kernel_version
	kernel_version=$(_boot_kernel_release "${kernel_rpm}")
	if [ -z "${kernel_version}" ]; then
		fail "${test_name}" "Could not read the kernel version out of ${rpm_name}"
		echo ""
		return
	fi

	local vmlinuz_path="/boot/vmlinuz-${kernel_version}"
	_boot_log "Expecting kernel ${kernel_version} at ${vmlinuz_path}"

	_boot_log "Checking that ${VM_IP} answers..."
	if ! ping -c 2 -W 2 "${VM_IP}" >> "${boot_log}" 2>&1; then
		fail "${test_name}" "VM ${VM_IP} is not reachable"
		echo ""
		return
	fi

	if ! _boot_ensure_sshpass; then
		fail "${test_name}" "Could not install sshpass"
		echo ""
		return
	fi

	_boot_log "Copying the RPM to the VM..."
	if ! vm_scp "${kernel_rpm}" /tmp/ >> "${boot_log}" 2>&1; then
		fail "${test_name}" "Could not copy ${rpm_name} to the VM"
		echo ""
		return
	fi

	_boot_log "Installing the RPM on the VM..."
	if ! vm_ssh "rpm -ivh --force /tmp/${rpm_name}" >> "${boot_log}" 2>&1; then
		fail "${test_name}" "Installing ${rpm_name} on the VM failed"
		echo ""
		return
	fi

	if ! vm_ssh "test -f ${vmlinuz_path}" >> "${boot_log}" 2>&1; then
		fail "${test_name}" "No kernel image at ${vmlinuz_path} after install"
		echo ""
		return
	fi

	_boot_log "Kernels known to the bootloader before the change:"
	vm_ssh "grubby --info ALL | grep -E '^kernel='" >> "${boot_log}" 2>&1

	_boot_log "Making ${vmlinuz_path} the default..."
	if ! vm_ssh "grubby --set-default=${vmlinuz_path}" >> "${boot_log}" 2>&1; then
		fail "${test_name}" "grubby could not set the default kernel"
		echo ""
		return
	fi

	local default_kernel
	default_kernel=$(vm_ssh "grubby --default-kernel" 2>> "${boot_log}")
	default_kernel="${default_kernel%$'\r'}"
	_boot_log "Default kernel is now ${default_kernel}"

	if [ "${default_kernel}" != "${vmlinuz_path}" ]; then
		fail "${test_name}" \
			"Default kernel is ${default_kernel}, expected ${vmlinuz_path}"
		echo ""
		return
	fi

	_boot_log "Rebooting the VM..."
	# The connection dies with the machine, so a failure here means nothing.
	vm_ssh "reboot" >> "${boot_log}" 2>&1 || true

	sleep 10
	_boot_log "Waiting up to ${BOOT_WAIT_SECONDS}s for the VM to come back..."
	if ! vm_wait_ssh "${BOOT_WAIT_SECONDS}"; then
		fail "${test_name}" \
			"VM did not answer ssh within ${BOOT_WAIT_SECONDS}s of rebooting"
		echo ""
		return
	fi

	local running_kernel
	running_kernel=$(vm_ssh "uname -r" 2>> "${boot_log}")
	running_kernel="${running_kernel%$'\r'}"

	_boot_log "Running kernel:  ${running_kernel}"
	_boot_log "Expected kernel: ${kernel_version}"

	if [ "${running_kernel}" = "${kernel_version}" ]; then
		pass "${test_name}"
	else
		fail "${test_name}" \
			"VM booted ${running_kernel}, expected ${kernel_version}"
	fi

	echo ""
}
