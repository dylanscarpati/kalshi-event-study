#!/usr/bin/env bash
# Installs or updates the recorder on the recording server and starts one
# recorder per exchange. Run as root, from an uploaded copy of the repository:
#
#     bash ops/install.sh
#
# Updating restarts every recorder, which leaves a gap of a few seconds in the
# recording. The script therefore refuses to run in the three minutes before a
# quarter hour, because those are the seconds the project measures.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run this as root" >&2; exit 1; }
id recorder >/dev/null 2>&1 || { echo "the 'recorder' user does not exist" >&2; exit 1; }

minute=$((10#$(date -u +%M) % 15))
if [ "$minute" -ge 12 ]; then
  echo "too close to a quarter hour; run again in $((16 - minute)) minutes" >&2
  exit 1
fi

src="$(cd "$(dirname "$0")/.." && pwd)"
app=/opt/btc-recorder

install -d -o recorder -g recorder /data
install -d "$app"
rm -rf "$app/recorder"
cp -r "$src/recorder" "$app/recorder"
rm -rf "$app/recorder/tests" "$app/recorder/__pycache__"

# A Python environment needs the python3-venv package. If an earlier attempt
# failed half way, the environment exists without pip; --clear rebuilds it.
dpkg -s python3-venv >/dev/null 2>&1 || apt-get install -y python3-venv
[ -x "$app/venv/bin/pip" ] || python3 -m venv --clear "$app/venv"
"$app/venv/bin/pip" install --quiet -r "$app/recorder/requirements.txt"
install -m 755 "$src/ops/health.sh" "$app/health.sh"
install -m 755 "$src/ops/check.sh" "$app/check.sh"

install -m 644 "$src/ops/btc-recorder@.service" /etc/systemd/system/btc-recorder@.service
systemctl daemon-reload

feeds=$("$app/venv/bin/python" "$app/recorder/recorder.py" --list)
for feed in $feeds; do
  systemctl enable "btc-recorder@$feed" >/dev/null 2>&1
  systemctl restart "btc-recorder@$feed"
done

sleep 8
echo "feed         state"
for feed in $feeds; do
  printf '%-12s %s\n' "$feed" "$(systemctl is-active "btc-recorder@$feed" || true)"
done
echo "In a minute, run 'bash $app/health.sh' to confirm data is arriving."
