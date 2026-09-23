#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/test.sh
# openEuler CI test suite — run pre-PR validation checks on kernel patches
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
set -uo pipefail

# euler/test.sh - openEuler CI Test Suite

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$SCRIPT_DIR")"

# Load configuration
CONFIG_FILE="${SCRIPT_DIR}/.configure"
DISTRO_CONFIG="${WORKDIR}/.distro_config"

if [ ! -f "${CONFIG_FILE}" ]; then
  echo "Error: Configuration file not found: ${CONFIG_FILE}" >&2
  echo "Run 'make config' first." >&2
  exit 1
fi

# shellcheck disable=SC1090
. "${CONFIG_FILE}"

if [ -f "${DISTRO_CONFIG}" ]; then
  . "${DISTRO_CONFIG}"
fi

PATCHES_DIR="${WORKDIR}/patches"
LOGS_DIR="${WORKDIR}/logs"
TEST_LOG="${LOGS_DIR}/test_results.log"

# src-openeuler/kernel, which carries check-kabi and the ABI whitelists.
# Which branch of it to read is derived from the target branch rather than
# fixed here; see _oe_kabi_branch in oe_build.sh.
KABI_KERNEL_DIR="${SCRIPT_DIR}/kernel"


# Colors
# Colours come from the shared helper, which leaves them empty when
# output is not a terminal so redirected logs stay free of escapes.
. "${SCRIPT_DIR}/../lib/log.sh"

# shellcheck source=../lib/torvalds.sh
. "${WORKDIR}/lib/torvalds.sh"
# shellcheck source=oe_build.sh
. "${SCRIPT_DIR}/oe_build.sh"
# lib/vm.sh and lib/boot_test.sh are deliberately not sourced: openEuler's
# CI never boots a kernel, so this distro has no boot test to need them.
# anolis still does, and still sources them.

# Function to list available tests
list_tests() {
  echo ""
  echo -e "${CYAN}╔═════════════════════════════════╗${NC}"
  echo -e "${CYAN}║   openEuler - Available Tests   ║${NC}"
  echo -e "${CYAN}╚═════════════════════════════════╝${NC}"
  echo ""
  echo -e "${GREEN}Test Name              Description${NC}"
  echo -e "${GREEN}─────────────────────────────────────────────────────────${NC}"
  echo -e "${CYAN}Static checks, seconds each:${NC}"
  echo -e "  1. oe_checkpatch       checkpatch.pl, skipping clean backports"
  echo -e "  2. oe_checkformat      Commit message headers"
  echo -e "  3. oe_checkdepend      Upstream Fixes: closure"
  echo -e "  4. oe_checkkabi        KABI keywords in message and diff"
  echo -e "  5. oe_checkconflict    Backports that diverge must say Conflicts:"
  echo -e "  6. oe_checkbinary      Binary files added by the series"
  echo ""
  echo -e "${CYAN}Builds, one per architecture, an hour each:${NC}"
  echo -e "  7. oe_build_x86_64     allmodconfig, defconfig, KABI, defconfig drift"
  echo -e "  8. oe_build_aarch64    as above, cross compiled            [off]"
  echo -e "  9. oe_build_arm        allmodconfig, cross compiled        [off]"
  echo -e " 10. oe_build_ppc        allmodconfig, cross compiled        [off]"
  echo -e " 11. oe_build_ppc64      allmodconfig, cross compiled        [off]"
  echo -e " 12. oe_build_riscv64    allmodconfig, cross compiled        [off]"
  echo -e " 13. oe_build_loongarch  allmodconfig, cross compiled        [off]"
  echo ""
  echo -e "  [off] means not enabled by default; openEuler runs these in"
  echo -e "  parallel across a fleet and we would run them in series."
  echo ""
  echo -e "${BLUE}Usage:${NC}"
  echo "  $0                     - Run all enabled tests"
  echo "  $0 list/--list/-l      - Show this list"
  echo "  $0 <test_name>         - Run specific test"
  echo ""
  echo -e "${YELLOW}Examples:${NC}"
  echo "  $0 oe_checkpatch"
  echo ""
  exit 0
}

