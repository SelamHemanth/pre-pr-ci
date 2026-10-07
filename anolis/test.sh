#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - anolis/test.sh
# OpenAnolis CI test suite — run pre-PR validation checks on kernel patches
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
set -uo pipefail

# anolis/test.sh - OpenAnolis CI Test Suite

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

LOGS_DIR="${WORKDIR}/logs"
TEST_LOG="${LOGS_DIR}/test_results.log"

# VM_IP is exported for convenience; the two passwords deliberately are not,
# so they stay out of /proc/<pid>/environ of every command a test runs.
export VM_IP

# Colors
# Colours come from the shared helper, which leaves them empty when
# output is not a terminal so redirected logs stay free of escapes.
. "${SCRIPT_DIR}/../lib/log.sh"

# Their scripts, and the spec their rpmbuild runs, call system programs
# by name, so they have to see the PATH a terminal here has and not the
# narrower one a service manager starts us with.
# shellcheck source=../lib/hostpath.sh
. "${WORKDIR}/lib/hostpath.sh"
hostpath_ensure

# shellcheck source=../lib/vm.sh
. "${WORKDIR}/lib/vm.sh"
# shellcheck source=../lib/boot_test.sh
. "${WORKDIR}/lib/boot_test.sh"

#: Where each case's object count from its last successful run is kept,
#: so the next run of it has something to measure against.  Read by
#: lib/progress.sh, so it is set before sourcing it.
PROGRESS_TOTALS="${WORKDIR}/.prci/progress"

# Draws a bar while a case runs, since their builds are silent.  The
# case's own output still goes only to its log, which is where their
# verdict is read from and must stay exactly as their scripts wrote it.
# shellcheck source=../lib/progress.sh
. "${WORKDIR}/lib/progress.sh"
# Counts the warnings their build raises and says which of them are the
# series'.  Nothing is filtered here: their scripts do not read warnings,
# so there is no verdict to protect and no reason to hide any.
# shellcheck source=../lib/warnings.sh
. "${WORKDIR}/lib/warnings.sh"
# Asks whether a case can succeed here before spending a build finding
# out that it cannot.
# shellcheck source=../lib/hostcheck.sh
. "${WORKDIR}/lib/hostcheck.sh"

