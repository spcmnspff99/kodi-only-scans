"""
smb_walker.py
~~~~~~~~~~~~~
SMB network directory crawler built on smbprotocol's high-level `smbclient` API.

Usage
-----
    setup_smb_session("nas.local", "myuser", "s3cret")

    for vf in walk_videos("nas.local", "media", "Movies"):
        print(vf.smb_uri, vf.nfo_unc)

    content = read_smb_file("\\\\\\\\nas.local\\\\media\\\\Movies\\\\Alien (1979)\\\\Alien.nfo")
"""

import logging
import os
import re
from dataclasses import dataclass
from typing import Iterator, List, Optional

import smbclient

logger = logging.getLogger(__name__)

# File extensions considered playable video files
VIDEO_EXTENSIONS: frozenset = frozenset({
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".ts", ".iso", ".strm", ".ogm", ".ogv", ".flv",
    ".divx", ".xvid", ".asf", ".rm", ".rmvb",
})


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class VideoFile:
    """Represents a single video file discovered on the SMB share."""
    unc_path: str           # \\server\share\path\to\file.mkv  (used by smbclient)
    smb_uri: str            # smb://server/share/path/to/file.mkv  (stored in Kodi DB)
    directory_uri: str      # smb://server/share/path/to/  (Kodi path table, ends with /)
    filename: str           # file.mkv
    unc_dir: str            # \\server\share\path\to  (smbclient directory operations)
    nfo_unc: Optional[str]  # \\server\share\path\to\file.nfo, or None if absent
    poster_unc: Optional[str]  # \\server\share\path\to\poster.jpg, or None if absent
    fanart_unc: Optional[str]  # \\server\share\path\to\fanart.jpg, or None if absent


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def setup_smb_session(
    server: str,
    username: str,
    password: str,
    port: int = 445,
    encrypt: bool = False,
) -> None:
    """Register (or re-register) an SMB session for *server*.

    Must be called before any ``smbclient`` calls. Safe to call multiple times.
    """
    smbclient.register_session(
        server,
        username=username,
        password=password,
        port=port,
        encrypt=encrypt,
    )
    logger.info("SMB session registered: %s@%s:%s", username, server, port)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_smb_file(unc_path: str, encoding: str = "utf-8") -> Optional[str]:
    """Read a text file from an SMB share and return its contents.

    Returns ``None`` if the file cannot be opened or decoded.
    """
    try:
        with smbclient.open_file(unc_path, mode="r", encoding=encoding, errors="replace") as fh:
            return fh.read()
    except Exception as exc:
        logger.warning("Cannot read SMB file %s: %s", unc_path, exc)
        return None


# ---------------------------------------------------------------------------
# Directory listing helpers
# ---------------------------------------------------------------------------

def list_smb_subdirs(unc_path: str) -> List[str]:
    """Return the names of immediate subdirectories under *unc_path*.

    Returns an empty list on any error.
    """
    try:
        return [e.name for e in smbclient.scandir(unc_path) if e.is_dir(follow_symlinks=False)]
    except Exception as exc:
        logger.warning("Cannot list subdirs of %s: %s", unc_path, exc)
        return []


def list_smb_files(unc_path: str) -> List[str]:
    """Return the names of immediate files under *unc_path*.

    Returns an empty list on any error.
    """
    try:
        return [e.name for e in smbclient.scandir(unc_path) if not e.is_dir(follow_symlinks=False)]
    except Exception as exc:
        logger.warning("Cannot list files of %s: %s", unc_path, exc)
        return []


def smb_dir_exists(unc_path: str) -> bool:
    """Return ``True`` if *unc_path* can be listed as a directory."""
    try:
        list(smbclient.scandir(unc_path))
        return True
    except Exception:
        return False


