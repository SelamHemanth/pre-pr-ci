#!/bin/bash
#
# What openEuler's build scripts are told by Jenkins.
#
# Their checkkabi.sh and checkbuild.sh run in euler/oe_hulk.sh, out of
# the submodule, unmodified.  Neither of them says which compiler to
# use, which architecture it is checking, or where the KABI whitelists
# are: their Jenkins job sets all of that before the script starts, one
# job per architecture, each on a node of the architecture it builds.
#
# There is no Jenkins here and one host for all seven architectures, so
# somebody has to answer those questions.  That is the whole of this
# file.  It decides nothing -- no row, no verdict, no exit status comes
# from here.  Every one of those is read out of their scripts.
#
# What it answers, and why their scripts cannot:
#
#   Which architectures exist, and which of them get checkkabi.sh rather
#   than checkbuild.sh.  Their Jenkins has a job per architecture and
#   each job names its own script; the matrix is read back out of their
#   conf/check_build.yaml through their own check_branch.py, so a
#   submodule update is all it takes to follow them.
#
#   Which kernel ARCH and which cross prefix go with each of their
#   architecture labels.  Their labels are not kernel ARCH values: ppc
#   and ppc64 are both ARCH=powerpc and differ only in the compiler.
#
#   Where the toolchain is.  Their setup_gcc unpacks one of their pinned
#   tarballs into /usr/local/$ARCH as root; a pre-submission check has
#   no business asking for that, so the same tarball goes under the
#   workspace.  Same compiler, same version, different prefix.
#
#   Which branch the whitelists are on.  Their get_check_kabi_script
#   clones src-openeuler/kernel every run to get check-kabi and the
#   three Module.kabi_* files.  We carry that repository as the
#   euler/kernel submodule, so this only has to put it on the branch
#   their get_kabi_whitelist_branch names.
#
#   Which -Wno-error= flags this compiler understands.  The one place we
#   can pass something they would fail, and deliberate: see
#   _OE_NO_WERROR.
#
#   Whether the series touches a Kconfig file.  Asked by one guard in
#   oe_hulk.sh and nowhere else.

# arch label -> kernel ARCH, cross prefix, toolchain tarball
#
# The labels on the left are openEuler's, the ones their
# conf/check_build.yaml is keyed by.  They are not kernel ARCH values:
# ppc and ppc64 are both ARCH=powerpc and differ only in the compiler.
_oe_arch_spec() {
  case "$1" in
    x86_64)    echo "x86_64  -                     -" ;;
    aarch64)   echo "arm64   aarch64-linux-        x86_64-gcc-12.2.0-nolibc-aarch64-linux.tar.gz" ;;
    arm)       echo "arm     arm-linux-gnueabi-    x86_64-gcc-12.2.0-nolibc-arm-linux-gnueabi.tar.gz" ;;
    ppc)       echo "powerpc powerpc-linux-        x86_64-gcc-12.2.0-nolibc-powerpc-linux.tar.gz" ;;
    ppc64)     echo "powerpc powerpc64-linux-      x86_64-gcc-12.2.0-nolibc-powerpc64-linux.tar.gz" ;;
    riscv64)   echo "riscv   riscv64-linux-        x86_64-gcc-12.2.0-nolibc-riscv64-linux.tar.gz" ;;
    loongarch) echo "loongarch loongarch64-linux-  -" ;;
    *)         return 1 ;;
  esac
}

# Every architecture named in their matrix, whatever it is set to.
#
# Their CI shows a check_build row for each of these, loongarch
# included, even though it is false on every branch in the file: the
# job exists and reports that it had nothing to do.  Read rather than
# copied, so a submodule update is all it takes to follow them.
#
# Deliberately not per-branch: which of them a given branch compiles is
# decided per run by _oe_branch_builds, and one that drops out there
# reports as skipped, which is what their job does too.
_oe_arches_they_build() {
  local conf="${SCRIPT_DIR}/hulk_robot_test/openEuler/conf/check_build.yaml"
  if [ ! -f "${conf}" ]; then
    # The submodule is not checked out, so every build would skip
    # anyway; naming none of them would just hide that.
    echo 'aarch64 arm x86_64 ppc ppc64 riscv64 loongarch'
    return 0
  fi
  awk -F: '
    /^[[:space:]]+[A-Za-z0-9_]+:[[:space:]]*(true|false)[[:space:]]*$/ {
      gsub(/[[:space:]]/, "", $1)
      if (!($1 in seen)) { seen[$1] = 1; order[++n] = $1 }
    }
    END { for (i = 1; i <= n; i++) printf "%s ", order[i] }
  ' "${conf}"
}

