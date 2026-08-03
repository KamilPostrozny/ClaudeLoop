#!/usr/bin/env bash
# Stand-in for `claude plugin ...`, driven by files rather than mocks --
# the same choice tests/ already makes for the CLI and for Jira.
#
# FAKE_PLUGIN_STATE: one plugin id per line, "!" prefix meaning installed
#   but disabled. Read by `plugin list --json`, written by install/enable.
# FAKE_PLUGIN_CALLS: every invocation's arguments, appended one per line.
# FAKE_PLUGIN_FAIL: a substring; any invocation containing it exits 1.
# FAKE_PLUGIN_NOOP_INSTALL: if set, `plugin install` reports success and
#   changes nothing, which is the case reconcile's re-check exists for.
set -u

if [ -n "${FAKE_PLUGIN_CALLS:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_PLUGIN_CALLS"
fi

if [ -n "${FAKE_PLUGIN_FAIL:-}" ] && [[ "$*" == *"$FAKE_PLUGIN_FAIL"* ]]; then
  echo "fake failure for: $*" >&2
  exit 1
fi

touch "$FAKE_PLUGIN_STATE"

case "$1 $2" in
  "plugin list")
    printf '['
    first=1
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      case "$line" in
        !*) enabled=false; id=${line#!} ;;
        *)  enabled=true;  id=$line ;;
      esac
      [ $first -eq 1 ] || printf ','
      first=0
      printf '{"id":"%s","scope":"user","enabled":%s}' "$id" "$enabled"
    done < "$FAKE_PLUGIN_STATE"
    printf ']\n'
    ;;
  "plugin install")
    if [ -z "${FAKE_PLUGIN_NOOP_INSTALL:-}" ]; then
      printf '%s\n' "$3" >> "$FAKE_PLUGIN_STATE"
    fi
    echo "installed $3"
    ;;
  "plugin enable")
    tmp=$(mktemp)
    sed "s|^!$3$|$3|" "$FAKE_PLUGIN_STATE" > "$tmp"
    mv "$tmp" "$FAKE_PLUGIN_STATE"
    echo "enabled $3"
    ;;
  "plugin marketplace")
    echo "marketplace $3 $4"
    ;;
  *)
    echo "fake claude: unexpected invocation: $*" >&2
    exit 2
    ;;
esac
