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

# KABI kernel submodule directory
KABI_KERNEL_DIR="${SCRIPT_DIR}/kernel"
KABI_BRANCH="openEuler-24.03-LTS-Next"

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
  echo -e "${CYAN}╔═════════════════════════════════╗${NC}"
  echo -e "${CYAN}║   openEuler - Available Tests   ║${NC}"
  echo -e "${CYAN}╚═════════════════════════════════╝${NC}"
  echo ""
  echo -e "${GREEN}Test Name              Description${NC}"
  echo -e "${GREEN}─────────────────────────────────────────────────────────${NC}"
  echo -e "${CYAN}openEuler's own gate, run from their code:${NC}"
  echo -e "  1. oe_checkpatch       checkpatch.pl, skipping clean backports"
  echo -e "  2. oe_checkformat      Commit message headers"
  echo -e "  3. oe_checkdepend      Upstream Fixes: closure"
  echo -e "  4. oe_checkkabi        KABI keywords in message and diff"
  echo -e "  5. oe_checkconflict    Backports that diverge must say Conflicts:"
  echo -e "  6. oe_checkbinary      Binary files added by the series"
  echo ""
  echo -e "${CYAN}Ours:${NC}"
  echo -e "  7. build_allmod        Build with allmodconfig"
  echo -e "  8. check_kabi          KABI whitelist against Module.symvers"
  echo -e "  9. rpm_build           Build kernel RPM packages"
  echo -e " 10. boot_kernel         Boot VM with built kernel"
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

