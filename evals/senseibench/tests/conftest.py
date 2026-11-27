"""SenseiBench definition tests: offline always (no key, no network), and nothing written outside tmp_path."""

import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """No live model call can happen in these tests: any connection attempt fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("a SenseiBench test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
