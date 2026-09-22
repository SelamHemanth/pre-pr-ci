#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/config_file.sh
# Safe in-place editing of the sourceable .configure files
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
#   config_set <file> <key> <value>
#

# Replace KEY's assignment in a .configure file, keeping its position, or
# append it if the key is new.
#
# Deliberately not "sed -i s|^KEY=.*|KEY=value|": these files carry passwords,
# and a password containing | or & corrupted the substitution, while one
# containing a quote produced a file that broke every later "source" of it.
# printf %q emits something bash can read back exactly.
config_set() {
	local file="$1" key="$2" value="$3"
	local quoted tmp

	quoted=$(printf '%q' "${value}")

	tmp=$(mktemp "${file}.XXXXXX") || return 1
	chmod 600 "${tmp}"

	if [ ! -f "${file}" ]; then
		printf '%s=%s\n' "${key}" "${quoted}" > "${tmp}"
		mv -f "${tmp}" "${file}"
		return
	fi

	if ! KEY="${key}" REPL="${key}=${quoted}" awk '
		BEGIN { key = ENVIRON["KEY"]; repl = ENVIRON["REPL"]; done = 0 }
		substr($0, 1, length(key) + 1) == key "=" {
			if (!done) { print repl; done = 1 }
			next
		}
		{ print }
		END { if (!done) print repl }
	' "${file}" > "${tmp}"; then
		rm -f "${tmp}"
		return 1
	fi

	mv -f "${tmp}" "${file}"
}
