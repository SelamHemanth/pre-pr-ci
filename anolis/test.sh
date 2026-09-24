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

# shellcheck source=../lib/torvalds.sh
. "${WORKDIR}/lib/torvalds.sh"
# shellcheck source=../lib/vm.sh
. "${WORKDIR}/lib/vm.sh"
# shellcheck source=../lib/boot_test.sh
. "${WORKDIR}/lib/boot_test.sh"

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
: "${BUILD_THREADS:=$(nproc)}"
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
run_kernel_build() {
  local test_name="$1"
  local config_target="$2"
  # The log stem defaults to the test name but can differ: the verdict has to
  # be the name registry.py knows, while the log file keeps the name it has
  # always had.
  local log_stem="${3:-$test_name}"
  local build_log="${LOGS_DIR}/${log_stem}.log"
  cd "${LINUX_SRC_PATH}"

  make clean > /dev/null 2>&1
  echo "  → Building kernel with ${config_target}..."
  if make "${config_target}" > "${build_log}" 2>&1 \
    && make -j"${BUILD_THREADS}" >> "${build_log}" 2>&1 \
    && make modules -j"${BUILD_THREADS}" >> "${build_log}" 2>&1; then
    pass "${test_name}"
  else
    fail "${test_name}" "Build failed (see ${build_log})"
  fi
  echo ""
}

sync_torvalds_repo() {
  TORVALDS_LOG_PREFIX="  → " torvalds_sync || true
  echo ""
}

# ---- TEST DEFINITIONS ----

test_check_kconfig() {
  echo -e "${BLUE}Test-1: check_Kconfig${NC}"
  cd "${LINUX_SRC_PATH}/anolis" 2>/dev/null || {
    skip "check_kconfig" "anolis/ directory not found"
    return
  }

  mkdir -p "${LINUX_SRC_PATH}/anolis/output" 2>/dev/null
  chmod -R u+w "${LINUX_SRC_PATH}/anolis/output" 2>/dev/null || true

  echo "  → Checking kconfig..."

  check_status=0

  # Step 1: Run the original check
  if ARCH=${kernel_arch} make dist-configs-check > "${LOGS_DIR}/check_Kconfig.log" 2>&1; then
	  echo "  → dist-configs-check passed" >> "${LOGS_DIR}/check_Kconfig.log"
  else
	  echo "  → dist-configs-check failed" >> "${LOGS_DIR}/check_Kconfig.log"
	  check_status=1
  fi

  # Step 2: Run dist-configs-update and check git working tree cleanliness
  echo "  → Running 'make dist-configs-update' to verify Kconfig baseline..." >> "${LOGS_DIR}/check_Kconfig.log"
  make dist-configs-update >> "${LOGS_DIR}/check_Kconfig.log" 2>&1

  # Capture git status output
  git_status_output=$(git status --porcelain=v1 2>&1)
  git_exit_code=$?

  if [ $git_exit_code -ne 0 ]; then
	  echo "  ERROR: 'git status' failed. Cannot verify working tree state." >> "${LOGS_DIR}/check_Kconfig.log"
	  check_status=1
  elif [ -n "$git_status_output" ]; then
	  # Working tree is NOT clean
	  echo "  ERROR: Kconfig baseline is outdated or inconsistent!" >> "${LOGS_DIR}/check_Kconfig.log"
	  echo "  The following changes were detected after 'make dist-configs-update':" >> "${LOGS_DIR}/check_Kconfig.log"
	  echo "$git_status_output" >> "${LOGS_DIR}/check_Kconfig.log"
	  echo "" >> "${LOGS_DIR}/check_Kconfig.log"
	  echo "  Please update the Kconfig baseline according to:" >> "${LOGS_DIR}/check_Kconfig.log"
	  echo "     ${SCRIPT_DIR}/'How_to_resolve_kconfig_test_failure?.md'" >> "${LOGS_DIR}/check_Kconfig.log"
	  check_status=1
  else
	  # Working tree is clean
	  echo "  → Kconfig baseline is consistent (working tree clean after update)." >> "${LOGS_DIR}/check_Kconfig.log"
  fi

  # Report results based on check_status
  if [ $check_status -eq 0 ]; then
    pass "check_kconfig"
  else
    fail "check_kconfig" "dist-configs-check failed (see ${LOGS_DIR}/check_Kconfig.log)"
  fi
  echo ""
}

