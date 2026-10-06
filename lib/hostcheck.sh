#!/bin/bash
# Whether this host can test the kernel it has been pointed at.
#
# Asked once, after configuration, because the answer is a property of
# the host and the tree and not of any one check.  A host that cannot
# build the tree will fail every case that compiles it, forty minutes
# at a time, and every one of those failures will look like a verdict on
# the series.  So the question is asked before anything is built, and
# when the answer is no nothing is run.
#
# The hard part is that a version comparison cannot answer it.  The tree
# states the tools it needs in Documentation/process/changes.rst, and
# every one of those is a *minimum*: a 5.10 tree asks for gcc 4.9 or
# newer and says nothing about an upper bound, because in 2020 there was
# no upper bound to state.  Anolis 23's gcc 12.3, perl 5.36 and python
# 3.11 all satisfy the table comfortably.  That is precisely why nothing
# in their CI catches this, and why a table of our own would be a guess.
#
# So the verdict comes from a probe and the table comes from their file.
# The probe compiles a few lines the way their build compiles them, with
# the flags read out of their own makefile, and either it compiles here
# or it does not.  The table is read from their documentation, and each
# host version is obtained by running the very command their
# documentation says to run.  Nothing here is a list we maintain.

# ── what the tree is ──────────────────────────────────────────────────

# Their Makefile, not "make kernelversion": this is asked on a page
# refresh, and spawning make in a kernel tree is not a thing to do on a
# timer.
hostcheck_kernel_version() {
  local mk="$1/Makefile" v p s

  [ -r "${mk}" ] || return 1
  v=$(sed -n 's/^VERSION = *//p' "${mk}" | head -1)
  p=$(sed -n 's/^PATCHLEVEL = *//p' "${mk}" | head -1)
  s=$(sed -n 's/^SUBLEVEL = *//p' "${mk}" | head -1)
  [ -n "${v}" ] && [ -n "${p}" ] || return 1
  echo "${v}.${p}${s:+.${s}}"
}

# ── what the tree says it needs, and what the host has ────────────────

# Their own table, read from their own file.
#
# It is a fixed-width table fenced by rows of '=', which is what gives
# the column boundaries: the fence is as wide as the columns are, so it
# is read for the widths rather than guessed at.  Each row names a tool,
# the minimum version, and the command to ask this host what it has --
# and that last column is the reason this can report a host version at
# all without a list of commands of our own.
_hostcheck_declared() {
  local doc="$1/Documentation/process/changes.rst"

  [ -r "${doc}" ] || return 1
  awk '
    # The fence: its runs of "=" are the column widths.
    /^=+ +=+ +=+ *$/ {
      if (!w1) {
        split($0, f, / +/)
        w1 = length(f[1]); w2 = length(f[2])
        next
      }
      intable = !intable
      next
    }
    !w1 || !intable { next }
    {
      name = substr($0, 1, w1)
      want = substr($0, w1 + 2, w2)
      how  = substr($0, w1 + w2 + 3)
      gsub(/^ +| +$/, "", name)
      gsub(/^ +| +$/, "", want)
      gsub(/^ +| +$/, "", how)
      if (name != "" && want != "" && how != "")
        printf "%s\t%s\t%s\n", name, want, how
    }
  ' "${doc}"
}

# The first version-looking number a tool prints about itself.  Their
# commands answer in a dozen different shapes ("gcc (GCC) 12.3.0",
# "GNU ld version 2.41-15"), and the number is the only part common to
# all of them.
_hostcheck_ask() {
  local how="$1" binary out
  # Half of their table is administrative tools that live in sbin, and
  # a service manager starts this with a narrower PATH than a login
  # shell has.  Without this the table has six fewer rows in the web
  # interface than on the command line, for no reason a reader could
  # work out.
  local PATH="${PATH}:/usr/local/sbin:/usr/sbin:/sbin"

  binary=${how%% *}
  command -v "${binary}" >/dev/null 2>&1 || return 1
  # Their command, run as they wrote it, but never able to wait on a
  # terminal or outlive the question being asked.
  out=$(timeout 5 bash -c "${how}" 2>&1 </dev/null) || true
  out=$(printf '%s\n' "${out}" \
        | grep -o -m1 '[0-9][0-9]*\(\.[0-9][0-9]*\)\+' | head -1)
  [ -n "${out}" ] || return 1
  echo "${out}"
}

