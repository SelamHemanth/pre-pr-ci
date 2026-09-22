#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/torvalds.sh
# Shared maintenance of the local bare mirror of mainline
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
# Requires TORVALDS_REPO.  Honours TORVALDS_LOG_PREFIX for indentation and the
# usual colour variables when the caller defines them.
#
#   torvalds_sync   - clone the mirror, or fetch into an existing one
#

TORVALDS_URL="${TORVALDS_URL:-https://github.com/torvalds/linux.git}"

# Skip the fetch when the mirror was updated this recently, in seconds.  The
# mirror is only consulted to decide whether a commit is upstream, so minutes
# of staleness cannot change an answer, and configure, check_dependency and
# the web build job each used to fetch a multi-gigabyte mirror in turn.  Set
# to 0 to fetch unconditionally.
TORVALDS_MAX_AGE="${TORVALDS_MAX_AGE:-1800}"

# Seconds since the mirror last completed a fetch, or nothing if unknown.
# git rewrites FETCH_HEAD on every fetch, including one that brought nothing.
_tv_age() {
	local stamp now
	stamp=$(stat -c '%Y' "${TORVALDS_REPO}/FETCH_HEAD" 2>/dev/null) || return 1
	now=$(date +%s)
	echo $(( now - stamp ))
}

_tv_say() {
	printf '%b\n' "${TORVALDS_LOG_PREFIX:-}$*"
}

_tv_trust() {
	# git refuses to read a repository owned by another user unless it is
	# listed here, which happens whenever the mirror and the caller were
	# created by different accounts.
	git config --global --add safe.directory "${TORVALDS_REPO}" 2>/dev/null || true
}

_tv_clone() {
	local rc

	_tv_say "${BLUE:-}Cloning mainline into ${TORVALDS_REPO}${NC:-}"
	_tv_say "A full mirror is several GB, so the first run takes a while."

	git clone --bare --progress "${TORVALDS_URL}" "${TORVALDS_REPO}" 2>&1 |
		stdbuf -oL tr '\r' '\n' |
		grep --line-buffered -oP '[0-9]+(?=%)' |
		awk '{printf "\rProgress: %d%%", $1; fflush()}'
	# The pipeline exists only to draw a progress bar, so the verdict has to
	# come from git itself and not from the last command in the pipe.
	rc=${PIPESTATUS[0]}
	printf '\n'

	if [ "${rc}" -ne 0 ]; then
		_tv_say "${RED:-}Clone failed: git exited ${rc}.${NC:-}"
		return 1
	fi

	_tv_trust
	_tv_say "${GREEN:-}Clone complete.${NC:-}"
}

_tv_fetch() {
	local output rc

	output=$(git -C "${TORVALDS_REPO}" fetch --all --tags --prune 2>&1)
	rc=$?

	if [ -n "${output}" ]; then
		printf '%s\n' "${output}"
	fi
	return "${rc}"
}

_tv_discard() {
	local owner

	owner=$(stat -c '%u' "${TORVALDS_REPO}" 2>/dev/null) || owner=""

	# Deliberately not escalating with sudo: a mirror owned by somebody else
	# is a setup problem, and piping a password into "sudo rm -rf" from a
	# script that may have no terminal is worse than stopping here.
	if [ -n "${owner}" ] && [ "${owner}" != "$(id -u)" ]; then
		_tv_say "${YELLOW:-}${TORVALDS_REPO} is owned by uid ${owner}, not by you.${NC:-}"
		_tv_say "Remove it yourself and try again:  sudo rm -rf ${TORVALDS_REPO}"
		return 1
	fi

	_tv_say "${BLUE:-}Removing the unusable mirror...${NC:-}"
	if ! rm -rf "${TORVALDS_REPO}"; then
		_tv_say "${RED:-}Could not remove ${TORVALDS_REPO}.${NC:-}"
		return 1
	fi
}

# Bring the mirror up to date, cloning it if it is missing and replacing it if
# it can no longer be fetched into.  Returns non-zero when the mirror is not
# usable afterwards.
torvalds_sync() {
	if [ -z "${TORVALDS_REPO:-}" ]; then
		_tv_say "${RED:-}TORVALDS_REPO is not set; skipping the mirror.${NC:-}"
		return 1
	fi

	if [ ! -d "${TORVALDS_REPO}" ]; then
		_tv_clone
		return
	fi

	_tv_trust

	local age
	if [ "${TORVALDS_MAX_AGE}" -gt 0 ] 2>/dev/null && age=$(_tv_age) \
	   && [ "${age}" -lt "${TORVALDS_MAX_AGE}" ]; then
		_tv_say "${GREEN:-}Mirror was updated $(( age / 60 ))m ago; not fetching again.${NC:-}"
		_tv_say "Set TORVALDS_MAX_AGE=0 to fetch regardless."
		return 0
	fi

	_tv_say "${BLUE:-}Updating the mainline mirror...${NC:-}"

	if _tv_fetch; then
		_tv_say "${GREEN:-}Mirror is up to date.${NC:-}"
		return 0
	fi

	_tv_say "${RED:-}Fetch failed; the mirror looks unusable.${NC:-}"
	_tv_discard || return 1
	_tv_clone
}
