"""pytest-only: tests that point $PROVISION_CONFIG_ROOT at a temp config root must not leak it
into the next test (each `make ci` script run is its own process, so this only matters under
`python3 -m pytest tests/`)."""
import os

import pytest


@pytest.fixture(autouse=True)
def _restore_config_root():
    prev = os.environ.get("PROVISION_CONFIG_ROOT")
    yield
    if prev is None:
        os.environ.pop("PROVISION_CONFIG_ROOT", None)
    else:
        os.environ["PROVISION_CONFIG_ROOT"] = prev
