#!/usr/bin/env bash
set -euo pipefail

# euler/configure.sh - openEuler Configuration Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${SCRIPT_DIR}/.configure"
TORVALDS_REPO="${WORKDIR}/.torvalds-linux"
LOGS_DIR="${WORKDIR}/logs"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

fail() {
	local stage="$1"
	local msg="$2"
	echo -e "${RED}[FAIL]${NC} ${stage}: ${msg}"
}

# Update only test configuration
update_tests() {

	# Load existing config if present
	if [[ -f "${CONFIG_FILE}" ]]; then
		# shellcheck disable=SC1090
		source "${CONFIG_FILE}"
	fi

	# Default: disable all tests
	RUN_TESTS="no"
	TEST_CHECK_DEPENDENCY="no"
	TEST_BUILD_ALLMOD="no"
	TEST_CHECK_KABI="no"
	TEST_CHECK_PATCH="no"
	TEST_CHECK_FORMAT="no"
	TEST_RPM_BUILD="no"
	TEST_BOOT_KERNEL="no"

	echo ""
	echo "Available tests:"
	echo "  1) check_dependency           - Check dependent commits"
	echo "  2) build_allmod               - Build with allmodconfig"
	echo "  3) check_kabi          	      - Check KABI whitelist against Module.symvers"
	echo "  4) check_patch                - Run checkpatch.pl validation"
	echo "  5) check_format               - Check code formatting"
	echo "  6) rpm_build                  - Build openEuler RPM packages"
	echo "  7) boot_kernel                - Boot test (requires remote setup)"
	echo ""

	read -r -p "Select tests to run (comma-separated, 'all', or 'none') [all]: " test_selection
	TEST_SELECTION="${test_selection:-all}"

	BOOT_SELECTED="no"
	RPM_BUILD_SELECTED="no"

	if [ "$TEST_SELECTION" == "all" ]; then
		RUN_TESTS="yes"
		TEST_CHECK_DEPENDENCY="yes"
		TEST_BUILD_ALLMOD="yes"
		TEST_CHECK_KABI="yes"
		TEST_CHECK_PATCH="yes"
		TEST_CHECK_FORMAT="yes"
		TEST_RPM_BUILD="yes"
		TEST_BOOT_KERNEL="yes"
	elif [ "$TEST_SELECTION" == "none" ]; then
		RUN_TESTS="no"
	else
		RUN_TESTS="yes"
		IFS=',' read -ra SELECTED <<< "$TEST_SELECTION"
		for test_num in "${SELECTED[@]}"; do
			case "${test_num// /}" in
				1) TEST_CHECK_DEPENDENCY="yes" ;;
				2) TEST_BUILD_ALLMOD="yes" ;;
				3) TEST_CHECK_KABI="yes" ;;
				4) TEST_CHECK_PATCH="yes" ;;
				5) TEST_CHECK_FORMAT="yes" ;;
				6) TEST_RPM_BUILD="yes" ; RPM_BUILD_SELECTED="yes" ;;
				7) TEST_BOOT_KERNEL="yes" ; BOOT_SELECTED="yes" ;;
			esac
		done
	fi

	# VM + Host config if boot test selected
	VM_IP="${VM_IP:-""}"
	VM_ROOT_PWD="${VM_ROOT_PWD:-""}"
	HOST_USER_PWD="${HOST_USER_PWD:-""}"

	rpms_dir="${LINUX_SRC_PATH}/anolis/outputs/rpmbuild/RPMS/x86_64"
	boot_log="${LOGS_DIR}/boot_kernel_rpm.log"

	# Boot test logic
	if [[ "$BOOT_SELECTED" == "yes" || "$TEST_SELECTION" == "all" ]]; then
		if [[ "$TEST_SELECTION" == "all" ]]; then
			echo ""
			echo "=== VM Boot Test Configuration ==="

			# Always prompt when user selects all
			read -r -p "VM IP address: " vm_ip
			VM_IP="${vm_ip}"
			read -r -s -p "VM root password: " vm_root_pwd
			VM_ROOT_PWD="${vm_root_pwd}"
			echo ""
		else
			# Only prompt if missing
			if [[ -z "${VM_IP:-}" ]]; then
				read -r -p "VM IP address: " vm_ip
				VM_IP="${vm_ip}"
			else
				echo "Using existing VM IP: ${VM_IP}" >> "${boot_log}"
			fi

			if [[ -z "${VM_ROOT_PWD:-}" ]]; then
				read -r -s -p "VM root password: " vm_root_pwd
				VM_ROOT_PWD="${vm_root_pwd}"
				echo ""
			else
				echo "Using existing VM root password (hidden)" >> "${boot_log}"
			fi
		fi

		# RPM check only if explicitly selected 6,
		# but skip if rpm_build (5) is also selected
		if [[ "${BOOT_SELECTED:-}" == "yes" ]]; then
			if [[ "${RPM_BUILD_SELECTED:-}" != "yes" ]]; then
				# Only check RPMs if 6 was selected alone
				rpms_dir="$HOME/rpmbuild/RPMS/x86_64"
				boot_log="${LOGS_DIR}/boot_kernel_rpm.log"

				if [ ! -d "${rpms_dir}" ]; then
					fail "boot_kernel_rpm" "RPMs directory not found: ${rpms_dir}. Choose rpm_build test also."
					exit 0   # graceful exit
				fi
				kernel_rpm=$(find "${rpms_dir}" -name "kernel-*.rpm" \
					! -name "*debuginfo*" ! -name "*devel*" ! -name "*headers*" -type f | head -n 1)

				if [ -z "${kernel_rpm}" ]; then
					fail "boot_kernel_rpm" "Kernel RPM not found in ${rpms_dir}. Choose rpm_build test also."
					exit 0   # graceful exit
				fi

				echo "→ Found kernel RPM: $(basename "${kernel_rpm}")" >> "${boot_log}"
			else
				echo "Skipping RPM check for boot_kernel because rpm_build is also selected." >> "${boot_log}"
			fi
		fi
	fi

	# Host config logic
	if [[ "$RPM_BUILD_SELECTED" == "yes" || "$TEST_SELECTION" == "all" ]]; then
		if [[ "$TEST_SELECTION" == "all" ]]; then
			echo ""
			echo "=== Host Configuration ==="
			# Always prompt when user selects all
			read -r -s -p "Host sudo password (for installing dependencies): " host_user_pwd
			HOST_USER_PWD="${host_user_pwd}"
			echo ""
		else
			# Only prompt if missing
			if [[ -z "${HOST_USER_PWD:-}" ]]; then
				read -r -s -p "Host sudo password (for installing dependencies): " host_user_pwd
				HOST_USER_PWD="${host_user_pwd}"
				echo ""
			else
				echo "Using existing Host sudo password (hidden)" >> "${boot_log}"
			fi
		fi
	fi

	# Update only test-related lines in .configure
	sed -i "s|^RUN_TESTS=.*|RUN_TESTS=\"${RUN_TESTS}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_CHECK_DEPENDENCY=.*|TEST_CHECK_DEPENDENCY=\"${TEST_CHECK_DEPENDENCY}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_BUILD_ALLMOD=.*|TEST_BUILD_ALLMOD=\"${TEST_BUILD_ALLMOD}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_CHECK_KABI=.*|TEST_CHECK_KABI=\"${TEST_CHECK_KABI}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_CHECK_PATCH=.*|TEST_CHECK_PATCH=\"${TEST_CHECK_PATCH}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_CHECK_FORMAT=.*|TEST_CHECK_FORMAT=\"${TEST_CHECK_FORMAT}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_RPM_BUILD=.*|TEST_RPM_BUILD=\"${TEST_RPM_BUILD}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_BOOT_KERNEL=.*|TEST_BOOT_KERNEL=\"${TEST_BOOT_KERNEL}\"|" "${CONFIG_FILE}"
	sed -i "s|^HOST_USER_PWD=.*|HOST_USER_PWD='${HOST_USER_PWD}'|" "${CONFIG_FILE}"
	sed -i "s|^VM_IP=.*|VM_IP=\"${VM_IP}\"|" "${CONFIG_FILE}"
	sed -i "s|^VM_ROOT_PWD=.*|VM_ROOT_PWD='${VM_ROOT_PWD}'|" "${CONFIG_FILE}"
	echo -e "${GREEN}Test configuration updated successfully.${NC}"
}

