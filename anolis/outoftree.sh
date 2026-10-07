#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - anolis/outoftree.sh
# Which commits are Anolis's own work, and whose sign-off they take
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# Source this file; it defines functions and runs nothing on its own.
#
# Anolis marks the work that is theirs rather than carried in from
# upstream by putting the distro's own name at the front of the
# subject.  It holds in their tree: of the last three thousand commits
# on devel-6.6, every one subject-prefixed that way carries no
# "commit <sha> upstream." line, and nothing else does.
#
# The distinction decides whose Signed-off-by belongs on the patch.  A
# backport is somebody else's work being carried across, and the
# sign-off this tool adds to it says exactly that much: this is who
# moved it.  Out-of-tree work is the author's own, and the sign-off on
# it is them certifying the DCO for something they wrote -- which
# nobody can do on their behalf.  So this tool does not sign those.
# It only makes sure the author already did, and writes their own
# sign-off, from their own From: line, when they have not.

# The prefix, taken from the name of the distro this directory is for
# rather than spelled out, so it stays right if the directory is ever
# the one next to it.
anolis_out_of_tree_mark() {
	printf '%s: ' "$(basename "$(cd "$(dirname "${BASH_SOURCE[0]}")" \
	                             && pwd)")"
}

# Whether a subject is marked as out-of-tree work.
anolis_subject_is_out_of_tree() {
	case "$1" in
		"$(anolis_out_of_tree_mark)"*) return 0 ;;
		*) return 1 ;;
	esac
}

# The author a sign-off would name, as they wrote themselves.
#
# Decoded when it has to be: git encodes a non-ASCII name in the From:
# header as RFC 2047, and a sign-off carrying "=?UTF-8?q?...?=" would
# match nothing, be read by nobody, and still look like it was there.
_anolis_decode() {
	case "$1" in
		*'=?'*)
			python3 -c 'import email.header, sys
print(str(email.header.make_header(email.header.decode_header(sys.argv[1]))))' \
				"$1" 2>/dev/null || printf '%s\n' "$1"
			;;
		*) printf '%s\n' "$1" ;;
	esac
}

# From a patch file as git format-patch wrote it.  The header's From:,
# not an in-body one: a patch that was sent on somebody else's behalf
# carries both, and the header is the one git am will use.
anolis_patch_author() {
	local from
	from=$(sed -n '/^$/q; s/^From: //p' "$1" | head -1)
	[ -n "${from}" ] || return 1
	_anolis_decode "${from}"
}

# From a commit, for the pass that reads the tree rather than patches.
anolis_commit_author() {
	local from
	from=$(git -C "$1" log -1 --format='%aN <%aE>' "$2" 2>/dev/null)
	[ -n "${from}" ] || return 1
	printf '%s\n' "${from}"
}

# The address out of an identity.
#
# Two spellings of the same person differ in the name and never in the
# address: git quotes a display name containing a full stop, the From:
# header may carry it RFC 2047-encoded, and %aN gives it bare.  A
# sign-off written from any of those has to be recognised as the same
# one, or the prepare pass adds a second and the gate then asks for a
# third.
anolis_identity_email() {
	printf '%s\n' "$1" | sed -n 's/.*<\([^>]*\)>.*/\1/p' | head -1
}

# Whether the message on stdin already carries a sign-off from that
# address, however the name beside it is spelled.
anolis_signed_off_by() {
	local email="$1" pattern

	[ -n "${email}" ] || return 1
	# The address goes into a regular expression, and an address may
	# contain a full stop or a plus; left alone they would match more
	# than the person they name.
	pattern=$(printf '%s' "${email}" | sed 's/[].[^$*\\]/\\&/g')
	grep -qi "^Signed-off-by:.*<${pattern}>"
}
