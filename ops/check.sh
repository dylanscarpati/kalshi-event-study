#!/usr/bin/env bash
# Checks the recorded data against each exchange's integrity information.
# Runs at low priority so the recorders are never slowed. Examples:
#
#     bash /opt/btc-recorder/check.sh              the last 24 hours
#     bash /opt/btc-recorder/check.sh --hours 2    the last 2 hours
#     bash /opt/btc-recorder/check.sh --date 2026-11-22
exec nice -n 15 /opt/btc-recorder/venv/bin/python /opt/btc-recorder/recorder/check.py --data-dir /data "$@"
