#!/usr/bin/env bash
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

# Export the variables so subshells can use them
export HOST_USER_PWD
export VM_IP
export VM_ROOT_PWD

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Function to list available tests
list_tests() {
  echo ""
  echo -e "${CYAN}╔═════════════════════════════════════╗${NC}"
  echo -e "${CYAN}║     OpenAnolis - Available Tests    ║${NC}"
  echo -e "${CYAN}╚═════════════════════════════════════╝${NC}"
  echo ""
  echo -e "${GREEN}Test Name                    Description${NC}"
  echo -e "${GREEN}─────────────────────────────────────────────────────────${NC}"
  echo -e "  1. check_kconfig           Validate kernel configuration"
  echo -e "  2. build_allyes_config     Build with allyesconfig"
  echo -e "  3. build_allno_config      Build with allnoconfig"
  echo -e "  4. build_anolis_defconfig  Build with anolis_defconfig"
  echo -e "  5. build_anolis_debug      Build with anolis-debug_defconfig"
  echo -e "  6. anck_rpm_build          Build ANCK RPM packages"
  echo -e "  7. check_kapi              Check kernel ABI compatibility"
  echo -e "  8. boot_kernel_rpm         Boot VM with built kernel RPM"
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

mkdir -p "${LOGS_DIR}"


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
    && make -j"$(nproc)" >> "${LOGS_DIR}/${test_name}.log" 2>&1 \
    && make modules -j"$(nproc)" >> "${LOGS_DIR}/${test_name}.log" 2>&1; then
    pass "${test_name}"
  else
    fail "${test_name}" "Build failed (see ${LOGS_DIR}/${test_name}.log)"
  fi
  echo ""
}

# ---- TEST DEFINITIONS ----

