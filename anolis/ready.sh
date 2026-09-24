#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - anolis/ready.sh
# Is the series prepared, or is there still work to do?
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# prepare.sh, test.sh and the web UI all need to know whether the
# series has been through preparation.  They ask here so they cannot
# give different answers.
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
: "${NUM_PATCHES:=0}"
: "${ANBZ_ID:=}"
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

ANBZ_TAG="ANBZ: #${ANBZ_ID}"
SOB_TAG="Signed-off-by: ${SIGNER_NAME} <${SIGNER_EMAIL}>"

cd "${LINUX_SRC_PATH}" || exit 1

have="$(git rev-list --count HEAD 2>/dev/null || echo 0)"
if [ "${have}" -lt "${NUM_PATCHES}" ]; then
  echo "only ${have} commit(s) on the branch, expected ${NUM_PATCHES}"
  exit 1
fi

while IFS= read -r sha; do
  msg="$(git log -1 --format='%B' "${sha}")"
  subject="$(git log -1 --format='%s' "${sha}")"
  if ! printf '%s' "${msg}" | grep -qF "${ANBZ_TAG}"; then
    echo "${sha:0:12} ${subject:0:60}: no ${ANBZ_TAG}"
    exit 1
  fi
  if ! printf '%s' "${msg}" | grep -qF "${SOB_TAG}"; then
    echo "${sha:0:12} ${subject:0:60}: no Signed-off-by"
    exit 1
  fi
done < <(git log --format='%H' -n "${NUM_PATCHES}" HEAD)

# A backport says where it came from.  cloud-kernel !13995 went in with
# "commit <sha> upstream." between the ANBZ tag and the body, and this
# gate used to pass a series with that line missing from every commit,
# which is the one thing a reader of the message cannot reconstruct.
#
# Only for commits that are backports: upstream_ref asks the mirror
# whether the patch exists there, and says nothing about original work,
# which has no commit to point at.
if [ -n "${TORVALDS_REPO:-}" ] && [ -d "${TORVALDS_REPO}" ]; then
  if ! missing="$(python3 "${SCRIPT_DIR}/upstream_ref.py" \
        --mirror "${TORVALDS_REPO}" \
        --kernel "${LINUX_SRC_PATH}" \
        --count "${NUM_PATCHES}")"; then
    printf '%s\n' "${missing}"
    exit 1
  fi
fi

echo "all ${NUM_PATCHES} commit(s) are ready to test"
