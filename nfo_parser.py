"""
nfo_parser.py
~~~~~~~~~~~~~
Parse Kodi-compatible .nfo XML files produced by Sonarr / Radarr.

Supported root tags
    <movie>           -> MovieNfo
    <tvshow>          -> TvShowNfo
    <episodedetails>  -> EpisodeNfo
"""

import xml.etree.ElementTree as ET
import re
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MovieNfo:
    title: str = ""
    originaltitle: str = ""
    sorttitle: str = ""
    outline: str = ""
    plot: str = ""
    tagline: str = ""
    year: str = ""
    premiered: str = ""
    mpaa: str = ""
    imdb_id: str = ""
    rating: str = ""
    votes: str = ""
    runtime: str = ""
    genre: List[str] = field(default_factory=list)
    studio: List[str] = field(default_factory=list)
    director: List[str] = field(default_factory=list)
    writer: List[str] = field(default_factory=list)
    country: List[str] = field(default_factory=list)
    thumb: str = ""
    fanart: str = ""


@dataclass
class TvShowNfo:
    title: str = ""
    originaltitle: str = ""
    sorttitle: str = ""
    plot: str = ""
    outline: str = ""
    mpaa: str = ""
    premiered: str = ""
    year: str = ""
    status: str = ""
    tvdb_id: str = ""
    imdb_id: str = ""
    genre: List[str] = field(default_factory=list)
    studio: List[str] = field(default_factory=list)
    thumb: str = ""
    fanart: str = ""


@dataclass
class EpisodeNfo:
    title: str = ""
    originaltitle: str = ""
    outline: str = ""
    plot: str = ""
    season: str = ""
    episode: str = ""
    aired: str = ""
    rating: str = ""
    votes: str = ""
    runtime: str = ""
    tvdb_id: str = ""
    director: List[str] = field(default_factory=list)
    writer: List[str] = field(default_factory=list)
    thumb: str = ""


@dataclass
class MovieGuess:
    title: str = ""
    year: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _text(el: ET.Element, tag: str, default: str = "") -> str:
    """Return the stripped text of *tag* child, or *default*."""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else default


def _texts(el: ET.Element, tag: str) -> List[str]:
    """Return a list of stripped text values for all *tag* children."""
    return [(c.text or "").strip() for c in el.findall(tag) if c.text]


def _parse_root(content: str) -> Optional[ET.Element]:
    """Parse XML content; return root element or None on error."""
    try:
        return ET.fromstring(content.strip())
    except ET.ParseError:
        return None


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------

def parse_movie_nfo(content: str) -> Optional[MovieNfo]:
    """Parse a movie .nfo XML string.  Returns None if not a <movie> document."""
    root = _parse_root(content)
    if root is None or root.tag != "movie":
        return None

    nfo = MovieNfo()
    nfo.title = _text(root, "title")
    nfo.originaltitle = _text(root, "originaltitle")
    nfo.sorttitle = _text(root, "sorttitle")
    nfo.outline = _text(root, "outline")
    nfo.plot = _text(root, "plot")
    nfo.tagline = _text(root, "tagline")
    nfo.year = _text(root, "year")
    nfo.premiered = _text(root, "premiered")
    nfo.mpaa = _text(root, "mpaa")
    nfo.rating = _text(root, "rating")
    nfo.votes = _text(root, "votes")
    nfo.runtime = _text(root, "runtime")

    # Prefer <uniqueid type="imdb"> over legacy <id>
    imdb_el = root.find("uniqueid[@type='imdb']")
    nfo.imdb_id = (imdb_el.text or "").strip() if imdb_el is not None else _text(root, "id")

    nfo.genre = _texts(root, "genre")
    nfo.studio = _texts(root, "studio")
    nfo.director = _texts(root, "director")
    nfo.writer = _texts(root, "credits")
    nfo.country = _texts(root, "country")

    thumb_el = root.find("thumb")
    if thumb_el is not None:
        nfo.thumb = (thumb_el.text or "").strip()

    fanart_el = root.find("fanart/thumb")
    if fanart_el is not None:
        nfo.fanart = (fanart_el.text or "").strip()

    return nfo


def parse_tvshow_nfo(content: str) -> Optional[TvShowNfo]:
    """Parse a tvshow .nfo XML string.  Returns None if not a <tvshow> document."""
    root = _parse_root(content)
    if root is None or root.tag != "tvshow":
        return None

    nfo = TvShowNfo()
    nfo.title = _text(root, "title")
    nfo.originaltitle = _text(root, "originaltitle")
    nfo.sorttitle = _text(root, "sorttitle")
    nfo.plot = _text(root, "plot")
    nfo.outline = _text(root, "outline")
    nfo.mpaa = _text(root, "mpaa")
    nfo.premiered = _text(root, "premiered")
    nfo.year = _text(root, "year")
    nfo.status = _text(root, "status")
    nfo.genre = _texts(root, "genre")
    nfo.studio = _texts(root, "studio")

    tvdb_el = root.find("uniqueid[@type='tvdb']")
    nfo.tvdb_id = (tvdb_el.text or "").strip() if tvdb_el is not None else _text(root, "id")

    imdb_el = root.find("uniqueid[@type='imdb']")
    if imdb_el is not None:
        nfo.imdb_id = (imdb_el.text or "").strip()

    thumb_el = root.find("thumb")
    if thumb_el is not None:
        nfo.thumb = (thumb_el.text or "").strip()

    fanart_el = root.find("fanart/thumb")
    if fanart_el is not None:
        nfo.fanart = (fanart_el.text or "").strip()

    return nfo


def parse_episode_nfo(content: str) -> Optional[EpisodeNfo]:
    """Parse an episode .nfo XML string.  Returns None if not <episodedetails>."""
    root = _parse_root(content)
    if root is None or root.tag != "episodedetails":
        return None

    nfo = EpisodeNfo()
    nfo.title = _text(root, "title")
    nfo.originaltitle = _text(root, "originaltitle")
    nfo.outline = _text(root, "outline")
    nfo.plot = _text(root, "plot")
    nfo.season = _text(root, "season")
    nfo.episode = _text(root, "episode")
    nfo.aired = _text(root, "aired")
    nfo.rating = _text(root, "rating")
    nfo.votes = _text(root, "votes")
    nfo.runtime = _text(root, "runtime")
    nfo.director = _texts(root, "director")
    nfo.writer = _texts(root, "credits")

    tvdb_el = root.find("uniqueid[@type='tvdb']")
    if tvdb_el is not None:
        nfo.tvdb_id = (tvdb_el.text or "").strip()

    thumb_el = root.find("thumb")
    if thumb_el is not None:
        nfo.thumb = (thumb_el.text or "").strip()

    return nfo


def guess_movie_from_filename(filename: str) -> Optional[MovieGuess]:
    """Guess a movie title/year from a filename or folder name."""
    stem = re.sub(r"\.[^.]+$", "", filename).strip()
    match = re.match(r"^(?P<title>.+?) \((?P<year>\d{4})\)", stem)
    if not match:
        return None

    title = match.group("title").replace(".", " ").replace("_", " ").strip()
    year = match.group("year")
    if not title:
        return None
    return MovieGuess(title=title, year=year)
