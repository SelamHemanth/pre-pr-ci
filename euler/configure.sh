#!/usr/bin/env bash
set -euo pipefail

# euler/configure.sh - openEuler Configuration Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${SCRIPT_DIR}/.configure"
TORVALDS_REPO="${WORKDIR}/.torvalds-linux"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo ""
echo "╔═══════════════════════════╗"
echo "║  openEuler Configuration  ║"
echo "╚═══════════════════════════╝"
echo ""

# Clone Torvalds repo if not exists
if [ ! -d "$TORVALDS_REPO" ]; then
  echo -e "${BLUE}Cloning Torvalds Linux repository...${NC}"
  git clone --bare https://github.com/torvalds/linux.git "$TORVALDS_REPO" 2>&1 | \
    stdbuf -oL tr '\r' '\n' | \
    grep -oP '\d+(?=%)' | \
    awk '{printf "\rProgress: %d%%", $1; fflush()}' || \
    echo -e "\r${GREEN}Repository cloned successfully${NC}"
  echo ""
else
  echo -e "${GREEN}Torvalds repository already exists${NC}"
  echo -e "${BLUE}Updating repository...${NC}"
  (cd "$TORVALDS_REPO" && git fetch --all --tags 2>&1 | grep -v "^From" || true)
  echo -e "${GREEN}Repository updated${NC}"
fi

# General Configuration
echo ""
echo "=== General Configuration ==="
read -r -p "Linux source code path: " linux_src
LINUX_SRC_PATH="${linux_src:-/home/amd/linux}"

read -r -p "Signed-off-by name: " signer_name
SIGNER_NAME="${signer_name:-Hemanth Selam}"

read -r -p "Signed-off-by email: " signer_email
SIGNER_EMAIL="${signer_email:-Hemanth.Selam@amd.com}"

read -r -p "Bugzilla ID: " bugzilla_id
BUGZILLA_ID="${bugzilla_id:-ID0OQX}"

echo ""
echo "Available patch categories:"
echo "  1) feature"
echo "  2) bugfix"
echo "  3) performance"
echo "  4) security"
echo ""
read -r -p "Select patch category [1-4] (default: 1): " category_choice
case "${category_choice:-1}" in
  1) PATCH_CATEGORY="feature" ;;
  2) PATCH_CATEGORY="bugfix" ;;
  3) PATCH_CATEGORY="performance" ;;
  4) PATCH_CATEGORY="security" ;;
  *) PATCH_CATEGORY="feature" ;;
esac

read -r -p "Number of patches to apply: " num_patches
NUM_PATCHES="${num_patches:-0}"

# Build Configuration
echo ""
echo "=== Build Configuration ==="
read -r -p "Number of build threads [$(nproc)]: " build_threads
BUILD_THREADS="${build_threads:-$(nproc)}"

# Test Configuration
echo ""
echo "=== Test Configuration ==="
echo ""
echo "Available tests:"
echo "  1) check_licence              - Validate license headers"
echo "  2) build_oedefconfig          - Build with openeuler_defconfig"
echo "  3) build_allmod               - Build with allmodconfig"
echo "  4) check_patch                - Run checkpatch.pl validation"
echo "  5) check_format               - Check code formatting"
echo "  6) check_kabi                 - Check KABI compatibility"
echo "  7) rpm_build                  - Build openEuler RPM packages"
echo "  8) boot_kernel                - Boot test (requires remote setup)"
echo ""

read -r -p "Select tests to run (comma-separated, 'all', or 'none') [all]: " test_selection
TEST_SELECTION="${test_selection:-all}"

# Parse test selection
if [ "$TEST_SELECTION" == "all" ] || [ -z "$TEST_SELECTION" ]; then
  RUN_TESTS="yes"
  TEST_CHECK_LICENCE="yes"
  TEST_BUILD_OEDEFCONFIG="yes"
  TEST_BUILD_ALLMOD="yes"
  TEST_CHECK_PATCH="yes"
  TEST_CHECK_FORMAT="yes"
  TEST_CHECK_KABI="yes"
  TEST_RPM_BUILD="yes"
  TEST_BOOT_KERNEL="yes"
elif [ "$TEST_SELECTION" == "none" ]; then
  RUN_TESTS="no"
  TEST_CHECK_LICENCE="no"
  TEST_BUILD_OEDEFCONFIG="no"
  TEST_BUILD_ALLMOD="no"
  TEST_CHECK_PATCH="no"
  TEST_CHECK_FORMAT="no"
  TEST_CHECK_KABI="no"
  TEST_RPM_BUILD="no"
  TEST_BOOT_KERNEL="no"
