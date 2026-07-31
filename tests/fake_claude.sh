#!/usr/bin/env bash
# Stand-in for the real `claude` CLI. Emits a canned stream-json stream.
#
# FAKE_LIMIT_FLAG: if set and the named file exists, delete it, emit a blocking
# rate_limit_event whose reset is already in the past, and exit non-zero
# without writing a result. The next invocation therefore succeeds, which is
# how the end-to-end test exercises the recover-and-resume path.
# FAKE_ARGS_OUT: if set, the invocation's arguments are appended to that file.
set -u

if [ -n "${FAKE_ARGS_OUT:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_ARGS_OUT"
fi

echo '{"type":"system","subtype":"init","session_id":"fake"}'
echo 'diagnostic noise on stderr' >&2

if [ -n "${FAKE_LIMIT_FLAG:-}" ] && [ -f "$FAKE_LIMIT_FLAG" ]; then
  rm -f "$FAKE_LIMIT_FLAG"
  past=$(( $(date +%s) - 120 ))
  echo "{\"type\":\"rate_limit_event\",\"rate_limit_info\":{\"status\":\"rejected\",\"resetsAt\":${past},\"rateLimitType\":\"five_hour\"}}"
  exit 1
fi

echo 'this line is not json'
printf '%s' '{"status":"done","summary":"fake work"}' > "$CLAUDELOOP_RESULT"
echo '{"type":"result","subtype":"success","total_cost_usd":0.5,"result":"ok"}'
