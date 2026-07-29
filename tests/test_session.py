import pytest

from portable_os_admin.container import ContainerManager
from portable_os_admin.errors import SessionClosedError
from portable_os_admin.session import AdminSession


def test_session_open_then_closed(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    session = AdminSession(container)
    assert session.is_open
    session.require_open()  # should not raise

    session.close()
    assert not session.is_open
    with pytest.raises(SessionClosedError):
        session.require_open()


def test_session_close_is_idempotent(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    session = AdminSession(container)
    session.close()
    session.close()  # must not raise