test_build_allyes_config() {
  echo -e "${BLUE}Test-2: build_allyes_config${NC}"
  run_kernel_build "build_allyes_config" "allyesconfig"
}

test_build_allno_config() {
  echo -e "${BLUE}Test-3: build_allno_config${NC}"
  run_kernel_build "build_allno_config" "allnoconfig"
}

test_build_anolis_defconfig() {
  echo -e "${BLUE}Test-4: build_anolis_defconfig${NC}"
  run_kernel_build "build_anolis_defconfig" "anolis_defconfig"
}

test_build_anolis_debug_defconfig() {
  echo -e "${BLUE}Test-5: build_anolis_debug_defconfig${NC}"
  run_kernel_build "build_anolis_debug" "anolis-debug_defconfig" \
                   "build_anolis_debug_defconfig"
}

test_anck_rpm_build() {
  echo -e "${BLUE}Test-6: anck_rpm_build${NC}"

  # Check and install required build dependencies only if missing
  local packages="audit-libs-devel binutils-devel libbpf-devel libcap-ng-devel libnl3-devel newt-devel pciutils-devel xmlto yum-utils"
  local missing_packages=""

  for pkg in $packages; do
    if ! rpm -q "$pkg" &>/dev/null; then
      missing_packages="$missing_packages $pkg"
    fi
  done

  if [ -n "$missing_packages" ]; then
    echo "  → Installing missing packages:$missing_packages" >> "${LOGS_DIR}/anck_rpm_build.log"
    # Not fatal: the packages may be present in a form rpm -q does not see,
    # and failing here would be worse than letting the build try.  But say so,
    # because otherwise the only clue is a missing header hundreds of lines
    # later in the log.
    if ! echo "${HOST_USER_PWD}" | sudo -S yum install -y $missing_packages \
            >> "${LOGS_DIR}/anck_rpm_build.log" 2>&1; then
      echo -e "${YELLOW}[WARN]${NC} anck_rpm_build: could not install:$missing_packages"
      echo -e "${YELLOW}       check the sudo password and repo access; continuing anyway${NC}"
    fi
  fi

  # Set build environment variables
  export BUILD_NUMBER="${BUILD_NUMBER:-0}"
  export BUILD_MODE="${BUILD_MODE:-devel}"
  export BUILD_VARIANT="${BUILD_VARIANT:-default}"
  export BUILD_EXTRA="${BUILD_EXTRA:-debuginfo}"

  cd "${LINUX_SRC_PATH}/anolis" || {
    fail "anck_rpm_build" "Cannot enter anolis directory"
    return
  }

  # Create symlink to kernel source if not exists
  [ ! -L "cloud-kernel" ] && ln -sf "${LINUX_SRC_PATH}" cloud-kernel

  # Create and clean outputs directory
  outputdir="${LINUX_SRC_PATH}/anolis/outputs"
  rm -rf "${outputdir}/rpmbuild"
  mkdir -p "${outputdir}"

  # Generate spec file if not exists or outdated
  if [ ! -f output/kernel.spec ] || [ "${LINUX_SRC_PATH}/anolis/Makefile" -nt output/kernel.spec ]; then
    make dist-genspec >> "${LOGS_DIR}/anck_rpm_build.log" 2>&1 || {
      fail "anck_rpm_build" "make dist-genspec failed"
      return
    }
  fi

  # Install spec dependencies only once
  if [ ! -f "${outputdir}/.deps_installed" ]; then
    echo "  → Installing build dependencies..." >> "${LOGS_DIR}/anck_rpm_build.log"
    if echo "${HOST_USER_PWD}" | sudo -S yum-builddep -y output/kernel.spec \
            >> "${LOGS_DIR}/anck_rpm_build.log" 2>&1; then
      # Only remember success.  Touching this unconditionally meant a single
      # failed yum-builddep was recorded as done, so every later run skipped
      # the install and failed identically until someone found and deleted
      # this hidden marker.
      touch "${outputdir}/.deps_installed"
    else
      echo -e "${YELLOW}[WARN]${NC} anck_rpm_build: yum-builddep failed; will retry next run"
    fi
  fi

  # Set ulimit and build
  ulimit -n 65535

  echo "  → Building RPMs..."
  if DIST=".an23" \
     DIST_BUILD_NUMBER=${BUILD_NUMBER} \
     DIST_OUTPUT=${outputdir} \
     DIST_BUILD_MODE=${BUILD_MODE} \
     DIST_BUILD_VARIANT=${BUILD_VARIANT} \
     DIST_BUILD_EXTRA=${BUILD_EXTRA} \
     make dist-rpms RPMBUILDOPTS="--define '%_smp_mflags -j${BUILD_THREADS}'" \
     >> "${LOGS_DIR}/anck_rpm_build.log" 2>&1; then

    local rpm_dir="${outputdir}/rpmbuild/RPMS"

    if [ -d "${rpm_dir}" ]; then
      local rpm_count=$(find "${rpm_dir}" -name "*.rpm" -type f | wc -l)
      echo -e "  → Binary RPMs (${rpm_count} packages): ${rpm_dir}" >> "${LOGS_DIR}/anck_rpm_build.log"
    fi

    pass "anck_rpm_build"
  else
    fail "anck_rpm_build" "RPM build failed (see ${LOGS_DIR}/anck_rpm_build.log)"
  fi

  echo ""
}