# Check if list command is requested
if [ "${1:-}" == "list" ] || [ "${1:-}" == "--list" ] || [ "${1:-}" == "-l" ]; then
  list_tests
fi

: "${LINUX_SRC_PATH:?missing in config}"
: "${SIGNER_NAME:?missing in config}"
: "${SIGNER_EMAIL:?missing in config}"
: "${TORVALDS_REPO:?missing in config}"

# Tests are also runnable against a config written before this setting
# existed, so fall back rather than aborting under "set -u".
: "${BUILD_THREADS:=$(nproc)}"
: "${NUM_PATCHES:=1}"

mkdir -p "${LOGS_DIR}"

echo ""

# Counters
TEST_RESULTS=()
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
SKIPPED_TESTS=0

if [ $(arch) == "x86_64" ]; then
  kernel_arch="x86"
elif [ $(arch) == "aarch64" ]; then
  kernel_arch="arm64"
else
  echo -e "${RED}Error: Not supported arch${NC}"
  exit 1
fi

pass() {
  local test_name="$1"
  echo -e "${GREEN}✓ PASS${NC}: ${test_name}"
  TEST_RESULTS+=("PASS:${test_name}")
  ((PASSED_TESTS++))
  ((TOTAL_TESTS++))
}

fail() {
  local test_name="$1"
  local reason="${2:-}"
  echo -e "${RED}✗ FAIL${NC}: ${test_name}"
  [ -n "$reason" ] && echo -e "  Reason: ${reason}"
  TEST_RESULTS+=("FAIL:${test_name}")
  ((FAILED_TESTS++))
  ((TOTAL_TESTS++))
}

skip() {
  local test_name="$1"
  local reason="${2:-}"
  echo -e "${YELLOW}⊘ SKIP${NC}: ${test_name}"
  [ -n "$reason" ] && echo -e "  Reason: ${reason}"
  TEST_RESULTS+=("SKIP:${test_name}")
  ((SKIPPED_TESTS++))
  ((TOTAL_TESTS++))
}

sync_torvalds_repo() {
  TORVALDS_LOG_PREFIX="  → " torvalds_sync || true
  echo ""
}

# ---- TEST DEFINITIONS ----

# openEuler's gate, run from openEuler's code.
#
# oe_checks.py drives the scripts in the hulk_robot_test submodule and turns
# what they print into an exit status: 0 passed or warned, 1 the patches were
# rejected, 2 the check could not run, 3 there was nothing to check.  Warnings
# are not failures here for the same reason they are not there -- the gate
# reports them and still lets the patch through.
run_oe_check() {
  local test_name="$1"
  local check="$2"
  local log="${LOGS_DIR}/${test_name}.log"
  local source_dir="${SCRIPT_DIR}/hulk_robot_test"

  echo -e "${BLUE}${test_name}${NC}"

  if [ ! -d "${source_dir}/openEuler/lib/static_checking/scripts" ]; then
    skip "${test_name}" "hulk_robot_test is not checked out; run 'git submodule update --init ${source_dir#"${WORKDIR}/"}'"
    echo ""
    return
  fi

  # checkformat, checkdepend and checkconflict resolve commits against
  # mainline, so the mirror has to be there first.  The freshness guard in
  # torvalds_sync means this is usually a no-op.
  case "${check}" in
    checkformat|checkdepend|checkconflict) sync_torvalds_repo ;;
  esac

  python3 "${SCRIPT_DIR}/oe_checks.py" "${check}" \
    --source "${source_dir}" \
    --kernel "${LINUX_SRC_PATH}" \
    --workdir "${WORKDIR}" \
    --mirror "${TORVALDS_REPO:-}" \
    --branch "${OE_TARGET_BRANCH:-OLK-6.6}" \
    --count "${NUM_PATCHES:-5}" 2>&1 | tee "${log}"

  case "${PIPESTATUS[0]}" in
    0) pass "${test_name}" ;;
    3) skip "${test_name}" "No commits to check" ;;
    2) fail "${test_name}" "The check could not run (see ${log})" ;;
    *) fail "${test_name}" "openEuler ${check} rejected the series (see ${log})" ;;
  esac

  echo ""
}

