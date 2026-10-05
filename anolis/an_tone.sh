#!/bin/bash
#
# Anolis's build gate, run out of Anolis's own scripts.
#
# anolis/test.sh reimplements their build cases in our own shell.  It
# tracks them loosely, and the places where it has drifted are the kind
# nobody notices until a series passes here and fails there: their
# build_allno_config does not run `make modules` and ours does, their
# two defconfig cases run `make olddefconfig` after the defconfig and
# ours does not, and their check_Kconfig runs on a fresh clone while
# ours runs `git status --porcelain` in the user's working tree, where
# an unrelated local edit is reported as the series breaking Kconfig.
#
# This file does not reimplement them.  It runs their scripts:
#
#   tone-cli/tests/anck-pack-and-boot/run.sh         the caselist, the
#                                                    verdict rule and
#                                                    the four markers
#   tone-cli/tests/anck-pack-and-boot/anck_build.py  the grouping, the
#                                                    parallelism and
#                                                    the log per case
#   tone-cli/tests/anck-pack-and-boot/anck_build.sh  the clone, the
#                                                    dependencies and
#                                                    every make line
#
# run.sh is a pure function library -- sourcing it executes nothing --
# so unlike openEuler's there is no `main "$@"` to strip.
#
# Their scripts are written for their CI: three build hosts that clone
# the kernel from gitee, apply a pull request to it, install packages
# as root, and build into /anck_build.  Three things on that list this
# machine will not give them, and each has one seam:
#
#   /anck_build          an absolute path under / that only root can
#                        create.  bwrap puts a scratch directory there,
#                        privately, per case.
#   the kernel repository  their clone line wants a URL and a branch
#                        named for one of their releases.  It gets a
#                        bare repository that borrows the user's
#                        objects and carries one such branch.
#   root                 yum and yum-builddep are real here, reached
#                        through the sudo password the tool already
#                        holds for this.
#
# What is left untouched is everything that decides a verdict: every
# make line, the dependency lists, the branch gates, the Kconfig check,
# their caselist, their grep for "<case>: pass" and their four markers.

set -u

AN_TONE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRCI_ROOT="$(dirname "${AN_TONE_DIR}")"
TONE_CLI_DIR="${AN_TONE_DIR}/tone-cli"

#: Their suite.  Everything this file runs comes out of here.
TONE_SUITE="${TONE_CLI_DIR}/tests/anck-pack-and-boot"

#: Where /anck_build is really kept.  Not /tmp: that is a tmpfs here,
#: and an allyesconfig build tree in RAM would take the machine down.
: "${AN_TONE_SCRATCH:=${PRCI_ROOT}/.anck-build}"

# anolis/test.sh runs this as a script rather than sourcing it, and it
# does not export the sudo password -- deliberately, so that it stays
# out of /proc/<pid>/environ of every command a test runs, and a kernel
# build runs a great many.  So the config is read here too, the way
# every other top-level script reads its own, and the password stays an
# ordinary shell variable that no child inherits.
if [ -z "${LINUX_SRC_PATH:-}" ] && [ -f "${AN_TONE_DIR}/.configure" ]; then
  # shellcheck disable=SC1090
  . "${AN_TONE_DIR}/.configure"
fi

# anolis/cases/lib.sh knows which branch of theirs a kernel belongs to,
# which their own code needs and does not work out for itself -- in
# their CI the branch is what the pull request was opened against.
if ! declare -f their_branch_for_kernel >/dev/null 2>&1; then
  # shellcheck source=cases/lib.sh
  . "${AN_TONE_DIR}/cases/lib.sh"
fi

# Their seven cases, in their caselist's order, each with the keyword
# their anck_build.py selects it by.  Both halves are theirs:
# run.sh:287 lists the cases, anck_build.py:41-55 maps the keywords.
_AN_TONE_CASES='
check_Kconfig:checkconfig
build_allyes_config:allyesconfig
build_allno_config:allnoconfig
build_anolis_defconfig:defconfig
build_anolis_debug_defconfig:debugconfig
anck_rpm_build:rpmbuild
build_perf:perf
'