# Function to list available tests
list_tests() {
  echo ""
  echo -e "${CYAN}╔═════════════════════════════════════╗${NC}"
  echo -e "${CYAN}║     OpenAnolis - Available Tests    ║${NC}"
  echo -e "${CYAN}╚═════════════════════════════════════╝${NC}"
  echo ""
  echo -e "${GREEN}Test Name                    Description${NC}"
  echo -e "${GREEN}─────────────────────────────────────────────────────────${NC}"
  echo -e "  1. check_dependency        Check commit dependencies"
  echo -e "  2. check_kconfig           Validate kernel configuration"
  echo -e "  3. build_allyes_config     Build with allyesconfig"
  echo -e "  4. build_allno_config      Build with allnoconfig"
  echo -e "  5. build_anolis_defconfig  Build with anolis_defconfig"
  echo -e "  6. build_anolis_debug      Build with anolis-debug_defconfig"
  echo -e "  7. anck_rpm_build          Build ANCK RPM packages"
  echo -e "  8. check_kapi              Check kernel ABI compatibility"
  echo -e "  9. boot_kernel_rpm         Boot VM with built kernel RPM"
  echo -e "  10. build_perf             Build anolis kernel perf tool"
  echo ""
  echo -e "${BLUE}Usage:${NC}"
  echo "  $0                         - Run all enabled tests"
  echo "  $0 list/--list/-l          - Show this list"
  echo "  $0 <test_name>             - Run specific test"
  echo ""
  echo -e "${YELLOW}Examples:${NC}"
  echo "  $0 check_kconfig"
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
#
# BUILD_THREADS is not among them any more: their anck_build.sh sets
# its own job count, one less than the processor count, and that is now
# what the builds use.
: "${NUM_PATCHES:=1}"

# An unprepared series has no ANBZ tag and no sign-off, so the checks
# would be reporting what preparation has not done yet rather than
# anything about the patches.
ready_rc=0
ready_why="$(bash "${SCRIPT_DIR}/ready.sh" 2>&1)" || ready_rc=$?
if [ "${ready_rc}" -ne 0 ]; then
  echo -e "${RED}The series is not prepared, so there is nothing to test yet.${NC}" >&2
  echo -e "${YELLOW}  ${ready_why}${NC}" >&2
  echo -e "${YELLOW}Run 'make prepare' first.${NC}" >&2
  exit 22
fi

mkdir -p "${LOGS_DIR}"


# Counters
TEST_RESULTS=()
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
SKIPPED_TESTS=0
WARNED_TESTS=0

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

# Their suites have four verdicts, not three: parse.awk turns ====WARN:
# into Warning, and their report shows it as its own state.  Folding it
# into pass would hide something they flagged, and into fail would
# reject a series they let through.
warn() {
  local test_name="$1"
  local reason="${2:-}"
  echo -e "${YELLOW}⚠ WARN${NC}: ${test_name}"
  [ -n "$reason" ] && echo -e "  Reason: ${reason}"
  TEST_RESULTS+=("WARN:${test_name}")
  ((WARNED_TESTS++))
  ((TOTAL_TESTS++))
}

# Common function to build kernel with given config target
# One case of their anck-pack-and-boot suite.
#
# anolis/an_tone.sh is where their scripts are run: their anck_build.py
# composes the command line and names the log, their anck_build.sh does
# the clone, the dependencies and every make line, and their own
# "<case>: pass" is what decides this row.  Nothing here knows what any
# of their cases builds, which is the point -- when they change a make
# line, updating the submodule is the whole of following them.
#
# The four statuses are their four markers, the same ones
# cases/lib.sh and anck_ci_test.sh already speak.
#
# $1 is their name for the case, $2 ours.  They differ in two places
# only -- check_Kconfig against check_kconfig, and their
# build_anolis_debug_defconfig against our build_anolis_debug -- and
# ours is what registry.py, the web interface and the TEST_* settings
# in the user's config are keyed on.
#
# $3 is the log's name, which is neither of those in two cases: the
# verdict has to be the name registry.py knows, while the log keeps
# the name it has always had and that registry.py points the web
# interface at.
run_their_build_case() {
  local theirs="$1" ours="${2:-$1}" stem="${3:-${2:-$1}}"
  local log="${LOGS_DIR}/${stem}.log"
  local rc=0 scratch

  echo "  → Running their ${theirs}..."

  # Their build cases end in `make -j $job_num -s`, so between their
  # configure step and their verdict there is nothing on stdout to show
  # -- for allyesconfig that is the best part of an hour.  The object
  # files appearing in the directory they build in are the sign of life
  # instead, and an_tone.sh is asked where that is rather than it being
  # worked out twice.
  scratch=$(bash "${SCRIPT_DIR}/an_tone.sh" --scratch "${theirs}" \
            2>/dev/null)

  # Their anck_build.py writes while the case runs; ours only receives
  # their output once it is over, so the phase is read from theirs.
  with_progress "${scratch}" "${log}" "${stem}" \
                "$(bash "${SCRIPT_DIR}/an_tone.sh" --case-log "${theirs}")" \
                bash "${SCRIPT_DIR}/an_tone.sh" "${theirs}" || rc=$?

  # Appended after their output rather than mixed into it, so their
  # verdict is read from the log exactly as their script wrote it.
  warnings_summarise "${log}" "${LINUX_SRC_PATH}"

  case ${rc} in
    0) pass "${ours}" ;;
    1) fail "${ours}" "Their ${theirs} failed (see ${log})" ;;
    3) skip "${ours}" "$(tail -n 4 "${log}")" ;;
    5) warn "${ours}" "Their ${theirs} flagged something (see ${log})" ;;
    *) fail "${ours}" "Their suite could not run (see ${log})" ;;
  esac
  echo ""
}