test_boot_kernel_rpm() {
  echo -e "${BLUE}Test-8: boot_kernel_rpm${NC}"

  run_boot_test "boot_kernel_rpm" \
    "${LINUX_SRC_PATH}/anolis/outputs/rpmbuild/RPMS/$(arch)" \
    "${LOGS_DIR}/boot_kernel_rpm.log"
}

test_check_kapi() {
  echo -e "${BLUE}Test-9: check_kapi${NC}"

  local KAPI_TEST_DIR="${SCRIPT_DIR}"
  local KABI_DW_DIR="${KAPI_TEST_DIR}/kabi-dw"
  local KABI_WHITELIST_DIR="${KAPI_TEST_DIR}/kabi-whitelist"
  local KAPI_LOG="${LOGS_DIR}/kapi_test.log"
  local KAPI_WITHOUT_BP="${KAPI_TEST_DIR}/kapiwithoutbp"
  local KAPI_WITH_BP="${KAPI_TEST_DIR}/kapiwithbp"
  local KAPI_DIFF_OUTPUT="${KAPI_TEST_DIR}/kapi_diff.txt"
  local KAPI_OP_DIR="${KAPI_TEST_DIR}/outputs"

  # Determine kernel branch for kabi-whitelist
  local KERNEL_VERSION=$(grep "^VERSION = " "${LINUX_SRC_PATH}/Makefile" | awk '{print $3}')
  local PATCHLEVEL=$(grep "^PATCHLEVEL = " "${LINUX_SRC_PATH}/Makefile" | awk '{print $3}')
  local KABI_BRANCH="devel-${KERNEL_VERSION}.${PATCHLEVEL}"

  echo "  → Checking KAPI..." > "$KAPI_LOG"

  # Ensure submodules are initialized
  if [ ! "$(ls "${KABI_DW_DIR}" 2>/dev/null)" ] || \
	  [ ! "$(ls "${KABI_WHITELIST_DIR}" 2>/dev/null)" ]; then
     echo "Initializing and updating submodules..." >> "$KAPI_LOG"
     git -C "${WORKDIR}" submodule update --init --recursive >> "$KAPI_LOG" 2>&1
     if [ $? -ne 0 ]; then
	     fail "check_kapi" "Failed to init/update submodules"
	     return
     fi
  fi

  # Update submodules
  echo "Updating submodules..." >> "$KAPI_LOG"
  git -C "${WORKDIR}" submodule update --remote --recursive >> "$KAPI_LOG"

  # Clean and build kabi-dw tool
  cd "${KABI_DW_DIR}"
  make clean >> "${KAPI_LOG}" 2>&1
  if ! make >> "${KAPI_LOG}" 2>&1; then
    fail "check_kapi" "Failed to build kabi-dw tool"
    return
  fi

  # Determine architecture
  local KABI_ARCH=""
  if [ "${kernel_arch}" == "x86" ] || [ "${kernel_arch}" == "x86_64" ]; then
    KABI_ARCH="x86_64"
  elif [ "${kernel_arch}" == "arm64" ] || [ "${kernel_arch}" == "aarch64" ]; then
    KABI_ARCH="aarch64"
  else
    fail "check_kapi" "Unsupported architecture: ${kernel_arch}"
    return
  fi

  # Set whitelist file path
  local WHITELIST_FILE="${KABI_WHITELIST_DIR}/kabi_whitelist_${KABI_ARCH}"
  if [ ! -f "${WHITELIST_FILE}" ]; then
    fail "check_kapi" "Whitelist file not found: ${WHITELIST_FILE}"
    return
  fi

  # Get current HEAD commit ID.  Must be the full hash: this is what the tree
  # gets reset to later, and an abbreviated one can become ambiguous.
  cd "${LINUX_SRC_PATH}"
  local HEAD_SHAID
  HEAD_SHAID=$(git rev-parse HEAD 2>> "${KAPI_LOG}")
  if [ -z "${HEAD_SHAID}" ]; then
    fail "check_kapi" "Failed to get HEAD commit ID"
    return
  fi

  # This test rewinds the kernel tree to build it with and without the
  # backports.  Restore it however the function exits, or a failed build
  # leaves the user's patches off HEAD with no indication why.
  _kapi_restore_tree() {
    local target="$1"
    if ! git -C "${LINUX_SRC_PATH}" reset --hard "${target}" >> "${KAPI_LOG}" 2>&1; then
      echo "  → WARNING: could not restore ${LINUX_SRC_PATH} to ${target}" |
        tee -a "${KAPI_LOG}"
    fi
  }
  trap '_kapi_restore_tree "${HEAD_SHAID}"; trap - RETURN' RETURN

  echo "  → Generating KAPI symbols..."

  # Reset to base (without backport patches)
  echo "  → Building kernel without backport patches..." >> "$KAPI_LOG"
  if ! git reset --hard "HEAD~${NUM_PATCHES}" >> "${KAPI_LOG}" 2>&1; then
    fail "check_kapi" "Could not rewind ${NUM_PATCHES} commits"
    return
  fi
  make mrproper >> "${KAPI_LOG}" 2>&1
  make anolis_defconfig >> "${KAPI_LOG}" 2>&1

  if ! make -j"${BUILD_THREADS}" >> "${KAPI_LOG}" 2>&1; then
    fail "check_kapi" "Failed to build kernel without BP"
    return
  fi

  # Check if vmlinux exists
  local VMLINUX_PATH="${LINUX_SRC_PATH}/vmlinux"
  if [ ! -f "${VMLINUX_PATH}" ]; then
    fail "check_kapi" "vmlinux not found (without BP)"
    return
  fi

  # Generate kABI without backport patches
  cd "${KAPI_TEST_DIR}"
  mkdir -p outputs
  "${KABI_DW_DIR}/kabi-dw" generate -s "${WHITELIST_FILE}" -o "${KAPI_OP_DIR}" "${VMLINUX_PATH}" > "${KAPI_WITHOUT_BP}" 2>&1

  # Reset back to HEAD (with backport patches)
  echo "  → Building kernel with backport patches..." >> "$KAPI_LOG"
  cd "${LINUX_SRC_PATH}"
  if ! git reset --hard "${HEAD_SHAID}" >> "${KAPI_LOG}" 2>&1; then
    fail "check_kapi" "Could not return the tree to ${HEAD_SHAID}"
    return
  fi
  make mrproper >> "${KAPI_LOG}" 2>&1
  make anolis_defconfig >> "${KAPI_LOG}" 2>&1

  if ! make -j"${BUILD_THREADS}" >> "${KAPI_LOG}" 2>&1; then
    fail "check_kapi" "Failed to build kernel with BP"
    return
  fi

  # Check if vmlinux exists
  if [ ! -f "${VMLINUX_PATH}" ]; then
    fail "check_kapi" "vmlinux not found (with BP)"
    return
  fi

  # Generate kABI with backport patches
  cd "${KAPI_TEST_DIR}"
  "${KABI_DW_DIR}/kabi-dw" generate -s "${WHITELIST_FILE}" -o "${KAPI_OP_DIR}" "${VMLINUX_PATH}" > "${KAPI_WITH_BP}" 2>&1

  # Compare the two kABI outputs
  echo "  → Comparing kABI symbols..."
  diff "${KAPI_WITH_BP}" "${KAPI_WITHOUT_BP}" > "${KAPI_DIFF_OUTPUT}" 2>&1
  local diff_exit_code=$?

  if [ ${diff_exit_code} -eq 0 ]; then
    pass "check_kapi"
  else
    # Extract only the symbol names from diff output (lines with "not found!")
    local unknown_symbols=$(grep "not found!" "${KAPI_DIFF_OUTPUT}" | grep -E "^[<>]" | sed 's/^[<>] //' | sed 's/ not found!$//')

    if [ -z "${unknown_symbols}" ]; then
      pass "check_kapi"
    else
      echo ""
      echo -e "${RED}  ✗ kABI symbols mismatch:${NC}"
      echo "  ========================================"
      echo "${unknown_symbols}"
      echo "  ========================================"
      echo ""

      mv "${KAPI_WITHOUT_BP}" "${LOGS_DIR}/"
      mv "${KAPI_WITH_BP}" "${LOGS_DIR}/"
      mv "${KAPI_DIFF_OUTPUT}" "${LOGS_DIR}/"

      fail "check_kapi" "kABI symbols mismatch detected"
    fi
  fi

  echo ""
}

