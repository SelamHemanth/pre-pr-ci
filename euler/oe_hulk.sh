#!/bin/bash
#
# openEuler's build gate, run out of openEuler's own scripts.
#
# euler/oe_build.sh reimplements checkkabi.sh and checkbuild.sh in our
# own shell.  It tracks them closely, but a reimplementation is a second
# thing to keep in step, and every time theirs changes ours is wrong
# until somebody notices.  This file does not reimplement them.  It
# sources them out of the submodule and calls their functions.
#
# Their scripts are written for one machine: a Jenkins node that clones
# the kernel, applies a pull request to it, installs a toolchain as
# root, and posts the verdict back to the pull request.  None of that is
# available before submission, and all of it is in a handful of named
# functions, so the whole adaptation is:
#
#   1. source their script with its "main" call stripped, which defines
#      their functions and nothing else,
#   2. redefine the functions that reach for Jenkins, the network, sudo
#      or a fresh clone,
#   3. call their main.
#
# What is left untouched is everything that decides a verdict:
# build_allmodconfig, build_defconfig, check_kabi, check_defconfig, the
# layout checks, and main itself -- the row labels, the order, the
# branch exemptions and the exit status are all read from their file.
#
# The shims are below, each with the reason it exists.  There are only
# two places where a shim does more than stand in for infrastructure,
# and both are called out where they appear: the tree we build in is the
# user's and not a clone, and the base revision is HEAD~NUM_PATCHES and
# not an unmerged clone.

# euler/oe_build.sh is where the host-side knowledge lives: which kernel
# ARCH and which toolchain tarball go with each of their architecture
# labels, how to unpack one, which branch the whitelists are on, and
# which -Wno-error= flags this compiler understands.  None of that is in
# their scripts, because their builder is told it by Jenkins.
if ! declare -f _oe_arch_spec >/dev/null 2>&1; then
  # shellcheck source=/dev/null
  . "$(dirname "${BASH_SOURCE[0]}")/oe_build.sh"
fi

# Their log_info/log_warn/log_error come from openeuler-jenkins, which
# import_openeuler_jenkins clones at the top of every run.  Cloning a
# repository to get three echo wrappers is not worth a network round
# trip, so they are here, in the format their console shows.
#
# log_error exits.  That is not stated anywhere we can read, but their
# powerpc job ends on the line after "[ERROR] build failed" with the
# build marked failed, having never reached git_am_pr or the second
# build that follows it -- so log_error is fatal and the rest of
# build_kernel never ran.
_hulk_stamp() { date '+%Y-%m-%d %H:%M:%S'; }
log_info()  { echo "[$(_hulk_stamp)] [ INFO ] $*"; }
log_warn()  { echo "[$(_hulk_stamp)] [WARNING] $*"; }
log_warning() { log_warn "$@"; }
log_error() { echo "[$(_hulk_stamp)] [ERROR] $*" >&2; exit 1; }

# Their $test_path, assembled as a directory of links to theirs.
#
# A copy rather than their directory itself, for two reasons.
# pr_comment_api.py has to be replaced, and writing into a submodule
# checkout would show up as a local modification of theirs.  And their
# check_layout_prepare runs `make -C $test_path/lib/kabi_guard dtg`,
# which drops an object file and a binary wherever it is pointed; that
# is their build artefact to make, but not in their checkout.
#
# Rebuilt from the submodule every run, so updating the submodule is
# still all it takes to follow them.  lib/ and conf/ together are a few
# hundred kilobytes of script.
#
# check_branch.py finds conf/ with os.path.dirname(os.path.abspath(
# __file__)), so conf/ has to sit beside the lib/ we hand it.
_hulk_overlay() {
  local sub="$1" dir="$2"

  rm -rf "${dir:?}"
  mkdir -p "${dir}" || return 1
  cp -r "${sub}/lib" "${sub}/conf" "${dir}/" || return 1

  # pr_comment_api.py posts the gate's verdict as a comment on the pull
  # request.  There is no pull request yet -- that is the point of
  # running this locally -- and it is called with a Jenkins API token we
  # do not have.
  cat > "${dir}/lib/pr_comment_api.py" <<'EOF'
# Stub: the real one comments on a pull request that does not exist yet.
import sys
sys.exit(0)
EOF
}

