#!/usr/bin/with-contenv bashio
# The add-on's entry point: four things a container needs that the loop cannot
# do for itself, then the loop.
set -e

# /data is the add-on's persistent volume, so making it HOME puts both
# ~/.claudeloop (config.toml, state.db, worktrees, clones) and Claude Code's
# own ~/.claude and ~/.claude.json in it. Everything survives a restart and an
# add-on update; nothing survives an uninstall.
export HOME=/data
# One variable, read by config.ingress(). It moves both servers' bind and drops
# the Host and token checks, because the supervisor is the only route in and it
# has already authenticated a Home Assistant user. See the S4 spec.
export CLAUDELOOP_INGRESS=1
# The add-on log is the only console this operator has; buffered output would
# reach it in silent minute-long bursts.
export PYTHONUNBUFFERED=1

if bashio::config.has_value 'claude_code_oauth_token'; then
    export CLAUDE_CODE_OAUTH_TOKEN="$(bashio::config 'claude_code_oauth_token')"
else
    bashio::log.warning \
        "No claude_code_oauth_token is set, so every session will fail to" \
        "authenticate. Run 'claude setup-token' on a machine with a browser" \
        "and paste the result into this add-on's configuration."
fi

# git refuses to commit without an identity, and a session discovering that has
# already been paid for. gpgsign is forced off here rather than through
# [session_env]: a headless box cannot unlock a signing key, and a target
# repository that sets commit.gpgsign = true would otherwise fail every commit.
# This reaches sessions the same way -- child_env starts from os.environ -- and
# a [session_env] entry still wins over it.
git config --global user.name "$(bashio::config 'git_user_name')"
git config --global user.email "$(bashio::config 'git_user_email')"
git config --global commit.gpgsign false
git config --global --add safe.directory '*'

# A headless `claude -p` that stops to ask whether this folder is trusted, or
# whether bypassing permissions is really intended, writes no result file --
# and the loop would nudge that session until max_resumes, paying each time.
if [ ! -f "$HOME/.claude.json" ]; then
    echo '{"hasCompletedOnboarding": true, "bypassPermissionsModeAccepted": true}' \
        > "$HOME/.claude.json"
fi

# Everything above runs as root, which is what an add-on container starts as
# and what /data is owned by. The loop must not: `claude --permission-mode
# bypassPermissions` refuses to run under uid 0, so every session would fail
# instantly with no result file. Hand /data to the unprivileged user first --
# it is HOME, and everything the loop writes goes under it.
chown -R claudeloop:claudeloop /data
# The add-on's own config folder, shown to the operator as
# addon_configs/<slug>/ by the File editor and Samba add-ons. It is where a
# task checklist goes: ClaudeLoop must be able to write the file to mark each
# task off, and a task it cannot mark is offered -- and paid for -- again on
# every poll. Ours alone, so handing it over is safe; /share is not.
#
# `if`, not `[ -d /config ] && chown ...`: under `set -e` a false test as the
# last statement of the script's flow would end the add-on rather than skip a
# line.
if [ -d /config ]; then
    if [ ! -e /config/tasks.md ]; then
        # Something for the operator to edit, since the wizard cannot create
        # it and an empty checklist just idles the loop with no explanation.
        printf '%s\n' \
            "# ClaudeLoop tasks. One task per line. \`- [ ]\` is pending;" \
            "# ClaudeLoop rewrites it to \`- [x]\` when the task is done and" \
            "# \`- [!]\` when it needs you. Anything else here is ignored." \
            "" \
            "- [ ] " > /config/tasks.md
    fi
    chown -R claudeloop:claudeloop /config
fi

# exec, so the loop is the process the supervisor's stop signal reaches rather
# than a shell that would leave it orphaned. setpriv rather than su/runuser
# because it drops privileges without touching the environment this script has
# just built. --setup blocks on the wizard and then falls through into the
# ordinary startup path, so this starts the loop either way; a first run needs
# no flag, since main() opens the wizard by itself when there is no
# config.toml.
cd /app
if bashio::config.true 'setup'; then
    bashio::log.info "setup is on: opening the wizard instead of starting a task."
    exec setpriv --reuid=claudeloop --regid=claudeloop --init-groups \
        python3 -m claudeloop --setup
fi
exec setpriv --reuid=claudeloop --regid=claudeloop --init-groups \
    python3 -m claudeloop
