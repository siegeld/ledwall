from pathlib import Path

_DEFAULT = "0.0.0"


def get_version() -> str:
    """Version from the VERSION file, mounted at /app/VERSION in the image.

    Falls back to walking up from this file for local (non-container) runs, but
    tolerantly: a short path must not raise, or the app cannot import at all.
    """
    candidates = [Path("/app/VERSION")]
    here = Path(__file__).resolve()
    candidates += [p / "VERSION" for p in here.parents]
    for p in candidates:
        try:
            v = p.read_text().strip()
            if v:
                return v
        except OSError:
            continue
    return _DEFAULT
