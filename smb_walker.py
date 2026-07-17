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

    # Yield video files in this directory
    for fname in file_names:
        ext = os.path.splitext(fname)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            continue

        stem = os.path.splitext(fname)[0]
        nfo_name = stem + ".nfo"
        nfo_unc = f"{unc_dir}\\{nfo_name}" if nfo_name in file_names else None

        dir_smb_uri = _unc_to_smb_dir_uri(server, share, unc_dir)

        yield VideoFile(
            unc_path=f"{unc_dir}\\{fname}",
            smb_uri=f"{dir_smb_uri.rstrip('/')}/{fname}",
            directory_uri=dir_smb_uri,
            filename=fname,
            unc_dir=unc_dir,
            nfo_unc=nfo_unc,
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