def resolve_share_path(
    server: str,
    share: str,
    configured_path: str,
    library_type: str,
) -> str:
    """Resolve a configured share path to an existing directory.

    Resolution strategy:
    1. Direct path lookup (as configured).
    2. Segment-by-segment case-insensitive / punctuation-insensitive matching.
    3. Root-level alias guess for common library names.

    Returns the best relative path inside the share. If no match can be found,
    returns the original configured path unchanged.
    """
    rel = configured_path.strip("/\\")
    if not rel:
        return rel

    direct_unc = build_unc(server, share, rel)
    if smb_dir_exists(direct_unc):
        return rel

    # Try segment-by-segment matching in case only case/spaces/underscores differ.
    resolved_segments: List[str] = []
    configured_segments = [p for p in rel.replace("\\", "/").split("/") if p]
    failed_segment: Optional[str] = None
    failed_parent_segments: List[str] = []
    failed_at_index = -1
    for segment in configured_segments:
        parent_rel = "/".join(resolved_segments)
        parent_unc = build_unc(server, share, parent_rel)
        candidates = list_smb_subdirs(parent_unc)
        match = _pick_matching_segment(segment, candidates)
        if not match:
            failed_segment = segment
            failed_parent_segments = list(resolved_segments)
            failed_at_index = len(resolved_segments)
            resolved_segments = []
            break
        resolved_segments.append(match)

    if resolved_segments:
        resolved_rel = "/".join(resolved_segments)
        if smb_dir_exists(build_unc(server, share, resolved_rel)):
            logger.info("Resolved SMB path %r -> %r", configured_path, resolved_rel)
            return resolved_rel

    # Movie-target fallback: if only the final segment fails, attempt a
    # title/year-aware match (e.g. "The Breadwinner (2017)" -> "Breadwinner (2017)").
    if (
        library_type.lower() == "movies"
        and failed_segment
        and configured_segments
        and failed_at_index == len(configured_segments) - 1
    ):
        parent_rel = "/".join(failed_parent_segments)
        parent_unc = build_unc(server, share, parent_rel)
        candidates = list_smb_subdirs(parent_unc)
        fallback = _pick_matching_movie_leaf(failed_segment, candidates)
        if fallback:
            resolved_rel = "/".join(failed_parent_segments + [fallback])
            if smb_dir_exists(build_unc(server, share, resolved_rel)):
                logger.info("Resolved SMB movie leaf %r -> %r", configured_path, resolved_rel)
                return resolved_rel

    # Alias fallback is intentionally conservative and only used for single-segment
    # configured paths to avoid accidentally remapping deep custom structures.
    if len(configured_segments) == 1:
        root_dirs = list_smb_subdirs(build_unc(server, share, ""))
        guessed = _guess_library_root(root_dirs, library_type)
        if guessed:
            logger.info("Guessed SMB %s path %r -> %r", library_type, configured_path, guessed)
            return guessed

    return rel


def _pick_matching_segment(segment: str, candidates: List[str]) -> Optional[str]:
    seg_lower = segment.lower()
    for c in candidates:
        if c.lower() == seg_lower:
            return c

    seg_norm = _normalize_name(segment)
    normalized_matches = [c for c in candidates if _normalize_name(c) == seg_norm]
    if len(normalized_matches) == 1:
        return normalized_matches[0]
    return None


def _pick_matching_movie_leaf(segment: str, candidates: List[str]) -> Optional[str]:
    """Best-effort movie folder matcher for final path segment mismatches."""
    seg_title, seg_year = _normalize_movie_folder_key(segment)
    if not seg_title:
        return None

    scored: List[tuple[int, str]] = []
    for candidate in candidates:
        cand_title, cand_year = _normalize_movie_folder_key(candidate)
        if not cand_title:
            continue

        score = 0
        if cand_title == seg_title:
            score += 4
        elif cand_title in seg_title or seg_title in cand_title:
            score += 2
        else:
            continue

        if seg_year and cand_year:
            if seg_year == cand_year:
                score += 3
            else:
                # Strong title match but conflicting year is likely wrong.
                continue
        elif seg_year or cand_year:
            score += 1

        scored.append((score, candidate))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score = scored[0][0]
    best_names = sorted([name for score, name in scored if score == best_score])
    if len(best_names) == 1:
        return best_names[0]
    return None


def _normalize_movie_folder_key(name: str) -> tuple[str, str]:
    text = name.strip().lower()
    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    year = year_match.group(0) if year_match else ""

    # Remove bracketed year and normalize common article placement variants.
    title = re.sub(r"\((19|20)\d{2}\)", "", text)
    title = re.sub(r",\s*(the|a|an)$", "", title)
    title = re.sub(r"^(the|a|an)\s+", "", title)
    return _normalize_name(title), year


def _guess_library_root(root_dirs: List[str], library_type: str) -> Optional[str]:
    if not root_dirs:
        return None

    aliases = {
        "movies": ["movies", "movie", "films", "film"],
        "tv": ["tv", "tvshow", "tvshows", "shows", "series", "television"],
    }.get(library_type.lower(), [])
    if not aliases:
        return None

    alias_norms = {_normalize_name(a) for a in aliases}
    scored: List[tuple[int, str]] = []

    for name in root_dirs:
        norm = _normalize_name(name)
        score = 0
        if norm in alias_norms:
            score = 3
        elif any(alias in norm for alias in alias_norms):
            score = 2
        if score:
            scored.append((score, name))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score = scored[0][0]
    best_names = [name for score, name in scored if score == best_score]
    if len(best_names) == 1:
        return best_names[0]
    return None


def _normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

