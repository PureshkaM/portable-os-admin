"""Admin session: the logical context created after a container is opened.

Implements "Требования к безопасности-1": every administrative function must
go through a session object obtained from an opened container, and
"Требования к безопасности-10": closing ends the session and scrubs its key.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .container import AdminContainer
from .errors import SessionClosedError


class AdminSession:
    def __init__(self, container: AdminContainer):
        self.container = container
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._closed = False

    @property
    def is_open(self) -> bool:
        return not self._closed

    def require_open(self) -> None:
        if self._closed:
            raise SessionClosedError("сеанс администратора завершён")

    def close(self) -> None:
        if self._closed:
            return
        self.container.close()
        self._closed = True
