#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/ready.sh
# Is the series prepared, or is there still work to do?
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# One question, one answer, one place.  prepare.sh asks it to decide
# whether it has anything to do, test.sh asks it to decide whether
# there is any point running, and the web UI asks it to decide what to
# grey out.  Those three used to answer it separately and could
# disagree, which is how a series got tested in a shape openEuler would
# have rejected on sight.
#
# Exit 0 and say so when the series is ready.  Exit 1 and say what is
# missing when it is not.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
CONFIG_FILE="${SCRIPT_DIR}/.configure"

if [ ! -f "${CONFIG_FILE}" ]; then
  echo "not configured yet; run 'make config'"
  exit 1
fi
# shellcheck disable=SC1090
. "${CONFIG_FILE}"

: "${LINUX_SRC_PATH:=}"
: "${TORVALDS_REPO:=}"
: "${NUM_PATCHES:=0}"
: "${SIGNER_NAME:=}"
: "${SIGNER_EMAIL:=}"

if [ -z "${LINUX_SRC_PATH}" ] || [ ! -d "${LINUX_SRC_PATH}/.git" ]; then
  echo "LINUX_SRC_PATH is not a git tree"
  exit 1
fi
if [ "${NUM_PATCHES}" -lt 1 ] 2>/dev/null; then
  echo "NUM_PATCHES is not set"
  exit 1
fi

exec python3 "${SCRIPT_DIR}/oe_ready.py" \
  --kernel "${LINUX_SRC_PATH}" \
  --mirror "${TORVALDS_REPO}" \
  --count "${NUM_PATCHES}" \
  --signer "Signed-off-by: ${SIGNER_NAME} <${SIGNER_EMAIL}>"
