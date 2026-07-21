"""
sidecars.py
~~~~~~~~~~~
Pure filename-parsing helpers for sidecar detection: external subtitles,
extended artwork, trailers, samples, extras folders, and multi-episode files.

No I/O happens in this module – every function works on plain strings so the
logic stays cheap to unit-test.
"""

import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# External subtitles
# ---------------------------------------------------------------------------

SUBTITLE_EXTENSIONS: frozenset = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"})

_SUBTITLE_CODEC_BY_EXT = {
    ".srt": "srt",
    ".ass": "ass",
    ".ssa": "ssa",
    ".vtt": "vtt",
    ".sub": "vobsub",
    ".idx": "vobsub",
}

_FORCED_TOKENS = {"forced"}
_SDH_TOKENS = {"sdh", "cc", "hi"}

# ISO-639 2/3-letter code, optionally with a region tag (pt-BR, en-US, zh-Hant).
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,4})?$")


@dataclass(frozen=True)
class SubtitleSidecar:
    """One external subtitle file belonging to a video file."""
    filename: str     # actual file name on the share
    language: str     # ISO code, lowercased (e.g. 'en', 'eng', 'pt-br')
    forced: bool
    sdh: bool
    codec: str        # 'srt', 'ass', 'ssa', 'vtt', 'vobsub'


def parse_subtitle_filename(video_stem: str, filename: str) -> Optional[SubtitleSidecar]:
    """Parse *filename* as an external subtitle for *video_stem*.

    Handles ``<stem>.en.srt``, ``<stem>.en.forced.srt``, ``<stem>.eng.sdh.ass``
    and region-tagged variants like ``<stem>.pt-BR.srt``.

    Returns ``None`` when the file is not a subtitle for this video, when the
    name carries tokens we do not understand, or when no language token is
    present (language-less files are skipped to avoid 'und' noise in the DB).
    """
    stem, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in SUBTITLE_EXTENSIONS:
        return None

    vs = video_stem.lower()
    stem_lower = stem.lower()
    if not stem_lower.startswith(vs):
        return None

    tail = stem[len(video_stem):]
    if not tail:
        return None  # '<stem>.srt' with no language token

    tokens = [t for t in re.split(r"[.\s_]+", tail) if t]
    language = ""
    forced = False
    sdh = False
    for token in tokens:
        low = token.lower()
        if low in _FORCED_TOKENS:
            forced = True
        elif low in _SDH_TOKENS:
            sdh = True
        elif _LANG_RE.match(low) and not language:
            language = low
        else:
            return None  # unrecognized token -> not a plain subtitle sidecar

    if not language:
        return None
    return SubtitleSidecar(
        filename=filename,
        language=language,
        forced=forced,
        sdh=sdh,
        codec=_SUBTITLE_CODEC_BY_EXT[ext],
    )


def dedupe_vobsub_pairs(subtitles: list) -> list:
    """Collapse VobSub ``.idx``/``.sub`` pairs into a single entry.

    A ``.sub`` file is dropped when an ``.idx`` entry with the same stem
    already exists (the pair is one logical subtitle stream).
    """
    idx_stems = {
        os.path.splitext(s.filename)[0].lower()
        for s in subtitles
        if s.filename.lower().endswith(".idx")
    }
    result = []
    for sub in subtitles:
        stem = os.path.splitext(sub.filename)[0].lower()
        if sub.filename.lower().endswith(".sub") and stem in idx_stems:
            continue
        result.append(sub)
    return result


# ---------------------------------------------------------------------------
# Artwork
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS: frozenset = frozenset({".jpg", ".jpeg", ".png", ".tbn"})

_SEASON_ART_RE = re.compile(
    r"^season[\s.\-_]*(?P<num>\d{1,2}|all|specials)[\s.\-_]+"
    r"(?P<kind>poster|fanart|banner|landscape|clearlogo|clearart|thumb)$",
    re.IGNORECASE,
)

# Folder-level artwork names -> Kodi art type.
# A bare 'thumb.jpg' at folder level is a landscape image by Kodi convention.
_FOLDER_ART_TYPES = {
    "poster": "poster",
    "folder": "poster",
    "fanart": "fanart",
    "backdrop": "fanart",
    "banner": "banner",
    "landscape": "landscape",
    "thumb": "landscape",
    "logo": "clearlogo",
    "clearlogo": "clearlogo",
    "clearart": "clearart",
    "characterart": "characterart",
    "disc": "discart",
    "discart": "discart",
}

# Stem-prefixed per-video artwork ('<video-stem>-<suffix>').
# '<stem>-thumb.jpg' at video level is a genuine thumb (episode thumbs).
_STEM_ART_TYPES = dict(_FOLDER_ART_TYPES)
_STEM_ART_TYPES["thumb"] = "thumb"


@dataclass(frozen=True)
class ArtSidecar:
    """One artwork file and its Kodi art classification."""
    filename: str
    art_type: str                     # poster/fanart/banner/landscape/clearlogo/clearart/characterart/discart/thumb
    season: Optional[int] = None      # None = not season art; -1 = all seasons; 0 = specials


