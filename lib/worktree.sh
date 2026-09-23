# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/worktree.sh
# Make sure the kernel tree is safe to rewrite before rewriting it
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# Preparing a series rewinds the branch with "git reset --hard", which
# destroys uncommitted changes to tracked files without asking.  So the
# check has to happen before any of it starts, and it has to be about
# the thing that is actually at risk.
#
# Expects LINUX_SRC_PATH, and the colours from lib/log.sh.

# Files openEuler's own checks leave in the kernel tree.  check_conflict.py
# renders each commit and its upstream counterpart to disk and writes a
# summary for the step that posts a comment on the pull request; under
# Jenkins the tree is discarded afterwards, so nothing there cleans up.
# They are not the user's files and there is no reason to ask about them.
_prci_sweep_check_artifacts() {
  local kernel="$1" removed=0 f
  for f in "${kernel}/checkconflict_diff_info.json" \
           "${kernel}"/branch_[0-9a-f]*.txt \
           "${kernel}"/mainline_[0-9a-f]*.txt \
           "${kernel}"/olk66_[0-9a-f]*.txt; do
    [ -f "${f}" ] || continue
    # Only ever remove something git does not know about, so a real
    # source file that happens to match cannot be lost.
    git -C "${kernel}" ls-files --error-unmatch "$(basename "${f}")" \
      >/dev/null 2>&1 && continue
    rm -f "${f}" && removed=$((removed + 1))
  done
  [ "${removed}" -eq 0 ] || echo -e "${BLUE}Cleared ${removed} leftover file(s) from a previous check run.${NC}"
}

# Refuse when the tree holds work that rewinding would destroy.
#
# Tracked changes only.  An untracked file survives "git reset --hard"
# and does not affect format-patch, so refusing over one means refusing
# over a stray editor backup -- which is how a run came to be thrown
# away over a JSON file that this tool had written itself.
require_clean_tree() {
  local kernel="${1:-${LINUX_SRC_PATH}}"

  _prci_sweep_check_artifacts "${kernel}"

  local dirty
  dirty="$(git -C "${kernel}" status --porcelain --untracked-files=no)"
  if [ -n "${dirty}" ]; then
    echo -e "${RED}The kernel tree has uncommitted changes, and preparing the" >&2
    echo -e "series rewinds the branch, which would destroy them:${NC}" >&2
    echo "${dirty}" | sed 's/^/  /' >&2
    echo -e "${YELLOW}Commit or stash them, then run this again.${NC}" >&2
    exit 12
  fi

  local untracked
  untracked="$(git -C "${kernel}" ls-files --others --exclude-standard)"
  if [ -n "${untracked}" ]; then
    # Harmless for the rewind, but worth saying: an untracked file that
    # a patch also adds will stop "git am" later on.
    local count
    count="$(printf '%s\n' "${untracked}" | wc -l)"
    echo -e "${YELLOW}Note: ${count} untracked file(s) in the kernel tree; leaving them alone.${NC}"
  fi
}
