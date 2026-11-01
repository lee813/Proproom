import pytest

from tests.handover.conftest import ctx, conn, fake_download, home, lib, nas


@pytest.fixture(autouse=True)
def _no_propd(monkeypatch):
    """Tests run operations in-process. A propd listening on the test host must never receive them; the
    forwarding tests (tests/impl/test_shared_models.py) start their own propd and lift this."""
    monkeypatch.setenv("PROP_LOCAL", "1")


@pytest.fixture(autouse=True)
def _idle_host(monkeypatch):
    """Jobs pause when the host is busy (/proc/loadavg). A render running on the test host must not decide whether a
    job test passes; tests that exercise the load rule patch the file themselves, after this."""
    from pathlib import Path
    read_text = Path.read_text

    def fake(self, *args, **kwargs):
        return "0.00 0.00 0.00 1/1 1\n" if str(self) == "/proc/loadavg" else read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake)
