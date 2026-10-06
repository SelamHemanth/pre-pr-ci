#!/bin/bash
# Why a package their scripts asked for could not be installed.
#
# Their kernel.spec carries a BuildRequires list written for Red Hat's
# packaging, and on Anolis 23 one of those names does not exist as a
# package -- not because anything is missing, but because this
# distribution ships the same contents under a different name and
# publishes no Provides or Obsoletes linking the two.  yum reports
# "Error: Some packages could not be found." and names nothing, so the
# same four lines appeared at the top of every case log with no way to
# tell whether they mattered.
#
# They do not, and that is worth saying once rather than leaving
# somebody to find out.  What is said is worked out here each time:
#
#   their spec is asked what it wants, with rpmspec
#   this host's rpm database is asked what provides each of those
#   its repositories are asked about whatever is left
#   its installed packages are searched for one named like the gap
#
# so no package name is written down anywhere in this file.  On a host
# where the name resolves, nothing prints; on a host where something is
# genuinely absent, it says that instead.

# The names a command was asking for.
#
# builddep is handed a spec file, which can be asked what it wants;
# anything else is handed the names themselves.
_pkg_wanted() {
  local arg spec=''

  for arg in "$@"; do
    case "${arg}" in
      *.spec) [ -r "${arg}" ] && spec="${arg}" ;;
    esac
  done

  if [ -n "${spec}" ]; then
    # Their own file, read by rpm's own parser rather than by a grep
    # for BuildRequires: the list is assembled by macros and conditions
    # that only rpm can resolve.
    rpmspec -q --buildrequires "${spec}" 2>/dev/null | awk '{ print $1 }'
    return
  fi

  # The first bare word is the verb -- install, builddep, remove -- and
  # the names follow it.  Recognised by position rather than by a list
  # of the verbs there are.
  local verb_seen=''
  for arg in "$@"; do
    case "${arg}" in
      -*|*=*|*/*) continue ;;   # flags, settings and paths
      '') continue ;;
    esac
    if [ -z "${verb_seen}" ]; then
      verb_seen=yes
      continue
    fi
    echo "${arg}"
  done
}

# A package installed here whose name ends the way a missing one does.
#
# The tail is taken from the name that was not found, so this is a
# search and not a mapping: foo-rpm-config asks after anything-rpm-config,
# and if this host happens to call it something else, that is what turns
# up.  Nothing is suggested when nothing matches.
_pkg_named_like() {
  local want="$1" tail="${1#*-}"

  # A name with no second component has no tail to match on, and
  # matching the whole of it would just be the name again.
  [ "${tail}" != "${want}" ] || return 0
  rpm -qa --qf '%{NAME}\n' 2>/dev/null \
    | grep -- "-${tail}\$" | grep -v "^${want}\$" | head -1
}

# Say, in a line or two, why a package command could not be satisfied.
#
# Prints nothing at all when every name resolves: a command can fail for
# its own reasons, and inventing a dependency problem to explain a
# network timeout would be worse than silence.
pkg_explain() {
  local names missing name have

  names=$(_pkg_wanted "$@" | sort -u)
  [ -n "${names}" ] || return 0
  command -v rpm >/dev/null 2>&1 || return 0

  # One query for all of them; rpm names the ones nothing provides.
  # shellcheck disable=SC2086
  missing=$(rpm -q --whatprovides ${names} 2>&1 \
            | sed -n 's/^no package provides \(.*\)$/\1/p')
  [ -n "${missing}" ] || return 0

  printf '%s\n' "${missing}" | while read -r name; do
    [ -n "${name}" ] || continue
    # Available but not installed is a different story, and not one to
    # explain away: their command should have installed it.
    if [ -n "$(dnf -q repoquery --whatprovides "${name}" 2>/dev/null)" ]; then
      echo "[prci] ${name} is in a repository but was not installed;" \
           "their command" >&2
      echo "[prci] stopped before it got that far." >&2
      continue
    fi
    have=$(_pkg_named_like "${name}")
    if [ -n "${have}" ]; then
      echo "[prci] ${name} is not a package on this host and no" \
           "repository offers it;" >&2
      echo "[prci] ${have} is installed and named like it, so nothing" \
           "is actually missing." >&2
    else
      echo "[prci] ${name} is not a package on this host and no" \
           "repository offers it," >&2
      echo "[prci] so their build requirement cannot be met by name" \
           "here." >&2
    fi
  done
}
