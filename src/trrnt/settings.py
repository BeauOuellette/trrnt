"""Tunable aria2 settings: the field spec, validation, and how each one applies.

aria2 accepts every option below through ``changeGlobalOption`` and answers
``OK`` — but that OK means three different things. Probed against aria2 1.37:

  ``live``     the running daemon changes behaviour immediately. Only the
               global throttles genuinely do this.
  ``new``      the value becomes the template for *newly added* downloads.
               Torrents already queued keep the copy they were added with.
  ``restart``  fixed at spawn — a bound socket or a process-level switch.
               aria2 stores the new value and still returns OK while nothing
               changes; ``enable-dht`` does not even store it. Needs a restart.

Presenting all three as "saved" is how a settings screen ends up lying to the
user, so every field carries its tier and the screen says what will actually
happen.

Two aria2 semantics are counter-intuitive enough to be worth stating outright,
because guessing gets both backwards:

  ``seed-ratio=0``  seeds *forever* (ignore ratio), not "do not seed".
  ``seed-time=0``   is what actually means "do not seed at all".
"""

import re
from dataclasses import dataclass
from typing import Any, Callable


TIER_LIVE = "live"
TIER_NEW = "new"
TIER_RESTART = "restart"

TIER_NOTE = {
    TIER_LIVE: "applies now",
    TIER_NEW: "applies to new downloads",
    TIER_RESTART: "needs an aria2 restart",
}


class InvalidSetting(ValueError):
    """A field value the user typed that aria2 would reject or misread."""


@dataclass(frozen=True)
class Field:
    """One tunable, and everything the screen and the daemon need to know."""

    key: str                      # config key under the aria2: section
    label: str                    # what the settings screen calls it
    tier: str
    parse: Callable[[str], Any]   # text from the input -> stored value
    hint: str = ""

    def note(self) -> str:
        return TIER_NOTE[self.tier]


# ── Parsers ──────────────────────────────────────────────────────────────────

_RATE_RE = re.compile(r"^\d+(\.\d+)?[KMkm]?$")


def parse_rate(text: str) -> str:
    """A speed limit in aria2's own notation: bare bytes/sec, or K / M suffix.

    Passed through as a string rather than normalised to bytes so that the
    config keeps reading the way the user wrote it ("5M", not "5242880").
    """
    value = (text or "").strip()
    if value == "":
        return "0"
    if not _RATE_RE.match(value):
        raise InvalidSetting(
            f"{text!r} is not a speed — use 0, or a number with an optional "
            "K or M suffix (e.g. 500K, 2M)"
        )
    return value


def parse_count(text: str) -> int:
    """A small positive integer."""
    value = (text or "").strip()
    try:
        n = int(value)
    except ValueError:
        raise InvalidSetting(f"{text!r} is not a whole number") from None
    if not 1 <= n <= 100:
        raise InvalidSetting("must be between 1 and 100")
    return n


def parse_ratio(text: str) -> float:
    """Share ratio. 0 is legal and means seed forever — see module docstring."""
    value = (text or "").strip()
    try:
        ratio = float(value)
    except ValueError:
        raise InvalidSetting(f"{text!r} is not a number") from None
    if ratio < 0:
        raise InvalidSetting("cannot be negative")
    return ratio


def parse_seed_time(text: str) -> str:
    """Minutes to seed. Empty means no time limit; 0 means never seed.

    Kept as a string precisely so that "" and "0" stay distinguishable — as
    floats they would both be falsey, and the two mean opposite things.
    """
    value = (text or "").strip()
    if value == "":
        return ""
    try:
        minutes = float(value)
    except ValueError:
        raise InvalidSetting(f"{text!r} is not a number of minutes") from None
    if minutes < 0:
        raise InvalidSetting("cannot be negative")
    return value


def parse_port(text: str) -> str:
    """A port, a range, or a comma-separated mix of both — aria2's syntax.

    Ports under 1024 need root and are never what a torrent client wants, so
    they are rejected here rather than at aria2's much later startup failure.
    """
    value = (text or "").strip()
    if not value:
        raise InvalidSetting("cannot be empty")
    for part in value.split(","):
        part = part.strip()
        bounds = part.split("-")
        if len(bounds) > 2 or not all(b.strip().isdigit() for b in bounds if b.strip()):
            raise InvalidSetting(f"{part!r} is not a port or a range like 6881-6999")
        numbers = [int(b) for b in bounds]
        if not all(1024 <= n <= 65535 for n in numbers):
            raise InvalidSetting("ports must be between 1024 and 65535")
        if len(numbers) == 2 and numbers[0] > numbers[1]:
            raise InvalidSetting(f"{part!r} runs backwards")
    return value


ENCRYPTION_CHOICES = ("off", "prefer", "require")


def parse_encryption(text: str) -> str:
    value = (text or "").strip().lower()
    if value not in ENCRYPTION_CHOICES:
        raise InvalidSetting(f"must be one of {', '.join(ENCRYPTION_CHOICES)}")
    return value


def parse_bool(text: str) -> bool:
    value = (text or "").strip().lower()
    if value in ("true", "yes", "on", "1"):
        return True
    if value in ("false", "no", "off", "0", ""):
        return False
    raise InvalidSetting(f"{text!r} is not true or false")