# The architectures whose ABI openEuler promises, and so the ones that get
# checkkabi.sh rather than checkbuild.sh.
_oe_arch_has_kabi() {
  [ "$1" = 'x86_64' ] || [ "$1" = 'aarch64' ]
}

# Their get_kabi_whitelist_branch: the whitelist for a kernel branch does
# not live on a branch of the same name.
_oe_kabi_branch() {
  case "$1" in
    openEuler-1.0-LTS)      echo 'openEuler-20.03-LTS-SP3' ;;
    OLK-5.10)               echo 'openEuler-22.03-LTS-Next' ;;
    OLK-6.6|openEuler-24.03*) echo 'openEuler-24.03-LTS-SP1' ;;
    *)                      echo "$1" ;;
  esac
}

# Ask their conf/check_build.yaml, through their own check_branch.py, so
# the matrix stays theirs to change.
#
# Their CI reads the exit status alone: zero build it, anything else skip
# it.  That is safe where the interpreter is known to work and dangerous
# here, because check_branch.py also exits 1 when its "import yaml" fails,
# and a missing module would then quietly report every architecture as one
# openEuler does not build.  A build gate that passes because it never ran
# is the worst outcome available, so a deliberate no is told apart from a
# broken script by the line their code prints on the way out.
#
#   0 build it, 3 openEuler does not build it here, 2 could not tell
_oe_arch_wanted() {
  local arch="$1" branch="$2" lib="$3"
  local out rc
  out=$(python3 "${lib}/check_branch.py" -b "${branch}" -a "${arch}" \
        -c check_build.yaml 2>&1)
  rc=$?
  [ -n "${out}" ] && echo "${out}"

  [ ${rc} -eq 0 ] && return 0
  case "${out}" in
    *'is set to false'*) return 3 ;;
  esac
  echo "check_branch.py could not answer for ${arch} on ${branch}" >&2
  return 2
}

# Unpack one of their pinned toolchains, once, and put it on PATH.
_oe_setup_gcc() {
  local arch="$1" tarball="$2" tools="$3" cache="$4"
  local stamp="${cache}/${arch}/.unpacked"

  if [ ! -f "${tools}/${tarball}" ]; then
    echo "toolchain ${tarball} is missing from the submodule" >&2
    return 1
  fi

  if [ ! -f "${stamp}" ]; then
    echo "  -> unpacking ${tarball}"
    rm -rf "${cache:?}/${arch}"
    mkdir -p "${cache}/${arch}" || return 1
    tar -zxf "${tools}/${tarball}" -C "${cache}/${arch}" || return 1
    touch "${stamp}"
  fi

  # The tarballs all unpack to gcc-12.2.0-nolibc/<triple>/bin.
  local bin
  bin=$(find "${cache}/${arch}" -maxdepth 3 -type d -name bin | head -1)
  if [ -z "${bin}" ]; then
    echo "no bin directory inside ${tarball}" >&2
    return 1
  fi
  # LD_LIBRARY_PATH is normally unset, and test.sh runs under "set -u",
  # so appending to it directly aborts the test before the compiler is
  # ever invoked.  Their script gets away with it because their builder
  # exports one; ours does not.
  export PATH="${PATH}:${bin}"
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+${LD_LIBRARY_PATH}:}${bin%/bin}/lib"
}

# Warnings stay warnings.
#
# Their gate has a "build warning" row, so a warning already has a place
# to be reported and does not need to stop the build to be noticed.  What
# makes -Werror wrong here rather than there is the compiler: their
# builder has the one their branch was written against, and we have
# whichever one the user's distribution ships.  OLK-6.6 does not compile
# its own hinic3 and hinic5 drivers under gcc 12.3 for that reason alone,
# on a branch whose own CI builds both configs green.  A local gate that
# rejects a series over that is predicting a verdict openEuler will not
# reach.
#
# Four variables, because the kernel assembles a compile in more than one
# place and a flag that reaches only some of them answers only some of
# the -Werror.  KCFLAGS and KAFLAGS are appended after everything
# scripts/Makefile.extrawarn set, so they land last; CFLAGS_KERNEL and
# CFLAGS_MODULE cover the built-in and modular halves, which is the half
# that matters under allmodconfig because almost everything is a module
# there.  Together they answer a blanket -Werror from CONFIG_WERROR,
# which allmodconfig turns on, and the explicit -Werror=<name> flags
# Makefile.extrawarn adds with no config behind them.
#
# Exported around the build rather than put on a command line, because
# the make lines they reach are openEuler's and are not ours to edit.
#
# Set per architecture by _oe_set_no_werror.  What is here is the part
# that is always safe and never sufficient on its own.
_OE_NO_WERROR_FLAGS='-Wno-error'
_OE_NO_WERROR=(KCFLAGS=-Wno-error KAFLAGS=-Wno-error)

