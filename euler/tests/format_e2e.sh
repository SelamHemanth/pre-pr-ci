#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - euler/tests/format_e2e.sh
# End-to-end check that prepare.sh emits what openEuler's checkformat accepts
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# prepare.sh writes the openEuler metadata header; openEuler's format.py
# decides whether that header is acceptable.  Nothing had ever run the
# two against each other, and they disagreed in six places.
#
# So: a throwaway kernel repo holding one commit per shape prepare.sh has
# to handle, prepare.sh run over it for real, and then their checkformat
# run over the result.  A case that prepare.sh refuses outright counts as
# handled -- refusing to emit a patch that will be rejected is the point.
#
# Usage: euler/tests/format_e2e.sh [mirror]
#
# The mirror is a clone of Linus's tree, needed to resolve upstream SHAs
# to release tags.  Nothing is written to it.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$(dirname "${HERE}")")"

MIRROR="${1:-${TORVALDS_REPO:-/home/amd/hemanth/linux}}"
if [ ! -d "${MIRROR}/.git" ] && [ ! -d "${MIRROR}/objects" ]; then
  echo "no mainline mirror at ${MIRROR}" >&2
  echo "pass one as the first argument, or set TORVALDS_REPO" >&2
  exit 2
fi

# Real mainline commits, so the SHA lookups and the subject comparison
# have something true to check against.
#
# TAGGED is old enough to sit inside a release, which is the case that is
# supposed to work.  UNTAGGED is past the newest tag, so "git describe
# --contains" fails on it -- that is the case prepare.sh used to answer
# with the literal string "mainline".
TAGGED_SHA='03b80ff8023adae6780e491f66e932df8165e3a0'
TAGGED_SUBJ='selftests/ftrace: Add new test case which checks non unique symbol'
UNTAGGED_SHA='0b271f7d7f5ed45bc498a03ce0aa9cfd8402fc71'
UNTAGGED_SUBJ='powerpc/iommu: Fix the overflow validation in iommu_tce_check_ioba'
FIXES_SHA='e5ed101a602873d65d2d64edaba93e8c73ec1b0f'
FIXES_SUBJ='mptcp: userspace pm allow creating id 0 subflow'
CONFLICT_SHA='41b07476da38ac2878a14e5b8fe0312c41ea36e3'
CONFLICT_SUBJ='ALSA: hda/realtek - ALC287 Realtek I2S speaker platform support'

SCRATCH="$(mktemp -d)"
trap 'rm -rf "${SCRATCH}"' EXIT

KERNEL="${SCRATCH}/kernel"
COPY="${SCRATCH}/pre-pr-ci"

# ---------------------------------------------------------------- fixtures

commit() {
  local subject="$1" body="$2" file="$3"
  echo "$(date +%s%N)" >> "${KERNEL}/${file}"
  git -C "${KERNEL}" add -A
  printf '%s\n\n%s\n' "${subject}" "${body}" |
    git -C "${KERNEL}" commit -q -F -
}

