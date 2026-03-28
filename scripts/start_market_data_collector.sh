#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${ROOT_DIR}/market_data/logs"

MODE="live"
BACKGROUND=0
EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage:
  start_market_data_collector.sh [--mode live|dryrun] [--background] [extra collector args...]

Examples:
  bash scripts/start_market_data_collector.sh
  bash scripts/start_market_data_collector.sh --background
  bash scripts/start_market_data_collector.sh --mode dryrun --poll-sec 0.5
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --background)
      BACKGROUND=1
      shift
      ;;
    --foreground)
      BACKGROUND=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ "${MODE}" != "live" && "${MODE}" != "dryrun" ]]; then
  echo "Invalid mode: ${MODE}" >&2
  usage >&2
  exit 1
fi

mkdir -p "${LOG_DIR}"

CMD=(
  conda run -n polymarket-bot python -m scripts.market_data_collector
  --mode "${MODE}"
)

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=("${EXTRA_ARGS[@]}")
fi

cd "${ROOT_DIR}"

if [[ "${BACKGROUND}" -eq 1 ]]; then
  TS="$(date +"%Y-%m-%dT%H-%M-%S")"
  LOG_PATH="${LOG_DIR}/collector-${MODE}-${TS}.log"
  nohup "${CMD[@]}" >"${LOG_PATH}" 2>&1 &
  PID=$!
  echo "market data collector started in background"
  echo "pid=${PID}"
  echo "log=${LOG_PATH}"
  exit 0
fi

exec "${CMD[@]}"