# The keyword their anck_build.py selects a case by, or nothing if the
# name is not one of theirs.
_an_tone_keyword() {
  local want="$1" pair
  for pair in ${_AN_TONE_CASES}; do
    [ "${pair%%:*}" = "${want}" ] && { echo "${pair#*:}"; return 0; }
  done
  return 1
}

# Their case names, which are the only names this file answers to.
an_tone_cases() {
  local pair
  for pair in ${_AN_TONE_CASES}; do echo "${pair%%:*}"; done
}

# Their scripts are not on disk unless the submodule is checked out,
# and a case that quietly passes without them is worse than one that
# says so.
_an_tone_ready() {
  if [ ! -f "${TONE_SUITE}/anck_build.sh" ]; then
    echo "tone-cli is not checked out, so there is nothing of theirs to run" >&2
    echo "  git submodule update --init anolis/tone-cli" >&2
    return 1
  fi
}

# A repository their clone line will accept, standing in for gitee.
#
# Their anck_build.sh:115 is
#
#   git clone --depth 1 -b $KERNEL_CI_REPO_BRANCH $KERNEL_CI_REPO_URL
#
# and the branch name is not decoration: the same name picks the
# ck-build branch, the dependency list and which of the two Kconfig
# checks runs.  So it has to be one of theirs, and the user's tree is
# on a pull request branch instead.
#
# This builds a bare repository that carries exactly that branch,
# pointing at the user's HEAD -- the series already applied, which is
# what their CI arrives at after the git am their KERNEL_CI_PR_ID
# triggers.  The objects are not copied: an alternates file lends it the
# user's object store, so it costs no disk and nothing is written to the
# user's tree.  The directory is named cloud-kernel.git because their
# anck_rpm_build does `ln -sf ../$anck_repo cloud-kernel` and
# $anck_repo is the basename of this URL.
_an_tone_repo() {
  local kernel="$1" branch="$2" at="$3" head

  head=$(git -C "${kernel}" rev-parse HEAD) || return 1

  rm -rf "${at:?}"
  git init -q --bare "${at}" || return 1
  echo "${kernel}/.git/objects" > "${at}/objects/info/alternates" || return 1
  git -C "${at}" update-ref "refs/heads/${branch}" "${head}" || return 1
}

# The sudo password, reachable without putting it in the environment.
#
# anolis/test.sh is explicit that the two passwords are deliberately
# not exported, "so they stay out of /proc/<pid>/environ of every
# command a test runs", and a kernel build runs a great many commands.
# So it goes where sudo itself expects to find one it may not prompt
# for: an askpass helper, named by SUDO_ASKPASS, reading a file that
# only the user can read.  Same posture as anolis/.configure, which is
# already a mode 0600 file holding this password.
#
# Removed by _an_tone_cleanup at the end of the run.
_an_tone_askpass() {
  local dir="$1" pwd_file="$1/.pw" helper="$1/askpass"

  mkdir -p "${dir}" || return 1

  [ -n "${HOST_USER_PWD:-}" ] || return 0

  ( umask 077 && printf '%s' "${HOST_USER_PWD}" > "${pwd_file}" ) || return 1
  chmod 600 "${pwd_file}" || return 1

  printf '#!/bin/sh\nexec cat %s\n' "${pwd_file}" > "${helper}" || return 1
  chmod 700 "${helper}" || return 1

  export SUDO_ASKPASS="${helper}"
}

_an_tone_cleanup() {
  rm -f "${AN_TONE_BIN:-/nonexistent}/.pw"
}

