#!/bin/sh
# Container entrypoint: optionally install extra Debian packages (MCPE_APT_PACKAGES,
# space-separated) before handing off to the CMD, for MCP servers that need tools
# beyond the baked-in toolchain (see the Dockerfile's build-tools layer).
#
# Runs BEFORE uvicorn so the tools exist before the supervisor starts any server.
# Failure is non-fatal: a typo'd package or an offline mirror must not brick the
# control plane — the affected servers fail with visible per-server errors instead.
#
# Idempotency: a marker in the container filesystem (NOT /data — the volume outlives
# a container recreate while installed packages do not) records the installed list,
# so a plain restart skips the 10-60s apt round-trip; a recreate or a changed list
# reinstalls.
set -u

MARKER=/var/lib/mcpelevator/.apt-packages-installed

if [ -n "${MCPE_APT_PACKAGES:-}" ]; then
    # shellcheck disable=SC2086  # word-splitting the space-separated list is intended
    want="$(printf '%s\n' ${MCPE_APT_PACKAGES} | sort -u | tr '\n' ' ')"
    if [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$want" ]; then
        echo "[mcpelevator] MCPE_APT_PACKAGES already installed (${want}) — skipping"
    else
        echo "[mcpelevator] installing MCPE_APT_PACKAGES: ${want}"
        # /var/lib/apt/lists/* is purged at image build, so update first.
        # shellcheck disable=SC2086
        if apt-get update -qq \
            && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ${MCPE_APT_PACKAGES}; then
            mkdir -p "$(dirname "$MARKER")"
            printf '%s' "$want" > "$MARKER"
            rm -rf /var/lib/apt/lists/*
            echo "[mcpelevator] MCPE_APT_PACKAGES installed"
        else
            echo "[mcpelevator] WARNING: MCPE_APT_PACKAGES install FAILED (check package names / network)." >&2
            echo "[mcpelevator] WARNING: continuing startup — servers needing these tools will fail to build." >&2
        fi
    fi
fi

exec "$@"
