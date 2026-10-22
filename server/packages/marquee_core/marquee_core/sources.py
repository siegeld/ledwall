"""Resolving a layer `src` to something the renderer can open.

Supported forms:
    /abs/path.png                 a file
    plex:Home Movies/ski.mp4      a path under a configured Plex library root
    shuffle:Home Movies           every video in a library, to be played at random
    youtube:<id or url>           resolved via yt-dlp to a direct stream URL
    cam:front-door                a named camera from the registry (RTSP)
    wowza:lobby                   a named Wowza application/stream
    rtsp:// rtmp:// http(s)://    passed straight to ffmpeg (HLS, MJPEG, …)

Plex gets first-class treatment because that is where this estate's home movies
already live: mediarbr1 mounts //example.com/dfs/Home Movies at /share/homemovies
and //example.com/dfs/Movies at /share/movies. Referring to them by library name
keeps a show portable if a mount point changes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field

DEFAULT_LIBRARIES = {
    "home movies": "/share/homemovies",
    "movies": "/share/movies",
}


class SourceError(RuntimeError):
    pass


@dataclass
class Resolver:
    libraries: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LIBRARIES))
    media_root: str = "/data/media"
    cache: dict[str, str] = field(default_factory=dict)
    # Live sources are registered centrally, by name.
    #
    # Camera and Wowza URLs carry credentials and change when a camera is
    # replaced. Putting them in the registry rather than in scene documents
    # means a show is safe to share, review and version, and a re-addressed
    # camera is one settings edit instead of a search across every show.
    cameras: dict[str, str] = field(default_factory=dict)
    wowza_base: str = ""          # e.g. rtsp://wowza.example.com:1935/live
    live_schemes: tuple = ("rtsp://", "rtmp://", "rtmps://", "srt://", "udp://")

    def resolve(self, src: str) -> str:
        if src.startswith("cam:"):
            return self._camera(src[4:])
        if src.startswith("wowza:"):
            return self._wowza(src[6:])
        if src.startswith(self.live_schemes):
            return src
        if src.startswith("plex:"):
            return self._plex(src[5:])
        if src.startswith("youtube:"):
            return self._youtube(src[8:])
        if src.startswith(("http://", "https://")):
            return src
        if os.path.isabs(src):
            if not os.path.exists(src):
                raise SourceError(f"no such file: {src}")
            return src
        # Relative paths are resolved under the managed media root, so a show
        # cannot reach arbitrary host files by writing '../../etc/passwd'.
        p = os.path.normpath(os.path.join(self.media_root, src))
        if not p.startswith(os.path.normpath(self.media_root) + os.sep):
            raise SourceError(f"path escapes media root: {src}")
        if not os.path.exists(p):
            raise SourceError(f"no such file: {p}")
        return p

    def _plex(self, rest: str) -> str:
        lib, _, path = rest.partition("/")
        root = self.libraries.get(lib.strip().lower())
        if not root:
            known = ", ".join(sorted(self.libraries)) or "none configured"
            raise SourceError(f"unknown plex library {lib!r} (known: {known})")
        p = os.path.normpath(os.path.join(root, path))
        if not p.startswith(os.path.normpath(root) + os.sep):
            raise SourceError("path escapes the library root")
        if not os.path.exists(p):
            raise SourceError(f"not in library: {rest}")
        return p

    # What counts as a film. Deliberately a fixed list rather than "anything
    # ffmpeg might open": a library shared over DFS contains sidecar files,
    # thumbnails and replication bookkeeping, and handing one of those to
    # ffmpeg produces a black scene rather than an error anyone would notice.
    VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mpg", ".mpeg", ".wmv")

    def pool(self, ref: str) -> list[str]:
        """Every video under a library (or a folder in one), sorted.

        Sorted, not shuffled: the caller owns the randomness, so the same
        library always yields the same list and a scene is reproducible when
        someone reports "it played X and then went black".

        Skips the bookkeeping that a DFS-replicated share carries --
        `DfsrPrivate`, `__DFSR_DIAGNOSTICS_TEST_FOLDER__`, dotfiles and
        AppleDouble `._` stubs. /share/homemovies has several of those next to
        the real folders, and a picker that can choose one is a picker that
        occasionally shows nothing.
        """
        lib, _, sub = ref.partition("/")
        root = self.libraries.get(lib.strip().lower())
        if not root:
            known = ", ".join(sorted(self.libraries)) or "none configured"
            raise SourceError(f"unknown library {lib!r} (known: {known})")
        base = os.path.normpath(os.path.join(root, sub))
        rootn = os.path.normpath(root)
        if base != rootn and not base.startswith(rootn + os.sep):
            raise SourceError("path escapes the library root")
        if not os.path.isdir(base):
            raise SourceError(f"not a folder in the library: {ref}")

        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith((".", "__")) and d != "DfsrPrivate"]
            for fn in filenames:
                if fn.startswith((".", "._")):
                    continue
                if fn.lower().endswith(self.VIDEO_EXTS):
                    out.append(os.path.join(dirpath, fn))
        if not out:
            raise SourceError(f"no videos in {ref}")
        return sorted(out)

    def _youtube(self, ref: str) -> str:
        """Resolve to a direct media URL with yt-dlp.

        Cached: resolution costs a network round trip and the URLs are stable
        for hours, so a scene that loops must not re-resolve every pass.
        """
        if ref in self.cache:
            return self.cache[ref]
        exe = shutil.which("yt-dlp")
        if not exe:
            raise SourceError("yt-dlp not installed in this image")
        url = ref if ref.startswith("http") else f"https://www.youtube.com/watch?v={ref}"
        try:
            out = subprocess.run(
                [exe, "-f", "best[height<=480]/best", "-g", url],
                capture_output=True, text=True, timeout=60, check=True,
            )
        except subprocess.CalledProcessError as e:
            raise SourceError(f"yt-dlp failed: {e.stderr.strip()[:200]}") from e
        except subprocess.TimeoutExpired as e:
            raise SourceError("yt-dlp timed out") from e
        direct = out.stdout.strip().splitlines()[0]
        self.cache[ref] = direct
        return direct

    def _camera(self, name: str) -> str:
        url = self.cameras.get(name.strip().lower())
        if not url:
            known = ", ".join(sorted(self.cameras)) or "none registered"
            raise SourceError(f"unknown camera {name!r} (known: {known})")
        return url

    def _wowza(self, stream: str) -> str:
        if not self.wowza_base:
            raise SourceError("no wowza_base configured")
        return f"{self.wowza_base.rstrip('/')}/{stream.strip('/')}"
