#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/progress.sh
# Shell front end to lib/progress.py
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# lib/progress.py does the counting and the drawing; this is the part
# both distros' test.sh call, so that a change to how the bar is started
# is made once rather than twice.
#
# There are two shapes because the two gates are reached differently.
# Anolis's cases are a script, so the bar runs the case as its own child
# and exits with the case's status -- nothing to start and stop, and no
# way for the bar to swallow a failure.  openEuler's build is a shell
# function their runner sources, so there is no command to put the bar
# in front of; it draws alongside instead.

#: Set by whoever sources this, if they want the counts remembered
#: between runs.  Without it every run is a first run: a live count and
#: no percentage.
PROGRESS_TOTALS="${PROGRESS_TOTALS:-}"

_progress_py() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  echo "${here}/progress.py"
}

# with_progress <watch_dir> <output_log> <name> <phases_log> command...
#
# The command's own output goes to <output_log> and nothing else does:
# that file is where a verdict is read from, so the bar must stay out of
# it.  <phases_log> is where to read the current phase from, which is
# not always the same file -- Anolis's anck_build.py writes its progress
# to /tmp/anck_<case>.log and ours only receives it once the case is
# over.
with_progress() {
  local watch="$1" log="$2" name="$3" phases="$4"
  shift 4

  local script
  script="$(_progress_py)"
  if [ -f "${script}" ]; then
    python3 "${script}" --watch "${watch}" --output "${log}" \
            --phases "${phases}" --totals "${PROGRESS_TOTALS}" \
            --name "${name}" -- "$@"
    return $?
  fi

  "$@" > "${log}" 2>&1
}

_PROGRESS_PID=''
_PROGRESS_WATCH=''
_PROGRESS_NAME=''
_PROGRESS_SINCE=''

# progress_watch <watch_dir> <phases_log> <name>
#
# For work this shell is doing itself.  Pair it with progress_unwatch.
progress_watch() {
  local script
  script="$(_progress_py)"

  _PROGRESS_PID=''
  _PROGRESS_WATCH="$1"
  _PROGRESS_NAME="$3"
  # Noted here rather than left to each of the two python runs, so that
  # the count that gets remembered covers the same build as the bar did.
  _PROGRESS_SINCE="$(date +%s)"

  [ -f "${script}" ] || return 0

  python3 "${script}" --watch "$1" --phases "$2" \
          --totals "${PROGRESS_TOTALS}" --name "$3" \
          --since "${_PROGRESS_SINCE}" --watch-only &
  _PROGRESS_PID=$!
}

# progress_unwatch [exit_status]
#
# The count is only remembered when the work succeeded: a build that
# stopped early left fewer objects behind than a whole one, and writing
# that down would make the next run's bar reach 100% and stay there.
progress_unwatch() {
  local rc="${1:-0}" script

  if [ -n "${_PROGRESS_PID}" ]; then
    kill "${_PROGRESS_PID}" 2>/dev/null
    wait "${_PROGRESS_PID}" 2>/dev/null
    _PROGRESS_PID=''
  fi

  script="$(_progress_py)"
  if [ "${rc}" -eq 0 ] && [ -f "${script}" ] && [ -n "${PROGRESS_TOTALS}" ]
  then
    python3 "${script}" --watch "${_PROGRESS_WATCH}" \
            --totals "${PROGRESS_TOTALS}" --name "${_PROGRESS_NAME}" \
            --since "${_PROGRESS_SINCE}" --record
  fi
}
