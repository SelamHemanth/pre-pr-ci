#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/vm.sh
# Shared access to the boot-test virtual machine
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
# Requires VM_IP and VM_ROOT_PWD.
#
#   vm_ssh <command...>      - run a command on the VM as root
#   vm_scp <local> <remote>  - copy a file to the VM
#   vm_wait_ssh [seconds]    - wait for the VM to answer ssh again
#

# The boot test reinstalls the VM regularly, so its host key changes and a
# recorded key would make ssh refuse to connect at all.  Keys are kept out of
# known_hosts rather than merely unchecked, which is what StrictHostKeyChecking
# alone does not achieve.
VM_SSH_OPTS=(
	-o StrictHostKeyChecking=no
	-o UserKnownHostsFile=/dev/null
	-o LogLevel=ERROR
	-o ConnectTimeout=10
)

# sshpass reads the password from SSHPASS with -e.  Passing it as -p would put
# the VM root password in the process list for every user on the machine.
vm_ssh() {
	SSHPASS="${VM_ROOT_PWD}" sshpass -e ssh "${VM_SSH_OPTS[@]}" \
		"root@${VM_IP}" "$@"
}

vm_scp() {
	local source="$1" target="$2"

	SSHPASS="${VM_ROOT_PWD}" sshpass -e scp "${VM_SSH_OPTS[@]}" \
		"${source}" "root@${VM_IP}:${target}"
}

# Poll until the VM accepts an ssh connection again, or give up.
vm_wait_ssh() {
	local timeout="${1:-300}"
	local waited=0

	while [ "${waited}" -lt "${timeout}" ]; do
		if vm_ssh true >/dev/null 2>&1; then
			return 0
		fi
		sleep 10
		waited=$((waited + 10))
	done
	return 1
}
