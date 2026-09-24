#!/bin/bash
# anck-ci-test -- their acceptance suite, run where they run it.
#
# Their Readme is explicit about the shape of this one:
#
#   内核验收测试套（单机版）。不包含编译构建过程，也不做装包与重启。
#   本测试套在重启之后运行，只做验收。
#   平台步骤链：install_rpm → reboot → run_case
#
# So the three cases it reports -- boot_kernel_rpm, check_kapi and
# check_dmesg -- all examine the *running* kernel, on a machine that
# has already had the series' RPM installed and been rebooted into it.
# check_kapi reads vmlinux out of the installed kernel-debuginfo for
# exactly that reason; it builds nothing.
#
# Running any of this on the developer's box would therefore be
# reading the developer's kernel, which has nothing to do with the
# series.  It runs on the VM instead: ours installs and reboots, the
# same two steps their platform does, and then their suite runs there
# unmodified and its verdicts are read back.
#
# Their suite is one invocation reporting three cases.  Their report
# shows three rows, so we want three rows, but running their run.sh
# once per row would reboot nothing and rebuild kabi-dw three times
# for three answers it already gave.  So: run it once, keep the
# output for the rest of this test.sh, and let each row read its own
# verdict out of it.
#
#   anck_ci_test.sh              every case, suite verdict
#   anck_ci_test.sh check_dmesg  that one case only

set -u

. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# The three their run() reports, and the only three.  Worth pinning:
# their run.sh warns on stderr when EXPECT_KERNEL_VERSION is unset,
# in the same ====WARN: form as a verdict, so anything reading the
# markers loosely would invent a case called EXPECT_KERNEL_VERSION
# and drag the suite down to a warning over it.
THEIR_CASES='boot_kernel_rpm check_kapi check_dmesg'

WANT_CASE="${1:-}"
if [ -n "${WANT_CASE}" ]; then
  case " ${THEIR_CASES} " in
    *" ${WANT_CASE} "*) ;;
    *)
      echo "anck-ci-test: their suite has no case '${WANT_CASE}'" >&2
      echo "  it reports: ${THEIR_CASES}" >&2
      exit "${CASE_ERROR}"
      ;;
  esac
fi

# shellcheck source=/dev/null
. "${WORKDIR:-$(dirname "${ANOLIS_DIR}")}/lib/vm.sh"

SUITE_NAME='anck-ci-test'
SUITE_SRC="${TONE_CLI_DIR}/tests/${SUITE_NAME}"
REMOTE_DIR="/tmp/prci-${SUITE_NAME}"

tone_cli_ready || tone_cli_missing || exit $?

# Keyed on our parent, which is the test.sh that asked: the three
# rows of one run share an answer, a later run gets a fresh one.
CACHE="${TMPDIR:-/tmp}/prci-${SUITE_NAME}-$PPID.out"

if [ -s "${CACHE}" ]; then
  echo "  -> reading ${WANT_CASE:-the suite} out of their earlier run"
  output="$(cat "${CACHE}")"
else

# The VM is the whole point of this suite, so an unset one is a skip
# with a reason rather than a failure: nothing was rejected, there was
# simply nowhere to run it.
if [ -z "${VM_IP:-}" ] || [ -z "${VM_ROOT_PWD:-}" ]; then
  echo "${SUITE_NAME}: no VM configured." >&2
  echo "  Their install_rpm -> reboot -> run_case chain needs a machine" >&2
  echo "  to install onto and reboot.  Set VM_IP and VM_ROOT_PWD" >&2
  echo "  ('make configure', or the VM fields in the web interface)." >&2
  exit "${CASE_SKIP}"
fi

if ! command -v sshpass >/dev/null 2>&1; then
  echo "${SUITE_NAME}: sshpass is not installed, so the VM cannot be reached" >&2
  exit "${CASE_ERROR}"
fi

if ! vm_ssh true >/dev/null 2>&1; then
  echo "${SUITE_NAME}: cannot reach root@${VM_IP} over ssh" >&2
  exit "${CASE_ERROR}"
fi

# Their check_kapi picks the kabi-whitelist baseline by branch and
# skips outright if the branch is not one of theirs, so this has to be
# the branch of the kernel under test rather than whatever is set.
if [ -n "${LINUX_SRC_PATH:-}" ]; then
  KERNEL_CI_REPO_BRANCH="$(their_branch_for_kernel "${LINUX_SRC_PATH}" || true)"
  export KERNEL_CI_REPO_BRANCH
fi

echo "  -> copying their ${SUITE_NAME} suite to ${VM_IP}"
vm_ssh "rm -rf ${REMOTE_DIR} && mkdir -p ${REMOTE_DIR}" || exit "${CASE_ERROR}"
for file in "${SUITE_SRC}"/*; do
  vm_scp "${file}" "${REMOTE_DIR}/" >/dev/null || exit "${CASE_ERROR}"
done

# Their run() calls check_kernel_version, then check_kapi and
# check_dmesg with its result.  Handing it the environment their CI
# would have handed it and calling run() is the whole of it.
echo "  -> running their suite on ${VM_IP}"
output=$(vm_ssh "
  set -u
  export KERNEL_CI_REPO_BRANCH='${KERNEL_CI_REPO_BRANCH:-}'
  export EXPECT_KERNEL_VERSION='${EXPECT_KERNEL_VERSION:-}'
  export PKG_CI_ABS_RPM_URL='${PKG_CI_ABS_RPM_URL:-}'
  export CHECK_KAPI='${CHECK_KAPI:-yes}'
  export CHECK_DMESG='${CHECK_DMESG:-yes}'
  export BOOT_DMESG_LEVELS='${BOOT_DMESG_LEVELS:-err}'
  export BOOT_DMESG_IGNORE='${BOOT_DMESG_IGNORE:-}'
  export TONE_BM_SUITE_DIR='${REMOTE_DIR}'
  upload_archives() { :; }
  . '${REMOTE_DIR}/run.sh'
  run
" 2>&1)

vm_ssh "rm -rf ${REMOTE_DIR}" >/dev/null 2>&1 || true
printf '%s\n' "${output}" > "${CACHE}"

fi

printf '%s\n' "${output}"

# Only the lines whose second field is one of their case names, so
# their stderr EXPECT_KERNEL_VERSION warning stays a warning in the
# log instead of becoming a verdict.
markers=$(printf '%s\n' "${output}" | awk -v want="${WANT_CASE}" '
  /^====(PASS|FAIL|SKIP|WARN):/ &&
  ($2 == "boot_kernel_rpm" || $2 == "check_kapi" || $2 == "check_dmesg") &&
  (want == "" || $2 == want)')

# Their parse.awk turns their four markers into the words their report
# shows, so a reader comparing the two sees the same names against the
# same verdicts.
echo
printf '%s\n' "${markers}" | awk -f "${SUITE_SRC}/parse.awk"

# Worst of what was asked for, in the order their report treats them:
# a failure outranks a warning, which outranks a pass.
if printf '%s' "${markers}" | grep -q '====FAIL:'; then
  exit "${CASE_FAIL}"
elif printf '%s' "${markers}" | grep -q '====WARN:'; then
  exit "${CASE_WARN}"
elif printf '%s' "${markers}" | grep -q '====PASS:'; then
  exit "${CASE_PASS}"
elif printf '%s' "${markers}" | grep -q '====SKIP:'; then
  exit "${CASE_SKIP}"
fi

echo "${SUITE_NAME}: their suite reported no verdict for ${WANT_CASE:-any case}" >&2
exit "${CASE_ERROR}"