# The -Werror=<name> flags this tree's makefiles set, as the
# -Wno-error=<name> that answers each, keeping only those this compiler
# understands.
#
# -Wno-error on its own is not enough, and that is the easy thing to get
# wrong: it undoes a blanket -Werror and leaves every explicit
# -Werror=<name> standing.  -Werror=designated-init is the one OLK-6.6's
# hinic drivers trip over, and it has to be answered by name.
#
# The names come out of the tree rather than a list here, so a kernel
# that adds one is covered.  Several of them are clang's, and handing gcc
# a -Wno-error= for a warning it does not have is a hard error -- which
# would fail every file in the tree instead of the one it was aimed at --
# so each candidate is put to the compiler once before it is used.
_oe_no_werror_names() {
  local cc="$1" probe flag accepted=''

  probe=$(mktemp --suffix=.c) || return 0
  printf 'int main(void) { return 0; }\n' > "${probe}"
  for flag in $(grep -rhoE '\-Werror=[A-Za-z0-9-]+' Makefile scripts/Makefile.* \
                  2>/dev/null | sed 's/^-Werror=/-Wno-error=/' | sort -u); do
    "${cc}" -fsyntax-only "${probe}" "${flag}" >/dev/null 2>&1 &&
      accepted="${accepted} ${flag}"
  done
  rm -f "${probe}"
  printf '%s' "${accepted# }"
}

# Call from the kernel root, once per architecture: the flag list depends
# on the tree's makefiles and on which compiler is about to read them.
_oe_set_no_werror() {
  local cc="${1}gcc" names

  command -v "${cc}" >/dev/null 2>&1 || cc='gcc'
  names=$(_oe_no_werror_names "${cc}")
  _OE_NO_WERROR_FLAGS="-Wno-error${names:+ ${names}}"
  _OE_NO_WERROR=("KCFLAGS=${_OE_NO_WERROR_FLAGS}"
                 "KAFLAGS=${_OE_NO_WERROR_FLAGS}")
}

# The Kconfig symbols in this tree that turn warnings into errors.
#
# The command line is only half of it.  CONFIG_WERROR is a config bit,
# and allmodconfig turns it on -- along with COMPILE_TEST, which is what
# drags in the drivers that do not survive a compiler their branch was
# never built with.  openeuler_defconfig ships it off, which is why
# their defconfig build does not need any of this.
#
# Read out of the tree rather than listed here: WERROR is the one that
# matters, but amdgpu, i915, kvm and powerpc each have their own, and a
# kernel that gains another should be covered without anyone having to
# notice.  Costs a quarter of a second against a build measured in tens
# of minutes.
_oe_werror_symbols() {
  grep -rhoE '^[[:space:]]*(menu)?config[[:space:]]+[A-Za-z0-9_]*WERROR[A-Za-z0-9_]*' \
    --include='Kconfig*' . 2>/dev/null | awk '{print $2}' | sort -u
}

# Could this series have introduced a Kconfig symbol at all?
#
# A symbol is offered by Kconfig because some Kconfig file says so, so a
# series that does not touch one cannot have added any.  One git diff,
# no checkout, nothing built -- which matters because the tree we are
# given is often dirty and a checkout there fails outright.
#
# Returns 0 when the series touches a Kconfig file, or when there is no
# series to ask about.  Says yes when it cannot tell: this only ever
# guards against blaming a patch for something, and an unanswerable
# question must not be the thing that excuses one.
_oe_series_touches_kconfig() {
  local back="$1"

  [ "${back}" -gt 0 ] || return 0
  git rev-parse --verify -q "HEAD~${back}" >/dev/null || return 0
  git diff --name-only "HEAD~${back}" HEAD 2>/dev/null \
    | grep -q 'Kconfig'
}

# Their get_check_kabi_script clones src-openeuler/kernel every run to get
# check-kabi and the whitelists.  We already carry that repo as the
# euler/kernel submodule, so this only has to put it on the right branch.
_oe_prepare_whitelists() {
  local branch
  branch=$(_oe_kabi_branch "$1")

  [ -e "${KABI_KERNEL_DIR}/.git" ] || return 1

  local on
  on=$(git -C "${KABI_KERNEL_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null)
  [ "${on}" = "${branch}" ] && return 0

  echo "  -> KABI whitelists: ${branch}"
  git -C "${KABI_KERNEL_DIR}" checkout -q "${branch}" 2>/dev/null && return 0
  git -C "${KABI_KERNEL_DIR}" fetch -q origin "${branch}" 2>/dev/null &&
    git -C "${KABI_KERNEL_DIR}" checkout -q "${branch}" 2>/dev/null
}
