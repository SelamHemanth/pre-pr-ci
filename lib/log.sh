# SPDX-License-Identifier: GPL-3.0-only
#
# Pre-PR CI - lib/log.sh
# Shared terminal output: colours, stages and verdicts
#
# Copyright (C) 2025 Advanced Micro Devices, Inc.
# Author: Hemanth Selam <Hemanth.Selam@amd.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# Source this from a script:  . "${SCRIPT_DIR}/../lib/log.sh"
#
# Every script used to define its own colour escapes unconditionally, so a
# run redirected to a file wrote raw escape sequences into it.  That made the
# logs in logs/ unpleasant to read in a pager and broke grep on anything
# adjacent to a colour change.  Here the codes are defined once, and only when
# something is there to interpret them.

# Precedence, strictest first:
#
#   NO_COLOR set to anything non-empty  -> never (no-color.org)
#   FORCE_COLOR set to other than 0/no  -> always, even down a pipe
#   otherwise                           -> only on a capable terminal
#
# FORCE_COLOR=0 has to mean off rather than on.  Treating "set at all" as a
# request for colour is the obvious reading and the wrong one: environments
# that disable colour do it by setting the variable to 0, so that spelling
# turned colour on in exactly the places that had asked for none.
_log_colour=no
if [ -n "${NO_COLOR:-}" ]; then
  _log_colour=no
elif [ -n "${FORCE_COLOR:-}" ] \
     && [ "${FORCE_COLOR}" != "0" ] && [ "${FORCE_COLOR}" != "no" ]; then
  _log_colour=yes
elif [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
  _log_colour=yes
fi

if [ "${_log_colour}" = "yes" ]; then
  GREEN='\033[0;32m'
  RED='\033[0;31m'
  YELLOW='\033[1;33m'
  BLUE='\033[0;34m'
  CYAN='\033[0;36m'
  MAGENTA='\033[0;35m'
  BOLD='\033[1m'
  DIM='\033[2m'
  NC='\033[0m'
else
  GREEN='' RED='' YELLOW='' BLUE='' CYAN='' MAGENTA='' BOLD='' DIM='' NC=''
fi
unset _log_colour
export GREEN RED YELLOW BLUE CYAN MAGENTA BOLD DIM NC

# Stage counter, so a long run says where it is without each script keeping
# its own tally.  LOG_STAGE_TOTAL is optional; without it the count is open
# ended and only the index is shown.
LOG_STAGE_INDEX=0

log_stage() {
  LOG_STAGE_INDEX=$((LOG_STAGE_INDEX + 1))
  local of=""
  [ -n "${LOG_STAGE_TOTAL:-}" ] && of="/${LOG_STAGE_TOTAL}"
  echo ""
  echo -e "${BOLD}${BLUE}==> [${LOG_STAGE_INDEX}${of}] $*${NC}"
}

# A step inside a stage.  Indented so the structure is readable at a glance
# even with the colours stripped out.
log_step()  { echo -e "  ${CYAN}->${NC} $*"; }
log_info()  { echo -e "     $*"; }
log_warn()  { echo -e "  ${YELLOW}!${NC}  $*"; }
log_error() { echo -e "  ${RED}x${NC}  $*" >&2; }

# Verdicts.  The web interface parses these lines out of the job log, so the
# "PASS: name" shape is load bearing: keep the verdict and the name adjacent
# and in this order.
log_pass() { echo -e "${GREEN}PASS${NC}: $1"; }
log_fail() {
  echo -e "${RED}FAIL${NC}: $1"
  [ -n "${2:-}" ] && echo -e "  Reason: $2"
  return 0
}
log_skip() {
  echo -e "${YELLOW}SKIP${NC}: $1"
  [ -n "${2:-}" ] && echo -e "  Reason: $2"
  return 0
}

# Seconds since the epoch at source time, for elapsed reporting.
LOG_STARTED_AT=$(date +%s)

log_elapsed() {
  local secs=$(( $(date +%s) - LOG_STARTED_AT ))
  printf '%dm%02ds' $((secs / 60)) $((secs % 60))
}
