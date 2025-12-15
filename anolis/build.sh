#!/usr/bin/env bash
set -euo pipefail

# anolis/build.sh - OpenAnolis specific build script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$SCRIPT_DIR")"

# Load configuration
CONFIG_FILE="${SCRIPT_DIR}/.configure"
DISTRO_CONFIG="${WORKDIR}/.distro_config"

if [ ! -f "${CONFIG_FILE}" ]; then
  echo "Error: Configuration file not found. Run 'make config' first." >&2
  exit 1
fi

# shellcheck disable=SC1090
. "${CONFIG_FILE}"

if [ -f "${DISTRO_CONFIG}" ]; then
  . "${DISTRO_CONFIG}"
fi

# Directories
PATCHES_DIR="${WORKDIR}/patches"
BKP_DIR="${PATCHES_DIR}/.bkp"
LOGS_DIR="${WORKDIR}/logs"
HEAD_ID_FILE="${WORKDIR}/.head_commit_id"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

: "${LINUX_SRC_PATH:?missing in config}"
: "${SIGNER_NAME:?missing in config}"
: "${SIGNER_EMAIL:?missing in config}"
: "${ANBZ_ID:?missing in config}"
: "${NUM_PATCHES:?missing in config}"
: "${BUILD_THREADS:=4}"

mkdir -p "${PATCHES_DIR}" "${BKP_DIR}" "${LOGS_DIR}"

# Validate repo
if [ ! -d "${LINUX_SRC_PATH}/.git" ]; then
  echo -e "${RED}Linux source path is not a git repo: ${LINUX_SRC_PATH}${NC}" >&2
  exit 10
fi

cd "${LINUX_SRC_PATH}"

# Handle dubious ownership issue
if ! git rev-parse --git-dir >/dev/null 2>&1; then
	echo -e "${YELLOW}Warning: Git detected dubious ownership in repository${NC}" >&2
	echo -e "${YELLOW}Attempting to add safe.directory exception...${NC}" >&2
	git config --global --add safe.directory "${LINUX_SRC_PATH}" 2>/dev/null || {
		echo -e "${RED}Failed to add safe.directory exception${NC}" >&2
		echo -e "${RED}Please run manually: git config --global --add safe.directory ${LINUX_SRC_PATH}${NC}" >&2
		exit 12
	}
	echo -e "${GREEN}Safe directory exception added successfully${NC}" >&2
fi

TOTAL_COMMITS="$(git rev-list --count HEAD 2>/dev/null || true)"
if [ -z "${TOTAL_COMMITS}" ] || [ "${TOTAL_COMMITS}" -lt "${NUM_PATCHES}" ]; then
  echo -e "${RED}Repo has insufficient commits (${TOTAL_COMMITS}) for NUM_PATCHES=${NUM_PATCHES}${NC}" >&2
  exit 11
fi

# Save current HEAD id for later reset (full SHA)
HEAD_ID="$(git rev-parse --verify HEAD)"
printf "%s\n" "${HEAD_ID}" > "${HEAD_ID_FILE}"
echo -e "${BLUE}Saved HEAD commit: ${HEAD_ID}${NC}"

TMP_FORMAT_DIR="$(mktemp -d "${WORKDIR}/formatpatches.XXXX")"
echo -e "${BLUE}Generating ${NUM_PATCHES} patches...${NC}"
git -c core.quiet=true format-patch -${NUM_PATCHES} -o "${TMP_FORMAT_DIR}" "HEAD~${NUM_PATCHES}..HEAD" >/dev/null 2>&1 || {
  git format-patch -${NUM_PATCHES} -o "${TMP_FORMAT_DIR}" "HEAD~${NUM_PATCHES}..HEAD"
}

