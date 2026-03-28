#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
COLLECTOR_SCRIPT="${ROOT_DIR}/scripts/start_market_data_collector.sh"
BOT_ENTRY="${ROOT_DIR}/main.py"
COLLECTOR_LOG_DIR="${ROOT_DIR}/market_data/logs"

SKIP_COLLECTOR=0
COLLECTOR_EXTRA_ARGS=()
COLLECTOR_PID=""

usage() {
  cat <<'EOF'
Usage:
  bash scripts/start_bot_with_market_data.sh [options]

Options:
  --skip-collector          Start the bot only.
  --collector-poll-sec N    Pass through to market data collector.
  --collector-pre-seconds N Pass through to market data collector.
  --collector-post-seconds N
                            Pass through to market data collector.
  --help, -h                Show this help message.

Examples:
  bash scripts/start_bot_with_market_data.sh
  bash scripts/start_bot_with_market_data.sh --collector-poll-sec 0.5
  bash scripts/start_bot_with_market_data.sh --skip-collector
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-collector)
      SKIP_COLLECTOR=1
      shift
      ;;
    --collector-poll-sec|--collector-pre-seconds|--collector-post-seconds)
      COLLECTOR_EXTRA_ARGS+=("${1}" "${2:-}")
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

detect_conda_base() {
  if [[ -n "${CONDA_EXE:-}" ]]; then
    dirname "$(dirname "${CONDA_EXE}")"
    return 0
  fi
  if command -v conda >/dev/null 2>&1; then
    conda info --base
    return 0
  fi
  return 1
}

CONDA_BASE="$(detect_conda_base || true)"
if [[ -z "${CONDA_BASE}" ]]; then
  echo "conda not found. Please install Conda or ensure 'conda' is on PATH." >&2
  exit 1
fi

# shellcheck source=/dev/null
source "${CONDA_BASE}/etc/profile.d/conda.sh"
conda activate polymarket-bot

MODE="$(python - <<'PY'
from core.config import SETTINGS
print("dryrun" if SETTINGS.dry_run else "live")
PY
)"

cleanup() {
  if [[ -n "${COLLECTOR_PID}" ]] && kill -0 "${COLLECTOR_PID}" >/dev/null 2>&1; then
    echo "[stack] stopping market data collector (pid=${COLLECTOR_PID})"
    kill "${COLLECTOR_PID}" >/dev/null 2>&1 || true
    wait "${COLLECTOR_PID}" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

echo "[stack] conda env activated: polymarket-bot"
echo "[stack] detected bot mode: ${MODE}"

if [[ "${SKIP_COLLECTOR}" -eq 0 ]]; then
  mkdir -p "${COLLECTOR_LOG_DIR}"
  TS="$(date +"%Y-%m-%dT%H-%M-%S")"
  COLLECTOR_LOG="${COLLECTOR_LOG_DIR}/stack-collector-${MODE}-${TS}.log"

  bash "${COLLECTOR_SCRIPT}" --mode "${MODE}" "${COLLECTOR_EXTRA_ARGS[@]}" >"${COLLECTOR_LOG}" 2>&1 &
  COLLECTOR_PID=$!
  sleep 1
  if ! kill -0 "${COLLECTOR_PID}" >/dev/null 2>&1; then
    echo "[stack] market data collector failed to start. Recent log:" >&2
    tail -n 50 "${COLLECTOR_LOG}" >&2 || true
    exit 1
  fi
  echo "[stack] market data collector started (pid=${COLLECTOR_PID})"
  echo "[stack] collector log: ${COLLECTOR_LOG}"
else
  echo "[stack] collector skipped by flag"
fi

echo "[stack] starting bot: ${BOT_ENTRY}"
python "${BOT_ENTRY}"
