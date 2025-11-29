#!/usr/bin/env bash
set -uo pipefail
# euler/clean.sh - openEuler specific cleanup

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(dirname "$SCRIPT_DIR")"

# Load configuration if exists
CONFIG_FILE="${SCRIPT_DIR}/.configure"
if [ -f "${CONFIG_FILE}" ]; then
  # shellcheck disable=SC1090
  . "${CONFIG_FILE}"
fi

echo "Cleaning openEuler specific artifacts..."

# Clean openeuler directory outputs
if [ -n "${LINUX_SRC_PATH:-}" ] && [ -d "${LINUX_SRC_PATH}/openeuler" ]; then
  echo "  → Cleaning ${LINUX_SRC_PATH}/openeuler/outputs"
  rm -rf "${LINUX_SRC_PATH}/openeuler/outputs"
  rm -rf "${LINUX_SRC_PATH}/openeuler/output"
  rm -f "${LINUX_SRC_PATH}/openeuler/kernel-rpms"
  rm -f "${LINUX_SRC_PATH}/openeuler/.deps_installed"
fi

# Clean euler directory outputs (if using euler naming)
if [ -n "${LINUX_SRC_PATH:-}" ] && [ -d "${LINUX_SRC_PATH}/euler" ]; then
  echo "  → Cleaning ${LINUX_SRC_PATH}/euler/outputs"
  rm -rf "${LINUX_SRC_PATH}/euler/outputs"
  rm -rf "${LINUX_SRC_PATH}/euler/output"
  rm -f "${LINUX_SRC_PATH}/euler/kernel-rpms"
  rm -f "${LINUX_SRC_PATH}/euler/.deps_installed"
fi

# Clean kernel build artifacts
if [ -n "${LINUX_SRC_PATH:-}" ] && [ -d "${LINUX_SRC_PATH}" ]; then
  echo "  → Cleaning kernel build artifacts"
  cd "${LINUX_SRC_PATH}"
  make clean > /dev/null 2>&1 || true
  make mrproper > /dev/null 2>&1 || true
fi

echo "openEuler cleanup complete"