else
  RUN_TESTS="yes"
  TEST_CHECK_LICENCE="no"
  TEST_BUILD_OEDEFCONFIG="no"
  TEST_BUILD_ALLMOD="no"
  TEST_CHECK_PATCH="no"
  TEST_CHECK_FORMAT="no"
  TEST_CHECK_KABI="no"
  TEST_RPM_BUILD="no"
  TEST_BOOT_KERNEL="no"

  # Parse comma-separated selections
  IFS=',' read -ra SELECTED <<< "$TEST_SELECTION"
  for test_num in "${SELECTED[@]}"; do
    case "${test_num// /}" in
      1) TEST_CHECK_LICENCE="yes" ;;
      2) TEST_BUILD_OEDEFCONFIG="yes" ;;
      3) TEST_BUILD_ALLMOD="yes" ;;
      4) TEST_CHECK_PATCH="yes" ;;
      5) TEST_CHECK_FORMAT="yes" ;;
      6) TEST_CHECK_KABI="yes" ;;
      7) TEST_RPM_BUILD="yes" ;;
      8) TEST_BOOT_KERNEL="yes" ;;
    esac
  done
fi

# Initialize optional variables
VM_IP=""
VM_ROOT_PWD=""
HOST_USER_PWD=""

# VM Configuration for Boot Test
if [[ "$TEST_BOOT_KERNEL" == "yes" ]]; then
  echo ""
  echo "=== VM Boot Test Configuration ==="
  read -r -p "VM IP address: " vm_ip
  VM_IP="${vm_ip}"
  read -r -s -p "VM root password: " vm_root_pwd
  VM_ROOT_PWD="${vm_root_pwd}"
  echo ""
fi

# Host sudo password (for RPM build dependencies)
if [[ "$TEST_RPM_BUILD" == "yes" ]]; then
  echo ""
  echo "=== Host Configuration ==="
  read -r -s -p "Host sudo password (for installing dependencies): " host_user_pwd
  HOST_USER_PWD="${host_user_pwd}"
  echo ""
fi

# Write configuration file
cat > "$CONFIG_FILE" <<EOF
# openEuler Configuration
# Generated: $(date)

# General Configuration
LINUX_SRC_PATH="${LINUX_SRC_PATH}"
SIGNER_NAME="${SIGNER_NAME}"
SIGNER_EMAIL="${SIGNER_EMAIL}"
BUGZILLA_ID="${BUGZILLA_ID}"
PATCH_CATEGORY="${PATCH_CATEGORY}"
NUM_PATCHES="${NUM_PATCHES}"

# Build Configuration
BUILD_THREADS="${BUILD_THREADS}"

# Test Configuration
RUN_TESTS="${RUN_TESTS}"
TEST_CHECK_LICENCE="${TEST_CHECK_LICENCE}"
TEST_BUILD_OEDEFCONFIG="${TEST_BUILD_OEDEFCONFIG}"
TEST_BUILD_ALLMOD="${TEST_BUILD_ALLMOD}"
TEST_CHECK_PATCH="${TEST_CHECK_PATCH}"
TEST_CHECK_FORMAT="${TEST_CHECK_FORMAT}"
TEST_CHECK_KABI="${TEST_CHECK_KABI}"
TEST_RPM_BUILD="${TEST_RPM_BUILD}"
TEST_BOOT_KERNEL="${TEST_BOOT_KERNEL}"

# Host Configuration
HOST_USER_PWD='${HOST_USER_PWD}'

# VM Configuration
VM_IP="${VM_IP}"
VM_ROOT_PWD='${VM_ROOT_PWD}'

# Repository Configuration
TORVALDS_REPO="${TORVALDS_REPO}"
EOF

echo ""
echo "╔═══════════════════════╗"
echo "║  Configuration Saved  ║"
echo "╚═══════════════════════╝"
echo ""
echo "Configuration saved to: ${CONFIG_FILE}"
echo ""
echo "Linux source: ${LINUX_SRC_PATH}"
echo "Patches to process: ${NUM_PATCHES}"
echo "Patch category: ${PATCH_CATEGORY}"
echo "Build threads: ${BUILD_THREADS}"
echo "Tests enabled: ${RUN_TESTS}"
echo ""
echo -e "${BLUE}Next: Run 'make build' to build${NC}"
echo ""