# The table, as the warning shows it: one row per tool this host
# actually has, with what the tree asks for beside it.  Tools the host
# does not have are left out rather than reported as missing -- most of
# the list is for filesystems and hardware that have nothing to do with
# building, and their absence is not what is wrong here.
#
# Any tool named in $2 is listed too, whether or not the tree's table
# mentions it.  That is where the tools a probe actually failed on go,
# and the answer for them is usually that the table does not mention
# them -- which is worth seeing, because a requirement the tree never
# states is a requirement nothing can check.
hostcheck_tool_table() {
  local tree="$1" extra="$2" name want how have

  printf '%-22s %-18s %s\n' "Tool" "This host" "Tree needs at least"
  while IFS=$'\t' read -r name want how; do
    have=$(_hostcheck_ask "${how}") || continue
    printf '%-22s %-18s %s\n' "${name}" "${have}" "${want}"
  done < <(_hostcheck_declared "${tree}")

  for name in ${extra}; do
    # Looked up in their table rather than assumed absent from it.
    want=$(_hostcheck_declared "${tree}" \
           | awk -v n="${name}" 'BEGIN{IGNORECASE=1}
               tolower($1) == tolower(n) { print $2; exit }')
    have=$(_hostcheck_ask "${name} --version") || continue
    printf '%-22s %-18s %s\n' "${name}" "${have}" \
           "${want:-not stated by the tree}"
  done
}

# ── what a probe says ─────────────────────────────────────────────────

# The warnings a build switches on for itself, read from its own tree.
_hostcheck_their_warnings() {
  local file="$1/tools/scripts/Makefile.include"

  [ -r "${file}" ] || return 1
  sed -n 's/^EXTRA_WARNINGS[[:space:]]*[:+]*=[[:space:]]*//p' "${file}" \
    | tr '\n' ' '
}

# And the warnings it switches back off for the file in question.
#
# This is the half that was missing, and it is the half that decides the
# answer.  perf turns on a wide set of warnings for everything it
# builds and then, for the two objects that include an interpreter's
# headers, turns several of them off again -- because those headers are
# not perf's to fix.  Which ones differ by kernel: a 5.10 tree switches
# off seven of them and a 6.6 tree switches off ten, and the three in
# between are exactly the three a 5.10 build dies on here.  So a probe
# that reads only the first list condemns every tree equally, which is
# no answer at all.
#
# The object is found by which include macro its flags use rather than
# by its name, so this follows them if they move or rename it.
_hostcheck_object_flags() {
  local file="$1/tools/perf/util/scripting-engines/Build" macro="$2"

  [ -r "${file}" ] || return 0
  # Their own line, with the macro references dropped: those are
  # include paths, which are asked for separately and by the same
  # commands their Makefile.config uses.
  awk -v macro="${macro}" '
    /^CFLAGS_[^ \t]*\.o[ \t]*\+?=/ && index($0, macro) {
      sub(/^[^=]*=[ \t]*/, "")
      gsub(/\$\([^)]*\)/, "")
      print
    }
  ' "${file}" | tr '\n' ' '
}