def walk_videos(server: str, share: str, base_path: str) -> Iterator[VideoFile]:
    """Recursively walk *base_path* on the SMB share and yield one
    :class:`VideoFile` per video file found.

    A sibling ``.nfo`` file (same stem, ``.nfo`` extension) is included if
    present; otherwise ``nfo_unc`` is ``None``.

    Args:
        server:    Hostname or IP address of the SMB server.
        share:     Share name (e.g. ``"media"``).
        base_path: Path within the share (e.g. ``"Movies"`` or ``"TV/Breaking Bad"``).
    """
    rel = base_path.strip("/\\").replace("/", "\\")
    unc_root = f"\\\\{server}\\{share}\\{rel}" if rel else f"\\\\{server}\\{share}"
    yield from _walk(server, share, unc_root)


def _walk(server: str, share: str, unc_dir: str) -> Iterator[VideoFile]:
    try:
        entries = list(smbclient.scandir(unc_dir))
    except Exception as exc:
        logger.warning("Cannot scandir %s: %s", unc_dir, exc)
        return

    file_names = set()
    dir_names = []

    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            dir_names.append(entry.name)
        else:
            file_names.add(entry.name)

    # Build a case-insensitive lookup once for predictable art sidecar matching.
    lower_file_names = {name.lower(): name for name in file_names}

    # Yield video files in this directory
    for fname in file_names:
        ext = os.path.splitext(fname)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            continue

        stem = os.path.splitext(fname)[0]
        nfo_unc = None
        nfo_candidates = [name for name in file_names if name.lower().endswith(".nfo")]
        stem_nfo_name = stem + ".nfo"
        if stem_nfo_name in file_names:
            nfo_unc = f"{unc_dir}\\{stem_nfo_name}"
        elif len(nfo_candidates) == 1:
            nfo_unc = f"{unc_dir}\\{nfo_candidates[0]}"
        elif nfo_candidates:
            preferred_nfo = sorted(
                nfo_candidates,
                key=lambda name: 0 if os.path.splitext(name)[0].lower() == stem.lower() else 1,
            )[0]
            nfo_unc = f"{unc_dir}\\{preferred_nfo}"

        poster_unc = None
        for art_name in ("poster.jpg", "poster.jpeg", "folder.jpg", "folder.jpeg"):
            matched = lower_file_names.get(art_name)
            if matched:
                poster_unc = f"{unc_dir}\\{matched}"
                break

        fanart_unc = None
        for art_name in ("fanart.jpg", "fanart.jpeg", "backdrop.jpg", "backdrop.jpeg"):
            matched = lower_file_names.get(art_name)
            if matched:
                fanart_unc = f"{unc_dir}\\{matched}"
                break

        dir_smb_uri = _unc_to_smb_dir_uri(server, share, unc_dir)

        yield VideoFile(
            unc_path=f"{unc_dir}\\{fname}",
            smb_uri=f"{dir_smb_uri.rstrip('/')}/{fname}",
            directory_uri=dir_smb_uri,
            filename=fname,
            unc_dir=unc_dir,
            nfo_unc=nfo_unc,
            poster_unc=poster_unc,
            fanart_unc=fanart_unc,
        )

    # Recurse
    for dname in dir_names:
        yield from _walk(server, share, f"{unc_dir}\\{dname}")


# ---------------------------------------------------------------------------
# Path conversion helper
# ---------------------------------------------------------------------------

def _unc_to_smb_dir_uri(server: str, share: str, unc_dir: str) -> str:
    """Convert a UNC directory path to a Kodi-style smb:// directory URI.

    ``\\\\server\\share\\Movies\\Alien (1979)``
    -> ``smb://server/share/Movies/Alien (1979)/``
    """
    prefix = f"\\\\{server}\\{share}"
    rel = unc_dir[len(prefix):].lstrip("\\").replace("\\", "/")
    if rel:
        return f"smb://{server}/{share}/{rel}/"
    return f"smb://{server}/{share}/"


def build_smb_file_uri(server: str, share: str, path: str) -> str:
    """Convert a share-relative path to a Kodi-style smb:// file URI."""
    clean_path = path.strip("/")
    return f"smb://{server}/{share}/{clean_path}" if clean_path else f"smb://{server}/{share}"

def build_unc(server: str, share: str, path: str) -> str:
    """Build a UNC path from server, share, and a relative path string."""
    rel = path.strip("/\\").replace("/", "\\")
    return f"\\\\{server}\\{share}\\{rel}" if rel else f"\\\\{server}\\{share}"


def build_smb_dir_uri(server: str, share: str, path: str) -> str:
    """Build an smb:// directory URI (always ends with ``/``)."""
    rel = path.strip("/\\").replace("\\", "/")
    if rel:
        return f"smb://{server}/{share}/{rel}/"
    return f"smb://{server}/{share}/"
