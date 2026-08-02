#!/bin/sh
# trrnt installer.
#
#   curl -fsSL https://raw.githubusercontent.com/BeauOuellette/trrnt/main/install.sh | sh
#
# This exists because "install Python, then pip, then figure out why the
# command is not on your PATH" is not an install story. It picks a tool
# installer that already solves isolation and PATH — uv, or pipx if that is
# what you have — and gets out of the way.
#
# Everything it does is printed before it happens. It installs nothing with
# sudo and touches nothing outside ~/.local.
#
# Overrides:
#   TRRNT_SOURCE=pypi|git   force where the package comes from
#   TRRNT_REF=<tag|branch>  install a specific ref when the source is git

set -eu

REPO="BeauOuellette/trrnt"
PACKAGE="trrnt"

BOLD=""; DIM=""; RED=""; GREEN=""; RESET=""
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m'); GREEN=$(printf '\033[32m')
    RESET=$(printf '\033[0m')
fi

say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$BOLD" "$RESET" "$*"; }
note() { printf '%s    %s%s\n' "$DIM" "$*" "$RESET"; }
die()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

has() { command -v "$1" >/dev/null 2>&1; }

# ── Platform ─────────────────────────────────────────────────────────────────
# Not a hard stop. trrnt's VPN enforcement watches for a utun interface, which
# is a macOS thing, so elsewhere the app runs but the kill switch cannot vouch
# for anything. Better to say so than to pretend.

if [ "$(uname -s)" != "Darwin" ]; then
    say "${RED}!${RESET} trrnt is built for macOS. On $(uname -s) the VPN"
    note "kill switch cannot detect a tunnel and downloads will be blocked"
    note "unless you set aria2.bt_interface by hand. Continuing anyway."
fi

# ── Where the package comes from ─────────────────────────────────────────────
# PyPI when it is published there, the git repo otherwise. Checking rather than
# hardcoding means this same script keeps working across that transition
# without anyone having to remember to edit it.

resolve_source() {
    if [ -n "${TRRNT_SOURCE:-}" ]; then
        printf '%s' "$TRRNT_SOURCE"
        return
    fi
    if has curl && curl -fsS -o /dev/null "https://pypi.org/pypi/$PACKAGE/json" 2>/dev/null; then
        printf 'pypi'
    else
        printf 'git'
    fi
}

SOURCE=$(resolve_source)
if [ "$SOURCE" = "pypi" ]; then
    SPEC="$PACKAGE"
    note_source="PyPI"
else
    SPEC="git+https://github.com/$REPO"
    [ -n "${TRRNT_REF:-}" ] && SPEC="$SPEC@$TRRNT_REF"
    note_source="GitHub ($REPO)"
fi

# ── An installer that handles isolation and PATH ─────────────────────────────

step "Installing trrnt from $note_source"

if ! has uv && ! has pipx; then
    note "no uv or pipx found — installing uv first (astral.sh, no sudo)"
    curl -LsSf https://astral.sh/uv/install.sh | sh \
        || die "could not install uv. Install it yourself and re-run:
    curl -LsSf https://astral.sh/uv/install.sh | sh"
    # uv lands in ~/.local/bin, which this shell has not looked at yet.
    PATH="$HOME/.local/bin:$PATH"
    export PATH
fi

if has uv; then
    note "using uv"
    # --force so re-running this script upgrades an existing install rather
    # than failing on "already installed".
    uv tool install --force "$SPEC" || die "uv could not install trrnt"
    BIN_HINT="$HOME/.local/bin"
else
    note "using pipx"
    pipx install --force "$SPEC" || die "pipx could not install trrnt"
    BIN_HINT="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$HOME/.local/bin")"
fi

# ── Confirm it is actually reachable ─────────────────────────────────────────
# Installing a command the user's shell cannot find is the classic way this
# goes wrong, and it looks like success from inside the script. Check.

say ""
if has trrnt; then
    step "${GREEN}Installed${RESET} — $(trrnt --version)"
    say ""
    say "  Run ${BOLD}trrnt${RESET} to start. The first launch opens a setup wizard:"
    note "installs Jackett and aria2, starts them, writes your config."
elif [ -x "$BIN_HINT/trrnt" ]; then
    step "${GREEN}Installed${RESET} — $("$BIN_HINT/trrnt" --version)"
    say ""
    say "  ${BOLD}$BIN_HINT is not on your PATH.${RESET} Add it:"
    say ""
    say "    echo 'export PATH=\"$BIN_HINT:\$PATH\"' >> ~/.zshrc && exec zsh"
    say ""
    note "then run: trrnt"
else
    die "trrnt was installed but the binary is not where expected ($BIN_HINT).
Try: uv tool list"
fi