test_build_perf() {
  echo -e "${BLUE}Test-7: build_perf${NC}"

  local perf_log="${LOGS_DIR}/build_perf.log"
  local perf_dir="${LINUX_SRC_PATH}/tools/perf"

  # Check and install required build dependencies only if missing
  local packages="glibc-static flex bison elfutils-libelf-devel openssl-devel dwarves libtraceevent-devel libcap-devel"
  local missing_packages=""

  for pkg in $packages; do
    if ! rpm -q "$pkg" &>/dev/null; then
      missing_packages="$missing_packages $pkg"
    fi
  done

  if [ -n "$missing_packages" ]; then
    echo "  → Installing missing packages:$missing_packages" | tee -a "${perf_log}"
    if ! echo "${HOST_USER_PWD}" | sudo -S yum install -y $missing_packages >> "${perf_log}" 2>&1; then
      fail "build_perf" "Failed to install perf dependencies (see ${perf_log})"
      echo ""
      return
    fi
  else
    echo "  → All perf dependencies already satisfied." | tee -a "${perf_log}"
  fi

  # Verify tools/perf directory exists
  if [ ! -d "${perf_dir}" ]; then
    fail "build_perf" "tools/perf directory not found: ${perf_dir}"
    echo ""
    return
  fi

  echo "  → Building perf..." | tee -a "${perf_log}"
  cd "${perf_dir}"

  if make -j"${BUILD_THREADS}" -s >> "${perf_log}" 2>&1; then
    pass "build_perf"
  else
    fail "build_perf" "perf build failed (see ${perf_log})"
  fi

  echo ""
}

test_check_dmesg() {
  echo -e "${BLUE}Test-10: check_dmesg${NC}"

  # Theirs is one of the three cases their anck-ci-test suite reports,
  # and it reads the log of the running kernel -- so it only means
  # anything on the VM, after the series' RPM is installed and booted.
  # The case script runs their suite there and hands back this row.
  local dmesg_log="${LOGS_DIR}/check_dmesg.log"

  bash "${SCRIPT_DIR}/cases/anck_ci_test.sh" check_dmesg \
    > "${dmesg_log}" 2>&1
  case $? in
    0) pass "check_dmesg" ;;
    3) skip "check_dmesg" "$(tail -n 4 "${dmesg_log}")" ;;
    5) warn "check_dmesg" "Their check flagged the boot log (see ${dmesg_log})" ;;
    1) fail "check_dmesg" "Errors in the boot log of the booted kernel (see ${dmesg_log})" ;;
    *) fail "check_dmesg" "Their suite could not run (see ${dmesg_log})" ;;
  esac

  echo ""
}

# ---- TEST EXECUTION ----
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