# Define their functions without running their main.
#
# Both scripts end in `main "$@"`, so sourcing one would run it before
# any shim is in place.  Dropping that one line leaves a file of
# definitions, which is what we want to source.  Their own `.
# $test_path/lib/common.sh` runs as part of it and defines the rest.
_hulk_load() {
  local script="$1"
  [ -f "${script}" ] || { echo "${script} is not checked out" >&2; return 1; }
  # shellcheck disable=SC1090
  . <(grep -v '^main "\$@"[[:space:]]*$' "${script}")
}

# import_openeuler_jenkins: clones openeuler-jenkins for its lib.sh and
# for PYTHONPATH.  The logging functions it brings are defined above;
# nothing else in it is reachable once pr_comment_api.py is a stub.
# $owner and $jenkins_api_host are set because their code reads them.
_hulk_shim_jenkins() {
  import_openeuler_jenkins() {
    owner="openeuler"
    jenkins_api_host="https://ci.openeuler.openatom.cn/"
  }

  # git_config: sets user.email, user.name and four http tuning knobs
  # --global, for the git am and the clones.  We apply no patches and
  # clone nothing, and rewriting the user's global git config to run a
  # build check would be indefensible.
  git_config() { :; }

  # install_build_tools: sudo yum install, and on OLK-6.6 a sudo sed
  # that repoints /etc/yum.repos.d/openEuler.repo at a newer release to
  # pull newer dwarves and binutils.  A pre-submission check does not
  # get to edit the system's repositories or install packages.  The
  # versions it prints are kept, because which pahole and which
  # binutils built the tree is the first thing asked about a build that
  # behaved differently from theirs.
  install_build_tools() {
    log_info "***** Start to install build tools *****"
    echo "gcc version: $(gcc --version 2>/dev/null | head -n1)"
    echo "pahole version: $(pahole --version 2>/dev/null)"
    echo "binutils version: $(ld --version 2>&1 | head -n1)"
    log_info "***** End to install build tools *****"
  }
}

# get_check_kabi_script: clones src-openeuler/kernel from gitcode, every
# run, for check-kabi and the three Module.kabi_* whitelists, and checks
# out the branch get_kabi_whitelist_branch names.  We carry that
# repository as the euler/kernel submodule already.
#
# Copied rather than linked into the path their check_kabi reads,
# because their next line is `chmod +x check-kabi`, and chmod through a
# link would change the mode of a file inside the submodule.
#
# KABI_CHECK is theirs: 0 means the whitelist branch could not be had,
# and their check_kabi then skips rather than failing.
_hulk_shim_whitelists() {
  get_check_kabi_script() {
    log_info "***** Get the corresponding check-kabi script *****"
    local want src dst f
    want=$(get_kabi_whitelist_branch)
    src="${KABI_KERNEL_DIR}"
    dst="${current_path}/src-openeuler/kernel"

    mkdir -p "${dst}" || { KABI_CHECK=0; return 0; }
    if ! _oe_prepare_whitelists "${tbranch}"; then
      log_warn "kabi whitelist branch ${want} is unavailable"
      KABI_CHECK=0
      return 0
    fi
    for f in check-kabi "Module.kabi_${arch}" "Module.kabi_ext1_${arch}" \
             "Module.kabi_ext2_${arch}"; do
      [ -f "${src}/${f}" ] && cp -f "${src}/${f}" "${dst}/${f}"
    done
    if [ ! -f "${dst}/check-kabi" ]; then
      KABI_CHECK=0
      return 0
    fi
    chmod +x "${dst}/check-kabi" || KABI_CHECK=0
  }
}

