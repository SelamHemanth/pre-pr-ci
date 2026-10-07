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
#   config_ask <prompt> [default]
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

# Ask for one value, offering a default and showing what pressing
# Enter will do.
#
# The default is the caller's to supply, and it has to be something
# this host or this user already said -- the last answer, or git's idea
# of who is committing -- never a value written into this tool.  A
# value written here is the same wrong answer for everyone who runs it:
# the name and address this used to fall back on were one person's, and
# a source path of theirs was offered to every other machine as if it
# existed.
#
# read -p writes its prompt to standard error, so this works inside a
# command substitution.
config_ask() {
	local prompt="$1" fallback="${2:-}" answer

	# End of input is an answer too -- it means "whatever you have" --
	# and the callers run under "set -e", where letting read's failure
	# out would end the configuration instead of accepting the default.
	if [ -n "${fallback}" ]; then
		read -r -p "${prompt} [${fallback}]: " answer || answer=''
	else
		read -r -p "${prompt}: " answer || answer=''
	fi
	printf '%s' "${answer:-${fallback}}"
}

# What this tool last recorded for a key, if anything.
#
# Read rather than sourced: the file carries passwords and arbitrary
# shell, and the caller wants one value out of it, not everything in it
# executed at prompt time.
config_last() {
	local file="$1" key="$2"

	[ -r "${file}" ] || return 0
	KEY="${key}" awk '
		BEGIN { key = ENVIRON["KEY"] }
		substr($0, 1, length(key) + 1) == key "=" {
			value = substr($0, length(key) + 2)
		}
		END {
			gsub(/^['"'"'"]|['"'"'"]$/, "", value)
			print value
		}
	' "${file}"
}
