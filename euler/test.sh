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
  echo -e "  1. check_dependency    Check patch dependencies"
  echo -e "  2. build_allmod        Build with allmodconfig"
  echo -e "  3. check_kabi          Check KABI whitelist against Module.symvers"
  echo -e "  4. check_patch         Run checkpatch.pl on patches"
  echo -e "  5. check_format        Validate commit message format"
  echo -e "  6. rpm_build           Build kernel RPM packages"
  echo -e "  7. boot_kernel         Boot VM with built kernel"
  echo ""
  echo -e "${BLUE}Usage:${NC}"
  echo "  $0                     - Run all enabled tests"
  echo "  $0 list/--list/-l      - Show this list"
  echo "  $0 <test_name>         - Run specific test"
  echo ""
  echo -e "${YELLOW}Examples:${NC}"
  echo "  $0 check_dependency"
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

test_check_dependency() {
  echo -e "${BLUE}Test-1: check_dependency${NC}"

  # Ensure Torvalds repo is present and up to date before running the check
  sync_torvalds_repo

  cd "${LINUX_SRC_PATH}"

  # Get list of applied commits (those that are ahead of the reset point)
  local applied_commits=()
  mapfile -t applied_commits < <(git log --format=%H HEAD | head -n "${NUM_PATCHES:-10}")

  if [ ${#applied_commits[@]} -eq 0 ]; then
    skip "check_dependency" "No commits to check"
    echo ""
    return
  fi

  echo "  → Checking ${#applied_commits[@]} commits for dependencies..."

  local commits_file="${SCRIPT_DIR}/.commits.txt"
  local dep_log="${LOGS_DIR}/check_dependency.log"
  local checkdepend_script="${WORKDIR}/lib/checkdepend.py"

  # Check if checkdepend.py exists
  if [ ! -f "${checkdepend_script}" ]; then
    fail "check_dependency" "checkdepend.py not found at ${checkdepend_script}"
    echo ""
    return
  fi

  # Extract upstream commit IDs and save to .commits.txt
  > "${commits_file}"

  for commit in "${applied_commits[@]}"; do
    local commit_body=$(git log -1 --format=%B "${commit}")

    # Extract upstream commit ID (full 40-char hash) from commit message
    local upstream_commit=$(echo "${commit_body}" | grep -oP '(?<=^commit )[a-f0-9]{40}' | head -1)

    if [ -n "${upstream_commit}" ]; then
      # Save the full 40-character hash
      echo "${upstream_commit}" >> "${commits_file}"
    fi
  done

  # Check if we have any commits to check
  local commit_count=$(wc -l < "${commits_file}")
  if [ ${commit_count} -eq 0 ]; then
    skip "check_dependency" "No upstream commit IDs found in patches"
    echo ""
    return
  fi

  # checkdepend.py reports the result in its exit status: 0 clean, 1 missing
  # dependencies, 2 could not run.  Grepping its output for "FAIL" used to
  # match any commit subject that happened to contain the word.
  python3 "${checkdepend_script}" "${LINUX_SRC_PATH}" "${TORVALDS_REPO}" \
    "${commits_file}" --output-dir "${LOGS_DIR}" > "${dep_log}" 2>&1
  case $? in
    0) pass "check_dependency" ;;
    1) fail "check_dependency" "Some commits have unfixed dependencies (see ${dep_log})" ;;
    *) fail "check_dependency" "checkdepend.py could not run (see ${dep_log})" ;;
  esac

  echo ""
}

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