# download_openeuler_kernel: copies a reference clone, fetches the pull
# request and merges it, leaving $current_path/openeuler/kernel-$BUILD_ID
# as the branch with the series on top.
#
# We are handed that tree instead.  So the shim puts a link where their
# clone would be, and the departure is not the link -- it is what the
# link points at:
#
#   Their tree is new every run; ours has been built in before, for
#   other architectures.  Their `make clean` does not help, because the
#   kernel keeps .config, Module.symvers and arch/*/include/generated
#   under mrproper and not clean, so a stale Module.symvers from another
#   architecture's build would survive into check_kabi, which reads it
#   with no way to know what produced it.  Hence distclean here, which
#   is what "a clone" means for a tree that is not one.
#
# Their main() finishes with `rm -rf .../kernel-$BUILD_ID`.  rm removes
# a symbolic link rather than following it, so their cleanup takes the
# link and leaves the user's tree alone.
_hulk_shim_kernel() {
  download_openeuler_kernel() {
    log_info "***** Start to download kernel of openeuler *****"
    local dst="${current_path}/openeuler/kernel-${BUILD_ID}"
    mkdir -p "${current_path}/openeuler" || return 1
    rm -rf "${dst}"
    ln -sfn "${LINUX_SRC_PATH}" "${dst}" || return 1
    cd "${dst}" || return 1
    make distclean >/dev/null 2>&1
    rm -f Module.symvers
    log_info "***** End to download kernel of openeuler *****"
  }
}

# The kABI layout check: check_layout_prepare, check_layout_new,
# build_defconfig_base, report_layout.
#
# Not run, and this is the only check of theirs we decline.  The
# submodule is ahead of what openEuler deploys: their aarch64, x86_64
# and powerpc jobs on 2026-09-30 print no "Prepare kABI layout check"
# line and produce no checklayout row, so this code is in their
# repository but not yet in their gate.  Running it would report a row
# their CI does not, which is the one thing a pre-submission check must
# not do.
#
# It also cannot answer here even when it works.  build_defconfig_base
# gets its baseline with `git checkout origin/$tbranch` in the build
# tree -- a scratch clone for them, the tree your series lives in for
# us -- and kabi-guard.py needs the ./vmlinux of a defconfig build that
# succeeded.  Without the baseline there is nothing to diff against.
#
# Turn these four back into calls through to theirs when their
# deployment catches up.
_hulk_shim_layout() {
  check_layout_prepare() {
    [ "${tbranch}" = 'OLK-6.6' ] || return 0
    log_info "***** kABI layout check: not deployed in their gate yet, skipped *****"
    return 0
  }
  check_layout_new()      { :; }
  build_defconfig_base()  { :; }
  report_layout()         { :; }
}

# setup_gcc, for the cross-architecture path: unpacks one of their pinned
# toolchain tarballs into /usr/local/$ARCH as root.  Same tarball, same
# compiler, unpacked under the workspace instead.  CROSS_COMPILE is set
# because their build_kernel reads it.
_hulk_shim_gcc() {
  setup_gcc() {
    log_info "***** Setup gcc *****"
    [ "${_HULK_TARBALL}" = '-' ] && { CROSS_COMPILE=''; return 0; }
    _oe_setup_gcc "${arch}" "${_HULK_TARBALL}" "${_HULK_SUB}/tools" \
      "${WORKDIR}/.toolchains" || return 1
    CROSS_COMPILE="${_HULK_CROSS}"
  }
}

