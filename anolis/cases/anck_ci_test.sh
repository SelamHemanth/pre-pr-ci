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

# Their suite reports all three in one run, and a row asked about one.
# The other two still run -- their run() calls all three, and booting
# once for three answers is the point of doing it that way -- but
# their verdicts belong to the other rows, and printing them here said
# "check_kapi: skipped, check_dmesg: passed" under a row that had been
# asked about neither.
#
# Only their marker lines are dropped, and only other cases'.  Every
# other line goes through untouched and as it arrives: it is what the
# machine was doing, and it is the reason to be reading this at all.
their_lines() {
  awk -v want="$1" -v names="${THEIR_CASES}" '
    BEGIN { n = split(names, c, " "); for (i = 1; i <= n; i++) known[c[i]] = 1 }
    {
      if (want != "" && /^====(PASS|FAIL|SKIP|WARN):/ &&
          ($2 in known) && $2 != want) next
      print
      fflush()
    }
  '
}

# Where the rest of the tool's headings come from, so this reads the
# same as every other stage and loses its colours off a terminal.
# shellcheck source=../../lib/log.sh
. "${WORKDIR:-$(dirname "${ANOLIS_DIR}")}/lib/log.sh"

heading() {
  echo ""
  echo -e "${BOLD}${BLUE}==> $*${NC}"
}

# Set once their output has been shown live, so the cached path knows
# it still has to show it and a fresh run does not print it twice.
SHOWN=''

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

# What their check_kernel_version compares uname -r against.
#
# Their platform sets this from the artefact it installed, and we are
# the platform here, so it comes from the kernel RPM their
# anck_rpm_build produced.  Left unset, their get_expect_kver falls
# back to the newest kernel-headers installed on the VM -- the
# distro's stock kernel, nothing to do with the series -- and then
# every case skips on a mismatch that fallback invented.  The boot
# test already set it when it ran; the other two cases run on their
# own just as often, and need it just as much.
# shellcheck source=../../lib/boot_test.sh
. "${WORKDIR:-$(dirname "${ANOLIS_DIR}")}/lib/boot_test.sh"

if [ -z "${EXPECT_KERNEL_VERSION:-}" ]; then
  if rpm_dir=$(bash "${ANOLIS_DIR}/an_tone.sh" --rpm-dir 2>/dev/null) &&
     EXPECT_KERNEL_VERSION=$(boot_expected_kver "${rpm_dir}"); then
    export EXPECT_KERNEL_VERSION
    echo "  -> expecting ${EXPECT_KERNEL_VERSION}," \
         "from the RPM their anck_rpm_build built"
  else
    echo "${SUITE_NAME}: their anck_rpm_build has produced no kernel RPM," >&2
    echo "  so there is no version to expect and theirs will fall back to" >&2
    echo "  whatever kernel-headers the VM already had." >&2
  fi
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
#
# Written to a file as it arrives rather than captured, so that their
# suite can be read while it is still running: check_kapi builds
# kabi-dw and compares every symbol, and holding all of that back
# until it finished left the row looking stuck.
heading "their ${SUITE_NAME} on ${VM_IP}, as their run() runs it"
LIVE="${CACHE}.live"
vm_ssh "
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
" 2>&1 | tee "${LIVE}" | their_lines "${WANT_CASE}"

vm_ssh "rm -rf ${REMOTE_DIR}" >/dev/null 2>&1 || true
mv -f "${LIVE}" "${CACHE}"
output="$(cat "${CACHE}")"
SHOWN='yes'

fi

# Read out of their earlier run rather than run again; shown here
# because there was no live run to have shown it.
[ -n "${SHOWN}" ] || printf '%s\n' "${output}" | their_lines "${WANT_CASE}"

# Only the lines whose second field is one of their case names, so
# their stderr EXPECT_KERNEL_VERSION warning stays a warning in the
# log instead of becoming a verdict.
markers=$(printf '%s\n' "${output}" \
          | awk -v want="${WANT_CASE}" -v names="${THEIR_CASES}" '
  BEGIN { n = split(names, c, " "); for (i = 1; i <= n; i++) known[c[i]] = 1 }
  /^====(PASS|FAIL|SKIP|WARN):/ && ($2 in known) && (want == "" || $2 == want)')

# Their parse.awk turns their four markers into the words their report
# shows, so a reader comparing the two sees the same names against the
# same verdicts.
heading "their verdict on ${WANT_CASE:-the suite}"
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
