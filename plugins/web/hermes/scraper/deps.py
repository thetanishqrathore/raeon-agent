"""Lazy dependency management for the native scraper.

The parsing stack (``lxml``, ``rank-bm25``) is intentionally NOT a core Hermes
dependency — it is lazy-installed into the active venv the first time the
``hermes`` web backend actually runs, mirroring how ``firecrawl-py`` / ``exa-py``
are handled (see :mod:`tools.lazy_deps`). A cold CLI that never scrapes pays
nothing.
"""

from __future__ import annotations

import importlib.util


def lxml_available() -> bool:
    """Cheap, side-effect-free check used by ``is_available`` / availability gates."""
    return importlib.util.find_spec("lxml") is not None


def bm25_available() -> bool:
    return importlib.util.find_spec("rank_bm25") is not None


def ensure_parse_deps() -> None:
    """Ensure ``lxml`` is importable, lazy-installing via :mod:`tools.lazy_deps`.

    Raises ``ImportError`` (with the install hint surfaced by lazy_deps) if the
    package cannot be made available — callers convert that into a per-URL
    ``error`` result rather than crashing the tool.
    """
    if lxml_available():
        return
    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("search.hermes", prompt=False)
    except ImportError:
        # tools.lazy_deps itself unavailable (standalone use) — fall through and
        # let the import below raise a clear ModuleNotFoundError.
        pass
    except Exception as exc:  # noqa: BLE001 — lazy_deps raises FeatureUnavailable w/ hint
        raise ImportError(str(exc)) from exc

    import lxml  # noqa: F401  — confirm it imports now


def ensure_bm25() -> bool:
    """Best-effort ensure of ``rank-bm25``; returns True if usable.

    BM25 pre-filtering is an optimisation, so we degrade gracefully (fall back to
    positional truncation) rather than raise when it's unavailable.
    """
    if bm25_available():
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure

        _lazy_ensure("search.hermes", prompt=False)
    except Exception:  # noqa: BLE001
        return False
    return bm25_available()
