#!/usr/bin/env bash
# Run the first-run setup wizard against a throwaway environment.
#
# Nothing here touches your real config, your real Jackett, or the aria2 that
# may be running your downloads:
#   * HOME is redirected to $SANDBOX, so config.yaml and the daemon state dir
#     land there and the real ~/.config/tget is never opened.
#   * A second Jackett runs on its own port with its own data folder, so
#     indexers the wizard adds are added to *that* one.
#   * A running aria2 is adopted, never owned — the wizard's shutdown path is
#     a no-op for a daemon it did not start.
#
# Clean up with:  scripts/sandbox-setup.sh --clean
set -euo pipefail

SANDBOX="${TRRNT_SANDBOX:-/tmp/trrnt-fresh}"
PORT="${TRRNT_SANDBOX_JACKETT_PORT:-9118}"
DATA="$SANDBOX/jackett-data"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${TRRNT_PYTHON:-$REPO/.venv/bin/python}"

# Stop the throwaway and wait for it to actually let go — Jackett keeps
# writing logs as it shuts down, and deleting under it fails the rm.
stop_jackett() {
    pkill -f "DataFolder $DATA" 2>/dev/null || true
    for _ in $(seq 1 20); do
        pgrep -f "DataFolder $DATA" >/dev/null 2>&1 || return 0
        sleep 0.5
    done
    pkill -9 -f "DataFolder $DATA" 2>/dev/null || true
    sleep 1
}

if [[ "${1:-}" == "--clean" ]]; then
    stop_jackett
    rm -rf "$SANDBOX"
    echo "sandbox removed"
    exit 0
fi

# A fresh wizard run means no config and no indexers: this script is meant to
# be re-runnable, and a previous run's leftovers would short-circuit the very
# steps it is supposed to exercise. Consumed here so it never reaches the CLI.
if [[ "${1:-}" == "--reset" ]]; then
    shift
    stop_jackett
    rm -rf "$DATA/Indexers" "$SANDBOX/config.yaml" "$SANDBOX/.config/tget/config.yaml"
    echo "sandbox reset to first-run state"
fi

mkdir -p "$DATA" "$SANDBOX/Library/Application Support/Jackett"

jackett_up() { curl -s -o /dev/null -m 2 "http://localhost:$PORT/UI/Dashboard"; }

if ! jackett_up; then
    JACKETT="$(command -v jackett || echo /opt/homebrew/bin/jackett)"
    if [[ ! -x "$JACKETT" ]]; then
        echo "jackett not found — install it, or run the wizard without a" >&2
        echo "throwaway instance (it will fall back to asking for a URL)." >&2
        exit 1
    fi
    echo "starting throwaway Jackett on :$PORT ..."
    nohup "$JACKETT" --Port "$PORT" --DataFolder "$DATA" --NoRestart \
        >"$SANDBOX/jackett.log" 2>&1 &
    disown
    for _ in $(seq 1 40); do
        jackett_up && break
        sleep 1
    done
    jackett_up || { echo "throwaway Jackett never came up; see $SANDBOX/jackett.log" >&2; exit 1; }
fi

# Point the sandbox HOME at the throwaway's config so the wizard auto-reads
# *its* API key and port — the same code path a real first run takes, aimed
# somewhere disposable.
ln -sf "$DATA/ServerConfig.json" \
    "$SANDBOX/Library/Application Support/Jackett/ServerConfig.json"

echo "sandbox HOME:     $SANDBOX"
echo "throwaway Jackett: http://localhost:$PORT (real one on 9117 untouched)"
echo

cd "$REPO"
exec env HOME="$SANDBOX" "$PYTHON" -m torrentcli.main "$@"
