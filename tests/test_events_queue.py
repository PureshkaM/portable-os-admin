"""Э20: очередь событий на носителе."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime"
_spec = importlib.util.spec_from_file_location("poa_events", RUNTIME / "poa_events.py")
events = importlib.util.module_from_spec(_spec)
sys.modules["poa_events"] = events
_spec.loader.exec_module(events)


def test_events_are_numbered_and_kept_until_acknowledged(tmp_path):
    queue = events.Queue(str(tmp_path))
    assert [queue.append("E01", {"slot": "A"}), queue.append("D02"), queue.append("E02")] == [1, 2, 3]
    queue.ack(2)
    assert [e["seq"] for e in queue.pending()] == [3]
    assert queue.append("X01") == 4, "номер не переиспользуется после очистки"


def test_overflow_drops_informational_first_and_keeps_a_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "LIMIT_BYTES", 300)
    monkeypatch.setattr(events, "RESERVE", 300)
    queue = events.Queue(str(tmp_path))
    while queue.append("E01") is not None:
        pass
    assert queue.append("C03") is not None, "критическое должно лечь в резерв"
    assert (tmp_path / "overflow").exists()


def test_cli_writes_params(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "QUEUE_DIR", str(tmp_path))
    monkeypatch.setattr(events.Queue.__init__, "__defaults__", (str(tmp_path),))
    assert events.main(["D01", "vid=1234", "pid=abcd"]) == 0
    assert events.Queue(str(tmp_path)).pending()[0]["p"] == {"vid": "1234", "pid": "abcd"}
