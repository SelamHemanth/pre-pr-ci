#!/bin/bash
#
# openEuler's build gate, run the way openEuler runs it.
#
# Their CI has two build scripts and picks between them by architecture:
#
#   checkkabi.sh   x86_64 and aarch64.  Builds allmodconfig, builds
#                  openeuler_defconfig, compares the result against the
#                  three KABI whitelists, and checks that the shipped
#                  openeuler_defconfig still covers every symbol the patch
#                  introduced.  These are the architectures openEuler
#                  ships, so these are the ones whose ABI is a promise.
#
#   checkbuild.sh  arm, powerpc, powerpc64, riscv64, loongarch.  Builds
#                  allmodconfig with a pinned cross toolchain, twice: once
#                  before the patches and once after.  The first build is
#                  not checked, it is there so the second one is
#                  incremental and its stderr contains warnings from the
#                  files the patch touched and nothing else.  A warning
#                  that the patch did not introduce is not the submitter's
#                  problem, and this is how their gate tells the
#                  difference.
#
# This file follows both, with two deliberate departures.
#
# Their setup_gcc unpacks a toolchain into /usr/local/$ARCH with sudo.  A
# pre-submission check has no business asking for root, so the same
# tarballs are unpacked under the workspace instead.  Same compiler, same
# version, different prefix.
#
# Their scripts clone the kernel and apply the PR with git am.  We are
# handed a tree that already has the commits on it, so where they say "the
# state before the patch" we say HEAD~NUM_PATCHES.

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

# The architectures whose ABI openEuler promises, and so the ones that get
# checkkabi.sh rather than checkbuild.sh.
_oe_arch_has_kabi() {
  [ "$1" = 'x86_64' ] || [ "$1" = 'aarch64' ]
}

# ARCH -> the directory under arch/ that holds it, which is what the
# kernel build calls SRCARCH.
#
# For every architecture here but one the two are the same word, which
# is why passing ARCH where a path was wanted went unnoticed: it only
# breaks on x86_64, whose configs live in arch/x86.  The effect was that
# openeuler_defconfig was reported as "not in this tree" on the one
# architecture everybody builds, taking the defconfig build, the kabi
# check and the defconfig consistency check silently with it.
_oe_srcarch() {
  case "$1" in
    x86_64|i386) echo 'x86' ;;
    sparc32|sparc64) echo 'sparc' ;;
    parisc64) echo 'parisc' ;;
    *) echo "$1" ;;
  esac
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

# The first few compiler errors, named, under the row that reports them.
#
# A build that failed and said nothing about why is a result nobody can
# act on.  It matters most in the case below, where the answer is "not
# your series": without the file name that reads as the tool excusing
# itself, and the file name is usually the whole explanation -- a driver
# this tree has never compiled here, or one fixed on the branch since
# the tree was taken.
_oe_report_errors() {
  local warnings="$1" limit="${2:-4}" lines

  # One line per file, not per error: a single bad struct produces a
  # dozen, and four of them from one driver hide the other drivers
  # that are the point of the list.
  lines=$(grep -aoE '[^ ]+\.[chS]:[0-9]+:[0-9]+: (fatal )?error: .*' \
          "${warnings}" 2>/dev/null \
          | awk -F: '!seen[$1]++' | head -n "${limit}")
  [ -n "${lines}" ] || return 0

  echo "  -> first error(s), one per file:"
  # Long include-relative paths wrap into unreadability; the directory
  # is what identifies the driver, so keep the head of the line.
  echo "${lines}" | cut -c1-160 | sed 's/^/       /'
}

# Was the tree already like this before the series?
#
# openEuler's CI never asks, because it builds their branch on their
# builder with their compiler, so a failure there really is the
# submitter's.  We build whatever tree the user points us at with
# whatever gcc they have, and those two disagree: OLK-6.6 does not
# compile its own hinic3 and hinic5 drivers under gcc 12.3, which has
# nothing to do with anybody's patch.  Reporting that as "your series
# was rejected" is worse than not checking, because it trains people to
# ignore the result.
#
# Asked only after something has already failed, so a healthy tree pays
# nothing for it.  Returns 0 when the failure is pre-existing.
_oe_failed_before_the_series() {
  local kernel="$1" kernel_arch="$2" cross="$3" jobs="$4" back="$5"
  local baseline="$6"
  local head rc

  [ "${back}" -gt 0 ] || return 1
  cd "${kernel}" || return 1
  head=$(git rev-parse HEAD) || return 1
  git rev-parse --verify -q "HEAD~${back}" >/dev/null || return 1

  echo "  -> it failed; rebuilding at HEAD~${back} to see whose fault it is"
  git checkout -q "HEAD~${back}" || return 1
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" allmodconfig \
    >/dev/null 2>&1
  # Kept, not discarded: when this build fails too, its errors are the
  # evidence that the breakage predates the series, and they are what
  # the row above is asserting.
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" -j"${jobs}" \
    >/dev/null 2>"${baseline}"
  rc=$?
  git checkout -q "${head}" || return 1

  [ ${rc} -ne 0 ]
}

