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

# What a kernel prints when it has tripped over itself.  Every string
# here is the kernel's own: "Call Trace:" from show_trace_log_lvl,
# "Oops" and "general protection fault" from die(), "WARNING: CPU:"
# from __warn(), "kernel BUG at" from BUG(), the panic line from
# panic().
#
# Looked for by name rather than by level, because the kernel prints a
# trace at whatever level the thing that tripped it used: a WARN_ON
# backtrace comes out at warning level and their check_dmesg, which
# reads the error levels, never sees it.
DMESG_TRACES='Call Trace:|kernel BUG at|BUG: |Oops|WARNING: CPU:|general protection fault|[Kk]ernel panic'

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
      # Our own tally, counted on the machine and read back here.
      if (/^prci-dmesg: /) next
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

# Their per-case switches, set from the cases this run will report.
#
# Their run() calls all three, and the two that are not boot_kernel_rpm
# have a switch of their own for the case where nobody is asking.  The
# set comes from the caller because one run of their suite answers all
# three rows and the rows share it: deciding from this row alone would
# have the first of them turn off the work the third needs.
#
# Anything already set wins, as everywhere else here.
# printk's own bookkeeping, which is not the kernel complaining about
# anything: it prints "<caller>: N callbacks suppressed" when a
# rate-limited message has been dropped.  Thirty-two of them on a VM
# that has been up a few hours, all from the audit queue, and a row
# that is amber for that is a row nobody reads.
#
# Added through their BOOT_DMESG_IGNORE, which their comment calls the
# way to extend the filtering, rather than by editing the list of
# theirs it is added to.  Anything already set still wins.
: "${BOOT_DMESG_IGNORE:=callbacks suppressed}"

CASES_WANTED="${THEIR_VM_CASES:-${WANT_CASE:-${THEIR_CASES}}}"
case " ${CASES_WANTED} " in
  *' check_kapi '*) ;;
  *) : "${CHECK_KAPI:=no}" ;;
esac
case " ${CASES_WANTED} " in
  *' check_dmesg '*) ;;
  *) : "${CHECK_DMESG:=no}" ;;
esac

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

RPM_DIR="$(bash "${ANOLIS_DIR}/an_tone.sh" --rpm-dir 2>/dev/null || true)"

if [ -z "${EXPECT_KERNEL_VERSION:-}" ]; then
  if [ -n "${RPM_DIR}" ] &&
     EXPECT_KERNEL_VERSION=$(boot_expected_kver "${RPM_DIR}"); then
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

# The debug packages their check_kapi reads the kernel's types out of,
# which their platform's install_rpm step has already put there.
#
# Only when this run is going to report that case, and only when there
# is a branch for it: they are 750M across the wire and near three
# gigabytes on the machine, and their check_kapi skips a branch with
# no kabi baseline anyway, so neither a run of the other two cases nor
# a branch outside their three pays for them.
#
# Their switch is what says so, not this row: the first of three rows
# to get here is the one that runs their suite for all three, and
# asking whether *this* row is check_kapi had it leave the vmlinux off
# the machine and then cache the skip for the row that wanted it.
if [ "${CHECK_KAPI:-yes}" != 'no' ] &&
   [ -n "${KERNEL_CI_REPO_BRANCH:-}" ] &&
   [ -n "${EXPECT_KERNEL_VERSION:-}" ]; then
  echo "  -> installing their kernel-debuginfo on ${VM_IP}," \
       "which their check_kapi reads"
  if ! boot_stage_debuginfo "${RPM_DIR}" "${EXPECT_KERNEL_VERSION}" \
                            /dev/stderr; then
    echo "  -> could not install it; their check_kapi will say it" \
         "cannot find vmlinux and skip"
  fi
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
  export BOOT_DMESG_LEVELS='${BOOT_DMESG_LEVELS:-emerg,alert,crit,err}'
  export BOOT_DMESG_IGNORE='${BOOT_DMESG_IGNORE:-}'
  export TONE_BM_SUITE_DIR='${REMOTE_DIR}'
  upload_archives() { :; }
  . '${REMOTE_DIR}/run.sh'
  run

  # The rest of what this row is for, with their function doing the
  # looking so that their ignore list keeps applying.
  if [ \"\${CHECK_DMESG}\" != 'no' ]; then
    echo ''
    echo '==> dmesg, past the level their check_dmesg fails on'

    traces=\$(dmesg -T 2>/dev/null | grep -E '${DMESG_TRACES}' || true)
    if [ -n \"\${traces}\" ]; then
      echo 'Call traces in the running kernel:'
      printf '%s\n' \"\${traces}\"
    else
      echo 'No call traces.'
    fi

    BOOT_DMESG_LEVELS='warn'
    warned=\$(check_dmesg 0 2>&1 | awk '
      /^=+show dmesg errors/        { inside = 1; next }
      /^====(PASS|FAIL|SKIP|WARN):/ { inside = 0 }
      inside')
    if [ -n \"\${warned}\" ]; then
      echo 'At warning level, after their ignore list:'
      printf '%s\n' \"\${warned}\"
    else
      echo 'Nothing at warning level, after their ignore list.'
    fi

    printf 'prci-dmesg: traces=%s warnings=%s\n' \
      \"\$(printf '%s' \"\${traces}\" | grep -c . || true)\" \
      \"\$(printf '%s' \"\${warned}\" | grep -c . || true)\"
  fi
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

# What their check_dmesg passing does not yet mean.
#
# Theirs is one question -- is there anything at the error levels --
# and this row is three.  A kernel that oopsed or hit a WARN_ON left a
# call trace, and that is a failure whatever level it was printed at;
# a kernel that only complained is not a failure but is not a clean
# boot either, and their four verdicts have a word for that.
#
# Only ever upgrades their verdict.  A check_dmesg they failed stays
# failed, and nothing here can turn a failure into a pass.
# Only for the row being reported.  One run of their suite answers
# all three rows out of the one cache, and the boot row saying how
# many warnings the dmesg row found helps nobody.
dmesg_tally=''
case "${markers}" in
  *': check_dmesg'*)
    dmesg_tally=$(printf '%s\n' "${output}" | grep '^prci-dmesg: ' | tail -n 1)
    ;;
esac
if [ -n "${dmesg_tally}" ]; then
  traces=${dmesg_tally#*traces=}; traces=${traces%% *}
  warnings=${dmesg_tally##*warnings=}
  verdict=''
  if [ "${traces:-0}" -gt 0 ] 2>/dev/null; then
    verdict='FAIL'
    echo ""
    echo "  -> ${traces} line(s) of call trace in the boot log, so this" \
         "is a failure"
  elif [ "${warnings:-0}" -gt 0 ] 2>/dev/null; then
    verdict='WARN'
    echo ""
    echo "  -> ${warnings} warning line(s) in the boot log, and no call" \
         "trace, so this is a warning"
  fi
  if [ -n "${verdict}" ]; then
    markers=$(printf '%s\n' "${markers}" \
              | sed "s/^====PASS: check_dmesg\$/====${verdict}: check_dmesg/")
  fi
fi

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