# ── The spec ─────────────────────────────────────────────────────────────────

FIELDS: tuple[Field, ...] = (
    Field(
        "max_download_rate", "Max download", TIER_LIVE, parse_rate,
        "0 = unlimited. Accepts 500K, 2M.",
    ),
    Field(
        "max_upload_rate", "Max upload", TIER_LIVE, parse_rate,
        "0 = unlimited. Cap this to keep the rest of your connection usable.",
    ),
    Field(
        "max_concurrent", "Max concurrent downloads", TIER_LIVE, parse_count,
        "Extras queue instead of all crawling at once.",
    ),
    Field(
        "seed_ratio", "Seed ratio", TIER_NEW, parse_ratio,
        "Stop seeding at this ratio. 0 seeds forever.",
    ),
    Field(
        "seed_time", "Seed time (minutes)", TIER_NEW, parse_seed_time,
        "Blank = no time limit. 0 = never seed.",
    ),
    Field(
        "encryption", "Encryption", TIER_NEW, parse_encryption,
        "require refuses unencrypted peers, which shrinks the swarm.",
    ),
    Field(
        "listen_port", "Listen port", TIER_RESTART, parse_port,
        "Pin one port if your VPN forwards one.",
    ),
    Field(
        "enable_lpd", "Local peer discovery", TIER_RESTART, parse_bool,
        "Announces your torrents to the LAN — outside the tunnel.",
    ),
)

FIELDS_BY_KEY = {f.key: f for f in FIELDS}


def parse_all(raw: dict[str, str]) -> dict[str, Any]:
    """Validate a whole screenful, reporting every bad field at once.

    Fixing one error only to be shown the next is a miserable way to fill in a
    form, so errors accumulate rather than raising on the first.
    """
    values: dict[str, Any] = {}
    errors: list[str] = []
    for key, text in raw.items():
        field = FIELDS_BY_KEY.get(key)
        if field is None:
            continue
        try:
            values[key] = field.parse(text)
        except InvalidSetting as e:
            errors.append(f"{field.label}: {e}")
    if errors:
        raise InvalidSetting("; ".join(errors))
    return values


# ── Translation to aria2 options ─────────────────────────────────────────────

def aria2_options(values: dict[str, Any], *, tiers: tuple[str, ...]) -> dict[str, str]:
    """aria2 option names and values for the requested tiers.

    Restart-tier options are deliberately absent from the ``live`` call sites:
    pushing them would get an ``OK`` back and change nothing, which is worse
    than not trying, because it reads as success.
    """
    out: dict[str, str] = {}

    if TIER_LIVE in tiers:
        if "max_download_rate" in values:
            out["max-overall-download-limit"] = str(values["max_download_rate"])
        if "max_upload_rate" in values:
            out["max-overall-upload-limit"] = str(values["max_upload_rate"])
        if "max_concurrent" in values:
            out["max-concurrent-downloads"] = str(values["max_concurrent"])

    if TIER_NEW in tiers:
        if "seed_ratio" in values:
            out["seed-ratio"] = str(values["seed_ratio"])
        seed_time = values.get("seed_time", "")
        if str(seed_time).strip() != "":
            out["seed-time"] = str(seed_time)
        if "encryption" in values:
            out.update(_encryption_options(values["encryption"]))

    if TIER_RESTART in tiers:
        if "listen_port" in values:
            out["listen-port"] = str(values["listen_port"])
            out["dht-listen-port"] = str(values["listen_port"])
        if "enable_lpd" in values:
            out["bt-enable-lpd"] = "true" if values["enable_lpd"] else "false"

    return out


def _encryption_options(mode: str) -> dict[str, str]:
    """MSE/PE level.

    ``--bt-force-encryption`` is only shorthand for the pair below; setting the
    pair directly keeps one knob per concept and makes the "prefer" case — take
    encryption when offered, still talk to legacy peers — expressible at all.
    """
    if mode == "require":
        return {"bt-require-crypto": "true", "bt-min-crypto-level": "arc4"}
    if mode == "prefer":
        return {"bt-require-crypto": "false", "bt-min-crypto-level": "arc4"}
    return {"bt-require-crypto": "false", "bt-min-crypto-level": "plain"}


def changed_tiers(before: dict[str, Any], after: dict[str, Any]) -> set[str]:
    """Which tiers actually have an edit in them.

    Drives the "what happens next" line: there is no reason to tell someone to
    restart the daemon when all they touched was a speed limit.
    """
    tiers = set()
    for field in FIELDS:
        if field.key not in after:
            continue
        if str(before.get(field.key, "")) != str(after[field.key]):
            tiers.add(field.tier)
    return tiers


def describe_seeding(ratio: float, seed_time: str) -> str:
    """Plain English for a pair of settings whose zeroes mean opposite things."""
    if str(seed_time).strip() == "0":
        return "Never seeds — stops as soon as a download completes."
    limits = []
    if float(ratio) > 0:
        limits.append(f"ratio {float(ratio):g}")
    if str(seed_time).strip() not in ("", "0"):
        limits.append(f"{float(seed_time):g} min")
    if not limits:
        return "Seeds forever, until you remove the torrent."
    if len(limits) == 1:
        return f"Seeds until {limits[0]}."
    return f"Seeds until {limits[0]} or {limits[1]}, whichever comes first."