# checkbuild.sh.  Baseline build, then the patches, then an incremental
# build whose stderr is the answer.
_oe_cross_build() {
  local kernel="$1" kernel_arch="$2" cross="$3" jobs="$4" back="$5"
  local warnings="$6"

  cd "${kernel}" || return 1

  local head
  head=$(git rev-parse HEAD) || return 1

  if [ "${back}" -gt 0 ] && git rev-parse --verify -q "HEAD~${back}" >/dev/null
  then
    echo "  -> baseline build at HEAD~${back}, warnings from it are not yours"
    git checkout -q "HEAD~${back}" || return 1
    make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" allmodconfig \
      >/dev/null 2>&1
    make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" -j"${jobs}" \
      >/dev/null 2>&1
    git checkout -q "${head}" || return 1
  else
    echo "  -> no baseline available, every warning will be reported"
  fi

  echo "  -> building allmodconfig for ${kernel_arch}"
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" allmodconfig \
    >/dev/null 2>&1
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" -j"${jobs}" \
    >/dev/null 2>"${warnings}"
}

# checkkabi.sh.  Four checks, each a row in their result table.
_oe_kabi_build() {
  local kernel="$1" kernel_arch="$2" cross="$3" jobs="$4" arch="$5"
  local whitelists="$6" warnings="$7" result="$8" back="$9"

  cd "${kernel}" || return 1

  echo "  -> building allmodconfig for ${kernel_arch}"
  make clean >/dev/null 2>&1
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" allmodconfig \
    >/dev/null 2>&1
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" oldconfig >/dev/null 2>&1
  if make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" -j"${jobs}" \
      >/dev/null 2>"${warnings}"; then
    echo "| ${arch} allmodconfig build | pass |" >> "${result}"
  elif _oe_failed_before_the_series "${kernel}" "${kernel_arch}" \
      "${cross}" "${jobs}" "${back}" "${warnings}.baseline"; then
    echo "| ${arch} allmodconfig build | broken already, not your series |" \
      >> "${result}"
    _oe_report_errors "${warnings}.baseline"
    # The warnings belong to the same pre-existing breakage, and
    # keeping them would fail the series through the warning gate
    # after the row above declined to.
    : > "${warnings}"
  else
    echo "| ${arch} allmodconfig build | fail |" >> "${result}"
    _oe_report_errors "${warnings}"
  fi

  # Everything below needs openeuler_defconfig, which only an openEuler
  # tree has.  Their CI never sees anything else; we might, and a missing
  # config is not a broken patch.
  local config_arch
  config_arch=$(_oe_srcarch "${kernel_arch}")
  if [ ! -f "arch/${config_arch}/configs/openeuler_defconfig" ]; then
    echo "| ${arch} openeuler_defconfig | skip, not in this tree |" \
      >> "${result}"
    return 0
  fi

  echo "  -> building openeuler_defconfig for ${kernel_arch}"
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" openeuler_defconfig \
    >/dev/null 2>&1
  # Their aarch64 job raises the frame-size warning threshold; without it
  # the build trips over frames that the shipped config accepts.
  if [ "${arch}" = 'aarch64' ] && grep -q 'CONFIG_FRAME_WARN' .config; then
    sed -i 's/CONFIG_FRAME_WARN=.*/CONFIG_FRAME_WARN=4096/' .config
  fi
  if make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" -j"${jobs}" \
      >/dev/null 2>>"${warnings}"; then
    echo "| ${arch} openeuler_defconfig build | pass |" >> "${result}"
  else
    echo "| ${arch} openeuler_defconfig build | fail |" >> "${result}"
    _oe_report_errors "${warnings}"
  fi

  _oe_check_kabi "${kernel}" "${arch}" "${whitelists}" "${warnings}" \
    "${result}"
  _oe_check_defconfig "${kernel}" "${kernel_arch}" "${arch}" "${warnings}" \
    "${result}" "${back}"
}

