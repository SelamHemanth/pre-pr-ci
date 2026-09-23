#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/build.sh
# openEuler build script — apply patches and build the kernel from source
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
set -euo pipefail
# euler/build.sh - openEuler Build Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$SCRIPT_DIR")"

# Load configuration
CONFIG_FILE="${SCRIPT_DIR}/.configure"
DISTRO_CONFIG="${WORKDIR}/.distro_config"
if [ ! -f "${CONFIG_FILE}" ]; then
  echo "Error: Configuration file not found. Run 'make config' first." >&2
  exit 1
fi

# shellcheck disable=SC1090
. "${CONFIG_FILE}"
if [ -f "${DISTRO_CONFIG}" ]; then
  . "${DISTRO_CONFIG}"
fi

# Directories
PATCHES_DIR="${WORKDIR}/patches"
BKP_DIR="${PATCHES_DIR}/.bkp"
LOGS_DIR="${WORKDIR}/logs"
HEAD_ID_FILE="${WORKDIR}/.head_commit_id"

# Colors
# Colours come from the shared helper, which leaves them empty when
# output is not a terminal so redirected logs stay free of escapes.
. "${SCRIPT_DIR}/../lib/log.sh"
: "${LINUX_SRC_PATH:?missing in config}"
: "${SIGNER_NAME:?missing in config}"
: "${SIGNER_EMAIL:?missing in config}"
: "${BUGZILLA_ID:?missing in config}"
: "${NUM_PATCHES:?missing in config}"
: "${BUILD_THREADS:=4}"
: "${TORVALDS_REPO:?missing in config}"
mkdir -p "${PATCHES_DIR}" "${BKP_DIR}" "${LOGS_DIR}"

# Validate repo
if [ ! -d "${LINUX_SRC_PATH}/.git" ]; then
  echo -e "${RED}Linux source path is not a git repo: ${LINUX_SRC_PATH}${NC}" >&2
  exit 10
fi

cd "${LINUX_SRC_PATH}"

# Handle dubious ownership issue
if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo -e "${YELLOW}Warning: Git detected dubious ownership in repository${NC}" >&2
        echo -e "${YELLOW}Attempting to add safe.directory exception...${NC}" >&2
        git config --global --add safe.directory "${LINUX_SRC_PATH}" 2>/dev/null || {
                echo -e "${RED}Failed to add safe.directory exception${NC}" >&2
                echo -e "${RED}Please run manually: git config --global --add safe.directory ${LINUX_SRC_PATH}${NC}" >&2
                exit 12
        }
        echo -e "${GREEN}Safe directory exception added successfully${NC}" >&2
fi

TOTAL_COMMITS="$(git rev-list --count HEAD 2>/dev/null || true)"
if [ -z "${TOTAL_COMMITS}" ] || [ "${TOTAL_COMMITS}" -lt "${NUM_PATCHES}" ]; then
  echo -e "${RED}Repo has insufficient commits (${TOTAL_COMMITS}) for NUM_PATCHES=${NUM_PATCHES}${NC}" >&2
  exit 11
fi

SOB_TAG="Signed-off-by: ${SIGNER_NAME} <${SIGNER_EMAIL}>"

echo -e "${BLUE}Checking if commits are already tagged with openEuler metadata...${NC}"

# SKIP_APPLY=true means commits+patches are already in place — no git am needed
SKIP_APPLY=false

# Two requirements, and they apply to every commit.  KABI fixes used to
# be excused the Signed-off-by, which openEuler's check_employee_id does
# not excuse anybody: it fails any patch without one.  The inclusion line
# is matched loosely because their third template accepts any word --
# hulk, virt, maillist -- and this only decides whether there is work to
# do, not whether the result is acceptable.
all_tagged=true
while IFS= read -r commit_hash; do
  commit_msg="$(git log -1 --format="%B" "${commit_hash}")"

  if ! echo "${commit_msg}" | grep -qE "^[[:alnum:] ]+ inclusion$"; then
    all_tagged=false
    break
  fi
  if ! echo "${commit_msg}" | grep -qF "${SOB_TAG}"; then
    all_tagged=false
    break
  fi
done < <(git log --format="%H" -n "${NUM_PATCHES}" HEAD)

if [ "${all_tagged}" = true ]; then
  echo -e "${GREEN}All ${NUM_PATCHES} commits already contain openEuler metadata.${NC}"
  echo -e "${YELLOW}Skipping format-patch, backup, reset, metadata modification, and patch apply steps.${NC}"
  echo ""
  SKIP_APPLY=true