# check_defconfig, kept and called, with one verdict it cannot reach on
# its own.
#
# Theirs copies the shipped openeuler_defconfig over .config, runs `make
# listnewconfig`, and fails the row if anything comes back -- a symbol
# Kconfig wants an answer for that the defconfig does not give.  On
# their builder the only way that happens is a Kconfig the series added
# and a defconfig it forgot to update, which is exactly what the check
# is for.
#
# Here it has a second cause.  `make listnewconfig` asks *this host's*
# compiler what it can do, and scripts/gcc-plugins/Kconfig gates five
# symbols on whether the compiler's plugin headers are installed:
#
#   depends on $(success,test -e $(shell,$(CC) -print-file-name=plugin)/include/plugin-version.h)
#
# This host has them, openEuler's builder does not, so Kconfig offers us
# CONFIG_GCC_PLUGINS and the four symbols under it and openeuler_defconfig
# has no answer for any of them.  Their own aarch64 and x86_64 jobs pass
# this row on the same branch and the same commit.
#
# Their check still decides the row.  All this adds is a reason not to
# believe it: if the series does not touch a single Kconfig file, it
# cannot have introduced a Kconfig symbol, and whatever came back is the
# host's.  The guard can only downgrade a fail, never create one, and it
# is asked only after their check has already failed -- so a series that
# does touch Kconfig gets their verdict untouched.
#
# What their check appended to build_output.txt is rolled back with it.
# That file is a gate of its own, read by their main() for the
# "| build warning | fail |" row, so leaving the symbols in it would
# fail the run by another name.
_hulk_shim_defconfig() {
  # Only checkkabi.sh has this check; checkbuild.sh's architectures ship
  # no openeuler_defconfig to be consistent with.
  declare -f check_defconfig >/dev/null 2>&1 || return 0

  # Their definition, under a second name, taken from the one we just
  # sourced rather than copied -- declare -f prints the body we loaded,
  # and dropping its first line leaves the body to re-bind.
  eval "_hulk_their_check_defconfig() $(declare -f check_defconfig | tail -n +2)"

  check_defconfig() {
    local dir="${current_path}/openeuler/kernel-${BUILD_ID}"
    local out="${dir}/build_output.txt" res="${dir}/result"
    local row="| ${arch} checkdefconfig | fail |"
    local before added

    before=$(wc -c < "${out}" 2>/dev/null) || before=0
    _hulk_their_check_defconfig
    grep -qxF "${row}" "${res}" 2>/dev/null || return 0

    # Their verdict stands unless the series cannot be responsible.
    _oe_series_touches_kconfig "${NUM_PATCHES:-0}" && return 0

    added=$(tail -c "+$((before + 1))" "${out}" 2>/dev/null | grep -E '^CONFIG_.*')
    truncate -s "${before}" "${out}" 2>/dev/null
    # "|" is not a metacharacter in a basic regular expression, so the
    # row matches itself.
    sed -i "s/^${row}$/| ${arch} checkdefconfig | pass |/" "${res}" 2>/dev/null
    log_info "${arch} checkdefconfig pass"
    echo "  -> $(printf '%s\n' "${added}" | grep -c .) symbol(s) have no answer in"
    echo "     openeuler_defconfig, but the series touches no Kconfig file, so"
    echo "     none of them can be its doing.  They are what this host's"
    echo "     toolchain offers and openEuler's builder does not:"
    printf '%s\n' "${added}" | sed 's/^/       /'
  }
}

# Their scripts write result, build_output.txt and the layout check's
# working files into the build directory, which for us is a link to the
# user's tree.  Their main() ends by deleting that directory, so on
# their builder these go with it; here the link goes and the files
# stay.  Named so the trap can clear them.
_HULK_LITTER=(result build_output.txt checklayout.log checklayoutres
              checklayout_fail checklayout_base_build.log kabi_all.txt
              new.layout base.layout layoutdiff)

# -Werror, in the two places the kernel sets it, for the one config
# where it is wrong.
#
# Theirs leaves it alone and is right to: their builder has the compiler
# their branch was written against.  Ours has whatever the user's
# distribution ships, and OLK-6.6 does not compile its own hinic3 and
# hinic5 drivers under gcc 12.3 for that reason alone -- on a branch
# whose own CI builds both configs green.  A local gate that rejects a
# series over that is predicting a verdict openEuler will not reach.
# Their gate has a "build warning" row, so a warning still has a place
# to be reported; it just does not get to stop the build.
#
# Both halves, because either alone is not enough, and they are not
# scoped the same way:
#
#   The command line, for both builds.  Four variables rather than one:
#   KCFLAGS and KAFLAGS land after everything scripts/Makefile.extrawarn
#   set, CFLAGS_KERNEL and CFLAGS_MODULE cover the built-in and modular
#   halves, and under allmodconfig almost everything is a module.
#
#   It would be tidier to do this for allmodconfig only and leave their
#   defconfig compile completely untouched, since that is the build the
#   three kabi rows read their Module.symvers from.  It does not work.
#   scripts/Makefile.extrawarn:90 adds -Werror=designated-init
#   unconditionally, not under a W= level, and openeuler_defconfig sets
#   CONFIG_HINIC3=m and CONFIG_HINIC5=m -- so the shipped defconfig
#   compiles the very drivers that trip it, and without the flags the
#   defconfig build fails on this host and takes the ABI comparison
#   with it.
#
#   The config bit, for allmodconfig only.  CONFIG_WERROR is not a flag
#   and no amount of -Wno-error answers a kernel configured to treat
#   warnings as errors.  This half really is allmodconfig's alone:
#   openeuler_defconfig ships CONFIG_WERROR off, so there is nothing to
#   turn off on their defconfig build and the hook below leaves it be.
_hulk_disable_werror_config() {
  local sym
  for sym in $(_oe_werror_symbols); do
    ./scripts/config --file .config --disable "${sym}" >/dev/null 2>&1
  done
  # Disabling a symbol by hand can leave one that depended on it
  # unanswered.  olddefconfig settles that without asking, and goes
  # through `command make` so it does not re-enter the hook below.
  command make olddefconfig >/dev/null 2>&1
}

