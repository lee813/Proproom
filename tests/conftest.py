import pytest

from tests.handover.conftest import ctx, conn, fake_download, home, lib, nas


@pytest.fixture(autouse=True)
def _no_propd(monkeypatch):
    """Tests run operations in-process. A propd listening on the test host must never receive them; the
    forwarding tests (tests/impl/test_shared_models.py) start their own propd and lift this."""
    monkeypatch.setenv("PROP_LOCAL", "1")
