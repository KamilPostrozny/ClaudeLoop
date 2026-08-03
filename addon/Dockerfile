# Built with the *repository root* as the build context, not this directory:
#
#     docker build -f addon/Dockerfile --build-arg BUILD_FROM=<base> .
#
# The orchestrator's source is the repository, and a Dockerfile cannot COPY
# from outside its context. Home Assistant's supervisor builds an add-on with
# the add-on's own folder as the context, so it cannot build this one -- which
# is why config.yaml names a prebuilt `image:` and .github/workflows/addon.yml
# is what produces it.
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:bookworm
FROM ${BUILD_FROM}

# Debian rather than Alpine: python3 here is 3.11, the floor this project
# requires, and the Claude Code CLI is built against glibc.
#
# The CLI is installed globally with npm, which puts the native binary it ships
# under /usr/local/lib and a launcher on PATH. The alternative --
# claude.ai/install.sh -- installs under $HOME, and run.sh moves HOME to /data:
# a volume that is empty at build time and mounted at runtime. Node is a build
# dependency rather than a runtime one; what actually runs is that binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        nodejs \
        npm \
        python3 \
        ripgrep \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

# Sessions run as this user, never as root: `claude --permission-mode
# bypassPermissions` refuses to start under uid 0 ("--dangerously-skip-
# permissions cannot be used with root/sudo privileges"), and an add-on's
# container is root by default. Found by running the image, not by reading
# about it -- as root every task failed instantly with no result file.
# /data is the home directory because run.sh moves HOME there.
RUN useradd --uid 1000 --user-group --home-dir /data --shell /bin/bash claudeloop

# The whole application layer. No pip install, no npm install for ClaudeLoop
# itself, no build step for the dashboard: standard library only, and one HTML
# file.
WORKDIR /app
COPY claudeloop /app/claudeloop

COPY addon/run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