# The "| build warning | fail |" row: kept, and narrowed to the warnings
# it was written to catch.
#
# Their main() adds it whenever build_output.txt is not empty -- any
# warning at all, on any branch but openEuler-1.0-LTS.  On their builder
# that is a sharp check, because a clean tree there compiles silently:
# their aarch64 run of 2026-09-22 passed every row, wrote nothing to
# build_output.txt, and finished SUCCESS with no warning row at all.
#
# Here the same tree is never silent.  The user's compiler is whatever
# their distribution ships, and OLK-6.6's hinic3 and hinic5 drivers
# produce 83 -Wdesignated-init and -Wincompatible-pointer-types
# warnings under gcc 12.3 that openEuler's builder does not emit -- on
# a branch whose own CI is green, in files no series has been near.
# Left alone, the row fails every run on this host and says nothing
# about the patch.
#
# So the file is narrowed to the files the series touches.  That is not
# an invention: it is what their other script does the expensive way.
# checkbuild.sh builds the branch twice, once before the patches and
# once after, with only the second build's stderr redirected, so that
# build_output.txt holds the warnings from the files the patch touched
# and nothing else.  checkkabi.sh skips the trick because it does not
# need it.  One git diff gets the same answer here without a second
# eight-minute build.
#
# The row can still fail, and fails for the thing it is for: a warning
# in a file the series touched.  Nothing is dropped when the series
# cannot be determined, and `make` errors are always kept.
_hulk_touched_files() {
  local back="${NUM_PATCHES:-0}"
  [ "${back}" -gt 0 ] || return 0
  git rev-parse --verify -q "HEAD~${back}" >/dev/null 2>&1 || return 0
  git diff --name-only "HEAD~${back}" HEAD 2>/dev/null
}

# Keep only the diagnostics that name a file the series touched.
#
# gcc reports in groups -- a "file:line:col: warning:" line, then the
# source it is pointing at, then any "note:" lines -- so the decision is
# made once per group and the rest of the group follows it.
#
# Paths are normalised before being compared because the kernel's own
# are not: hinic5 compiles through
# drivers/.../nic/linux/../../../sdk/knldk/lld/hinic5_lld.c, which is
# the same file git names without the dot-dots.
_HULK_ONLY_OURS=$(cat <<'AWK'
function norm(p,   parts, m, i, out, k, r) {
  m = split(p, parts, "/"); k = 0
  for (i = 1; i <= m; i++) {
    if (parts[i] == "..") { if (k > 0) k-- }
    else if (parts[i] != "." && parts[i] != "") { out[++k] = parts[i] }
  }
  r = ""
  for (i = 1; i <= k; i++) r = r (i > 1 ? "/" : "") out[i]
  return r
}
BEGIN {
  n = split(TOUCHED, a, "\n")
  for (i = 1; i <= n; i++) if (a[i] != "") ours[norm(a[i])] = 1
  keep = 1
}
/^[^ \t]+:[0-9]+:[0-9]+: (warning|note|error):/ ||
/^[^ \t]+: (In function|In file included from)/ {
  f = $0; sub(/:.*/, "", f)
  keep = (norm(f) in ours)
}
# A build that stopped is reported whatever broke it: the row that
# failed has already said so, and hiding why would be worse than noise.
/^make(\[[0-9]+\])?:/ { print; next }
{ if (keep) print }
AWK
)

