#!/usr/bin/env bash
# The daily paper run, as launchd / cron / systemd start it (an example: copy it and edit the
# four paths). It does one session at most: run it as often as you like, a session already
# done is not run again, and orders are never sent twice.
#
# Exit code (python -m tradewind.live.daily.session): 0 done or nothing to do, 2 usage,
# 3 stale prices, 4 done with alerts, 5 run failed, 6 broker unreachable, 7 already running.
set -u

PYTHON="$HOME/venvs/tradewind/bin/python"    # the environment with tradewind (and ib_async for IBKR)
META="$HOME/pwb-paper/meta.yaml"             # the meta-portfolio (its python: has pwb-toolbox)
DIR="$HOME/pwb-paper"                        # state, proposals, reports
BROKER="sim"                                 # sim, or ibkr (TWS / IB Gateway paper, port 7497 / 4002)

# secrets stay out of this file: ALPHAVANTAGE_API_KEY=..., TRADEWIND_NOTIFY_CMD=... (optional)
ENV_FILE="$HOME/.config/tradewind/daily.env"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

ARGS=(--meta "$META" --dir "$DIR" --broker "$BROKER" --fresh-prices alphavantage
      --alert-gap 0.01 --alert-loss 0.03)
# proposals only by default; TRADEWIND_SEND=1 (in the env file) places the orders too
if [ "${TRADEWIND_SEND:-0}" = "1" ]; then ARGS+=(--send); fi

mkdir -p "$DIR/logs"
LOG="$DIR/logs/$(date +%Y-%m-%d).log"
{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') ==="
  "$PYTHON" -m tradewind.live.daily.session "${ARGS[@]}" "$@"
  code=$?
  echo "=== exit $code ==="
} >>"$LOG" 2>&1
exit "${code:-1}"
