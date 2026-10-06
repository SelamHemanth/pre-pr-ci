#!/bin/bash
# Compiler warnings, separated into the ones a series is answerable for
# and the ones that were already there.
#
# A 5.10 tree built with this host's gcc 12.3, binutils 2.41 and grep
# 3.11 emits hundreds of warnings that have nothing to do with anybody's
# patch: -Wdangling-pointer and -Warray-compare did not exist when the
# code was written, 5.10's overflow.h and minmax.h trip the newer
# typecheck diagnostics, and the assembler now objects to mnemonics the
# kernel has used for twenty years.  Read as a list, that buries the one
# warning that would matter.
#
# The distinction is drawn the way openEuler's own CI draws it.  Their
# checkbuild.sh builds the branch twice -- once before the patches and
# once after, with only the second build's stderr redirected -- so that
# build_output.txt holds the warnings from the files the patch touched
# and nothing else.  One `git diff` reaches the same answer without the
# second build.
#
# What is done with the answer differs, because their scripts differ.
# openEuler fails a build on any warning at all (checkbuild.sh:63), so
# there the file their script reads is narrowed before it reads it.
# Anolis does not look at warnings, so there nothing is filtered and
# nothing is hidden: a count is appended, and their log stands.

# The files a series touches, or nothing if that cannot be established.
#
# Nothing rather than a guess: a filter that cannot tell whose warning
# it is has no business dropping any of them.
warnings_touched_files() {
  local repo="${1:-.}" back="${2:-${NUM_PATCHES:-0}}"

  [ "${back}" -gt 0 ] 2>/dev/null || return 0
  git -C "${repo}" rev-parse --verify -q "HEAD~${back}" >/dev/null 2>&1 \
    || return 0
  git -C "${repo}" diff --name-only "HEAD~${back}" HEAD 2>/dev/null
}

# Path normalisation, shared by both of the awk programs below.
#
# The kernel's own paths are not normalised and git's are: hinic5
# compiles through
# drivers/.../nic/linux/../../../sdk/knldk/lld/hinic5_lld.c, which is
# the same file git names without the dot-dots.
_WARNINGS_NORM=$(cat <<'AWK'
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
}
AWK
)

# Keep only the diagnostics that name a file the series touched.
#
# gcc reports in groups -- a "file:line:col: warning:" line, then the
# source it is pointing at, then any "note:" lines -- so the decision is
# made once per group and the rest of the group follows it.
_WARNINGS_ONLY_OURS="${_WARNINGS_NORM}
$(cat <<'AWK'
BEGIN { keep = 1 }
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
)"

# Count them instead, and name the ones that are the series' own.
#
# Both spellings of a warning are counted: gcc's
# "file:line:col: warning:" and the assembler's "file:line: Warning:",
# which has no column and a capital.
#
# Errors are counted alongside them because of build_perf, where every
# diagnostic is an error -- perf compiles with -Werror, so the same
# warnings that the kernel tolerates stop the build there.  Whose files
# they are in is the first thing worth knowing about a failure.
_WARNINGS_COUNT="${_WARNINGS_NORM}
$(cat <<'AWK'
/^[^ \t]+:[0-9]+:[0-9]+: (warning|error):/ ||
/^[^ \t]+:[0-9]+: [Ww]arning:/ {
  if ($0 ~ /error:/) errors++; else warnings++
  f = $0; sub(/:.*/, "", f)
  if (norm(f) in ours) { mine[++m] = $0 }
  next
}
# objtool and the linker report against the object, not the source.
/^[^ \t]+\.o: warning:/ { warnings++ }
END {
  printf "%d\n%d\n%d\n", warnings, errors, m
  for (i = 1; i <= m; i++) print mine[i]
}
AWK
)"

# Narrow a file in place, for the script that reads it to decide.
#
# `before` is how long the file was before the build ran, so that an
# earlier step's output is left exactly as that step wrote it.
warnings_keep_only_ours() {
  local out="$1" before="${2:-0}" repo="${3:-.}" touched tmp
  touched=$(warnings_touched_files "${repo}")
  # No series to ask about, or no way to ask: their file stands.
  [ -n "${touched}" ] || return 0
  [ -s "${out}" ] || return 0

  tmp=$(mktemp) || return 0
  head -c "${before}" "${out}" > "${tmp}" 2>/dev/null
  tail -c "+$((before + 1))" "${out}" 2>/dev/null \
    | awk -v TOUCHED="${touched}" "${_WARNINGS_ONLY_OURS}" >> "${tmp}"
  mv -f "${tmp}" "${out}"
}

# Say what the warnings in a log amount to, without touching them.
#
# Appended to the log the reader already has open, so the answer is
# where the question was asked.
warnings_summarise() {
  local log="$1" repo="${2:-.}" touched counted warnings errors mine what

  [ -s "${log}" ] || return 0
  touched=$(warnings_touched_files "${repo}") || return 0

  counted=$(awk -v TOUCHED="${touched}" "${_WARNINGS_COUNT}" "${log}") \
    || return 0
  warnings=$(printf '%s\n' "${counted}" | sed -n 1p)
  errors=$(printf '%s\n' "${counted}" | sed -n 2p)
  mine=$(printf '%s\n' "${counted}" | sed -n 3p)
  [ $((warnings + errors)) -gt 0 ] 2>/dev/null || return 0

  if [ "${warnings}" -eq 0 ]; then
    what="${errors} compiler errors"
  elif [ "${errors}" -gt 0 ]; then
    what="${warnings} compiler warnings and ${errors} errors"
  else
    what="${warnings} compiler warnings"
  fi

  {
    if [ -z "${touched}" ]; then
      echo "[prci] ${what} in this build. Which of them are the" \
           "series' own cannot"
      echo "[prci] be told from here, so none are claimed either way."
    elif [ "${mine}" -eq 0 ]; then
      echo "[prci] ${what} in this build, none of them in a file this" \
           "series"
      echo "[prci] touches. This is a 5.10 tree built with a toolchain" \
           "newer than"
      echo "[prci] the code: diagnostics it was never written to" \
           "satisfy, in files"
      echo "[prci] no series has been near."
    else
      echo "[prci] ${what} in this build, ${mine} of them in a file" \
           "this series"
      echo "[prci] touches:"
      printf '%s\n' "${counted}" | tail -n +4 | sed 's/^/[prci]   /'
      echo "[prci] openEuler's CI fails a build for any warning;" \
           "Anolis's does not."
    fi
  } >> "${log}"
}
