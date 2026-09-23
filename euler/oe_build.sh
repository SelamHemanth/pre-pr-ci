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
  export PATH="${PATH}:${bin}"
  export LD_LIBRARY_PATH="${LD_LIBRARY_PATH}:${bin%/bin}/lib"
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
  local whitelists="$6" warnings="$7" result="$8"

  cd "${kernel}" || return 1

  echo "  -> building allmodconfig for ${kernel_arch}"
  make clean >/dev/null 2>&1
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" allmodconfig \
    >/dev/null 2>&1
  make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" oldconfig >/dev/null 2>&1
  if make ARCH="${kernel_arch}" CROSS_COMPILE="${cross}" -j"${jobs}" \
      >/dev/null 2>"${warnings}"; then
    echo "| ${arch} allmodconfig build | pass |" >> "${result}"
  else
    echo "| ${arch} allmodconfig build | fail |" >> "${result}"
  fi

  # Everything below needs openeuler_defconfig, which only an openEuler
  # tree has.  Their CI never sees anything else; we might, and a missing
  # config is not a broken patch.
  local config_arch="${kernel_arch}"
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
  fi

  _oe_check_kabi "${kernel}" "${arch}" "${whitelists}" "${warnings}" \
    "${result}"
  _oe_check_defconfig "${kernel}" "${kernel_arch}" "${arch}" "${warnings}" \
    "${result}"
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

# Their check_defconfig: a patch that adds a Kconfig symbol has to add it
# to the shipped defconfig too, or the next build silently loses it.
_oe_check_defconfig() {
  local kernel="$1" kernel_arch="$2" arch="$3" warnings="$4" result="$5"
  local defconfig="${kernel}/arch/${kernel_arch}/configs/openeuler_defconfig"

  [ -f "${defconfig}" ] || return 0

  cd "${kernel}" || return 1
  cp "${defconfig}" .config
  if make listnewconfig 2>/dev/null | grep -E '^CONFIG_.*' >> "${warnings}"
  then
    echo "| ${arch} checkdefconfig | fail |" >> "${result}"
    {
      echo "The configs listed above are introduced but the"
      echo "openeuler_defconfig for ${arch} is not updated; configure and"
      echo "run 'make update_oedefconfig' to update it."
    } >> "${warnings}"
  else
    echo "| ${arch} checkdefconfig | pass |" >> "${result}"
  fi
}

# Entry point.  Prints a report and answers:
#   0 everything passed, 1 something failed, 2 could not run,
#   3 this architecture is not built on this branch
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
      "${arch}" "${KABI_KERNEL_DIR}" "${warnings}" "${result}"
  else
    # The row records whether make succeeded.  Whether it complained on
    # the way is a separate question, asked once below for both paths, so
    # that their branch exemptions get a say -- counting a warning as a
    # failed row here would decide the verdict before they are consulted.
    if _oe_cross_build "${kernel}" "${kernel_arch}" "${cross}" "${jobs}" \
        "${back}" "${warnings}"; then
      echo "| ${arch} allmodconfig build | pass |" >> "${result}"
    else
      echo "| ${arch} allmodconfig build | fail |" >> "${result}"
    fi
  fi

  echo
  cat "${result}"
  echo

  local rc=0
  if grep -q 'fail' "${result}"; then
    rc=1
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
