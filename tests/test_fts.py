"""The server's protocol tests (server/tests/fts/test_fts.py), run with this repo's client/fts.py.

Pytest collects the tests and fixtures imported here; conftest.py makes
`fts` resolve to client/fts.py, which test_client_is_this_repos checks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import fts

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("fts_protocol_tests", ROOT / "server/tests/fts/test_fts.py")
_tests = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _tests
_spec.loader.exec_module(_tests)
from fts_protocol_tests import *  # noqa: E402,F403
from fts_protocol_tests import Link, adafruit  # noqa: E402,F401  (used by test_watch_fs, live_adafruit_interop)


def test_client_is_this_repos() -> None:
    assert Path(fts.__file__).resolve() == ROOT / "client" / "fts.py"
    assert _tests.fts is fts
    # The server's suite carries a copy of this client; they must not drift.
    assert (ROOT / "server/tests/fts/fts.py").read_bytes() == (ROOT / "client/fts.py").read_bytes()