test_oe_checkpatch()    { run_oe_check "oe_checkpatch"    "checkpatch"; }
test_oe_checkformat()   { run_oe_check "oe_checkformat"   "checkformat"; }
test_oe_checkdepend()   { run_oe_check "oe_checkdepend"   "checkdepend"; }
test_oe_checkkabi()     { run_oe_check "oe_checkkabi"     "checkkabi"; }
test_oe_checkconflict() { run_oe_check "oe_checkconflict" "checkconflict"; }
test_oe_checkbinary()   { run_oe_check "oe_checkbinary"   "checkbinary"; }

# openEuler's build gate, one architecture per test because that is one
# job per architecture in their CI.  oe_build.sh does the work; this only
# turns its exit status into a verdict.  An architecture their matrix does
# not build on this branch reports skipped rather than passed, so a run
# that checked nothing cannot look like a run that checked everything.
run_oe_build() {
  local arch="$1"
  local test_name="oe_build_${arch}"
  local log="${LOGS_DIR}/${test_name}.log"

  echo -e "${BLUE}${test_name}${NC}"

  if [ ! -d "${SCRIPT_DIR}/hulk_robot_test/openEuler/lib" ]; then
    skip "${test_name}" "hulk_robot_test is not checked out; run 'git submodule update --init euler/hulk_robot_test'"
    echo ""
    return
  fi

  oe_build_arch "${arch}" 2>&1 | tee "${log}"

  case "${PIPESTATUS[0]}" in
    0) pass "${test_name}" ;;
    3) skip "${test_name}" "openEuler does not build ${arch} on ${OE_TARGET_BRANCH:-OLK-6.6}" ;;
    2) skip "${test_name}" "The build could not be set up (see ${log})" ;;
    4) skip "${test_name}" "The tree does not build ${arch} without your series either (see ${log})" ;;
    *) fail "${test_name}" "openEuler's ${arch} build gate rejected the series (see ${log})" ;;
  esac

  echo ""
}

test_oe_build_x86_64()    { run_oe_build "x86_64"; }
test_oe_build_aarch64()   { run_oe_build "aarch64"; }
test_oe_build_arm()       { run_oe_build "arm"; }
test_oe_build_ppc()       { run_oe_build "ppc"; }
test_oe_build_ppc64()     { run_oe_build "ppc64"; }
test_oe_build_riscv64()   { run_oe_build "riscv64"; }
test_oe_build_loongarch() { run_oe_build "loongarch"; }

# ---- TEST EXECUTION ----

# Check if specific test is requested
SPECIFIC_TEST="${1:-}"

if [ -n "$SPECIFIC_TEST" ]; then
  # Run specific test directly
  echo -e "${BLUE}Running specific test: ${SPECIFIC_TEST}${NC}"
  echo ""

  case "$SPECIFIC_TEST" in
    oe_checkpatch)
      test_oe_checkpatch
      ;;
    oe_checkformat)
      test_oe_checkformat
      ;;
    oe_checkdepend)
      test_oe_checkdepend
      ;;
    oe_checkkabi)
      test_oe_checkkabi
      ;;
    oe_checkconflict)
      test_oe_checkconflict
      ;;
    oe_checkbinary)
      test_oe_checkbinary
      ;;
    oe_build_x86_64)
      test_oe_build_x86_64
      ;;
    oe_build_aarch64)
      test_oe_build_aarch64
      ;;
    oe_build_arm)
      test_oe_build_arm
      ;;
    oe_build_ppc)
      test_oe_build_ppc
      ;;
    oe_build_ppc64)
      test_oe_build_ppc64
      ;;
    oe_build_riscv64)
      test_oe_build_riscv64
      ;;
    oe_build_loongarch)
      test_oe_build_loongarch
      ;;
    *)
      echo -e "${RED}Error: Unknown test '$SPECIFIC_TEST'${NC}"
      echo ""
      echo "Available tests:"
      for t in oe_checkpatch oe_checkformat oe_checkdepend oe_checkkabi \
               oe_checkconflict oe_checkbinary \
               oe_build_x86_64 oe_build_aarch64 oe_build_arm oe_build_ppc \
               oe_build_ppc64 oe_build_riscv64 oe_build_loongarch; do
        echo "  - ${t}"
      done
      echo ""
      echo "Run '$0 list' for detailed information"
      exit 1
      ;;
  esac