# ---- TEST DEFINITIONS ----

test_check_kconfig() {
  echo -e "${BLUE}Test-1: check_Kconfig${NC}"
  run_their_build_case check_Kconfig check_kconfig check_Kconfig
}

test_build_allyes_config() {
  echo -e "${BLUE}Test-2: build_allyes_config${NC}"
  run_their_build_case build_allyes_config
}

test_build_allno_config() {
  echo -e "${BLUE}Test-3: build_allno_config${NC}"
  run_their_build_case build_allno_config
}

test_build_anolis_defconfig() {
  echo -e "${BLUE}Test-4: build_anolis_defconfig${NC}"
  run_their_build_case build_anolis_defconfig
}

test_build_anolis_debug_defconfig() {
  echo -e "${BLUE}Test-5: build_anolis_debug_defconfig${NC}"
  run_their_build_case build_anolis_debug_defconfig build_anolis_debug \
                     build_anolis_debug_defconfig
}

test_anck_rpm_build() {
  echo -e "${BLUE}Test-6: anck_rpm_build${NC}"

  # Theirs does not run `make dist-rpms`, which is what this used to
  # do.  Their anck_build.sh clones the ck-build harness from
  # src-anolis-sig, links the kernel into it as cloud-kernel,
  # generates the spec, installs its build requirements and runs
  # ck-build's own build.sh -- and the RPMs land in
  # ck-build/outputs/0, which is where their anck_boot_test then
  # reads them from.  All of that is theirs now.
  run_their_build_case anck_rpm_build
}

# Their anck-ci-test, which reports boot_kernel_rpm, check_kapi and
# check_dmesg.  All three read the running kernel, so all three need
# the series' RPM installed and booted first, and they share one run of
# their suite -- cases/anck_ci_test.sh caches it per caller.
#
# EXPECT_KERNEL_VERSION is what their check_kernel_version compares
# uname -r against.  It comes from the RPM their anck_rpm_build built;
# left unset, theirs falls back to the newest installed kernel-headers
# and warns, which on the VM is whatever was there before the series.
run_their_vm_case() {
  local case_name="$1" stem="${2:-$1}"
  local log="${LOGS_DIR}/${stem}.log"
  local rc=0

  # Nothing to count for these -- their suite builds kabi-dw and reads
  # dmesg on the VM, not here -- so the bar reports their steps and how
  # long they have been running.
  with_progress '' "${log}" "${stem}" "${log}" \
                bash "${SCRIPT_DIR}/cases/anck_ci_test.sh" "${case_name}" \
                || rc=$?

  case ${rc} in
    0) pass "${case_name}" ;;
    1) fail "${case_name}" "Their ${case_name} failed (see ${log})" ;;
    3) skip "${case_name}" "$(vm_skip_reason "${log}")" ;;
    5) warn "${case_name}" "Their ${case_name} flagged something (see ${log})" ;;
    *) fail "${case_name}" "Their suite could not run (see ${log})" ;;
  esac
  echo ""
}

# Why their acceptance suite skipped.
#
# Their run.sh checks boot_kernel_rpm first and skips the rest when it
# fails, because check_kapi and check_dmesg both examine the running
# kernel and there is nothing to say about the series if the machine is
# not booted into it.  Their marker alone does not say that, so the
# line of theirs that explains it is carried up instead.
vm_skip_reason() {
  local log="$1" why

  why=$(grep -m1 -E '^(Error|Warning): ' "${log}" 2>/dev/null)
  if [ -n "${why}" ]; then
    echo "${why}"
    echo "Their suite skips the acceptance cases until the VM is booted"
    echo "into the series' kernel.  Set TEST_BOOT_KERNEL=yes to have"
    echo "boot_kernel_rpm install it and reboot first."
    return
  fi

  tail -n 4 "${log}"
}