# yum, for a script that is used to being root.
#
# Their anck_build.sh installs build dependencies with yum and
# yum-builddep, and their kernel.spec's build requirements are not
# something to guess at or pre-install by hand: letting their own
# dependency lists run is the point.  The tool already holds a sudo
# password for exactly this -- anolis/test.sh has been installing
# their package list with it -- so these hand their command through.
#
# Written as scripts on PATH rather than shell functions because their
# anck_build.sh runs as its own `bash` process, where a function
# defined here would not exist.
_an_tone_bin() {
  local dir="$1"

  mkdir -p "${dir}" || return 1

  local tool
  for tool in yum yum-builddep dnf; do
    cat > "${dir}/${tool}" <<EOF
#!/bin/bash
# Their script expects to be root.  Hand it through, and say so, so the
# log shows which of their dependency lists was installed and when.
echo "[prci] ${tool} \$*"
if [ -n "\${SUDO_ASKPASS:-}" ] && [ -x "\${SUDO_ASKPASS}" ]; then
  # Their real exit status, not a cheerful one.  Most of their yum
  # lines go unchecked, but build_perf's does not -- it reports
  # "Failed to install perf dependencies" and stops -- so swallowing a
  # failure here would turn their clear message into a compile error
  # hundreds of lines later.
  sudo -A "/usr/bin/${tool}" "\$@"
  exit \$?
fi
echo "[prci] no sudo password is configured, so nothing was installed." >&2
echo "[prci] set it with 'make configure' if a case fails on a missing" >&2
echo "[prci] header; a dependency already present needs nothing." >&2
exit 0
EOF
    chmod +x "${dir}/${tool}" || return 1
  done
}

