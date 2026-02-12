#!/usr/bin/env bash
set -euo pipefail

# anolis/configure.sh - OpenAnolis Configuration Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$SCRIPT_DIR")"
CONFIG_FILE="${SCRIPT_DIR}/.configure"
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
	TEST_CHECK_KCONFIG="no"
	TEST_BUILD_ALLYES="no"
	TEST_BUILD_ALLNO="no"
	TEST_BUILD_DEFCONFIG="no"
	TEST_BUILD_DEBUG="no"
	TEST_RPM_BUILD="no"
	TEST_CHECK_KAPI="no"
	TEST_BOOT_KERNEL="no"

	echo ""
	echo "Available tests:"
	echo "  1) check_dependency		- Check commit dependencies"
	echo "  2) check_Kconfig	 	  - Validate Kconfig settings"
	echo "  3) build_allyes_config	  - Build with allyesconfig"
	echo "  4) build_allno_config	  	  - Build with allnoconfig"
	echo "  5) build_anolis_defconfig  	  - Build with anolis_defconfig"
	echo "  6) build_anolis_debug_defconfig - Build with anolis-debug_defconfig"
	echo "  7) anck_rpm_build	  	  - Build ANCK RPM packages"
	echo "  8) check_kapi		  	  - Check KAPI compatibility"
	echo "  9) boot_kernel_rpm	  	  - Boot test (requires remote setup)"
	echo ""

	read -r -p "Select tests to run (comma-separated, 'all', or 'none') [all]: " test_selection
	TEST_SELECTION="${test_selection:-all}"

	RPM_BUILD_SELECTED="no"
	BOOT_SELECTED="no"

	if [ "$TEST_SELECTION" == "all" ]; then
		RUN_TESTS="yes"
		TEST_CHECK_DEPENDENCY="yes"
		TEST_CHECK_KCONFIG="yes"
		TEST_BUILD_ALLYES="yes"
		TEST_BUILD_ALLNO="yes"
		TEST_BUILD_DEFCONFIG="yes"
		TEST_BUILD_DEBUG="yes"
		TEST_RPM_BUILD="yes"
		TEST_CHECK_KAPI="yes"
		TEST_BOOT_KERNEL="yes"
	elif [ "$TEST_SELECTION" == "none" ]; then
		RUN_TESTS="no"
	else
		RUN_TESTS="yes"
		IFS=',' read -ra SELECTED <<< "$TEST_SELECTION"
		for test_num in "${SELECTED[@]}"; do
			case "${test_num// /}" in
				1) TEST_CHECK_DEPENDENCY="yes" ;;
				2) TEST_CHECK_KCONFIG="yes" ;;
				3) TEST_BUILD_ALLYES="yes" ;;
				4) TEST_BUILD_ALLNO="yes" ;;
				5) TEST_BUILD_DEFCONFIG="yes" ;;
				6) TEST_BUILD_DEBUG="yes" ;;
				7) TEST_RPM_BUILD="yes" ; RPM_BUILD_SELECTED="yes" ;;
				8) TEST_CHECK_KAPI="yes" ;;
				9) TEST_BOOT_KERNEL="yes" ; BOOT_SELECTED="yes" ;;
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
                                rpms_dir="${LINUX_SRC_PATH}/anolis/outputs/rpmbuild/RPMS/x86_64"
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
	sed -i "s|^TEST_CHECK_KCONFIG=.*|TEST_CHECK_KCONFIG=\"${TEST_CHECK_KCONFIG}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_BUILD_ALLYES=.*|TEST_BUILD_ALLYES=\"${TEST_BUILD_ALLYES}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_BUILD_ALLNO=.*|TEST_BUILD_ALLNO=\"${TEST_BUILD_ALLNO}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_BUILD_DEFCONFIG=.*|TEST_BUILD_DEFCONFIG=\"${TEST_BUILD_DEFCONFIG}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_BUILD_DEBUG=.*|TEST_BUILD_DEBUG=\"${TEST_BUILD_DEBUG}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_RPM_BUILD=.*|TEST_RPM_BUILD=\"${TEST_RPM_BUILD}\"|" "${CONFIG_FILE}"
	sed -i "s|^TEST_CHECK_KAPI=.*|TEST_CHECK_KAPI=\"${TEST_CHECK_KAPI}\"|" "${CONFIG_FILE}"
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

# General Configuration
echo "=== General Configuration ==="
read -r -p "Linux source code path: " linux_src
LINUX_SRC_PATH="${linux_src:-/home/amd/linux}"

read -r -p "Signed-off-by name: " signer_name
SIGNER_NAME="${signer_name:-Hemanth Selam}"

read -r -p "Signed-off-by email: " signer_email
SIGNER_EMAIL="${signer_email:-Hemanth.Selam@amd.com}"