test_check_patch() {
  echo -e "${BLUE}Test-4: check_patch${NC}"
  
  # Check if checkpatch.pl exists
  local CHECKPATCH="${LINUX_SRC_PATH}/scripts/checkpatch.pl"
  if [ ! -f "${CHECKPATCH}" ]; then
    fail "check_patch" "checkpatch.pl not found at ${CHECKPATCH}"
    echo ""
    return
  fi
  
  # Check if patches directory exists
  if [ ! -d "${PATCHES_DIR}" ]; then
    fail "check_patch" "Patches directory not found at ${PATCHES_DIR}"
    echo ""
    return
  fi
  
  # Find all patch files (excluding .bkp directory)
  local patch_files=()
  mapfile -t patch_files < <(find "${PATCHES_DIR}" -maxdepth 1 -name "*.patch" -type f | sort)
  
  if [ ${#patch_files[@]} -eq 0 ]; then
    skip "check_patch" "No patches found in ${PATCHES_DIR}"
    echo ""
    return
  fi
  
  echo "  → Checking ${#patch_files[@]} patches..."
  
  local total_errors=0
  local total_warnings=0
  local failed_patches=0
  local checkpatch_log="${LOGS_DIR}/check_patch.log"
  
  > "${checkpatch_log}"  # Clear log file

  # Define ignore list
  local IGNORES_FOR_MAIN=(
    CONFIG_DESCRIPTION
    FILE_PATH_CHANGES
    GERRIT_CHANGE_ID
    GIT_COMMIT_ID
    UNKNOWN_COMMIT_ID
    FROM_SIGN_OFF_MISMATCH
    REPEATED_WORD
    COMMIT_COMMENT_SYMBOL
    BLOCK_COMMENT_STYLE
    AVOID_EXTERNS
    AVOID_BUG
    NOT_UNIFIED_DIFF
    COMMIT_LOG_LONG_LINE
    SPACING
    LONG_LINE_COMMENT
  )

  # Join array into comma-separated string
  local ignore_str
  ignore_str=$(IFS=, ; echo "${IGNORES_FOR_MAIN[*]}")

  for patch_file in "${patch_files[@]}"; do
    local patch_name=$(basename "${patch_file}")
    echo "    Checking: ${patch_name}" >> "${checkpatch_log}"
    
    # Run checkpatch with ignore list and capture output
    local output=$("${CHECKPATCH}" --show-types --no-tree --ignore "${ignore_str}" "${patch_file}" 2>&1)
    echo "${output}" >> "${checkpatch_log}"
    echo "" >> "${checkpatch_log}"
    
    # Filter out the specific error we want to ignore
    local filtered_output=$(echo "${output}" | grep -v "ERROR: Please use git commit description style")
    
    # Count errors and warnings from filtered output
    local errors=$(echo "${filtered_output}" | grep -c "^ERROR:" || true)
    local warnings=$(echo "${filtered_output}" | grep -c "^WARNING:" || true)
    
    total_errors=$((total_errors + errors))
    total_warnings=$((total_warnings + warnings))
    
    if [ ${errors} -gt 0 ]; then
      failed_patches=$((failed_patches + 1))
      echo "      ${patch_name}: ${errors} error(s), ${warnings} warning(s)" >> "${checkpatch_log}"
    fi
  done
  
  echo "  → Total: ${total_errors} errors, ${total_warnings} warnings across ${#patch_files[@]} patches"
  
  if [ ${total_errors} -gt 0 ]; then
    fail "check_patch" "${failed_patches} patch(es) have errors (see ${checkpatch_log})"
  else
    if [ ${total_warnings} -gt 0 ]; then
      echo -e "  ${YELLOW}→${NC} ${total_warnings} warning(s) found (non-fatal)"
    fi
    pass "check_patch"
  fi
  
  echo ""
}

test_check_format() {
  echo -e "${BLUE}Test-5: check_format${NC}"
  
  cd "${LINUX_SRC_PATH}"
  
  # Get list of applied commits (those that are ahead of the reset point)
  local applied_commits=()
  mapfile -t applied_commits < <(git log --oneline --no-merges HEAD | head -n "${NUM_PATCHES:-10}" | awk '{print $1}')
  
  if [ ${#applied_commits[@]} -eq 0 ]; then
    skip "check_format" "No commits to check"
    echo ""
    return
  fi
  
  echo "  → Checking ${#applied_commits[@]} commits for proper format..."
  
  local format_log="${LOGS_DIR}/check_format.log"
  > "${format_log}"
  
  local format_errors=0
  local expected_sob="Signed-off-by: ${SIGNER_NAME} <${SIGNER_EMAIL}>"
  
  for commit in "${applied_commits[@]}"; do
    local commit_msg=$(git log -1 --format=%B "${commit}")
    local commit_subject=$(git log -1 --format=%s "${commit}")
    
    echo "Checking commit: ${commit} - ${commit_subject}" >> "${format_log}"
    echo "---" >> "${format_log}"
    
    local has_error=0
    
    # Check for mainline inclusion header
    if ! echo "${commit_msg}" | grep -q "^mainline inclusion"; then
      echo "  ✗ Missing 'mainline inclusion' header" >> "${format_log}"
      has_error=1
    fi
    
    # Check for 'from mainline-' line
    if ! echo "${commit_msg}" | grep -q "^from mainline-"; then
      echo "  ✗ Missing 'from mainline-' line" >> "${format_log}"
      has_error=1
    fi
    
    # Check for commit line
    if ! echo "${commit_msg}" | grep -q "^commit [a-f0-9]\{40\}"; then
      echo "  ✗ Missing upstream commit ID" >> "${format_log}"
      has_error=1
    fi
    
    # Check for category line
    if ! echo "${commit_msg}" | grep -q "^category:"; then
      echo "  ✗ Missing 'category:' line" >> "${format_log}"
      has_error=1
    fi
    
    # Check for bugzilla line
    if ! echo "${commit_msg}" | grep -q "^bugzilla: https://atomgit.com/openeuler/kernel/issues/"; then
      echo "  ✗ Missing or incorrect 'bugzilla:' line" >> "${format_log}"
      has_error=1
    fi
    
    # Check for CVE line
    if ! echo "${commit_msg}" | grep -q "^CVE:"; then
      echo "  ✗ Missing 'CVE:' line" >> "${format_log}"
      has_error=1
    fi
    
    # Check for Reference line
    if ! echo "${commit_msg}" | grep -q "^Reference: https://github.com/torvalds/linux/commit/"; then
      echo "  ✗ Missing 'Reference:' line" >> "${format_log}"
      has_error=1
    fi
    
    # Check for separator line
    if ! echo "${commit_msg}" | grep -q "^--------------------------------"; then
      echo "  ✗ Missing separator line '--------------------------------'" >> "${format_log}"
      has_error=1
    fi
    
    # Check for new Signed-off-by line
    if ! echo "${commit_msg}" | grep -q "^${expected_sob}"; then
      echo "  ✗ Missing expected Signed-off-by: ${expected_sob}" >> "${format_log}"
      has_error=1
    else
      # Extract upstream commit ID
      local upstream_commit=$(echo "${commit_msg}" | grep "^commit " | awk '{print $2}')
      
      if [ -n "${upstream_commit}" ]; then
        # Get the last Signed-off-by from upstream commit in Torvalds repo
        cd "${TORVALDS_REPO}"
        local upstream_last_sob=$(git log -1 --format=%B "${upstream_commit}" 2>/dev/null | grep "^Signed-off-by:" | tail -1)
        cd "${LINUX_SRC_PATH}"
        
        # Get all Signed-off-by lines from current commit
        local all_sobs=$(echo "${commit_msg}" | grep "^Signed-off-by:")
        local current_last_sob=$(echo "${all_sobs}" | tail -1)
        
        # Check if the last sob is the same as upstream (which means we didn't add our new one)
        if [ -n "${upstream_last_sob}" ] && [ "${current_last_sob}" == "${upstream_last_sob}" ]; then
          echo "  ✗ New Signed-off-by line not added (last SOB matches upstream)" >> "${format_log}"
          has_error=1
        elif [ "${current_last_sob}" != "${expected_sob}" ]; then
          echo "  ✗ Last Signed-off-by does not match expected: ${expected_sob}" >> "${format_log}"
          echo "    Found: ${current_last_sob}" >> "${format_log}"
          has_error=1
        fi
      fi
    fi
    
    if [ ${has_error} -eq 1 ]; then
      format_errors=$((format_errors + 1))
      echo "  Result: FAIL" >> "${format_log}"
    else
      echo "  Result: PASS" >> "${format_log}"
    fi
    
    echo "" >> "${format_log}"
  done
  
  if [ ${format_errors} -gt 0 ]; then
    fail "check_format" "${format_errors} commit(s) have format errors (see ${format_log})"
  else
    pass "check_format"
  fi
  
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
    check_dependency)
      test_check_dependency
      ;;
    build_allmod)
      test_build_allmod
      ;;
    check_kabi)
      test_check_kabi
      ;;
    check_patch)
      test_check_patch
      ;;
    check_format)
      test_check_format
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
      echo "  - check_dependency"
      echo "  - build_allmod"
      echo "  - check_kabi"
      echo "  - check_patch"
      echo "  - check_format"
      echo "  - rpm_build"
      echo "  - boot_kernel"
      echo ""
      echo "Run '$0 list' for detailed information"
      exit 1
      ;;
  esac
else
  # Run all enabled tests
  [ "${TEST_CHECK_DEPENDENCY:-yes}" == "yes" ] && test_check_dependency
  [ "${TEST_BUILD_ALLMOD:-yes}" == "yes" ] && test_build_allmod
  [ "${TEST_CHECK_KABI:-yes}" == "yes" ] && test_check_kabi
  [ "${TEST_CHECK_PATCH:-yes}" == "yes" ] && test_check_patch
  [ "${TEST_CHECK_FORMAT:-yes}" == "yes" ] && test_check_format
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