# Their check_kabi, using their check-kabi script against the three
# whitelists that ship beside it.  Not every branch has all three, and a
# whitelist that is absent is not a failure -- that is how they say the
# ABI is not promised here.
_oe_check_kabi() {
  local kernel="$1" arch="$2" whitelists="$3" warnings="$4" result="$5"
  local tool="${whitelists}/check-kabi"

  if [ ! -x "${tool}" ] && ! chmod +x "${tool}" 2>/dev/null; then
    echo "| ${arch} checkkabi | skip, no check-kabi script |" >> "${result}"
    return 0
  fi
  if [ ! -f "${kernel}/Module.symvers" ]; then
    echo "| ${arch} checkkabi | skip, no Module.symvers |" >> "${result}"
    return 0
  fi

  local kind list label
  for kind in '' 'ext1_' 'ext2_'; do
    list="${whitelists}/Module.kabi_${kind}${arch}"
    label="${arch} ${kind:+${kind%_} }checkkabi"
    [ -f "${list}" ] || continue
    if "${tool}" -k "${list}" -s "${kernel}/Module.symvers" \
        2>>"${warnings}"; then
      echo "| ${label} | pass |" >> "${result}"
    else
      echo "| ${label} | fail |" >> "${result}"
    fi
  done
}

# The symbols Kconfig would offer that the shipped defconfig does not
# answer, which is their whole check_defconfig.
_oe_new_symbols() {
  local defconfig="$1"

  [ -f "${defconfig}" ] || return 0
  cp "${defconfig}" .config || return 1
  make listnewconfig 2>/dev/null | grep -E '^CONFIG_' || true
}

# Their check_defconfig: a patch that adds a Kconfig symbol has to add it
# to the shipped defconfig too, or the next build silently loses it.
#
# Whether a symbol is "new" depends on the host as much as on the tree.
# Kconfig only offers GCC_PLUGINS and the RANDSTRUCT choices where the
# compiler's plugin headers are installed, so a developer machine with
# gcc-plugin-devel reports five symbols their builder never sees, none
# of which any patch went near.  Their CI can read the raw list because
# it builds a known branch in a known container; we cannot.
#
# So ask the same question of the tree before the series and keep the
# difference.  That is the only part a patch can be answerable for.
_oe_check_defconfig() {
  local kernel="$1" kernel_arch="$2" arch="$3" warnings="$4" result="$5"
  local back="${6:-0}"
  local src defconfig
  src=$(_oe_srcarch "${kernel_arch}")
  defconfig="${kernel}/arch/${src}/configs/openeuler_defconfig"

  [ -f "${defconfig}" ] || return 0
  cd "${kernel}" || return 1

  local mine
  mine=$(_oe_new_symbols "${defconfig}")
  if [ -z "${mine}" ]; then
    echo "| ${arch} checkdefconfig | pass |" >> "${result}"
    return 0
  fi

  local before='' head
  if [ "${back}" -gt 0 ] && git rev-parse --verify -q "HEAD~${back}" >/dev/null
  then
    head=$(git rev-parse HEAD) || return 1
    if git checkout -q "HEAD~${back}" 2>/dev/null; then
      before=$(_oe_new_symbols "arch/${src}/configs/openeuler_defconfig")
      git checkout -q "${head}" || return 1
    fi
  fi

  local added
  added=$(comm -23 <(printf '%s\n' "${mine}" | sort -u) \
                   <(printf '%s\n' "${before}" | sort -u))

  if [ -z "${added}" ]; then
    echo "| ${arch} checkdefconfig | pass |" >> "${result}"
    # Counted, not listed.  The only symbols worth a reader's attention
    # are the ones a patch could do something about, and these are not
    # those: they are unanswered on this machine whatever is checked
    # out.  Said on the log rather than in the warnings file, which is
    # a gate, because it is a fact about the host and must not fail the
    # run.
    echo "  -> $(printf '%s\n' "${mine}" | wc -l) symbol(s) are unanswered"
    echo "     before the series as well, so they are this host's offering"
    echo "     and not the series' doing; not listed."
    return 0
  fi

  echo "| ${arch} checkdefconfig | fail |" >> "${result}"
  {
    printf '%s\n' "${added}"
    echo "The configs listed above are introduced but the"
    echo "openeuler_defconfig for ${arch} is not updated; configure and"
    echo "run 'make update_oedefconfig' to update it."
  } >> "${warnings}"
}