def classify_art_file(filename: str, video_stem: Optional[str] = None) -> Optional[ArtSidecar]:
    """Classify *filename* as Kodi artwork.

    - Season art: ``season01-poster.jpg``, ``season-all-poster.jpg``,
      ``season-specials-poster.jpg`` (and banner/fanart/landscape/clearlogo/thumb).
    - Stem-prefixed (Radarr/TMM style): ``<stem>-poster.jpg``, ``<stem>-clearlogo.png``,
      ``<stem>-thumb.jpg``, ``<stem>.tbn``.
    - Folder-level: ``poster.jpg``, ``fanart.jpg``, ``banner.jpg``, ``logo.png``,
      ``disc.png`` etc.

    Returns ``None`` for files that are not recognizable artwork.
    """
    stem, ext = os.path.splitext(filename)
    ext = ext.lower()
    if ext not in IMAGE_EXTENSIONS:
        return None
    lower = stem.lower()

    season_match = _SEASON_ART_RE.match(lower)
    if season_match:
        num = season_match.group("num").lower()
        season = -1 if num == "all" else 0 if num == "specials" else int(num)
        kind = season_match.group("kind").lower()
        art_type = "landscape" if kind == "thumb" else _FOLDER_ART_TYPES.get(kind, kind)
        return ArtSidecar(filename=filename, art_type=art_type, season=season)

    if video_stem:
        vs = video_stem.lower()
        if lower == vs and ext == ".tbn":
            return ArtSidecar(filename=filename, art_type="thumb")
        prefix = vs + "-"
        if lower.startswith(prefix):
            suffix = lower[len(prefix):]
            art_type = _STEM_ART_TYPES.get(suffix)
            if art_type:
                return ArtSidecar(filename=filename, art_type=art_type)
        # Fall through: folder-level artwork (logo.png, landscape.jpg, ...)
        # lives next to the video and must still be classified.

    art_type = _FOLDER_ART_TYPES.get(lower)
    if art_type:
        return ArtSidecar(filename=filename, art_type=art_type)
    return None


# ---------------------------------------------------------------------------
# Trailers / samples
# ---------------------------------------------------------------------------

_TRAILER_STEM_RE = re.compile(r"^(?:.*[\s.\-_])?trailer\d*$", re.IGNORECASE)
_SAMPLE_STEM_RE = re.compile(r"^(?:.*[\s.\-_])?sample$", re.IGNORECASE)


def is_trailer_file(filename: str) -> bool:
    """True for ``<name>-trailer.mkv`` / ``trailer.mp4`` style files."""
    stem = os.path.splitext(filename)[0]
    return bool(_TRAILER_STEM_RE.match(stem))


def is_sample_file(filename: str) -> bool:
    """True for ``<name>-sample.mkv`` / ``sample.mkv`` style files."""
    stem = os.path.splitext(filename)[0]
    return bool(_SAMPLE_STEM_RE.match(stem))


# ---------------------------------------------------------------------------
# Multi-episode files
# ---------------------------------------------------------------------------

_EPISODE_INDEX_RE = re.compile(r"[Ss](\d{1,3})((?:[Ee]\d{1,4})+)")


def parse_episode_indices(filename: str) -> Optional[Tuple[int, list]]:
    """Extract ``(season, [episode numbers])`` from an episode filename.

    ``Show.S01E01E02.mkv`` -> ``(1, [1, 2])``; returns ``None`` when the name
    carries no SxxEyy pattern.
    """
    stem = os.path.splitext(filename)[0]
    match = _EPISODE_INDEX_RE.search(stem)
    if not match:
        return None
    season = int(match.group(1))
    episodes = [int(e) for e in re.findall(r"[Ee](\d{1,4})", match.group(2))]
    if not episodes:
        return None
    return season, episodes


# ---------------------------------------------------------------------------
# Extras folders (Kodi Video Versions / Extras feature)
# ---------------------------------------------------------------------------

# Folder name (lowercased) -> extras type name registered in videoversiontype.
# Kodi generates these names from the folder via GenerateVideoExtra and stores
# them with owner=AUTO; we mirror that behaviour.
EXTRAS_FOLDER_TYPE_NAMES = {
    "extras": "Other",
    "extra": "Other",
    "behind the scenes": "Behind The Scenes",
    "deleted scenes": "Deleted Scenes",
    "deleted scene": "Deleted Scenes",
    "featurettes": "Featurettes",
    "featurette": "Featurettes",
    "interviews": "Interviews",
    "interview": "Interviews",
    "scenes": "Scenes",
    "shorts": "Shorts",
    "trailers": "Trailer",
}


def extras_folder_type(dir_name: str) -> Optional[str]:
    """Return the extras type name for a directory name, or ``None``."""
    return EXTRAS_FOLDER_TYPE_NAMES.get(dir_name.strip().lower())
