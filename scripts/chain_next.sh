#!/usr/bin/env bash
# Self-chain for news-relay (2026-10-14 rewrite). Keeps ~30 min cadence with the built-in GITHUB_TOKEN (no new secret).
# Rule: skip ONLY if a run created AFTER this run exists (queued/running). Older/finishing runs are ignored
# (the old "any other run" check raced with the parent that dispatched us and killed the chain on Oct 13 23:42 Riyadh).
# Self-heal: after dispatching, confirm a newer run appeared; retry the dispatch up to 3 times. The cron schedule stays as backup.
# Env: GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_REF_NAME, RELAY_START (epoch s), CHAIN_PERIOD (s, default 1680), GH (default gh), SLEEP (default sleep)
set -uo pipefail
GH=${GH:-gh}; SLEEP=${SLEEP:-sleep}; PERIOD=${CHAIN_PERIOD:-1680}
ME=$($GH api "repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" --jq .created_at 2>/dev/null || true)
if [ -z "$ME" ]; then echo "::warning::cannot read own created_at - assuming none newer"; ME="9999"; fi
newer() {
  $GH run list --repo "$GITHUB_REPOSITORY" --workflow news-relay.yml --limit 30 --json databaseId,status,createdAt \
    --jq "[.[] | select(.databaseId != ${GITHUB_RUN_ID} and .createdAt > \"$ME\" and (.status==\"queued\" or .status==\"pending\" or .status==\"waiting\" or .status==\"requested\" or .status==\"in_progress\"))] | length" 2>/dev/null || echo 0
}
n=$(newer); if [ "${n:-0}" != "0" ]; then echo "a newer run exists ($n) - it carries the chain"; exit 0; fi
el=$(( $(date +%s) - ${RELAY_START:-$(date +%s)} )); w=$(( PERIOD - el )); [ "$w" -lt 30 ] && w=30
echo "sleeping ${w}s"; $SLEEP "$w"
n=$(newer); if [ "${n:-0}" != "0" ]; then echo "a newer run appeared ($n) - it carries the chain"; exit 0; fi
for i in 1 2 3; do
  if $GH workflow run news-relay.yml --repo "$GITHUB_REPOSITORY" --ref "${GITHUB_REF_NAME}"; then
    for k in 1 2 3 4 5 6; do $SLEEP 10; n=$(newer); [ "${n:-0}" != "0" ] && { echo "next run dispatched and visible (try $i)"; exit 0; }; done
    echo "::warning::dispatch accepted but no newer run visible (try $i)"
  else
    echo "::warning::dispatch failed (try $i)"; $SLEEP 20
  fi
done
echo "::warning::chain could not be continued - the cron schedule will restart it"
exit 0