_hulk_keep_only_ours() {
  local out="$1" before="$2" touched tmp
  touched=$(_hulk_touched_files)
  # No series to ask about, or no way to ask: their file stands.
  [ -n "${touched}" ] || return 0
  [ -s "${out}" ] || return 0

  tmp=$(mktemp) || return 0
  head -c "${before}" "${out}" > "${tmp}" 2>/dev/null
  tail -c "+$((before + 1))" "${out}" 2>/dev/null \
    | awk -v TOUCHED="${touched}" "${_HULK_ONLY_OURS}" >> "${tmp}"
  mv -f "${tmp}" "${out}"
}

# Around each of their builds, so that what a build appended is judged
# as soon as it appends it.  Their check_kabi and check_defconfig write
# to the same file afterwards and are left alone: check-kabi's own
# output is a verdict, not a warning.
_hulk_shim_builds() {
  local f
  for f in build_allmodconfig build_defconfig build_kernel; do
    declare -f "${f}" >/dev/null 2>&1 || continue
    eval "_hulk_their_${f}() $(declare -f "${f}" | tail -n +2)"
    eval "${f}() {
      local out=\"\${current_path}/openeuler/kernel-\${BUILD_ID}/build_output.txt\"
      local before rc
      before=\$(wc -c < \"\${out}\" 2>/dev/null) || before=0
      _hulk_their_${f}
      rc=\$?
      _hulk_keep_only_ours \"\${out}\" \"\${before}\"
      return \${rc}
    }"
  done
}

# Their build_allmodconfig configures and compiles inside one function:
#
#   make clean; make allmodconfig; make oldconfig; make -j$NR_CPUS
#
# so there is no line between the config and the compile to hook, and
# rewriting the function would be rewriting the check.  `make` is the
# seam instead: a shell function by that name shadows the binary for
# their code, and the config bit goes off as the last config step
# returns, before the compile that reads it.
#
# Matched on the goal rather than the call, so it fires for their
# checkkabi.sh (make allmodconfig; make oldconfig) and for their
# checkbuild.sh (make ARCH=.. allmodconfig) alike, and not at all for
# openeuler_defconfig.
_hulk_shim_make() {
  make() {
    command make "$@"
    local rc=$?
    [ ${rc} -eq 0 ] || return ${rc}
    case " $* " in
      *' allmodconfig '*|*' oldconfig '*) _hulk_disable_werror_config ;;
    esac
    return 0
  }
}

# Their builds say plain `make`, with no ARCH and no CROSS_COMPILE,
# because each of their jobs runs on a node of the architecture it is
# checking.  We check seven architectures from one host.
#
# The kernel's top-level Makefile has `ARCH ?= $(SUBARCH)` and
# `CROSS_COMPILE ?=`, so an exported value is taken and their make lines
# need no editing at all -- which is the point: these two exports are
# the whole of the cross-compilation difference.
#
# The -Wno-error= list is worked out here, because it depends on the
# tree's makefiles and on which compiler is about to read them, and both
# are known only now.  See _hulk_disable_werror_config above for why the
# command-line half covers both builds and the config bit does not.
_hulk_export_arch() {
  local kernel_arch="$1" cross="$2" kernel="$3"
  export ARCH="${kernel_arch}"
  export CROSS_COMPILE="${cross}"
  local flags
  flags=$(cd "${kernel}" &&
    { _oe_set_no_werror "${cross}" >/dev/null 2>&1
      printf '%s' "${_OE_NO_WERROR_FLAGS}"; })
  export KCFLAGS="${flags}" KAFLAGS="${flags}" \
         CFLAGS_KERNEL="${flags}" CFLAGS_MODULE="${flags}"
}