# Common function to build kernel with given config target
run_kernel_build() {
  local test_name="$1"
  local config_target="$2"
  cd "${LINUX_SRC_PATH}"

  make clean > /dev/null 2>&1
  echo "  → Building kernel with ${config_target}..."
  if make "${config_target}" > "${LOGS_DIR}/${test_name}.log" 2>&1 \
    && make -j"${BUILD_THREADS}" >> "${LOGS_DIR}/${test_name}.log" 2>&1 \
    && make modules -j"${BUILD_THREADS}" >> "${LOGS_DIR}/${test_name}.log" 2>&1; then
    pass "${test_name}"
  else
    fail "${test_name}" "Build failed (see ${LOGS_DIR}/${test_name}.log)"
  fi
  echo ""
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
    --branch "${OE_TARGET_BRANCH:-master}" \
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

test_build_allmod() {
  echo -e "${BLUE}Test-2: build_allmod${NC}"
  run_kernel_build "build_allmod" "allmodconfig"
}

test_check_kabi() {
  echo -e "${BLUE}Test-3: check_kabi${NC}"

  local kabi_log="${LOGS_DIR}/check_kabi.log"
  local symvers="${LINUX_SRC_PATH}/Module.symvers"

  local kabi_build_log="${LOGS_DIR}/kabi_build.log"
  {
    echo "KABI pre-build log"
    echo "Date: $(date)"
    echo ""
  } > "${kabi_build_log}"

  # Ensure submodules are initialized
  if [ ! "$(ls "${KABI_KERNEL_DIR}" 2>/dev/null)" ]; then
    echo "Initializing and updating submodules..." >> "${kabi_build_log}"
    git -C "${WORKDIR}" submodule update --init --recursive >> "${kabi_build_log}" 2>&1
    if [ $? -ne 0 ]; then
	fail "check_kabi" "Failed to init/update submodules"
	return
    fi
  fi

  # Update submodules
  echo "Updating submodules..." >> "${kabi_build_log}"
  git -C "${WORKDIR}" submodule update --remote --recursive >> "${kabi_build_log}"

  # ---- Ensure kernel submodule is on the correct branch ----
  if [ ! -d "${KABI_KERNEL_DIR}/.git" ] && [ ! -f "${KABI_KERNEL_DIR}/.git" ]; then
    fail "check_kabi" "kernel submodule not found at ${KABI_KERNEL_DIR}."
    echo ""
    return
  fi

  local current_branch
  current_branch=$(git -C "${KABI_KERNEL_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "DETACHED")

  if [ "${current_branch}" != "${KABI_BRANCH}" ]; then
    echo "  → kernel submodule is on '${current_branch}', switching to '${KABI_BRANCH}'..." >> "${kabi_build_log}"
    if ! git -C "${KABI_KERNEL_DIR}" checkout "${KABI_BRANCH}" > /dev/null 2>&1; then
      if git -C "${KABI_KERNEL_DIR}" fetch origin "${KABI_BRANCH}" > /dev/null 2>&1 \
        && git -C "${KABI_KERNEL_DIR}" checkout "${KABI_BRANCH}" > /dev/null 2>&1; then
        echo "  → Switched to '${KABI_BRANCH}' (fetched from remote)" >> "${kabi_build_log}"
      else
        fail "check_kabi" "Failed to checkout branch '${KABI_BRANCH}' in kernel submodule"
        echo ""
        return
      fi
    else
      echo "  → Switched to '${KABI_BRANCH}'" >> "${kabi_build_log}"
    fi
  else
    echo "  → kernel submodule is on correct branch: ${KABI_BRANCH}" >> "${kabi_build_log}"
  fi

  # ---- Select whitelist file based on arch ----
  local kabi_ref_file
  if [ "$(arch)" == "x86_64" ]; then
    kabi_ref_file="${KABI_KERNEL_DIR}/Module.kabi_ext2_x86_64"
  elif [ "$(arch)" == "aarch64" ]; then
    kabi_ref_file="${KABI_KERNEL_DIR}/Module.kabi_ext2_aarch64"
  else
    fail "check_kabi" "Unsupported architecture: $(arch)"
    echo ""
    return
  fi

  # ---- Verify whitelist exists ----
  if [ ! -f "${kabi_ref_file}" ]; then
    fail "check_kabi" "KABI whitelist not found: ${kabi_ref_file}"
    echo ""
    return
  fi

  local symbol_count
  symbol_count=$(grep -c $'\t' "${kabi_ref_file}" 2>/dev/null || echo 0)
  echo "  → Whitelist : $(basename "${kabi_ref_file}") (${symbol_count} symbols)" >> "${kabi_build_log}"

  # ---- Build kernel to produce a fresh Module.symvers ----
  echo "  → Building kernel to generate Module.symvers..." | tee -a "${kabi_build_log}"
  cd "${LINUX_SRC_PATH}"

  echo "  → make mrproper..." >> "${kabi_build_log}"
  if ! make mrproper >> "${kabi_build_log}" 2>&1; then
    fail "check_kabi" "make mrproper failed (see ${kabi_build_log})"
    echo ""
    return
  fi

  echo "  → make openeuler_defconfig..." >> "${kabi_build_log}"
  if ! make openeuler_defconfig >> "${kabi_build_log}" 2>&1; then
    fail "check_kabi" "make openeuler_defconfig failed (see ${kabi_build_log})"
    echo ""
    return
  fi

  echo "  → make -j${BUILD_THREADS}..." >> "${kabi_build_log}"
  if ! make -j"${BUILD_THREADS}" >> "${kabi_build_log}" 2>&1; then
    fail "check_kabi" "kernel build failed (see ${kabi_build_log})"
    echo ""
    return
  fi

  if [ ! -f "${symvers}" ]; then
    fail "check_kabi" "Module.symvers not produced after build (see ${kabi_build_log})"
    echo ""
    return
  fi

  echo "  → Symvers   : ${symvers}" >> "${kabi_build_log}"

  # ---- Write log header ----
  {
    echo "openEuler KABI Whitelist Check"
    echo "Date     : $(date)"
    echo "Branch   : ${KABI_BRANCH}"
    echo "Arch     : $(arch)"
    echo "Whitelist: ${kabi_ref_file}"
    echo "Symvers  : ${symvers}"
    echo ""
  } > "${kabi_log}"

  # ---- Run the comparison ----
  local kabi_output
  kabi_output=$(python3 - "${symvers}" "${kabi_ref_file}" <<'PYEOF'
import sys

def load_symfile(path):
    fields_map = {}
    line_map = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            symbol = parts[1]
            fields_map[symbol] = parts
            line_map[symbol] = line
    return fields_map, line_map

def compare(sym_fields, sym_lines, ref_fields, ref_lines):
    changed, moved, lost = [], [], []
    for symbol, ref_parts in ref_fields.items():
        ref_hash   = ref_parts[0]
        ref_module = ref_parts[2] if len(ref_parts) >= 3 else ""
        if symbol in sym_fields:
            sym_parts  = sym_fields[symbol]
            sym_hash   = sym_parts[0]
            sym_module = sym_parts[2] if len(sym_parts) >= 3 else ""
            if ref_hash != sym_hash:
                changed.append(symbol)
            if ref_module != sym_module:
                moved.append(symbol)
        else:
            lost.append(symbol)
    return changed, moved, lost

sym_fields, sym_lines = load_symfile(sys.argv[1])
ref_fields, ref_lines = load_symfile(sys.argv[2])

changed, moved, lost = compare(sym_fields, sym_lines, ref_fields, ref_lines)

if changed:
    print(f"*** ERROR - ABI BREAKAGE WAS DETECTED ***")
    print(f"The following {len(changed)} whitelisted symbol(s) have a changed CRC:")
    print("  [ current Module.symvers ]")
    for s in changed:
        print("    " + sym_lines.get(s, "<missing>"))
    print("  [ reference whitelist ]")
    for s in changed:
        print("    " + ref_lines.get(s, "<missing>"))
    print()

if lost:
    print(f"*** ERROR - ABI BREAKAGE WAS DETECTED ***")
    print(f"The following {len(lost)} whitelisted symbol(s) are missing from the build:")
    for s in lost:
        print("    " + ref_lines.get(s, "<missing>"))
    print()

if moved:
    print(f"*** WARNING - ABI SYMBOLS MOVED ***")
    print(f"The following {len(moved)} whitelisted symbol(s) moved to a different module:")
    print("  [ current Module.symvers ]")
    for s in moved:
        print("    " + sym_lines.get(s, "<missing>"))
    print("  [ reference whitelist ]")
    for s in moved:
        print("    " + ref_lines.get(s, "<missing>"))
    print()

if not changed and not lost and not moved:
    print(f"All {len(ref_fields)} whitelisted symbols OK.")

if changed or lost:
    sys.exit(1)
elif moved:
    sys.exit(2)
else:
    sys.exit(0)
PYEOF
  )
  local py_exit=$?

  echo "${kabi_output}" >> "${kabi_log}"

  case ${py_exit} in
    0)
      echo "RESULT: PASSED" >> "${kabi_log}"
      pass "check_kabi"
      ;;
    2)
      echo "RESULT: WARN (symbols moved)" >> "${kabi_log}"
      echo -e "  ${YELLOW}→ Some whitelisted symbols moved modules (see ${kabi_log})${NC}"
      pass "check_kabi"
      ;;
    *)
      echo "RESULT: FAILED" >> "${kabi_log}"
      echo ""
      echo "${kabi_output}" | grep -A3 "ERROR\|lost\|changed" | head -30 | sed 's/^/  /'
      echo ""
      fail "check_kabi" "KABI whitelist breakage detected (see ${kabi_log})"
      ;;
  esac

  echo ""
}

