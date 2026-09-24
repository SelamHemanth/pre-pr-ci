# Shared ground for the Anolis cases.
#
# Every file in this directory runs exactly one case from Anolis's own
# CI, named the way their caselist names it, and says how it went in
# their words.  Two of their suites are involved:
#
#   tone-cli/tests/anck-ci-test/run.sh          boot_kernel_rpm,
#                                               check_kapi, check_dmesg
#   tone-cli/tests/anck-pack-and-boot/anck_build.sh
#                                               the build cases
#
# The first is a pure function library -- sourcing it executes nothing
# -- so those cases source it and call their function, unmodified.  The
# second is a CI entry point that clones the kernel into /anck_build
# and takes its parameters positionally, so those cases carry the make
# lines across instead, each quoting the line of theirs it came from.
#
# Their verdicts are four, and parse.awk is where they are spelled:
#
#   ====PASS: <case>   -> Pass
#   ====FAIL: <case>   -> Fail
#   ====SKIP: <case>   -> Skip
#   ====WARN: <case>   -> Warning
#
# so this speaks the same four.  A case that reports Warning is one
# Anolis prints and still accepts, and folding it into either Pass or
# Fail would misreport their gate in one direction or the other.

set -u

CASES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANOLIS_DIR="$(dirname "${CASES_DIR}")"
TONE_CLI_DIR="${ANOLIS_DIR}/tone-cli"

#: Exit statuses, read by anolis/test.sh.  Warning has one of its own
#: for the same reason openEuler's does: a warning that exits 0 is a
#: pass nobody reads, and one that exits 1 is a rejection they never
#: made.
readonly CASE_PASS=0
readonly CASE_FAIL=1
readonly CASE_ERROR=2
readonly CASE_SKIP=3
readonly CASE_WARN=5

# Their suites read their CI's environment and do not default most of
# it, because in their CI it is always set.  Give every name they read
# a value before sourcing them, so their code runs unaltered instead
# of being edited to suit us, and so "set -u" here does not turn an
# unset CI variable into an error their own runs never see.
#
# The values are only defaults: anything already exported wins, which
# is how a caller points check_kapi at a downloaded debuginfo rpm or
# silences a known-harmless dmesg line.
shim_their_ci_env() {
  local suite="$1"

  # Which branch of theirs the kernel belongs to.  Their kabi checks
  # refuse to run on anything else, and their whitelist repo has one
  # baseline per branch, so this is not cosmetic.
  : "${KERNEL_CI_REPO_BRANCH:=}"
  : "${KERNEL_CI_REPO_URL:=}"
  : "${KERNEL_CI_PR_ID:=}"

  # Where their check_kapi falls back to for a vmlinux when
  # kernel-debuginfo is not installed.  Empty means "only look at what
  # is already on this machine", and they skip if that finds nothing.
  : "${PKG_CI_ABS_RPM_URL:=}"

  # The kernel boot_kernel_rpm expects to find running.  Theirs falls
  # back to the newest installed kernel-headers and warns.
  : "${EXPECT_KERNEL_VERSION:=}"

  # Their per-case off switches.  Only the exact string "no" disables
  # a case, so anything else leaves their behaviour untouched.
  : "${CHECK_KAPI:=yes}"
  : "${CHECK_DMESG:=yes}"

  # dmesg levels and the caller's extra ignore pattern.  Theirs
  # defaults the levels to err already; this keeps set -u happy
  # without changing the value.
  : "${BOOT_DMESG_LEVELS:=err}"
  : "${BOOT_DMESG_IGNORE:=}"

  # parse.awk lives beside the suite being run.
  : "${TONE_BM_SUITE_DIR:=${suite}}"

  export KERNEL_CI_REPO_BRANCH KERNEL_CI_REPO_URL KERNEL_CI_PR_ID \
         PKG_CI_ABS_RPM_URL EXPECT_KERNEL_VERSION CHECK_KAPI \
         CHECK_DMESG BOOT_DMESG_LEVELS BOOT_DMESG_IGNORE \
         TONE_BM_SUITE_DIR

  # Part of their harness rather than their tests, and not on the path
  # here.  Defined so their code can call it without us editing theirs.
  upload_archives() { :; }
}

# Their scripts are not on disk unless the submodule is checked out,
# and a case that quietly passes without them is worse than one that
# says so.
tone_cli_ready() {
  [ -d "${TONE_CLI_DIR}/tests" ]
}

tone_cli_missing() {
  echo "tone-cli is not checked out; run" >&2
  echo "  git submodule update --init anolis/tone-cli" >&2
  return "${CASE_ERROR}"
}

# Read their own verdict out of output that follows parse.awk's format,
# rather than guessing from an exit status they did not intend as one.
# check_kapi is the reason this exists: their comment is explicit that
# kabi-dw compare returns 2 for "difference detected" and that this is
# a normal result, not a failure.
verdict_from_their_output() {
  local output="$1"
  case "${output}" in
    *'====FAIL:'*) return "${CASE_FAIL}" ;;
    *'====WARN:'*) return "${CASE_WARN}" ;;
    *'====SKIP:'*) return "${CASE_SKIP}" ;;
    *'====PASS:'*) return "${CASE_PASS}" ;;
  esac
  return "${CASE_ERROR}"
}

# The branch of theirs that matches the kernel under test.  Their
# kabi-whitelist ships one baseline per branch and their
# branch_supported() takes only these three, so a kernel on any other
# branch has no baseline to be compared against and their own gate
# skips it.
their_branch_for_kernel() {
  local kernel="$1" version patchlevel
  version=$(make -C "${kernel}" -s kernelversion 2>/dev/null) || return 1
  patchlevel=${version%%-*}
  case "${patchlevel}" in
    5.10*) echo 'devel-5.10' ;;
    6.6*)  echo 'devel-6.6' ;;
    7.0*)  echo 'devel-7.0' ;;
    *)     return 1 ;;
  esac
}