# Entry point.  Prints a report and answers:
#   0 everything passed, 1 something failed, 2 could not run,
#   3 this architecture is not built on this branch,
#   4 the tree does not build without the series either
oe_build_arch() {
  local arch="$1"
  local kernel="${LINUX_SRC_PATH}"
  local sub="${SCRIPT_DIR}/hulk_robot_test/openEuler"
  local branch="${OE_TARGET_BRANCH:-OLK-6.6}"
  local jobs="${BUILD_THREADS:-$(nproc)}"
  local back="${NUM_PATCHES:-0}"

  local spec kernel_arch cross tarball
  spec=$(_oe_arch_spec "${arch}") || { echo "unknown arch ${arch}" >&2; return 2; }
  read -r kernel_arch cross tarball <<< "${spec}"
  [ "${cross}" = '-' ] && cross=''

  if [ ! -d "${sub}/lib" ]; then
    echo "hulk_robot_test is not checked out" >&2
    return 2
  fi

  _oe_arch_wanted "${arch}" "${branch}" "${sub}/lib" || return $?

  local scratch
  scratch=$(mktemp -d) || return 2
  local warnings="${scratch}/build_output.txt"
  local result="${scratch}/result"
  : > "${warnings}"
  echo "| check | result |" > "${result}"

  if [ "${tarball}" != '-' ]; then
    _oe_setup_gcc "${arch}" "${tarball}" "${sub}/tools" \
      "${WORKDIR}/.toolchains" || { rm -rf "${scratch}"; return 2; }
  elif [ -n "${cross}" ]; then
    echo "no toolchain ships for ${arch}; openEuler does not build it" >&2
    rm -rf "${scratch}"
    return 2
  fi

  if _oe_arch_has_kabi "${arch}"; then
    _oe_prepare_whitelists "${branch}" || {
      echo "  -> KABI whitelists unavailable, the ABI will not be compared"
    }
    _oe_kabi_build "${kernel}" "${kernel_arch}" "${cross}" "${jobs}" \
      "${arch}" "${KABI_KERNEL_DIR}" "${warnings}" "${result}" "${back}"
  else
    # The row records whether make succeeded.  Whether it complained on
    # the way is a separate question, asked once below for both paths, so
    # that their branch exemptions get a say -- counting a warning as a
    # failed row here would decide the verdict before they are consulted.
    if _oe_cross_build "${kernel}" "${kernel_arch}" "${cross}" "${jobs}" \
        "${back}" "${warnings}"; then
      echo "| ${arch} allmodconfig build | pass |" >> "${result}"
    elif _oe_failed_before_the_series "${kernel}" "${kernel_arch}" \
        "${cross}" "${jobs}" "${back}"; then
      echo "| ${arch} allmodconfig build | broken already, not your series |" \
        >> "${result}"
      : > "${warnings}"
    else
      echo "| ${arch} allmodconfig build | fail |" >> "${result}"
    fi
  fi

  echo
  cat "${result}"
  echo

  # "broken already" deliberately does not contain "fail", so a tree
  # that does not compile without the series reports as unbuildable
  # rather than as a rejected patch.  The distinction is the difference
  # between a result somebody acts on and one they learn to ignore.
  local rc=0
  if grep -q '| fail |' "${result}"; then
    rc=1
  elif grep -q 'broken already' "${result}"; then
    rc=4
  fi

  if [ -s "${warnings}" ]; then
    echo "build warnings:"
    cat "${warnings}"
    echo
    # Their one exemption, kept: openEuler-1.0-LTS is old enough that its
    # warnings are nobody's fault, and OLK-5.10 powerpc likewise.
    if [ "${branch}" != 'openEuler-1.0-LTS' ] && \
       ! { [ "${branch}" = 'OLK-5.10' ] && [ "${kernel_arch}" = 'powerpc' ]; }
    then
      rc=1
    fi
  fi

  rm -rf "${scratch}"
  return ${rc}
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
