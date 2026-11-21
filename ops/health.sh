#!/usr/bin/env bash
# Prints the state of every recorder. Run on the recording server:
#
#     bash ops/health.sh
#
# A healthy feed shows "active", a last write within a few seconds, and few
# retries. "last write" is the age of the newest file's latest change; the
# recorder writes to disk every two seconds while messages arrive.
app=/opt/btc-recorder
now=$(date +%s)

printf '%-10s %-9s %-11s %-12s %-12s %-11s %s\n' \
  feed state last-write files-today size-today retries-24h restarts
for feed in $("$app/venv/bin/python" "$app/recorder/recorder.py" --list); do
  unit="btc-recorder@$feed"
  state=$(systemctl is-active "$unit")
  restarts=$(systemctl show -p NRestarts --value "$unit")
  retries=$(journalctl -u "$unit" --since "-24h" --no-pager 2>/dev/null | grep -c "retrying in")
  today="/data/$feed/$(date -u +%F)"
  newest=$(find "/data/$feed" -name '*.jsonl.gz' -printf '%T@\n' 2>/dev/null | sort -n | tail -1)
  if [ -n "$newest" ]; then age="$((now - ${newest%.*})) s ago"; else age="never"; fi
  files=$(find "$today" -name '*.jsonl.gz' 2>/dev/null | wc -l)
  size=$(du -sh "$today" 2>/dev/null | cut -f1)
  printf '%-10s %-9s %-11s %-12s %-12s %-11s %s\n' \
    "$feed" "$state" "$age" "$files" "${size:-0}" "$retries" "$restarts"
done

echo
df -h /data | awk 'NR==1 || NR==2'
echo
echo "clock: $(chronyc tracking 2>/dev/null | grep 'System time' || echo 'chrony not available')"
