#!/bin/bash
# Starts IB Gateway (paper) through IBC unless it is already running. The scheduler calls it
# (launchd on a Mac, systemd on Linux); you can run it by hand too. An example: copy it next
# to IBC (~/ibc/) and check the paths.
#
# It starts nothing while config.ini has no login, so that a scheduler loaded too early
# does not fail to log in again and again.
set -u
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
IBC_DIR="$HOME/ibc"                                # IBC's files and its config.ini
INI="$IBC_DIR/config.ini"
PORT=4002                                          # IB Gateway paper
if [ "$(uname)" = Darwin ]; then
  START="$IBC_DIR/gatewaystartmacos.sh"            # from the IBC release, paths edited
else
  START="$IBC_DIR/gatewaystart.sh"
fi
stamp() { date '+%F %T'; }

if ! grep -q '^IbLoginId=[^[:space:]]' "$INI"; then
  echo "$(stamp) IbLoginId is empty in $INI: nothing started" >&2
  exit 78                                          # EX_CONFIG
fi

# already there, started by IBC or by hand (IBC's java command line names the Gateway folder)
if pgrep -f "IB Gateway|ibgateway" >/dev/null || lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "$(stamp) IB Gateway is already running: nothing to do"
  exit 0
fi

echo "$(stamp) starting IB Gateway through IBC"
exec "$START" -inline