test_boot_kernel_rpm() {
  echo -e "${BLUE}Test-8: boot_kernel_rpm${NC}"

  local boot_log="${LOGS_DIR}/boot_kernel_rpm.log"
  local rpm_dir

  # Their anck_rpm_build leaves the RPMs in ck-build/outputs/0, which
  # is where their own anck_boot_test reads them from.
  if ! rpm_dir=$(bash "${SCRIPT_DIR}/an_tone.sh" --rpm-dir 2>/dev/null); then
    skip "boot_kernel_rpm" \
         "Their anck_rpm_build has not produced any RPMs yet"
    echo ""
    return
  fi

  # Installing the RPM and rebooting are the two steps their tone
  # platform performs before their suite runs; neither is a test.
  #
  # Their platform installs the whole artefact set, and their
  # check_kapi takes vmlinux out of the debug packages in it.  Those
  # are most of a gigabyte, so they go across only when that case is
  # going to read them.
  BOOT_WANT_DEBUGINFO="${TEST_CHECK_KAPI:-yes}"
  export BOOT_WANT_DEBUGINFO
  echo "  → Installing the RPM on ${VM_IP:-the VM} and rebooting..."
  local rc=0
  boot_install_and_reboot "${rpm_dir}" "${boot_log}" || rc=$?
  case ${rc} in
    0) ;;
    3) skip "boot_kernel_rpm" "${BOOT_REASON}"; echo ""; return ;;
    *) fail "boot_kernel_rpm" "${BOOT_REASON} (see ${boot_log})"
       echo ""; return ;;
  esac

  # Now their code decides whether the kernel that came up is the one
  # the series built.
  export EXPECT_KERNEL_VERSION="${BOOT_EXPECT_KVER}"
  run_their_vm_case boot_kernel_rpm
}

test_check_kapi() {
  echo -e "${BLUE}Test-9: check_kapi${NC}"

  # Theirs clones kabi-dw and the branch's kabi-whitelist itself,
  # unpacks vmlinux from the installed kernel-debuginfo, runs
  # kabi-dw generate and compare, and judges on whether the func--
  # blocks it filters out are empty -- explicitly not on compare's exit
  # status, which is 2 whenever anything differs at all.  That last
  # point is why this is not worth reimplementing.
  run_their_vm_case check_kapi kapi_test
}

test_build_perf() {
  echo -e "${BLUE}Test-7: build_perf${NC}"
  run_their_build_case build_perf
}

test_check_dmesg() {
  echo -e "${BLUE}Test-10: check_dmesg${NC}"
  run_their_vm_case check_dmesg
}

# ---- TEST EXECUTION ----

# Asked before anything is built, and once for the whole suite: whether
# this host can build the tree at all is a property of the two of them,
# not of any one check.  A host that cannot will fail the compiling
# cases one after another, each of them looking like a verdict on the
# series, so nothing is run until it can.
if ! HOSTCHECK_WHY=$(hostcheck_report "${LINUX_SRC_PATH}"); then
  echo -e "${YELLOW}⚠ Not running: this host cannot test this kernel${NC}"
  echo ""
  echo "${HOSTCHECK_WHY}"
  echo ""
  echo -e "${YELLOW}No checks were run, so nothing here is a verdict on" \
          "the series.${NC}"
  exit 0
fi

# Check if specific test is requested
SPECIFIC_TEST="${1:-}"