else
  echo -e "${BLUE}Commits not fully tagged. Running full patch generation and modification...${NC}"

  # Save current HEAD id for later reset (full SHA)
  HEAD_ID="$(git rev-parse --verify HEAD)"
  printf "%s\n" "${HEAD_ID}" > "${HEAD_ID_FILE}"
  echo -e "${BLUE}Saved HEAD commit: ${HEAD_ID}${NC}"

  TMP_FORMAT_DIR="$(mktemp -d "${WORKDIR}/formatpatches.XXXX")"
  echo -e "${BLUE}Generating ${NUM_PATCHES} patches...${NC}"
  git -c core.quiet=true format-patch -${NUM_PATCHES} -o "${TMP_FORMAT_DIR}" "HEAD~${NUM_PATCHES}..HEAD" >/dev/null 2>&1 || {
    git format-patch -${NUM_PATCHES} -o "${TMP_FORMAT_DIR}" "HEAD~${NUM_PATCHES}..HEAD"
  }

  # Backup existing patches and move new ones
  mkdir -p "${BKP_DIR}"
  for ex in "${PATCHES_DIR}"/*.patch; do
    [ -f "${ex}" ] || continue
    cp -f "${ex}" "${BKP_DIR}/$(basename "${ex}").bak-$(date +%s)"
  done
  rm -f "${PATCHES_DIR}"/*.patch || true
  mv "${TMP_FORMAT_DIR}"/*.patch "${PATCHES_DIR}/" 2>/dev/null || true
  rm -rf "${TMP_FORMAT_DIR}"

  # Reset repo back by NUM_PATCHES commits so we can re-apply
  if ! git reset --hard "HEAD~${NUM_PATCHES}" >/dev/null 2>&1; then
    echo -e "${RED}Could not rewind ${NUM_PATCHES} commits; refusing to continue.${NC}" >&2
    exit 1
  fi
  echo -e "${YELLOW}HEAD is now at $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)${NC}"

  echo -e "${BLUE}Modifying patches with openEuler metadata and Signed-off-by tags...${NC}"

  # oe_header.py writes the header, and refuses when it cannot write one
  # that openEuler's format.py will accept.  Refusing matters: the header
  # used to be written on a best-effort basis, so an unresolvable SHA or a
  # commit in no release produced a patch that looked finished and was
  # rejected by the gate later.  Better to stop here, where the tree has
  # not been rewound yet and the message says what is missing.
  refused=0
  for p in "${PATCHES_DIR}"/*.patch; do
    [ -f "${p}" ] || continue
    cp -f "${p}" "${BKP_DIR}/$(basename "${p}")"

    if summary=$(python3 "${SCRIPT_DIR}/oe_header.py" "${p}" \
        --mirror "${TORVALDS_REPO}" \
        --kernel "${LINUX_SRC_PATH}" \
        --bugzilla "${BUGZILLA_ID}" \
        --signer "${SOB_TAG}" \
        --branch "${OE_TARGET_BRANCH:-OLK-6.6}" 2>&1); then
      echo -e "  ${GREEN}✓${NC} $(basename "${p}") — ${summary}"
    else
      echo -e "  ${RED}✗${NC} $(echo "${summary}" | sed '2,$s/^/    /')"
      refused=$((refused + 1))
    fi
  done

  if [ "${refused}" -ne 0 ]; then
    echo ""
    echo -e "${RED}${refused} patch(es) cannot be given a header openEuler will accept.${NC}" >&2
    echo -e "${YELLOW}Nothing has been applied; restoring ${HEAD_ID}.${NC}" >&2
    git reset --hard "${HEAD_ID}" >/dev/null 2>&1 || true
    exit 21
  fi
fi

# Ensure repo clean
if [ -n "$(git status --porcelain)" ]; then
  echo -e "${RED}Linux source tree is not clean. Commit or stash changes before running.${NC}" >&2
  exit 12
fi

git config user.name "${SIGNER_NAME}"
git config user.email "${SIGNER_EMAIL}"

# ── If commits are already applied, skip the patch apply loop entirely ──
if [ "${SKIP_APPLY}" = true ]; then
  echo -e "${GREEN}Patches already applied — skipping git am step.${NC}"
  echo -e "${YELLOW}Proceeding to next process (make test)...${NC}"
  echo ""
  echo -e "${GREEN}✓ openEuler build process completed successfully${NC}"
  echo -e "Run ${YELLOW}'make test'${NC} to execute openEuler-specific tests"
  exit 0
fi

# Collect patch filenames in lexical order
mapfile -t PATCH_LIST < <(ls -1 "${PATCHES_DIR}"/*.patch 2>/dev/null || true)
TOTAL_SELECTED="${#PATCH_LIST[@]}"
if [ "${TOTAL_SELECTED}" -eq 0 ]; then
  echo -e "${RED}No patches found in ${PATCHES_DIR}${NC}" >&2
  exit 13
fi
echo ""
echo -e "Total patches to process: ${TOTAL_SELECTED}"
echo -e "Build threads: ${BUILD_THREADS}"
echo ""

# Apply all patches
idx=0
for pf in "${PATCH_LIST[@]}"; do
  idx=$((idx+1))
  name="$(basename "${pf}")"
  echo -e "${BLUE}[${idx}/${TOTAL_SELECTED}] Processing: ${name}${NC}"
  # Apply patch
  if git -C "${LINUX_SRC_PATH}" am --3way "${pf}" >/dev/null 2>&1; then
    echo -e "  Applying   : ${GREEN}✓ PASS${NC}"
  else
    git -C "${LINUX_SRC_PATH}" am --abort >/dev/null 2>&1 || true
    echo -e "  Applying   : ${RED}✗ FAIL${NC}"
    echo ""
    echo -e "${RED}Error: git am failed for ${name}${NC}"
    exit 20
  fi
  echo ""
done

echo ""
echo -e "${GREEN}✓ openEuler build process completed successfully${NC}"
echo -e "Run ${YELLOW}'make test'${NC} to execute openEuler-specific tests"
exit 0