read -r -p "Anolis Bugzilla ID: " anbz_id
ANBZ_ID="${anbz_id:-12345}"

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
echo "  1) check_dependency	      - Check commit dependencies"
echo "  2) check_Kconfig              - Validate Kconfig settings"
echo "  3) build_allyes_config        - Build with allyesconfig"
echo "  4) build_allno_config         - Build with allnoconfig"
echo "  5) build_anolis_defconfig     - Build with anolis_defconfig"
echo "  6) build_anolis_debug_defconfig - Build with anolis-debug_defconfig"
echo "  7) anck_rpm_build             - Build ANCK RPM packages"
echo "  8) check_kapi                 - Check KAPI compatibility"
echo "  9) boot_kernel_rpm            - Boot test (requires remote setup)"
echo ""

read -r -p "Select tests to run (comma-separated, 'all', or 'none') [all]: " test_selection
TEST_SELECTION="${test_selection:-all}"

# Parse test selection
if [ "$TEST_SELECTION" == "all" ] || [ -z "$TEST_SELECTION" ]; then
  RUN_TESTS="yes"
  TEST_CHECK_DEPENDENCY="yes"
  TEST_CHECK_KCONFIG="yes"
  TEST_BUILD_ALLYES="yes"
  TEST_BUILD_ALLNO="yes"
  TEST_BUILD_DEFCONFIG="yes"
  TEST_BUILD_DEBUG="yes"
  TEST_RPM_BUILD="yes"
  TEST_CHECK_KAPI="yes"
  TEST_BOOT_KERNEL="yes"
elif [ "$TEST_SELECTION" == "none" ]; then
  RUN_TESTS="no"
  TEST_CHECK_DEPENDENCY="no"
  TEST_CHECK_KCONFIG="no"
  TEST_BUILD_ALLYES="no"
  TEST_BUILD_ALLNO="no"
  TEST_BUILD_DEFCONFIG="no"
  TEST_BUILD_DEBUG="no"
  TEST_RPM_BUILD="no"
  TEST_CHECK_KAPI="no"
  TEST_BOOT_KERNEL="no"
else
  RUN_TESTS="yes"
  TEST_CHECK_DEPENDENCY="no"
  TEST_CHECK_KCONFIG="no"
  TEST_BUILD_ALLYES="no"
  TEST_BUILD_ALLNO="no"
  TEST_BUILD_DEFCONFIG="no"
  TEST_BUILD_DEBUG="no"
  TEST_RPM_BUILD="no"
  TEST_CHECK_KAPI="no"
  TEST_BOOT_KERNEL="no"
  
  # Parse comma-separated selections
  IFS=',' read -ra SELECTED <<< "$TEST_SELECTION"
  for test_num in "${SELECTED[@]}"; do
    case "${test_num// /}" in
      1) TEST_CHECK_DEPENDENCY="yes" ;;
      2) TEST_CHECK_KCONFIG="yes" ;;
      3) TEST_BUILD_ALLYES="yes" ;;
      4) TEST_BUILD_ALLNO="yes" ;;
      5) TEST_BUILD_DEFCONFIG="yes" ;;
      6) TEST_BUILD_DEBUG="yes" ;;
      7) TEST_RPM_BUILD="yes" ;;
      8) TEST_CHECK_KAPI="yes" ;;
      9) TEST_BOOT_KERNEL="yes" ;;
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
# OpenAnolis Configuration
# Generated: $(date)

# General Configuration
LINUX_SRC_PATH="${LINUX_SRC_PATH}"
SIGNER_NAME="${SIGNER_NAME}"
SIGNER_EMAIL="${SIGNER_EMAIL}"
ANBZ_ID="${ANBZ_ID}"
NUM_PATCHES="${NUM_PATCHES}"

# Build Configuration
BUILD_THREADS="${BUILD_THREADS}"

# Test Configuration
RUN_TESTS="${RUN_TESTS}"
TEST_CHECK_DEPENDENCY="${TEST_CHECK_DEPENDENCY}"
TEST_CHECK_KCONFIG="${TEST_CHECK_KCONFIG}"
TEST_BUILD_ALLYES="${TEST_BUILD_ALLYES}"
TEST_BUILD_ALLNO="${TEST_BUILD_ALLNO}"
TEST_BUILD_DEFCONFIG="${TEST_BUILD_DEFCONFIG}"
TEST_BUILD_DEBUG="${TEST_BUILD_DEBUG}"
TEST_RPM_BUILD="${TEST_RPM_BUILD}"
TEST_CHECK_KAPI="${TEST_CHECK_KAPI}"
TEST_BOOT_KERNEL="${TEST_BOOT_KERNEL}"

# Host Configuration
HOST_USER_PWD='${HOST_USER_PWD}'

# VM Configuration
VM_IP="${VM_IP}"
VM_ROOT_PWD='${VM_ROOT_PWD}'
EOF

echo ""
echo "Linux source: ${LINUX_SRC_PATH}"
echo "Patches to process: ${NUM_PATCHES}"
echo "Build threads: ${BUILD_THREADS}"
echo "Tests enabled: ${RUN_TESTS}"
echo ""
echo -e "Run ${YELLOW}'make build'${NC} to build"
exit 0
