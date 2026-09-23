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

echo -e "${BLUE}Checking whether the series has already been prepared...${NC}"

# SKIP_APPLY=true means commits+patches are already in place — no git am needed
SKIP_APPLY=false

# ready.sh is the one place that decides this, because preparing a
# series that is already prepared is not a harmless no-op: it rewrites
# history, throws away any Conflicts: description written by hand, and
# leaves the branch short of its commits if it is interrupted.
ready_rc=0
ready_why="$(bash "${SCRIPT_DIR}/ready.sh" 2>&1)" || ready_rc=$?
if [ "${ready_rc}" -eq 0 ]; then
  echo -e "${GREEN}Already prepared: ${ready_why}${NC}"
  echo -e "${YELLOW}Nothing to do. Run 'make test' to test them.${NC}"
  echo ""
  SKIP_APPLY=true

else
  echo -e "${BLUE}Not prepared yet (${ready_why}); preparing the series...${NC}"

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

  # The commits the patches were formatted from, oldest first, which is
  # the order format-patch numbers them in.  They survive the rewind
  # below as unreferenced objects, and oe_header.py needs them: telling
  # whether a backport diverges from upstream means diffing the commit
  # against the upstream one, and a patch file on its own is not enough
  # to do that the way openEuler does it.
  mapfile -t ORIG_COMMITS < <(git rev-list --reverse -n "${NUM_PATCHES}" HEAD)

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
  idx=0
  undescribed=()
  for p in "${PATCHES_DIR}"/*.patch; do
    [ -f "${p}" ] || continue
    cp -f "${p}" "${BKP_DIR}/$(basename "${p}")"
    orig="${ORIG_COMMITS[${idx}]:-}"
    idx=$((idx + 1))

    if summary=$(python3 "${SCRIPT_DIR}/oe_header.py" "${p}" \
        --mirror "${TORVALDS_REPO}" \
        --kernel "${LINUX_SRC_PATH}" \
        --commit "${orig}" \
        --bugzilla "${BUGZILLA_ID}" \
        --signer "${SOB_TAG}" \
        --branch "${OE_TARGET_BRANCH:-OLK-6.6}" 2>&1); then
      echo -e "  ${GREEN}✓${NC} $(basename "${p}") — ${summary}"
      case "${summary}" in
        *'description left to you'*)
          undescribed+=("$(basename "${p}")") ;;
      esac
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

  # A Conflicts: section with a placeholder in the brackets satisfies
  # checkconflict, because all their regex asks is that something is
  # bracketed.  It will not satisfy a reviewer, and this is the one part
  # of the section nothing here can work out: why the backport had to
  # differ is in the submitter's head, not in the diff.
  if [ "${#undescribed[@]}" -ne 0 ]; then
    echo ""
    echo -e "${YELLOW}${#undescribed[@]} patch(es) differ from upstream and now carry a Conflicts:"
    echo -e "section with a placeholder description. The gate accepts it; a reviewer"
    echo -e "will not. Edit the text between the brackets before you send:${NC}"
    for u in "${undescribed[@]}"; do
      echo -e "${YELLOW}  - ${u}${NC}"
    done
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
  echo -e "${GREEN}✓ Patches are prepared for openEuler${NC}"
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
echo -e "${GREEN}✓ Patches are prepared for openEuler${NC}"
echo -e "Run ${YELLOW}'make test'${NC} to execute openEuler-specific tests"
exit 0
