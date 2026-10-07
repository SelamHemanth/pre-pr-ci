#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/hostpath.sh
# The PATH a shell on this host has, for the processes that are not given one
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# A distro's build scripts call system programs by name, and so does the
# kernel spec their rpmbuild runs: the last command of its %build is a
# bare "depmod".  From a terminal that resolves, because a login shell
# has assembled PATH out of /etc/profile and /etc/profile.d.  Under a
# service manager it does not -- a unit's PATH is only what the unit
# says, and nothing makes that the same list -- so their build fails on
# a program that is installed and that the same command finds by hand.
#
# No directory is named in this file.  Which ones a shell here gets is
# the distro's decision and differs between them, so the answer is asked
# of a login shell of this account rather than written down.

# What a login shell of this account ends up with.  The profile it runs
# is free to print as it goes, so the marker is how its PATH is told
# apart from whatever else came out with it.
# Remembered, because starting a login shell costs as much as the whole
# question it answers and callers ask it once per tool they look up.
_hostpath_login() {
  local shell marker out

  if [ -n "${_HOSTPATH_LOGIN+set}" ]; then
    [ -n "${_HOSTPATH_LOGIN}" ] || return 1
    printf '%s\n' "${_HOSTPATH_LOGIN}"
    return 0
  fi
  _HOSTPATH_LOGIN=''

  shell=$(getent passwd "$(id -un)" 2>/dev/null | cut -d: -f7)
  [ -x "${shell}" ] || shell=$(command -v bash) || return 1

  marker='__prci_path__'
  out=$("${shell}" -lc "printf '%s%s\n' '${marker}' \"\$PATH\"" \
        2>/dev/null </dev/null) || return 1

  out=$(printf '%s\n' "${out}" | sed -n "s/^${marker}//p" | tail -n 1)
  [ -n "${out}" ] || return 1
  _HOSTPATH_LOGIN="${out}"
  printf '%s\n' "${out}"
}

# Add every directory that shell has and this process does not.  What we
# were already given keeps its order and stays in front, so a suite's own
# shim directory still shadows the real tool; only the missing entries
# are appended.
hostpath_ensure() {
  local login dir

  login=$(_hostpath_login) || return 0

  local IFS=:
  for dir in ${login}; do
    [ -n "${dir}" ] || continue
    case ":${PATH}:" in
      *":${dir}:"*) ;;
      *) PATH="${PATH}:${dir}" ;;
    esac
  done
  export PATH
}