# Run one architecture through their script.
#
# A subshell, for two reasons: their functions cd about and we are
# sourced into somebody else's shell, and their main() ends in `exit`,
# which here exits the subshell with their status and nothing else.
#
# set +u because their code is not written for it -- $exitcode,
# $commentid and $LD_LIBRARY_PATH are all read before being set -- and
# our test.sh runs under set -u.
oe_hulk_arch() (
  set +u
  local_arch="$1"
  _HULK_SUB="${SCRIPT_DIR}/hulk_robot_test/openEuler"

  spec=$(_oe_arch_spec "${local_arch}") || {
    echo "unknown arch ${local_arch}" >&2; exit 2; }
  read -r kernel_arch cross _HULK_TARBALL <<< "${spec}"
  [ "${cross}" = '-' ] && cross=''
  _HULK_CROSS="${cross}"

  scratch=$(mktemp -d) || exit 2
  trap 'cd /; rm -rf "${scratch}"
        for f in "${_HULK_LITTER[@]}"; do rm -f "${LINUX_SRC_PATH}/${f}"; done' EXIT

  # Their variables, by their names, because their code reads them.
  export test_path="${scratch}/test_path"
  export tbranch="${OE_TARGET_BRANCH:-OLK-6.6}"
  export arch="${local_arch}"
  export BUILD_ID="prci-$$"
  _hulk_overlay "${_HULK_SUB}" "${test_path}" || exit 2

  _hulk_export_arch "${kernel_arch}" "${cross}" "${LINUX_SRC_PATH}"

  # checkkabi.sh has no setup_gcc, because its two architectures are the
  # two openEuler ships and each of those jobs runs on a node of its own
  # architecture -- there is nothing to cross-compile with.  We check
  # both from one host, so the toolchain has to be on PATH before their
  # script starts.  checkbuild.sh does call setup_gcc, and the shim for
  # it below lands on the same unpacked copy, which is stamped and so
  # only unpacks once.
  if [ "${_HULK_TARBALL}" != '-' ]; then
    _oe_setup_gcc "${arch}" "${_HULK_TARBALL}" "${_HULK_SUB}/tools" \
      "${WORKDIR}/.toolchains" || exit 2
  fi

  # current_path is whatever their script's first line finds, so we are
  # in the scratch directory before it is sourced.
  cd "${scratch}" || exit 2

  if _oe_arch_has_kabi "${local_arch}"; then
    _hulk_load "${_HULK_SUB}/checkkabi.sh" || exit 2
  else
    _hulk_load "${_HULK_SUB}/checkbuild.sh" || exit 2
  fi

  # After sourcing, so that these win over common.sh's definitions and
  # so that _hulk_shim_defconfig can rebind the check_defconfig we just
  # loaded.
  _hulk_shim_jenkins
  _hulk_shim_whitelists
  _hulk_shim_kernel
  _hulk_shim_layout
  _hulk_shim_gcc
  _hulk_shim_defconfig
  _hulk_shim_make
  _hulk_shim_builds

  # Their main() asks check_branch.py whether this branch builds this
  # architecture and, if not, exits 0 -- so their PR comment shows the
  # job SUCCESS with nothing in it.  Asked here as well, with their
  # script, for the one thing their exit status cannot carry: the
  # difference between "checked it and it passed" and "openEuler does
  # not build this here".  Both are a pass; only one of them checked
  # anything, and a run that checked nothing must not read like a run
  # that checked everything.
  _oe_arch_wanted "${local_arch}" "${tbranch}" "${test_path}/lib" || exit $?

  NR_CPUS="${BUILD_THREADS:-$(nproc)}"

  # Their main() ends in exit, so it gets a subshell of its own and we
  # read its status rather than inheriting it.  Everything below is
  # reporting: their builder gets the table out of the build directory
  # with pr_comment_api.py, which is the one piece of theirs we stubbed,
  # so somebody has to print it.
  rc=0
  if _oe_arch_has_kabi "${local_arch}"; then
    ( main ) || rc=$?
  else
    # checkbuild.sh's main takes the kernel ARCH and the cross prefix,
    # the two values its Jenkins job passes in.  It writes no table at
    # all -- a build either succeeded or it did not, and the console is
    # the whole report.
    ( main "${kernel_arch}" "${cross%-linux-*}" ) || rc=$?
  fi

  # Only the table.  Their main() already prints build_output.txt, on the
  # "build warning:" line it logs before deciding the verdict.
  if [ -s "${LINUX_SRC_PATH}/result" ]; then
    echo
    cat "${LINUX_SRC_PATH}/result"
    echo
  fi
  exit ${rc}
)