make_kernel_repo() {
  local only_good="${1:-0}"
  rm -rf "${KERNEL}"
  mkdir -p "${KERNEL}"
  git -C "${KERNEL}" init -q -b OLK-6.6
  git -C "${KERNEL}" config user.name 'Fixture'
  git -C "${KERNEL}" config user.email 'fixture@example.com'
  git -C "${KERNEL}" config commit.gpgsign false

  # A tree checkpatch and the kernel Makefile probes will accept.
  ( cd "${KERNEL}" && touch COPYING CREDITS Kbuild MAINTAINERS Makefile README &&
    mkdir -p Documentation arch include drivers fs init ipc kernel lib )
  git -C "${KERNEL}" add -A
  git -C "${KERNEL}" commit -q -m 'base: empty kernel tree'

  # ---- shapes prepare.sh should be able to complete a header for ----

  # A backport of a commit that has shipped in a release.
  commit "${TAGGED_SUBJ}" \
    "commit ${TAGGED_SHA} upstream.

Upstream body text." 'kernel/a.c'

  # The same, written the way the stable trees write it.  prepare.sh used
  # to not recognise this at all and emit no header.
  commit "${TAGGED_SUBJ}" \
    "[ Upstream commit ${TAGGED_SHA} ]

Upstream body text." 'kernel/e.c'

  # A KABI fix: no upstream commit, KABI in the subject.  This one used
  # to be given no Signed-off-by on purpose.
  commit 'KABI: reserve padding in struct foo' \
    "Reserve space so the ABI survives the next field.

No upstream equivalent." 'kernel/d.c'

  # A bugfix carrying a Fixes: tag too short for their regex, which
  # wants exactly twelve characters.
  commit "${FIXES_SUBJ}" \
    "commit ${FIXES_SHA} upstream.

Fixes: ${TAGGED_SHA:0:8} (\"an earlier commit\")

Upstream body text." 'kernel/h.c'

  # A backport whose diff is nothing like the upstream commit's, which
  # is what openEuler calls a conflict.  It carries the author's own
  # explanation under the heading this tree writes it under; that prose
  # should end up inside the brackets their checker wants.
  commit "${CONFLICT_SUBJ}" \
    "commit ${CONFLICT_SHA} upstream.

Upstream body text.

[Backport Changes]
    The target tree already uses the neighbouring bit, so the flag moved
    to the one it occupies upstream. Every reference is by macro name,
    so no other file changes." 'kernel/i.c'

  [ "${only_good}" = 1 ] && return 0

  # ---- shapes prepare.sh should refuse, because the gate would ----

  # Past the newest tag, so no release contains it.  This used to be
  # given the *preceding* release, claiming it shipped in a version that
  # does not contain it.
  commit "${UNTAGGED_SUBJ}" \
    "commit ${UNTAGGED_SHA} upstream.

Upstream body text." 'kernel/b.c'

  # A short SHA that is in no tree, so it cannot be widened to forty.
  commit 'net: something that does not exist upstream' \
    "commit deadbeef1234 upstream.

Upstream body text." 'kernel/c.c'

  # A subject somebody edited, which their checkformat compares against
  # the upstream commit and rejects.
  commit 'selftests/ftrace: a subject somebody edited' \
    "commit ${TAGGED_SHA} upstream.

Upstream body text." 'kernel/f.c'

  # Forty characters of nothing.  The right shape, naming no commit.
  commit 'fs: a commit from some tree we do not mirror' \
    "commit ffffffffffffffffffffffffffffffffffffffff upstream.

Upstream body text." 'kernel/g.c'
}

NUM_GOOD=5
NUM_FIXTURES=9

# ------------------------------------------------------------------- setup

# A hardlinked copy, so prepare.sh's real .configure and the running service
# are left alone.  The submodules are shared rather than copied; nothing
# here writes to them.
make_project_copy() {
  local count="$1"
  rm -rf "${COPY}"
  mkdir -p "${COPY}"
  cp -al "${PROJECT}/euler" "${PROJECT}/lib" "${COPY}/" 2>/dev/null ||
    cp -a "${PROJECT}/euler" "${PROJECT}/lib" "${COPY}/"

  cat > "${COPY}/euler/.configure" <<EOF
LINUX_SRC_PATH="${KERNEL}"
SIGNER_NAME="Fixture Signer"
SIGNER_EMAIL="fixture@example.com"
BUGZILLA_ID="12345"
PATCH_CATEGORY="bugfix"
NUM_PATCHES="${count}"
OE_TARGET_BRANCH="OLK-6.6"
BUILD_THREADS="4"
TORVALDS_REPO="${MIRROR}"
EOF
}

run_build() {
  local count="$1" rc=0
  make_project_copy "${count}"
  ( cd "${COPY}" && bash euler/prepare.sh ) > "${SCRATCH}/build.log" 2>&1 || rc=$?
  return ${rc}
}

# --------------------------------------------------------------------- run

echo "mirror : ${MIRROR}"
echo "scratch: ${SCRATCH}"
echo

failures=0

# ---- phase one: prepare.sh must refuse what the gate would reject -----------
#
# The point is not that these fail, it is that they fail here, before the
# tree has been rewound and before anybody has waited for a CI run.

echo "=== phase 1: patches prepare.sh should refuse ==="
make_kernel_repo 0
build_rc=0
run_build "${NUM_FIXTURES}" || build_rc=$?

grep -E '^  (✓|✗)|^    ' "${SCRATCH}/build.log" | sed 's/^/  /' || true
echo
if [ "${build_rc}" -eq 21 ]; then
  echo "  prepare.sh refused the series and left the tree alone, as it should."
else
  echo "  UNEXPECTED: prepare.sh exited ${build_rc}, wanted 21 (refused)."
  failures=$((failures + 1))
fi

# The tree must be exactly where it started.  A prepare.sh that refuses
# halfway and leaves the branch rewound is worse than one that never
# checked.
if [ -n "$(git -C "${KERNEL}" status --porcelain)" ]; then
  echo "  UNEXPECTED: the kernel tree was left dirty."
  failures=$((failures + 1))
fi
if [ "$(git -C "${KERNEL}" rev-list --count HEAD)" -ne "$((NUM_FIXTURES + 1))" ]; then
  echo "  UNEXPECTED: the kernel tree was left rewound."
  failures=$((failures + 1))
fi
echo

# ---- phase two: what it accepts must pass their checkformat ---------------

echo "=== phase 2: patches prepare.sh should complete ==="
make_kernel_repo 1
build_rc=0
run_build "${NUM_GOOD}" || build_rc=$?
grep -E '^  (✓|✗)|^    ' "${SCRATCH}/build.log" | sed 's/^/  /' || true
echo
if [ "${build_rc}" -ne 0 ]; then
  echo "  UNEXPECTED: prepare.sh exited ${build_rc}; it should have finished."
  sed 's/^/    /' "${SCRATCH}/build.log"
  exit 1
fi

# Their regex wants a Fixes: tag of exactly twelve characters.
if git -C "${KERNEL}" log -n "${NUM_GOOD}" --format='%B' |
     grep -qE '^Fixes: [0-9a-f]{12} \('; then
  echo "  Fixes: tag normalised to twelve characters."
else
  echo "  UNEXPECTED: no twelve-character Fixes: tag in the result."
  failures=$((failures + 1))
fi

# Every patch needs one, including the KABI fix that used to be exempt.
missing_sob=$(git -C "${KERNEL}" log -n "${NUM_GOOD}" --format='%H %B' |
  awk '/^[0-9a-f]{40} /{if (h && !s) print h; h=$1; s=0}
       /^Signed-off-by:/{s=1}
       END{if (h && !s) print h}' | wc -l)
if [ "${missing_sob}" -eq 0 ]; then
  echo "  Signed-off-by on every patch."
else
  echo "  UNEXPECTED: ${missing_sob} patch(es) without Signed-off-by."
  failures=$((failures + 1))
fi
echo

NUM_FIXTURES="${NUM_GOOD}"

# ---- preparing twice must be a no-op -------------------------------------
#
# Preparing rewrites history. Doing it again to a prepared series costs a
# rebuild of everything downstream and replaces any Conflicts: text
# written by hand with a fresh placeholder, so "already done" has to be
# detected rather than discovered halfway through.

head_before="$(git -C "${KERNEL}" rev-parse HEAD)"
rerun_rc=0
( cd "${COPY}" && bash euler/prepare.sh ) > "${SCRATCH}/rerun.log" 2>&1 || rerun_rc=$?

if [ "${rerun_rc}" -ne 0 ]; then
  echo "  UNEXPECTED: preparing an already-prepared series exited ${rerun_rc}."
  failures=$((failures + 1))
elif ! grep -q 'Already prepared' "${SCRATCH}/rerun.log"; then
  echo "  UNEXPECTED: the second run did not recognise the series as prepared."
  sed 's/^/    /' "${SCRATCH}/rerun.log" | head -n 20
  failures=$((failures + 1))
elif [ "$(git -C "${KERNEL}" rev-parse HEAD)" != "${head_before}" ]; then
  echo "  UNEXPECTED: the second run rewrote the branch."
  failures=$((failures + 1))
else
  echo "  Preparing twice is a no-op; the branch is untouched."
fi
echo

if [ "${VERBOSE:-0}" = 1 ]; then
  echo "=== the commit messages prepare.sh produced ==="
  git -C "${KERNEL}" log --format='%n----- %h %s%n%b' -n "${NUM_FIXTURES}" |
    sed 's/^/  /'
  echo
fi

oe_check() {
  python3 "${COPY}/euler/oe_checks.py" "$1" \
    --source "${PROJECT}/euler/hulk_robot_test" \
    --kernel "${KERNEL}" \
    --workdir "${COPY}" \
    --mirror "${MIRROR}" \
    --branch OLK-6.6 \
    --count "${NUM_FIXTURES}" > "${SCRATCH}/$1.log" 2>&1
}

oe_check checkformat
check_rc=$?

# Every fixture here diverges from the commit it names, so checkconflict
# has something to say about all of them.  It passing means the
# Conflicts: sections prepare.sh wrote are in the shape their regex wants,
# which is the whole reason for generating them.
oe_check checkconflict
conflict_rc=$?

if [ "${VERBOSE:-0}" = 1 ]; then
  echo "=== openEuler checkformat ==="
  sed 's/^/  /' "${SCRATCH}/checkformat.log"
  echo
fi

# checkformat prints "check <sha> <subject>" and then either "check
# success" or "check <sha> <subject> failed" with the reason on the
# following lines.  Turn that into one row per fixture.
echo "=== per patch ==="
python3 - "${SCRATCH}/checkformat.log" <<'PY'
import re, sys

# oe_checks.py indents the tool's own output, so compare on stripped text.
lines = [l.strip() for l in
         open(sys.argv[1], encoding='utf-8', errors='replace').read().splitlines()]

HEADER = re.compile(r'^check ([0-9a-f]{7,40}) (.*?)( failed)?$')

seen, order = {}, []
for i, line in enumerate(lines):
    m = HEADER.match(line)
    if not m:
        continue
    sha, subject, failed = m.group(1), m.group(2), bool(m.group(3))
    if sha not in seen:
        order.append(sha)
        seen[sha] = {'subject': subject, 'ok': None, 'why': ''}

    # Everything up to the next header belongs to this patch.  Reading
    # past it attributes one patch's reason to another, which is worse
    # than no reason at all.
    body = []
    for l in lines[i + 1:]:
        if HEADER.match(l):
            break
        if l:
            body.append(l)

    if failed:
        # The reason is either a sentence, or the format template with
        # an arrow marking the line that did not match.
        arrow = next((l for l in body if '<------' in l), None)
        why = arrow.split('<------')[1] if arrow else (body[0] if body else '')
        seen[sha].update(ok=False, why=why)
    elif body and body[0] == 'check success':
        seen[sha]['ok'] = True

for sha in order:
    r = seen[sha]
    mark = 'accepted' if r['ok'] else 'REJECTED'
    print('  %-8s %-62s %s' % (mark, r['subject'][:62], r['why'][:70]))
PY

echo
if [ "${check_rc}" -ne 0 ]; then
  echo "  openEuler's checkformat rejected a patch prepare.sh was happy with."
  failures=$((failures + 1))
fi

if [ "${VERBOSE:-0}" = 1 ]; then
  echo "=== openEuler checkconflict ==="
  sed 's/^/  /' "${SCRATCH}/checkconflict.log"
  echo
fi

if [ "${conflict_rc}" -eq 0 ]; then
  echo "  Conflicts: sections accepted by openEuler's checkconflict."
else
  echo "  UNEXPECTED: checkconflict rejected the Conflicts: sections prepare.sh wrote:"
  grep -vE '^\s*$' "${SCRATCH}/checkconflict.log" | tail -n 20 | sed 's/^/    /'
  failures=$((failures + 1))
fi

echo
echo "=== verdict ==="
if [ "${failures}" -eq 0 ]; then
  echo "  prepare.sh agrees with openEuler's checkformat and checkconflict."
  exit 0
fi
echo "  ${failures} disagreement(s) between prepare.sh and openEuler's gate."
exit 1