if [[ "${1:-}" == "--tests" ]]; then
	update_tests
	exit 0
fi

CONFIG_FILE="euler/.configure"

# Extract HOST_USER_PWD safely
get_host_password() {
  if [ -f "$CONFIG_FILE" ]; then
    HOST_PASS=$(grep "^HOST_USER_PWD=" "$CONFIG_FILE" | cut -d"'" -f2)
    echo "$HOST_PASS"
  fi
}

delete_repo() {
  if [ -d "$TORVALDS_REPO" ]; then
    echo -e "${BLUE}Removing corrupted repository...${NC}"

    OWNER=$(stat -c '%U' "$TORVALDS_REPO")

    if [ "$OWNER" = "root" ]; then
      echo -e "${YELLOW}Repository owned by root.${NC}"

      HOST_PASS=$(get_host_password)

      if [ -n "$HOST_PASS" ]; then
        echo "$HOST_PASS" | sudo -S rm -rf "$TORVALDS_REPO"
      else
        echo -e "${RED}Root password not found in config.${NC}"
        read -s -p "Enter sudo password to remove repo: " HOST_PASS
        echo ""
        echo "$HOST_PASS" | sudo -S rm -rf "$TORVALDS_REPO"
      fi
    else
      rm -rf "$TORVALDS_REPO"
    fi
  fi
}