else
  # Run all enabled tests.  openEuler's own checks go first: they are the
  # cheapest and they are the ones the real gate will apply, so there is no
  # sense compiling for forty minutes before finding out the series is
  # rejected on its commit messages.
  [ "${TEST_OE_CHECKPATCH:-yes}" == "yes" ] && test_oe_checkpatch
  [ "${TEST_OE_CHECKFORMAT:-yes}" == "yes" ] && test_oe_checkformat
  [ "${TEST_OE_CHECKDEPEND:-yes}" == "yes" ] && test_oe_checkdepend
  [ "${TEST_OE_CHECKKABI:-yes}" == "yes" ] && test_oe_checkkabi
  [ "${TEST_OE_CHECKCONFLICT:-yes}" == "yes" ] && test_oe_checkconflict
  [ "${TEST_OE_CHECKBINARY:-yes}" == "yes" ] && test_oe_checkbinary
  # Then the builds, native first.  The cross builds default to off: one
  # allmodconfig per architecture is openEuler's fleet working in
  # parallel and our one machine working in series.
  [ "${TEST_OE_BUILD_X86_64:-yes}" == "yes" ]   && test_oe_build_x86_64
  [ "${TEST_OE_BUILD_AARCH64:-no}" == "yes" ]   && test_oe_build_aarch64
  [ "${TEST_OE_BUILD_ARM:-no}" == "yes" ]       && test_oe_build_arm
  [ "${TEST_OE_BUILD_PPC:-no}" == "yes" ]       && test_oe_build_ppc
  [ "${TEST_OE_BUILD_PPC64:-no}" == "yes" ]     && test_oe_build_ppc64
  [ "${TEST_OE_BUILD_RISCV64:-no}" == "yes" ]   && test_oe_build_riscv64
  [ "${TEST_OE_BUILD_LOONGARCH:-no}" == "yes" ] && test_oe_build_loongarch
fi

# ---- SUMMARY ----
{
  echo "openEuler Test Report"
  echo "====================="
  echo "Date: $(date)"
  echo "Kernel Source: ${LINUX_SRC_PATH}"
  echo ""
  echo "Test Results:"
  echo "-------------"
  for result in "${TEST_RESULTS[@]}"; do
    echo "$result"
  done
  echo ""
  echo "Summary:"
  echo "--------"
  echo "Total Tests: ${TOTAL_TESTS}"
  echo "Passed: ${PASSED_TESTS}"
  echo "Failed: ${FAILED_TESTS}"
  echo "Skipped: ${SKIPPED_TESTS}"
} > "${TEST_LOG}"

echo -e "${GREEN}============${NC}"
echo -e "${GREEN}Test Summary${NC}"
echo -e "${GREEN}============${NC}"
echo "Total Tests: ${TOTAL_TESTS}"
echo -e "Passed:  ${GREEN}${PASSED_TESTS}${NC}"
echo -e "Failed:  ${RED}${FAILED_TESTS}${NC}"
echo -e "Skipped: ${YELLOW}${SKIPPED_TESTS}${NC}"
echo ""
echo -e "${BLUE}Full report: ${TEST_LOG}${NC}"
echo ""

if [ "${FAILED_TESTS}" -gt 0 ]; then
  echo -e "${RED}✗ Some tests failed${NC}"
  exit 1
else
  echo -e "${GREEN}✓ All tests passed or skipped${NC}"
  exit 0
fi