# Their suite directory, with anck_build.sh replaced by a sandbox.
#
# Their anck_build.py:28 runs the case as
#
#   bash {script} {config} {repo_url} {branch} {pr_id} > /tmp/anck_{}.log
#
# where {script} is the path handed to it.  So the sandbox goes in as
# that script: it creates /anck_build for this case and execs their
# real anck_build.sh inside it with the arguments untouched.  Their
# run.sh and their anck_build.py are copied byte for byte and run
# unedited.
#
# A copy rather than their directory itself because a file has to be
# replaced, and writing into a submodule checkout would show up as a
# local modification of theirs.  Rebuilt every run, so updating the
# submodule is still all it takes to follow them.
_an_tone_overlay() {
  local dir="$1"

  rm -rf "${dir:?}"
  mkdir -p "${dir}" || return 1
  cp "${TONE_SUITE}"/* "${dir}/" || return 1

  cat > "${dir}/anck_build.sh" <<EOF
#!/bin/bash
# Stub: their anck_build.sh, run where it can have its /anck_build.
exec bash "${AN_TONE_DIR}/an_tone.sh" --sandbox "\$@"
EOF
  chmod +x "${dir}/anck_build.sh" || return 1
}

# /anck_build, which their scripts hardcode and only root can create.
#
# One empty directory at the root of the filesystem, which their own
# build hosts have.  It is never written to: every case mounts its own
# scratch over it, so it stays the empty mount point it starts as.
_an_tone_mountpoint() {
  [ -d /anck_build ] && return 0

  echo "[prci] their scripts build in /anck_build, which does not exist yet."
  echo "[prci] creating it once, as an empty mount point owned by $(id -un)."
  sudo -A mkdir -p /anck_build || return 1
  sudo -A chown "$(id -u):$(id -g)" /anck_build || return 1
}

# Run their anck_build.sh with the /anck_build it hardcodes.
#
# Each case gets its own, by mounting its scratch directory over the
# empty one in a mount namespace of its own.  That is what lets their
# three parallel groups run here at all: on their CI the groups are
# three separate hosts and do not share /anck_build, and sharing one
# would have them deleting each other's kernel tree.
#
# The namespace needs root to create, but their build must not run as
# root, so the privilege is given up again immediately -- `unshare`
# makes the mount, `setpriv` drops back to the user, and their script
# starts from there.  Their yum lines still reach root, through sudo
# and the askpass helper above; that is also why this is not bwrap,
# which sets the "no new privileges" flag and so leaves sudo unusable.
# sudo keeps the rest of the environment with -E, but not these two:
# secure_path overrides PATH, which is where the yum shims are, and
# always_set_home makes HOME /root, which their rpmbuild would then try
# to build under.  So both are carried across by hand and restored
# after the privilege is dropped.
_an_tone_sandbox() {
  local case_name="${1:?}" mine groups

  mine="${AN_TONE_SCRATCH}/${case_name}"
  mkdir -p "${mine}" || return 1

  # Real supplementary groups, not none: dropping them silently would
  # be one more way this differs from a plain shell.
  groups=$(id -G | tr ' ' ',')

  exec sudo -A -E unshare --mount -- \
    bash -c '
      mount --bind "$1" /anck_build || exit 1
      exec setpriv --reuid="$2" --regid="$3" --groups="$4" -- \
           env PATH="$5" HOME="$6" "${@:7}"
    ' _ "${mine}" "$(id -u)" "$(id -g)" "${groups}" "${PATH}" "${HOME}" \
    bash "${TONE_SUITE}/anck_build.sh" "$@"
}

# Their run.sh's functions, and shims for the parts of their harness
# that are not in the suite.
_an_tone_load() {
  # shellcheck source=/dev/null
  . "${TONE_SUITE}/run.sh" || return 1

  # upload_archives belongs to their tone harness, which collects a
  # file into the job's artefacts.  Ours keeps it where the user is
  # already looking for logs.
  upload_archives() {
    local file
    for file in "$@"; do
      [ -f "${file}" ] && cp -f "${file}" "${AN_TONE_LOGS}/" 2>/dev/null
    done
    return 0
  }

  # logger is theirs too: it runs a command and records it.  Only their
  # boot and kapi paths use it, and those are stubbed below.
  logger() { "$@"; }
}

# boot_kernel_rpm and check_kapi, which this suite does not own here.
#
# Their anck-pack-and-boot drives both over ssh from the build host,
# and their anck-ci-test does the single-machine version on the machine
# that has the RPM installed.  We run anck-ci-test, because the RPM is
# built here and installed there, and because it reports check_dmesg as
# well -- pack-and-boot defines check_dmesg but its run() never calls
# it.
#
# Stubbed silently rather than skipped: a skip here would be a row
# anck-ci-test is about to report properly, and two rows for one case
# reading differently is worse than one.
_an_tone_shim_boot() {
  anck_boot_test() { return 0; }
  check_kapi() { return 0; }
}

# Their anck_build(), which on their CI is
#
#   prepare_build_repo
#   python $TONE_BM_SUITE_DIR/anck_build.py $TONE_BM_SUITE_DIR/anck_build.sh
#   upload_archives $(find /anck_build/ck-build/outputs -name *.rpm)
#
# The middle line is kept, pointed at the overlay, which is the whole
# of the adaptation.  prepare_build_repo writes a yum repository file
# into /etc/yum.repos.d for their 6.6 and 7.0 branches; it is dropped
# because it needs root at /etc and because the repository it adds is
# reachable from their network and not from here -- if a dependency is
# genuinely missing, their yum-builddep says which.
#
# The find is kept, so the RPMs anck_rpm_build produced are collected
# the way theirs collects them.
_an_tone_shim_build() {
  anck_build() {
    local py="${AN_TONE_OVERLAY}/anck_build.py"

    python3 "${py}" "${AN_TONE_OVERLAY}/anck_build.sh"

    # shellcheck disable=SC2046
    upload_archives $(find "${AN_TONE_SCRATCH}" -path '*/ck-build/outputs/*' \
                           -name '*.rpm' 2>/dev/null)
    return 0
  }
}

# Everything their scripts read out of their CI's environment.
#
# Their code does not default most of it, because in their CI it is
# always set.  Anything already exported wins, which is how their own
# CK_BUILDER_BRANCH and BUILD_EXTRA overrides stay available without
# editing theirs.
_an_tone_env() {
  local kernel="$1" branch="$2" repo="$3"

  export KERNEL_CI_REPO_URL="${repo}"
  export KERNEL_CI_REPO_BRANCH="${branch}"

  # Empty on purpose.  It is the pull request their CI applies with git
  # am, and there is no pull request yet -- that is the point of running
  # this before submission.  Their anck_build.sh:122 then takes the
  # "Skip apply patch" path, which is correct: the clone above is
  # already the series.
  export KERNEL_CI_PR_ID=''

  # Their anck_build.py reads these to decide which host each group
  # builds on.  Empty means this machine, which is what their third
  # group already does, and what the user asked for: the VM has no
  # resources to build a kernel with.
  export YES_BUILDER=''
  export DEF_BUILDER=''
  export REMOTE_HOST=''

  # parse.awk and anck_build.py are looked up under this.
  export TONE_BM_SUITE_DIR="${AN_TONE_OVERLAY}"
  export TONE_BM_RUN_DIR="${AN_TONE_OVERLAY}"

  : "${AN_TONE_LOGS:=${PRCI_ROOT}/logs}"
  mkdir -p "${AN_TONE_LOGS}"
  export AN_TONE_LOGS

  export PATH="${AN_TONE_BIN}:${PATH}"

  # The sandbox is this same file re-entered as a script, so it has to
  # arrive at the same scratch directory the parent chose.
  export AN_TONE_SCRATCH
}

# Set up the scratch, the repository, the overlay and the shims, and
# source their run.sh.  Shared by every entry point below.
_an_tone_prepare() {
  local kernel="${LINUX_SRC_PATH:?LINUX_SRC_PATH is not set}" branch repo

  _an_tone_ready || return 1

  if ! branch=$(their_branch_for_kernel "${kernel}"); then
    echo "an_tone: $(make -C "${kernel}" -s kernelversion 2>/dev/null)" \
         "is not one of their CI branches" >&2
    echo "  their anck_build.sh takes 4.19, 5.10, 6.1, 6.6 and 7.0" >&2
    return 1
  fi

  mkdir -p "${AN_TONE_SCRATCH}" || return 1
  AN_TONE_OVERLAY="${AN_TONE_SCRATCH}/.suite"
  AN_TONE_BIN="${AN_TONE_SCRATCH}/.bin"
  repo="${AN_TONE_SCRATCH}/cloud-kernel.git"

  _an_tone_repo "${kernel}" "${branch}" "${repo}" || return 1
  _an_tone_overlay "${AN_TONE_OVERLAY}" || return 1
  _an_tone_bin "${AN_TONE_BIN}" || return 1
  _an_tone_askpass "${AN_TONE_BIN}" || return 1
  _an_tone_mountpoint || return 1

  _an_tone_env "${kernel}" "${branch}" "${repo}"
  _an_tone_load || return 1
  _an_tone_shim_boot
  _an_tone_shim_build

  # However this run ends, the password file does not outlive it.
  trap _an_tone_cleanup EXIT INT TERM

  echo "[prci] their anck-pack-and-boot, branch ${branch}," \
       "kernel $(git -C "${kernel}" rev-parse --short HEAD)"
}

# Their whole suite: their grouping, their parallelism, their caselist.
#
# This is their run(), unedited.  It calls the anck_build above, then
# walks their caselist reading each case's verdict out of its log.
an_tone_suite() {
  _an_tone_prepare || return 2

  local out rc=0
  out=$(run 2>&1) || rc=$?
  printf '%s\n' "${out}"

  echo
  printf '%s\n' "${out}" | awk -f "${AN_TONE_OVERLAY}/parse.awk"

  _an_tone_verdict_rc "${out}"
}

# One case of theirs.
#
# Their run() walks the whole caselist, and a case the user switched off
# has no log for it to read, which their loop would report as a failure
# rather than as not asked for.  So a single case is built through their
# anck_build.py -- which is what names the log and composes the command
# line -- and then judged by their rule, which is anck-pack-and-boot's
# run.sh:291
#
#   tail -n 5 /tmp/anck_${case}.log | grep -q "$case: pass"
#
# and reported through their own show_result, so the marker and the word
# in the report are theirs.
an_tone_case() {
  local want="${1:?a case name}" keyword

  if ! keyword=$(_an_tone_keyword "${want}"); then
    echo "an_tone: their suite has no case '${want}'" >&2
    echo "  it reports: $(an_tone_cases | tr '\n' ' ')" >&2
    return 2
  fi

  _an_tone_prepare || return 2

  # Their anck_build.py selects by keyword; theirs builds all seven when
  # this is unset.
  export testcases="${keyword}"

  local log="/tmp/anck_${want}.log"
  rm -f "${log}"

  local out rc=0
  out=$(
    # Their branch gate, which their run.sh applies before reading the
    # log: on devel-4.19 their dist-configs-check does not exist.
    if [ "${KERNEL_CI_REPO_BRANCH}" = 'devel-4.19' ] && \
       [ "${want}" = 'check_Kconfig' ]; then
      skip "${want}"
      exit 0
    fi

    anck_build
    upload_archives "${log}"
    tail -n 5 "${log}" 2>/dev/null | grep -q "${want}: pass"
    show_result "${want}" $?
  ) || rc=$?

  # Their build output is the log their py wrote, not stdout.
  [ -f "${log}" ] && cat "${log}"
  printf '%s\n' "${out}"

  echo
  printf '%s\n' "${out}" | awk -f "${AN_TONE_OVERLAY}/parse.awk"

  _an_tone_verdict_rc "${out}"
}

# Their four markers, as four exit statuses.
#
# parse.awk is where their markers are spelled, and Warning is one of
# them: a case they print and still accept.  Folding it into pass would
# hide something they flagged and into fail would reject a series they
# let through, so it keeps a status of its own, matching the one
# anolis/cases/lib.sh already uses.
_an_tone_verdict_rc() {
  case "$1" in
    *'====FAIL:'*) return 1 ;;
    *'====WARN:'*) return 5 ;;
    *'====SKIP:'*) return 3 ;;
    *'====PASS:'*) return 0 ;;
  esac
  return 2
}

# Where their anck_rpm_build left its RPMs, which is where their own
# anck_boot_test reads them from: /anck_build/ck-build/outputs/0, as
# seen from outside the sandbox.
an_tone_rpm_dir() {
  local dir="${AN_TONE_SCRATCH}/anck_rpm_build/ck-build/outputs/0"
  [ -d "${dir}" ] || return 1
  echo "${dir}"
}

# The kernel version those RPMs will boot as.
#
# Their anck_boot_test works it out the same way, at
# anck-pack-and-boot/run.sh:201:
#
#   kver_new=$(find $anck_rpms_dir -name kernel-headers*.rpm | \
#       head -n 1 | xargs rpm -qp --queryformat="%{VERSION}-%{RELEASE}.%{ARCH}\n")
#
# Their anck-ci-test reads it as EXPECT_KERNEL_VERSION, and falls back
# to the newest installed kernel-headers with a warning when it is
# unset -- which on the VM would be whatever was there before.
an_tone_expect_kver() {
  local dir headers

  dir=$(an_tone_rpm_dir) || return 1
  headers=$(find "${dir}" -name 'kernel-headers*.rpm' | head -n 1)
  [ -n "${headers}" ] || return 1

  rpm -qp --queryformat='%{VERSION}-%{RELEASE}.%{ARCH}\n' "${headers}" \
     2>/dev/null
}

# Called as a script: --sandbox is the overlay's anck_build.sh calling
# back in, anything else is a case name.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-}" in
    --sandbox)      shift; _an_tone_sandbox "$@" ;;
    --rpm-dir)      an_tone_rpm_dir ;;
    --expect-kver)  an_tone_expect_kver ;;
    --cases)        an_tone_cases ;;
    '')             an_tone_suite ;;
    *)              an_tone_case "$1" ;;
  esac
fi
