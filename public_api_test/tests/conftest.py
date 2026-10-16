"""Offline by default, including collection-time imports."""
import socket
import time
import pytest
import dotenv
import requests

_guard = None


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", help="Explicitly enable marked live tests")


def pytest_configure(config):
    global _guard
    config.addinivalue_line("markers", "live: opt-in real Public API request")
    if config.getoption("--run-live"):
        return
    _guard = pytest.MonkeyPatch()
    for key in ("PUBLIC_API_KEY", "PUBLIC_ACCESS_TOKEN", "PUBLIC_ACCOUNT_ID"):
        _guard.delenv(key, raising=False)
    _guard.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    def blocked(*args, **kwargs):
        raise AssertionError("Offline pytest prohibits real network I/O; mock HTTP or opt in with --run-live")
    _guard.setattr(requests.adapters.HTTPAdapter, "send", blocked)
    _guard.setattr(socket.socket, "connect", blocked)
    _guard.setattr(socket.socket, "connect_ex", blocked)
    def no_wait(seconds):
        if seconds > 0:
            raise AssertionError("Offline tests must use a simulated clock, not real sleeps")
    _guard.setattr(time, "sleep", no_wait)


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        for item in items:
            if item.get_closest_marker("live"):
                item.add_marker(pytest.mark.skip(reason="Requires explicit --run-live"))


def pytest_unconfigure(config):
    if _guard is not None:
        _guard.undo()
