"""The trrnt marks: the wordmark, and the mask the home screen leads with.

One home for the pixels: main.py prints the wordmark into scrollback while
services start, and the TUI renders the same rows once it owns the terminal.
Two copies would drift the moment anyone redraws the logo.

The mask is baked in as data rather than generated. It was traced from a
reference image and hand-tuned, and neither the source nor Pillow belongs in
the package for something that never changes at runtime.
"""

from rich.text import Text

from . import __version__

SPLASH = [
    "█████ ████  ████  █   █ █████",
    "  █   █   █ █   █ ██  █   █  ",
    "  █   ████  ████  █ █ █   █  ",
    "  █   █  █  █  █  █  ██   █  ",
    "  █   █   █ █   █ █   █   █  ",
]

# The violet family, darkest first. Every value is an exact xterm-256 slot, so
# it survives Terminal.app's 256-colour quantisation unchanged — an
# interpolated ramp would land on whatever the cube happens to round to.
# Runs from the selection (60) through the accent (140) to a light tint.
VIOLET_RAMP = ["#5f5f87", "#875faf", "#875fd7", "#af87d7", "#d7afff"]

# The wordmark reads top-to-bottom light-to-dark, so it takes the ramp
# reversed rather than keeping a second copy of the same five values.
SPLASH_COLORS = list(reversed(VIOLET_RAMP))

TAGLINE = "terminal torrent aggregator"
# The home screen leads with the mask instead of the wordmark, so nothing else
# on that screen says the product's name. The line under the mask has to.
HOME_TITLE = f"trrnt — {TAGLINE}"

# Straight from the package, not from installed metadata. The metadata lookup
# keyed on the distribution name, so renaming the distribution turned the home
# screen's version into "dev" — silently, because the fallback is a legitimate
# state when running from a checkout. One source of truth cannot drift.
VERSION = __version__


def splash_text() -> Text:
    """The wordmark with its gradient, as one renderable for a Static."""
    text = Text()
    for i, (line, color) in enumerate(zip(SPLASH, SPLASH_COLORS)):
        if i:
            text.append("\n")
        text.append(line, style=f"bold {color}")
    return text


# ── The mask ─────────────────────────────────────────────────────────────────
# 52 columns x 23 rows, one glyph per cell. Character shading rather than block
# pixels: half blocks give square pixels but every mark is the same solid
# rectangle, which reads as pixel art. A glyph carries shape as well as weight,
# which is what makes this read as woven.
#
# Each row is (glyphs, colour indices) — one digit per column, indexing
# VIOLET_RAMP. Parallel strings rather than tuples of pairs so the art stays
# legible in the source, which is the only place it will ever be edited.
MASCOT_WIDTH = 52

MASCOT: tuple[tuple[str, str], ...] = (
    ('             ·@   ;#X    ##+ : .##.. +@.            ',
     '0000000000000040002430000442010044000240000000000000'),
    ('             +#;  x+#;  =x;X   xxx;  +@;            ',
     '0000000000000242003242003323000333200242000000000000'),
    ('            :·xX ;+;=#  #+;X=  @;:@  #=x            ',
     '0000000000001133022234004223300411400423000000000000'),
    ('            . x= =++=# ++:+=X +x++== #:;            ',
     '0000000000000032022224022122402322220412000000000000'),
    ('          ;.  ;;::  :;:::.·;;;·:;.;x·=+++x          ',
     '0000000000100021110011111012221120131322230000000000'),
    ('          ;··+x++=x+;;==X#=xxxXX=x==+;+:·+          ',
     '0000000000111232223222323433333333322221020000000000'),
    (' .::·    :x==;==+;;;++;+++++++==x=xxXxX#X#+    .·:· ',
     '0011100001322222222222122222222233333334442000001100'),
    ('+x:;++;::·:+;==+XXxxXxx=xxxX#xxxx=xx++=====++=xx+;==',
     '2312222111122222333333333333433333332222232222332132'),
    ('=X+::;;;;·;xXXXXxxXxxxxxxxXXxxXXxxX#XXXxX=+xxx=;·=XX',
     '2321112211233333333333333333333333343333332333221244'),
    ('.:=xx=;;::xX=+==++++++++;::;++======x=xXXX+xxx=XXX+.',
     '0123322211333222222222222112222222233334332333333320'),
    ('  .·+++;:+#+ ·;==xx=xxxx·=+:xXxx=xxx==;·X#=+=xxx+·  ',
     '0001222212420122233233331221333333332321342233332000'),
    ('    ·;;:·=X:·=xx=====xxx+XXXXx==x=xxx=X·:#x;=x=:    ',
     '0000111112311333333333332433333333333331143223210000'),
    ('     :;··=x·:+=+xx;+xx=+;#x+=+xX++Xx=xx=:@X;;x+     ',
     '0000011113311222332233321432223322332332143123200000'),
    ('     ·;:·=x.;;;xx · ·X=·=#X++Xx.·.·x==x+·@x;;+:     ',
     '0000011112302213301013312432233010032332143212100000'),
    ('   ;=====;. +;;xx;::=x=:x@@x;xX;::+xx=x= :+=====;.  ',
     '0002222321002213321123213443233111233232012322221000'),
    ('  =#x==+++:.:=:;=xxxx;:xX@@@X;=xxxXx;=X;·====xx=#X  ',
     '0024332222101212233332134444313333331332023223334300'),
    ('  X=x+==+=;..·· :=x=+::::=+;x;;=x==··:+·:x=+=+==xX  ',
     '0033322222200110123221111222322233211121132222233400'),
    ('  =x=;##x;;.·;.+xx====+;:;+xXXxx=xxx;;+·:x;X#x;x=X. ',
     '0023224431201102333233221123333333331120131343233300'),
    ('  ==;:X#x:+ :X·+=::==·.·:;+;··+=+;xx+x@;·=:X#x:==X  ',
     '0023114431201312211220011222112321332341121343123300'),
    ('  ;=:·xX=.:. ;·++.=@#::;;;+;;.=@#:=x;==··:·xX=:==+  ',
     '0022113330100112202441112222202441332231111333123200'),
    ('     ·=x;  ·:·.:+;;@X;=xxXXxx+;@#=x=···;· .=x=      ',
     '0000012320011101222431333333322442321112100233000000'),
    ('     :XX=   .::····=x·:==;+==·;X;:;;;;:.  .x#x      ',
     '0000013430000111011331122223211311122210000343000000'),
    ('      ..       ..:·.·.·=x+=X=.···:·:·      ..       ',
     '0000000000000000011010123223300111111000000000000000'),
)


def mascot_text() -> Text:
    """The mask as one renderable for a Static."""
    text = Text()
    for i, (glyphs, colours) in enumerate(MASCOT):
        if i:
            text.append("\n")
        for ch, idx in zip(glyphs, colours):
            if ch == " ":
                text.append(" ")          # no style on empty cells
            else:
                text.append(ch, style=VIOLET_RAMP[int(idx)])
    return text