test_rpm_build() {
  echo -e "${BLUE}Test-6: rpm_build${NC}"

  cd "${LINUX_SRC_PATH}"

  local rpm_log="${LOGS_DIR}/rpm_build.log"
  local rpms_dir="$HOME/rpmbuild/RPMS/x86_64"

  > "${rpm_log}"

  echo "  → Cleaning source tree..." >> "${rpm_log}"
  if ! make distclean >> "${rpm_log}" 2>&1; then
    fail "rpm_build" "Failed to clean source tree (see ${rpm_log})"
    echo ""
    return
  fi

  echo "  → Configuring kernel with openeuler_defconfig..." >> "${rpm_log}"
  if ! make openeuler_defconfig >> "${rpm_log}" 2>&1; then
    fail "rpm_build" "Failed to configure kernel (see ${rpm_log})"
    echo ""
    return
  fi

  echo "  → Building RPM packages..." | tee -a "${rpm_log}"
  if ! make -j"${BUILD_THREADS}" rpm-pkg >> "${rpm_log}" 2>&1; then
    fail "rpm_build" "Failed to build RPM packages (see ${rpm_log})"
    echo ""
    return
  fi

  # Check if RPMs were created
  echo "  → Checking for generated RPMs..." >> "${rpm_log}"

  if [ ! -d "${rpms_dir}" ]; then
    fail "rpm_build" "RPMs directory not found: ${rpms_dir}"
    echo ""
    return
  fi

  # Find kernel and headers RPM
  local kernel_rpm=$(find "${rpms_dir}" -name "kernel-[0-9]*.rpm" ! -name "*headers*" -type f | head -n 1)
  local headers_rpm=$(find "${rpms_dir}" -name "kernel-headers-*.rpm" -type f | head -n 1)

  local rpm_count=0

  if [ -n "${kernel_rpm}" ]; then
    echo "  → Found kernel RPM: $(basename ${kernel_rpm})" >> "${rpm_log}"
    rpm_count=$((rpm_count + 1))
  else
    echo "  → Kernel RPM not found" >> "${rpm_log}"
  fi

  if [ -n "${headers_rpm}" ]; then
    echo "  → Found headers RPM: $(basename ${headers_rpm})" >> "${rpm_log}"
    rpm_count=$((rpm_count + 1))
  else
    echo "  → Headers RPM not found" >> "${rpm_log}"
  fi

  if [ ${rpm_count} -eq 2 ]; then
    echo "  → RPM build location: ${rpms_dir}" >> "${rpm_log}"
    pass "rpm_build"
  else
    fail "rpm_build" "Expected 2 RPMs (kernel + headers), found ${rpm_count} (see ${rpm_log})"
  fi

  echo ""
}

test_boot_kernel() {
  echo -e "${BLUE}Test-7: boot_kernel${NC}"

  run_boot_test "boot_kernel" \
    "${HOME}/rpmbuild/RPMS/$(arch)" \
    "${LOGS_DIR}/boot_kernel.log"
}

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
    build_allmod)
      test_build_allmod
      ;;
    check_kabi)
      test_check_kabi
      ;;
    rpm_build)
      test_rpm_build
      ;;
    boot_kernel)
      test_boot_kernel
      ;;
    *)
      echo -e "${RED}Error: Unknown test '$SPECIFIC_TEST'${NC}"
      echo ""
      echo "Available tests:"
      for t in oe_checkpatch oe_checkformat oe_checkdepend oe_checkkabi \
               oe_checkconflict oe_checkbinary build_allmod check_kabi \
               rpm_build boot_kernel; do
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
  [ "${TEST_BUILD_ALLMOD:-yes}" == "yes" ] && test_build_allmod
  [ "${TEST_CHECK_KABI:-yes}" == "yes" ] && test_check_kabi
  [ "${TEST_RPM_BUILD:-yes}" == "yes" ] && test_rpm_build
  [ "${TEST_BOOT_KERNEL:-yes}" == "yes" ] && test_boot_kernel
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
