"""The trrnt wordmark, shared by the pre-TUI splash and the home screen.

One home for the pixels: main.py prints them into scrollback while services
start, and the TUI's HomeScreen renders the same rows once it owns the
terminal. Two copies would drift the moment anyone redraws the logo.
"""

from importlib.metadata import PackageNotFoundError, version

from rich.text import Text

SPLASH = [
    "█████ ████  ████  █   █ █████",
    "  █   █   █ █   █ ██  █   █  ",
    "  █   ████  ████  █ █ █   █  ",
    "  █   █  █  █  █  █  ██   █  ",
    "  █   █   █ █   █ █   █   █  ",
]

# Top-to-bottom gradient in the TUI's violet family. Each value is an exact
# xterm-256 slot, so it survives Terminal.app's 256-colour quantisation
# unchanged — an interpolated ramp would land on whatever the cube rounds to.
# Runs from a light tint through the accent (140) down to the selection (60).
SPLASH_COLORS = ["#d7afff", "#af87d7", "#875fd7", "#875faf", "#5f5f87"]

TAGLINE = "terminal torrent aggregator"

try:
    VERSION = version("torrent-cli")
except PackageNotFoundError:  # running from a checkout without an install
    VERSION = "dev"


def splash_text() -> Text:
    """The wordmark with its gradient, as one renderable for a Static."""
    text = Text()
    for i, (line, color) in enumerate(zip(SPLASH, SPLASH_COLORS)):
        if i:
            text.append("\n")
        text.append(line, style=f"bold {color}")
    return text