# Can the scripting engines perf links against be compiled here?
#
# Writes the reason it cannot to stdout and returns 1.  Returns 0 both
# when it can and when the question cannot be answered: a probe that
# could not run is not evidence, and must never be why a case is
# refused.
hostcheck_perf_scripting() {
  local tree="$1" flags at rc=0 lang inc
  local -a probes=()

  flags=$(_hostcheck_their_warnings "${tree}") || return 0
  [ -n "${flags}" ] || return 0
  command -v gcc >/dev/null 2>&1 || return 0
  at=$(mktemp -d) || return 0

  # The same two commands their tools/perf/Makefile.config runs, at
  # :787 and :285, to find these headers.
  local perl_inc python_inc
  perl_inc=$(perl -MExtUtils::Embed -e ccopts 2>/dev/null)
  python_inc=$(python3-config --includes 2>/dev/null)

  printf '#include <EXTERN.h>\n#include <perl.h>\nint p(void);\nint p(void) { return 0; }\n' \
    > "${at}/perl.c"
  printf '#include <Python.h>\nint q(void);\nint q(void) { return 0; }\n' \
    > "${at}/python.c"

  [ -n "${perl_inc}" ] && probes+=("perl")
  [ -n "${python_inc}" ] && probes+=("python3")
  # What was actually compiled here, for the table to report on: the
  # rows that matter are the ones a probe touched.
  HOSTCHECK_PROBED="${probes[*]}"
  # Neither interpreter's headers are installed, so their build will not
  # compile them and there is nothing here to object to.
  [ "${#probes[@]}" -gt 0 ] || { rm -rf "${at}"; return 0; }

  for lang in "${probes[@]}"; do
    inc="${perl_inc}" macro='PERL_EMBED_CCOPTS'
    if [ "${lang}" = python3 ]; then
      inc="${python_inc}" macro='PYTHON_EMBED_CCOPTS'
    fi
    # Their global list first and their per-object list after it, in that
    # order, because that is the order their build puts them in and the
    # second is what undoes the first.
    local quiet
    quiet=$(_hostcheck_object_flags "${tree}" "${macro}")
    # shellcheck disable=SC2086
    gcc -c -o /dev/null "${at}/${lang%3}.c" -Werror ${flags} ${quiet} ${inc} \
        2> "${at}/${lang}.log" && continue

    # Every line below is assembled from what just happened, because
    # the same probe runs on hosts and trees this was not written for.
    # A sentence that cannot be checked against the log is not printed.
    local count first offenders version where
    count=$(grep -c ': error:' "${at}/${lang}.log")
    first=$(grep -m1 ': error:' "${at}/${lang}.log")
    # Named by the compiler rather than by us, so this stays correct if
    # their flag list changes.
    offenders=$(sed -n 's/.*\[-W\(error=[a-z0-9-]*\)\].*/-W\1/p' \
                "${at}/${lang}.log" | sort -u | paste -sd' ' -)
    version=$(_hostcheck_ask "${lang} --version")
    where=${first%%:*}

    # One sentence, then a separator, then the evidence.  The sentence
    # is all anybody is shown until they ask: a compiler error quoted
    # at somebody who did not ask for it is just noise in front of the
    # thing they need, which is that nothing is going to run.
    echo "Its ${lang} ${version:-(version unknown)} cannot compile what" \
         "their perf build"
    echo -n "compiles: ${count} errors"
    # Whether the kernel is implicated is a fact about the path the
    # compiler printed, not something to assume either way.
    case "${where}" in
      "${tree}"/*) echo " in the kernel tree itself." ;;
      /*)          echo ", none of them in the kernel tree." ;;
      *)           echo "." ;;
    esac
    echo "---"
    echo "  first error: ${first}"
    if [ -n "${offenders}" ]; then
      echo "  raised by:   ${offenders}"
      echo "  switched on: by their own tools/scripts/Makefile.include," \
           "not by us"
    fi
    # Only said when their list really does carry the flag that looks
    # like it should have prevented this, and only when these headers
    # really do arrive the way that stops it applying.
    case " ${flags} " in
      *" -Wno-system-headers "*)
        case "${inc}" in
          *-I*)
            echo "  not caught:  that list has -Wno-system-headers," \
                 "which does not reach"
            echo "               these headers because they arrive" \
                 "through -I, not -isystem"
            ;;
        esac
        ;;
    esac
    rc=1
    break
  done

  rm -rf "${at}"
  return "${rc}"
}

# How many rows of a table have a host version older than the tree
# wants.  Compared with sort -V, so "4.9" and "12.3.0" come out the
# right way round and no rule of ours decides what a version is.
_hostcheck_behind() {
  local have want n=0

  # Two or more spaces is the column gap, which is what alignment
  # means: the tool names have single spaces inside them.
  while IFS=$'\t' read -r have want; do
    # A column that does not start with a digit is prose, not a version
    # ("not stated by the tree"), and there is nothing to compare.
    case "${want}" in
      [0-9]*) ;;
      *) continue ;;
    esac
    [ "${have}" = "${want}" ] && continue
    [ "$(printf '%s\n%s\n' "${have}" "${want}" | sort -V | head -1)" \
      = "${have}" ] && n=$((n + 1))
  done < <(printf '%s\n' "$1" | tail -n +2 \
           | awk -F'  +' 'NF >= 3 { print $2 "\t" $3 }')
  echo "${n}"
}

# ── the answer ────────────────────────────────────────────────────────

# Is this host fit to test this tree?  Prints the warning if not.
#
# The reason comes first, because that is what a person needs; the
# table follows it, because that is what they will want next.
hostcheck_report() {
  local tree="$1" version reason

  [ -n "${tree}" ] && [ -d "${tree}" ] || return 0
  version=$(hostcheck_kernel_version "${tree}") || version="this"

  # Written to a file rather than captured, so the probe runs in this
  # shell and can report what it compiled.
  local said
  said=$(mktemp) || return 0
  if hostcheck_perf_scripting "${tree}" > "${said}"; then
    rm -f "${said}"
    return 0
  fi
  reason=$(cat "${said}")
  rm -f "${said}"

  # The headline, then the one-sentence reason: the probe separates its
  # own sentence from its own evidence with the same marker, so the
  # first part of what it said belongs up here with the headline.
  echo "This host is not fit to test a ${version} kernel."
  echo "${reason%%$'\n'---*}"
  # Everything above is the answer; everything below is the evidence,
  # which the web interface keeps behind "view more" and splits off
  # here rather than by recognising our own prose.
  echo "---"
  case "${reason}" in
    *$'\n'---*) echo "${reason#*$'\n'---$'\n'}" ;;
  esac
  echo ""
  echo "---"
  echo ""
  # The surprising part, and the one thing here that must never be
  # asserted: that every version in the table is fine by the tree's own
  # reckoning.  It is counted, because on another host it will not be
  # true and the opposite sentence is the one that should print.
  local table rows behind
  table=$(hostcheck_tool_table "${tree}" "${HOSTCHECK_PROBED}")
  rows=$(printf '%s\n' "${table}" | tail -n +2 | grep -c .)
  behind=$(_hostcheck_behind "${table}")

  if [ "${rows}" -eq 0 ]; then
    echo "The tree does not state the tool versions it needs anywhere" \
         "this could"
    echo "read, so there is nothing below to compare against."
  elif [ "${behind}" -eq 0 ]; then
    echo "Nothing below is out of date: the tree states a minimum for" \
         "each of these"
    echo "and this host meets all ${rows}. A minimum cannot say how new" \
         "is too new,"
    echo "which is why nothing in the tree's own requirements catches" \
         "this."
  else
    echo "${behind} of the ${rows} tools below are older than the tree" \
         "asks for, which"
    echo "may be the whole of it. The rest meet their minimums."
  fi
  echo ""
  printf '%s\n' "${table}"
  return 1
}

# Run directly, so the web interface and the test scripts get their
# answer from this one implementation rather than two.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  hostcheck_report "$1"
  exit $?
fi