if [ -n "$SPECIFIC_TEST" ]; then
  # Run specific test directly
  echo -e "${BLUE}Running specific test: ${SPECIFIC_TEST}${NC}"
  echo ""

  case "$SPECIFIC_TEST" in
    check_kconfig)
      test_check_kconfig
      ;;
    build_allyes_config)
      test_build_allyes_config
      ;;
    build_allno_config)
      test_build_allno_config
      ;;
    build_anolis_defconfig)
      test_build_anolis_defconfig
      ;;
    build_anolis_debug)
      test_build_anolis_debug_defconfig
      ;;
    anck_rpm_build)
      test_anck_rpm_build
      ;;
    build_perf)
      test_build_perf
      ;;
    boot_kernel_rpm)
      test_boot_kernel_rpm
      ;;
    check_kapi)
      test_check_kapi
      ;;
    check_dmesg)
      test_check_dmesg
      ;;
    *)
      echo -e "${RED}Error: Unknown test '$SPECIFIC_TEST'${NC}"
      echo ""
      echo "Available tests:"
      echo "  - check_kconfig"
      echo "  - build_allyes_config"
      echo "  - build_allno_config"
      echo "  - build_anolis_defconfig"
      echo "  - build_anolis_debug"
      echo "  - anck_rpm_build"
      echo "  - build_perf"
      echo "  - boot_kernel_rpm"
      echo "  - check_kapi"
      echo "  - check_dmesg"
      echo ""
      echo "Run '$0 list' for detailed information"
      exit 1
      ;;
  esac
else
  # In the order their report lists them: the build cases their
  # abs_build job runs, then the acceptance cases their anck-ci-test
  # job runs on a machine booted into the series.
  [ "${TEST_CHECK_KCONFIG:-yes}" == "yes" ] && test_check_kconfig
  [ "${TEST_BUILD_ALLYES:-yes}" == "yes" ] && test_build_allyes_config
  [ "${TEST_BUILD_ALLNO:-yes}" == "yes" ] && test_build_allno_config
  [ "${TEST_BUILD_DEFCONFIG:-yes}" == "yes" ] && test_build_anolis_defconfig
  [ "${TEST_BUILD_DEBUG:-yes}" == "yes" ] && test_build_anolis_debug_defconfig
  [ "${TEST_RPM_BUILD:-yes}" == "yes" ] && test_anck_rpm_build
  [ "${TEST_BUILD_PERF:-yes}" == "yes" ] && test_build_perf
  [ "${TEST_BOOT_KERNEL:-yes}" == "yes" ] && test_boot_kernel_rpm
  [ "${TEST_CHECK_KAPI:-yes}" == "yes" ] && test_check_kapi
  [ "${TEST_CHECK_DMESG:-yes}" == "yes" ] && test_check_dmesg
fi

# ---- SUMMARY ----
{
  echo "OpenAnolis Test Report"
  echo "======================"
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
  echo "Warned: ${WARNED_TESTS}"
  echo "Skipped: ${SKIPPED_TESTS}"
} > "${TEST_LOG}"

echo -e "${GREEN}============${NC}"
echo -e "${GREEN}Test Summary${NC}"
echo -e "${GREEN}============${NC}"
echo "Total Tests: ${TOTAL_TESTS}"
echo -e "Passed:  ${GREEN}${PASSED_TESTS}${NC}"
echo -e "Failed:  ${RED}${FAILED_TESTS}${NC}"
echo -e "Warned:  ${YELLOW}${WARNED_TESTS}${NC}"
echo -e "Skipped: ${YELLOW}${SKIPPED_TESTS}${NC}"
echo ""
echo -e "${BLUE}Full report: ${TEST_LOG}${NC}"
echo ""

if [ "${FAILED_TESTS}" -gt 0 ]; then
  echo -e "${RED}✗ Some tests failed${NC}"
  exit 1
elif [ "${WARNED_TESTS}" -gt 0 ]; then
  echo -e "${YELLOW}⚠ Nothing failed, but ${WARNED_TESTS} check(s) flagged something${NC}"
  exit 0
else
  echo -e "${GREEN}✓ All tests passed or skipped${NC}"
  exit 0
fi