test_check_kconfig() {
  echo -e "${BLUE}Test-1: check_Kconfig${NC}"
  cd "${LINUX_SRC_PATH}/anolis" 2>/dev/null || {
    skip "check_Kconfig" "anolis/ directory not found"
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
    pass "check_Kconfig"
  else
    fail "check_Kconfig" "dist-configs-check failed (see ${LOGS_DIR}/check_Kconfig.log)"
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
  run_kernel_build "build_anolis_debug_defconfig" "anolis-debug_defconfig"
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
    echo "${HOST_USER_PWD}" | sudo -S yum install -y $missing_packages >> "${LOGS_DIR}/anck_rpm_build.log" 2>&1 || true
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
    echo "${HOST_USER_PWD}" | sudo -S yum-builddep -y output/kernel.spec >> "${LOGS_DIR}/anck_rpm_build.log" 2>&1 || true
    touch "${outputdir}/.deps_installed"
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
     make dist-rpms RPMBUILDOPTS="--define '%_smp_mflags -j16'" \
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

  local rpms_dir="${LINUX_SRC_PATH}/anolis/outputs/rpmbuild/RPMS/x86_64"
  local boot_log="${LOGS_DIR}/boot_kernel_rpm.log"

  # Check if RPMs exist
  if [ ! -d "${rpms_dir}" ]; then
    fail "boot_kernel_rpm" "RPMs directory not found: ${rpms_dir}"
    echo ""
    return
  fi

  echo "  → VM booting with build RPM..."

  # Find kernel RPM (not debuginfo, not devel, not headers)
  local kernel_rpm=$(find "${rpms_dir}" -name "kernel-*.rpm" ! -name "*debuginfo*" ! -name "*devel*" ! -name "*headers*" -type f | head -n 1)

  if [ -z "${kernel_rpm}" ]; then
    fail "boot_kernel_rpm" "Kernel RPM not found in ${rpms_dir}"
    echo ""
    return
  fi

  echo "  → Found kernel RPM: $(basename ${kernel_rpm})" >> "${boot_log}"

  # Check VM connectivity
  echo "  → Checking VM connectivity (${VM_IP})..." >> "${boot_log}"
  if ! ping -c 2 "${VM_IP}" >> "${boot_log}" 2>&1; then
    fail "boot_kernel_rpm" "VM ${VM_IP} is not reachable"
    echo ""
    return
  fi
  echo "  → VM is reachable" >> "${boot_log}"

  # Install sshpass if not available (for password authentication)
  if ! command -v sshpass &> /dev/null; then
    echo "  → Installing sshpass..." >> "${boot_log}"
    if ! echo "${HOST_USER_PWD}" | sudo -S yum install -y sshpass >> "${boot_log}" 2>&1; then
	    echo "  → yum install failed, trying manual build..." >> "${boot_log}"
	    (
	    cd /tmp || exit 1
	    wget https://sourceforge.net/projects/sshpass/files/latest/download -O sshpass.tar.gz >> "${boot_log}" 2>&1
	    tar -xzf sshpass.tar.gz >> "${boot_log}" 2>&1
	    cd sshpass-* || exit 1
	    ./configure >> "${boot_log}" 2>&1
	    make >> "${boot_log}" 2>&1
	    echo "${HOST_USER_PWD}" | sudo -S make install >> "${boot_log}" 2>&1
    ) || {
	    fail "boot_kernel_rpm" "Failed to install sshpass manually"
		echo ""
		return
	}
    fi
  fi

  # Copy kernel RPM to VM
  echo "  → Copying kernel RPM to VM..." >> "${boot_log}"
  if ! sshpass -p "${VM_ROOT_PWD}" scp -o StrictHostKeyChecking=no "${kernel_rpm}" root@"${VM_IP}":/tmp/ >> "${boot_log}" 2>&1; then
    fail "boot_kernel_rpm" "Failed to copy RPM to VM"
    echo ""
    return
  fi
  echo "  → RPM copied successfully" >> "${boot_log}"

  local rpm_name=$(basename "${kernel_rpm}")

  # Install kernel RPM on VM
  echo "  → Installing kernel RPM on VM..." >> "${boot_log}"
  if ! sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no root@"${VM_IP}" "rpm -ivh --force /tmp/${rpm_name}" >> "${boot_log}" 2>&1; then
    fail "boot_kernel_rpm" "Failed to install kernel RPM"
    echo ""
    return
  fi
  echo "  → Kernel installed successfully" >> "${boot_log}"

  # Extract kernel version from RPM name
  local kernel_version=$(echo "${rpm_name}" | sed 's/kernel-//' | sed 's/.rpm$//')
  local vmlinuz_path="/boot/vmlinuz-${kernel_version}"
  echo "  → Expected kernel version: ${kernel_version}" >> "${boot_log}"
  echo "  → Expected vmlinuz path: ${vmlinuz_path}" >> "${boot_log}"

  # Verify kernel was installed
  echo "  → Verifying kernel installation..." >> "${boot_log}"
  if ! sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no root@"${VM_IP}" "test -f ${vmlinuz_path}" >> "${boot_log}" 2>&1; then
    fail "boot_kernel_rpm" "Kernel image not found at ${vmlinuz_path}"
    echo ""
    return
  fi

  # List all available kernels
  echo "  → Available kernels before setting default:" >> "${boot_log}"
  sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no root@"${VM_IP}" "grubby --info ALL | grep -E '^kernel='" >> "${boot_log}" 2>&1

  # Set new kernel as default using grubby
  echo "  → Setting new kernel as default boot option using grubby..." >> "${boot_log}"
  if ! sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no root@"${VM_IP}" "grubby --set-default=${vmlinuz_path}" >> "${boot_log}" 2>&1; then
    fail "boot_kernel_rpm" "Failed to set default kernel with grubby"
    echo ""
    return
  fi

  # Verify default kernel was set
  echo "  → Verifying default kernel setting..." >> "${boot_log}"
  local default_kernel=$(sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no root@"${VM_IP}" "grubby --default-kernel" 2>> "${boot_log}")
  echo "  → Default kernel set to: ${default_kernel}" >> "${boot_log}"

  if [[ "${default_kernel}" != "${vmlinuz_path}" ]]; then
    fail "boot_kernel_rpm" "Failed to set default kernel. Expected: ${vmlinuz_path}, Got: ${default_kernel}"
    echo ""
    return
  fi

  # Reboot VM
  echo "  → Rebooting VM..." >> "${boot_log}"
  sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no root@"${VM_IP}" "reboot" >> "${boot_log}" 2>&1 || true

  # Wait for VM to go down
  echo "  → Waiting for VM to shutdown..." >> "${boot_log}"
  sleep 10

  # Wait for VM to come back up (max 5 minutes)
  echo "  → Waiting for VM to boot up (max 5 minutes)..." >> "${boot_log}"
  local wait_count=0
  local max_wait=60  # 60 * 5 seconds = 5 minutes

  while [ $wait_count -lt $max_wait ]; do
    if ping -c 1 -W 1 "${VM_IP}" >> "${boot_log}" 2>&1; then
      sleep 10  # Wait a bit more for SSH to be ready
      if sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@"${VM_IP}" "echo 'VM is up'" >> "${boot_log}" 2>&1; then
        echo "  → VM booted successfully" >> "${boot_log}"
        break
      fi
    fi
    sleep 5
    wait_count=$((wait_count + 1))
  done

  if [ $wait_count -ge $max_wait ]; then
    fail "boot_kernel_rpm" "VM did not boot within 5 minutes"
    echo ""
    return
  fi

  # Check running kernel version
  echo "  → Checking running kernel version..." >> "${boot_log}"
  local running_kernel=$(sshpass -p "${VM_ROOT_PWD}" ssh -o StrictHostKeyChecking=no root@"${VM_IP}" "uname -r" 2>> "${boot_log}")

  echo "  → Running kernel: ${running_kernel}" >> "${boot_log}"
  echo "  → Expected kernel: ${kernel_version}" >> "${boot_log}"

  # Verify if the installed kernel is running (exact match)
  if [[ "${running_kernel}" == "${kernel_version}" ]]; then
    echo -e "  → VM booted with new kernel: ${running_kernel}" >> "${boot_log}"
    pass "boot_kernel_rpm"
  else
    fail "boot_kernel_rpm" "VM booted with different kernel. Expected: ${kernel_version}, Got: ${running_kernel}"
  fi

  echo ""
}

test_check_kapi() {
  echo -e "${BLUE}Test-7: check_kapi${NC}"

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

  # Clean tools and previous outputs
  rm -f "${KAPI_DIFF_OUTPUT}" "${KAPI_WITH_BP}" "${KAPI_WITHOUT_BP}"
  rm -rf "${KAPI_OP_DIR}" "${KABI_DW_DIR}" "${KABI_WHITELIST_DIR}"

  echo "  → Checking KAPI..." > "$KAPI_LOG"

  # Clone kabi-dw repo
  echo "  → Cloning kabi-dw repository..." >> "$KAPI_LOG"
  if ! git clone https://gitee.com/anolis/kabi-dw.git "${KABI_DW_DIR}" >> "${KAPI_LOG}" 2>&1; then
    fail "check_kapi" "Failed to clone kabi-dw repository"
    return
  fi

  # Clone kabi-whitelist repository
  echo "  → Cloning kabi-whitelist repository (branch: ${KABI_BRANCH})..." >> "$KAPI_LOG"
  if ! git clone --depth 1 -b "${KABI_BRANCH}" https://gitee.com/anolis/kabi-whitelist.git "${KABI_WHITELIST_DIR}" >> "${KAPI_LOG}" 2>&1; then
    fail "check_kapi" "Failed to clone kabi-whitelist repository (branch ${KABI_BRANCH} may not exist)"
    return
  fi

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

  # Get current HEAD commit ID
  cd "${LINUX_SRC_PATH}"
  local HEAD_SHAID=$(git log --oneline -1 | awk '{print $1}')
  if [ -z "${HEAD_SHAID}" ]; then
    fail "check_kapi" "Failed to get HEAD commit ID"
    return
  fi

  echo "  → Generating KAPI symbols..."

  # Reset to base (without backport patches)
  echo "  → Building kernel without backport patches..." >> "$KAPI_LOG"
  git reset --hard HEAD~${NUM_PATCHES} >> "${KAPI_LOG}" 2>&1
  make mrproper >> "${KAPI_LOG}" 2>&1
  make anolis_defconfig >> "${KAPI_LOG}" 2>&1

  if ! make -j"$(nproc)" >> "${KAPI_LOG}" 2>&1; then
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
  git reset --hard ${HEAD_SHAID} >> "${KAPI_LOG}" 2>&1
  make mrproper >> "${KAPI_LOG}" 2>&1
  make anolis_defconfig >> "${KAPI_LOG}" 2>&1

  if ! make -j"$(nproc)" >> "${KAPI_LOG}" 2>&1; then
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
    check_kapi)
      test_check_kapi
      ;;
    boot_kernel_rpm)
      test_boot_kernel_rpm
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
      echo "  - check_kapi"
      echo "  - boot_kernel_rpm"
      echo ""
      echo "Run '$0 list' for detailed information"
      exit 1
      ;;
  esac
else
  # Run all enabled tests
  [ "${TEST_CHECK_KCONFIG:-yes}" == "yes" ] && test_check_kconfig
  [ "${TEST_BUILD_ALLYES:-yes}" == "yes" ] && test_build_allyes_config
  [ "${TEST_BUILD_ALLNO:-yes}" == "yes" ] && test_build_allno_config
  [ "${TEST_BUILD_DEFCONFIG:-yes}" == "yes" ] && test_build_anolis_defconfig
  [ "${TEST_BUILD_DEBUG:-yes}" == "yes" ] && test_build_anolis_debug_defconfig
  [ "${TEST_RPM_BUILD:-yes}" == "yes" ] && test_anck_rpm_build
  [ "${TEST_CHECK_KAPI:-yes}" == "yes" ] && test_check_kapi
  [ "${TEST_BOOT_KERNEL:-yes}" == "yes" ] && test_boot_kernel_rpm
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
