"""Plumbing for the archived v1 history import tests (issue 347).

The integration test here needs the fixtures (`dsn`, `conn`, `payload`, `refdata`), the `integration` marker and the helper modules
that live in tests/integration. That conftest cannot be a parent of this folder any more, so load it once under another module name
and re-export what the tests use. Nothing in tests/integration is changed or copied."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_INTEGRATION = Path(__file__).resolve().parents[2] / "integration"
if str(_INTEGRATION) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION))

_spec = importlib.util.spec_from_file_location("_integration_conftest", _INTEGRATION / "conftest.py")
assert _spec is not None and _spec.loader is not None
_ic = importlib.util.module_from_spec(_spec)
sys.modules["_integration_conftest"] = _ic
_spec.loader.exec_module(_ic)

dsn = _ic.dsn
conn = _ic.conn
payload = _ic.payload
refdata = _ic.refdata
pytest_configure = _ic.pytest_configure
pytest_collection_modifyitems = _ic.pytest_collection_modifyitems
pytest_runtest_logreport = _ic.pytest_runtest_logreport
pytest_collectreport = _ic.pytest_collectreport
pytest_sessionfinish = _ic.pytest_sessionfinish
