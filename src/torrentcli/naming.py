"""Release-name parsing and destination recommendation.

Torrent titles arrive as scene strings — site prefixes, dotted separators,
quality tags, group suffixes: "www.Site.com.The.Agency.S02E03.2160p.WEB.SKIZ".
This module recovers what a human filing the download cares about (title,
season/episode, year) and turns it into a clean display name plus a
destination that matches how the library is already laid out: TV as
<root>/<Show>/Season <N>, movies flat in the category root, everything else
in a folder named after the release.

Parsing is heuristic by nature. The results only ever pre-fill an editable
prompt, so a miss costs the user a few keystrokes, not a wrong move.
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParsedRelease:
    """The human-relevant parts of a scene release title."""

    title: str = ""
    season: int | None = None
    episode: int | None = None
    year: int | None = None

    @property
    def is_episodic(self) -> bool:
        return self.season is not None or self.episode is not None


# TLDs that appear in the site-prefix spam glued onto release names. Kept to
# ones actually seen in the wild rather than the full IANA list, because the
# pattern "word.word" also matches dotted titles ("The.Agency") — the TLD
# whitelist is what keeps a title's second word from being eaten as a domain.
_TLDS = (
    "com|net|org|info|io|me|cc|to|se|ag|la|li|lol|ws|vip|pw|xyz|site|club|"
    "life|fun|pro|one|top|st|nz|tf|mx|ph|rs|nl|am|fm|re|gg|is|it|cx|ch|eu|"
    "us|uk|ca|de|tw|biz|tl|mom|torrent"
)

_SITE_PREFIXES = (
    # "[ www.Speed.cx ] - " / "[eztv.re]" — bracketed groups, but only when a
    # dot is inside: "[REC]" is a movie, "[www.arabp2p.net]" is spam.
    re.compile(r"^\[[^\]]*\.[^\]]*\][\s._-]*"),
    # "www.Torrenting.com." / "www.UIndex.org - "
    re.compile(rf"^www[\s._-]+[a-z0-9-]{{1,40}}[\s._-]+(?:{_TLDS})\b[\s._-]*", re.I),
    # "Torrenting.com - " (no www, but an explicit separator after the domain)
    re.compile(rf"^[a-z0-9-]{{1,40}}\.(?:{_TLDS})\s*[-–—:]\s*", re.I),
)

# S02E03, S02E03E04, S2E3
_SEASON_EP = re.compile(r"^S(\d{1,2})[\s.]?E(\d{1,3})(?:[-E]\d{1,3})*$", re.I)
# 2x03
_CROSS_EP = re.compile(r"^(\d{1,2})x(\d{2,3})$", re.I)
# S03 alone — a season pack
_SEASON_ONLY = re.compile(r"^S(\d{1,2})$", re.I)
_YEAR = re.compile(r"^(19\d{2}|20\d{2})$")

# Tokens that mark the end of the title: quality/source/codec/audio tags.
# Anything from the first of these onward is release plumbing, not name.
_TAG_TOKENS = {
    "480p", "576p", "720p", "1080p", "1080i", "2160p", "4320p", "4k", "uhd",
    "web", "web-dl", "webdl", "web-rip", "webrip", "bluray", "blu-ray",
    "bdrip", "brrip", "bdremux", "remux", "hdtv", "dvdrip", "dvd", "hdrip",
    "amzn", "nf", "atvp", "dsnp", "hulu", "max", "hmax", "hbo", "it",
    "itunes", "pcok", "pmtp", "stan", "crav", "cr", "peck",
    "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "xvid", "divx",
    "10bit", "8bit", "hdr", "hdr10", "hdr10+", "dv", "dovi", "sdr",
    "aac", "ac3", "eac3", "dd", "ddp", "dts", "dts-hd", "truehd", "atmos",
    "flac", "mp3", "opus", "ddpa",
    "proper", "repack", "rerip", "real", "extended", "unrated", "theatrical",
    "imax", "internal", "limited", "uncut", "dubbed", "subbed", "multi",
    "dual", "retail", "hybrid", "complete", "series", "season", "collection",
    "duology", "trilogy", "unabridged", "abridged",
}

# Prefix-matched tag shapes that carry trailing digits or group glue:
# "x265-Vyndros", "DDP5", "H", "6CH", "5" "1" (split from 5.1), "DD+7"
_TAG_PATTERNS = (
    re.compile(r"^(x|h)\.?26[45]", re.I),
    re.compile(r"^\d{3,4}p$", re.I),
    re.compile(r"^dd?p?a?[+\d]", re.I),
    re.compile(r"^\d{1,2}ch$", re.I),
    re.compile(r"^hdr10", re.I),
    re.compile(r"^(bluray|blu-ray|webrip|web-dl)", re.I),
)

# Small words stay lowercase mid-title: "House of the Dragon".
_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in",
    "of", "on", "or", "the", "to", "with",
}


def _strip_site_prefix(raw: str) -> str:
    """Remove leading tracker-site spam, repeatedly until none matches."""
    text = raw.strip()
    changed = True
    while changed:
        changed = False
        for pattern in _SITE_PREFIXES:
            stripped = pattern.sub("", text, count=1)
            if stripped != text:
                text = stripped.strip()
                changed = True
    return text


def _is_tag(token: str) -> bool:
    low = token.lower()
    if low in _TAG_TOKENS:
        return True
    return any(p.match(token) for p in _TAG_PATTERNS)


def _smart_case(tokens: list[str], scene: bool) -> str:
    """Title-case scene tokens without wrecking words that carry real casing.

    `scene` marks dot/underscore-separated input, where capitalization is
    machine noise and small words get normalized ("House.Of.The.Dragon" →
    "House of the Dragon"). Space-separated input is something a human
    already cased — only fully lower/upper tokens are touched there.
    """
    out = []
    for i, tok in enumerate(tokens):
        if tok.isupper() and len(tok) <= 3:
            out.append(tok)  # acronym: DTF, IT, F1
        elif tok.isupper() or tok.islower() or (scene and tok.istitle()):
            low = tok.lower()
            out.append(low if (low in _SMALL_WORDS and i > 0) else low.capitalize())
        else:
            out.append(tok)
    return " ".join(out)


def parse_release_name(raw: str) -> ParsedRelease:
    """Extract title / season / episode / year from a release title.

    Also accepts already-clean names ("The Agency s02e03"), which is what
    makes the folder recommendation track the name the user types.
    """
    text = _strip_site_prefix(raw)
    scene = "." in text or "_" in text
    # Parens/brackets only ever wrap years and tag blobs; drop the chars and
    # let token classification sort out the contents.
    text = re.sub(r"[()\[\]{}]", " ", text)
    tokens = [t for t in re.split(r"[.\s_]+", text) if t]

    season: int | None = None
    episode: int | None = None
    year: int | None = None
    first_marker: int | None = None
    last_year_idx: int | None = None
    skip_next = False

    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue

        m = _SEASON_EP.match(tok)
        if m:
            season = season or int(m.group(1))
            episode = episode or int(m.group(2))
            first_marker = first_marker if first_marker is not None else i
            continue
        m = _CROSS_EP.match(tok)
        if m:
            season = season or int(m.group(1))
            episode = episode or int(m.group(2))
            first_marker = first_marker if first_marker is not None else i
            continue
        if tok.lower() == "season" and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            season = season or int(tokens[i + 1])
            first_marker = first_marker if first_marker is not None else i
            skip_next = True
            continue
        m = _SEASON_ONLY.match(tok)
        if m and i > 0:  # a title is never just "S04"; mid-string it's a pack
            season = season or int(m.group(1))
            first_marker = first_marker if first_marker is not None else i
            continue
        # A year is only a year when it isn't the opening token — "1917" and
        # "2012" are titles. The *last* year before the tags wins, so
        # "Blade Runner 2049 2017" keeps 2049 in the title.
        if _YEAR.match(tok) and i > 0:
            year = int(tok)
            last_year_idx = i
            continue
        if _is_tag(tok):
            first_marker = first_marker if first_marker is not None else i
            continue

    cut_candidates = [c for c in (first_marker, last_year_idx) if c is not None]
    cut = min(cut_candidates) if cut_candidates else len(tokens)
    title_tokens = tokens[:cut]

    if not title_tokens:
        # Degenerate input like "S02E03.1080p" — salvage whatever isn't
        # classifiable so the prompt at least has something editable.
        title_tokens = [
            t for t in tokens
            if not (_SEASON_EP.match(t) or _CROSS_EP.match(t)
                    or _SEASON_ONLY.match(t) or _YEAR.match(t) or _is_tag(t))
        ]

    return ParsedRelease(
        title=_smart_case(title_tokens, scene),
        season=season,
        episode=episode,
        year=year,
    )


def suggest_name(parsed: ParsedRelease, category: str = "") -> str:
    """The clean display name a download should be filed under."""
    title = parsed.title
    if not title:
        return ""
    if parsed.season is not None and parsed.episode is not None:
        return f"{title} S{parsed.season:02d}E{parsed.episode:02d}"
    if parsed.episode is not None:
        return f"{title} E{parsed.episode:02d}"
    if parsed.season is not None:
        return f"{title} Season {parsed.season}"
    if category == "movies" and parsed.year:
        return f"{title} ({parsed.year})"
    return title


def _match_existing(root: Path, name: str) -> str | None:
    """An existing folder in `root` that is this name up to case, if any.

    Reusing the library's own spelling is what stops "FROM" from being
    created next to "From".
    """
    if not name:
        return None
    try:
        entries = [e.name for e in root.iterdir() if e.is_dir()]
    except OSError:
        return None
    folded = name.casefold()
    for entry in entries:
        if entry.casefold() == folded:
            return entry
    return None


def recommend_folder(parsed: ParsedRelease, category: str, root: str | Path) -> Path:
    """Where a release named like this belongs, mirroring the library layout.

    TV: <root>/<Show>/Season <N>. Movies: flat in <root> (the file itself
    carries the name). Everything else: <root>/<clean name>.
    """
    root = Path(root)
    if parsed.is_episodic or category == "tv":
        show = _match_existing(root, parsed.title) or parsed.title
        if not show:
            return root
        folder = root / show
        if parsed.season is not None:
            folder = folder / f"Season {parsed.season}"
        return folder
    if category == "movies":
        return root
    name = suggest_name(parsed, category)
    return root / name if name else root