# Clone Torvalds repo if not exists
if [ ! -d "$TORVALDS_REPO" ]; then
  echo -e "${BLUE}Cloning Torvalds Linux repository...${NC}"
  git clone --bare https://github.com/torvalds/linux.git "$TORVALDS_REPO" 2>&1 | \
    stdbuf -oL tr '\r' '\n' | \
    grep -oP '\d+(?=%)' | \
    awk '{printf "\rProgress: %d%%", $1; fflush()}' || \
    git config --global --add safe.directory $TORVALDS_REPO
  echo -e "\r${GREEN}Repository cloned successfully${NC}"
  echo ""
else
  echo -e "${GREEN}Torvalds repository already exists${NC}"
  echo -e "${BLUE}Updating repository...${NC}"
  if ! (cd "$TORVALDS_REPO" && git fetch --all --tags 2>&1 | grep -v "^From"); then
    echo -e "${RED}Fetch failed. Re-cloning repository...${NC}"
    delete_repo # Delete existing torvalds linux repo
    echo -e "${BLUE}Re-cloning Torvalds Linux repository...${NC}"
    git clone --bare https://github.com/torvalds/linux.git "$TORVALDS_REPO" 2>&1 | \
      stdbuf -oL tr '\r' '\n' | \
      grep -oP '\d+(?=%)' | \
      awk '{printf "\rProgress: %d%%", $1; fflush()}' || \
      git config --global --add safe.directory $TORVALDS_REPO
    echo -e "${GREEN}Repository re-cloned successfully${NC}"
  else
    echo -e "${GREEN}Repository updated${NC}"
  fi
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
echo "  1) check_dependency           - Check dependent commits"
echo "  2) build_allmod               - Build with allmodconfig"
echo "  3) check_kabi          	      - Check KABI whitelist against Module.symvers"
echo "  4) check_patch                - Run checkpatch.pl validation"
echo "  5) check_format               - Check code formatting"
echo "  6) rpm_build                  - Build openEuler RPM packages"
echo "  7) boot_kernel                - Boot test (requires remote setup)"
echo ""

read -r -p "Select tests to run (comma-separated, 'all', or 'none') [all]: " test_selection
TEST_SELECTION="${test_selection:-all}"

# Parse test selection
if [ "$TEST_SELECTION" == "all" ] || [ -z "$TEST_SELECTION" ]; then
  RUN_TESTS="yes"
  TEST_CHECK_DEPENDENCY="yes"
  TEST_BUILD_ALLMOD="yes"
  TEST_CHECK_KABI="yes"
  TEST_CHECK_PATCH="yes"
  TEST_CHECK_FORMAT="yes"
  TEST_RPM_BUILD="yes"
  TEST_BOOT_KERNEL="yes"
elif [ "$TEST_SELECTION" == "none" ]; then
  RUN_TESTS="no"
  TEST_CHECK_DEPENDENCY="no"
  TEST_BUILD_ALLMOD="no"
  TEST_CHECK_KABI="no"
  TEST_CHECK_PATCH="no"
  TEST_CHECK_FORMAT="no"
  TEST_RPM_BUILD="no"
  TEST_BOOT_KERNEL="no"
else
  RUN_TESTS="yes"
  TEST_CHECK_DEPENDENCY="no"
  TEST_BUILD_ALLMOD="no"
  TEST_CHECK_KABI="no"
  TEST_CHECK_PATCH="no"
  TEST_CHECK_FORMAT="no"
  TEST_RPM_BUILD="no"
  TEST_BOOT_KERNEL="no"

  # Parse comma-separated selections
  IFS=',' read -ra SELECTED <<< "$TEST_SELECTION"
  for test_num in "${SELECTED[@]}"; do
    case "${test_num// /}" in
      1) TEST_CHECK_DEPENDENCY="yes" ;;
      2) TEST_BUILD_ALLMOD="yes" ;;
      3) TEST_CHECK_KABI="yes" ;;
      4) TEST_CHECK_PATCH="yes" ;;
      5) TEST_CHECK_FORMAT="yes" ;;
      6) TEST_RPM_BUILD="yes" ;;
      7) TEST_BOOT_KERNEL="yes" ;;
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
TEST_CHECK_DEPENDENCY="${TEST_CHECK_DEPENDENCY}"
TEST_BUILD_ALLMOD="${TEST_BUILD_ALLMOD}"
TEST_CHECK_KABI="${TEST_CHECK_KABI}"
TEST_CHECK_PATCH="${TEST_CHECK_PATCH}"
TEST_CHECK_FORMAT="${TEST_CHECK_FORMAT}"
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
echo "Linux source: ${LINUX_SRC_PATH}"
echo "Patches to process: ${NUM_PATCHES}"
echo "Patch category: ${PATCH_CATEGORY}"
echo "Build threads: ${BUILD_THREADS}"
echo "Tests enabled: ${RUN_TESTS}"
echo ""
echo -e "Run ${YELLOW}'make build'${NC} to build"
exit 0