# Backup existing patches and move new ones
mkdir -p "${BKP_DIR}"
for ex in "${PATCHES_DIR}"/*.patch; do
  [ -f "${ex}" ] || continue
  cp -f "${ex}" "${BKP_DIR}/$(basename "${ex}").bak-$(date +%s)"
done
rm -f "${PATCHES_DIR}"/*.patch || true
mv "${TMP_FORMAT_DIR}"/*.patch "${PATCHES_DIR}/" 2>/dev/null || true
rm -rf "${TMP_FORMAT_DIR}"

# Reset repo back by NUM_PATCHES commits so we can re-apply
git reset --hard "HEAD~${NUM_PATCHES}" >/dev/null 2>&1 || true
echo -e "${YELLOW}HEAD is now at $(git rev-parse --short HEAD) $(git log -1 --pretty=%s)${NC}"

echo -e "${BLUE}Modifying patches with ANBZ and Signed-off-by tags...${NC}"
# Modify patches in-place with required formatting
for p in "${PATCHES_DIR}"/*.patch; do
  [ -f "${p}" ] || continue
  cp -f "${p}" "${BKP_DIR}/$(basename "${p}")"

  # Insert ANBZ after Subject
  awk -v ANBZ="ANBZ: #${ANBZ_ID}" '
    BEGIN { in_sub=0; printed_anbz=0 }
    {
      if (!in_sub) {
        print $0
        if ($0 ~ /^Subject:/) { in_sub=1; next }
      } else if (in_sub && !printed_anbz) {
        if ($0 ~ /^$/) {
          print ""
          print ANBZ
          print ""
          printed_anbz=1
          next
        } else {
          print $0
          next
        }
      } else {
        print $0
      }
    }
    END {
      if (in_sub && !printed_anbz) {
        print ""
        print ANBZ
        print ""
      }
    }' "${p}" > "${p}.tmp" && mv "${p}.tmp" "${p}"

  # Insert Signed-off-by before first '---'
  awk -v SOB="Signed-off-by: ${SIGNER_NAME} <${SIGNER_EMAIL}>" '
    BEGIN { inserted=0 }
    {
      if (!inserted && $0 ~ /^---$/) {
        print SOB
        inserted=1
      }
      print $0
    }
    END {
      if (!inserted) {
        print ""
        print SOB
      }
    }' "${p}" > "${p}.tmp" && mv "${p}.tmp" "${p}"
done

# Ensure repo clean
if [ -n "$(git status --porcelain)" ]; then
  echo -e "${RED}Linux source tree is not clean. Commit or stash changes before running.${NC}" >&2
  exit 12
fi

git config user.name "${SIGNER_NAME}"
git config user.email "${SIGNER_EMAIL}"

# Build function for OpenAnolis
run_anolis_build() {
  local repo_dir="$1"
  local logpath="$2"
  echo "OpenAnolis Build log for ${logpath}" > "${logpath}"
  ( cd "${repo_dir}" && bash -lc "make clean && make anolis_defconfig && make -j${BUILD_THREADS} -s && make modules -j${BUILD_THREADS} -s" ) >> "${logpath}" 2>&1
  return $?
}

# Collect patch filenames in lexical order
mapfile -t PATCH_LIST < <(ls -1 "${PATCHES_DIR}"/*.patch 2>/dev/null || true)
TOTAL_SELECTED="${#PATCH_LIST[@]}"
if [ "${TOTAL_SELECTED}" -eq 0 ]; then
  echo -e "${RED}No patches found in ${PATCHES_DIR}${NC}" >&2
  exit 13
fi

echo ""
echo -e "Total patches to process: ${TOTAL_SELECTED}"
echo -e "Build threads: ${BUILD_THREADS}"
echo ""

# Apply and build one patch at a time
summary=()
idx=0

for pf in "${PATCH_LIST[@]}"; do
  idx=$((idx+1))
  name="$(basename "${pf}")"

  echo -e "${BLUE}[${idx}/${TOTAL_SELECTED}] Processing: ${name}${NC}"

  # Apply patch
  if git -C "${LINUX_SRC_PATH}" am --3way "${pf}" >/dev/null 2>&1; then
    echo -e "  Applying   : ${GREEN}✓ PASS${NC}"
  else
    git -C "${LINUX_SRC_PATH}" am --abort >/dev/null 2>&1 || true
    echo -e "  Applying   : ${RED}✗ FAIL${NC}"
    echo ""
    echo -e "${RED}Error: git am failed for ${name}${NC}"
    echo -e "${YELLOW}No build was attempted for this patch${NC}"
    exit 20
  fi

  # Build patch
  logfile="${LOGS_DIR}/${name}.log"
  if run_anolis_build "${LINUX_SRC_PATH}" "${logfile}"; then
    echo -e "  Building   : ${GREEN}✓ PASS${NC}"
    summary+=( "${name}:PASS" )
  else
    echo -e "  Building   : ${RED}✗ FAIL${NC}"
    summary+=( "${name}:FAIL" )
    echo ""
    echo -e "${RED}Error: Build failed for ${name}${NC}"
    echo -e "${YELLOW}Refer to the log: ${logfile}${NC}"
    exit 21
  fi
  echo ""
done

echo ""
echo -e "${GREEN}✓ OpenAnolis build process completed successfully${NC}"
echo -e "Run ${YELLOW}'make test'${NC} to execute OpenAnolis-specific tests"
exit 0
