"""UC-7 "Выдача кода восстановления доступа"."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...recovery import RecoveryService
from ..widgets import MessageBar, add_field, error_guard


class RecoveryFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Выдача кода восстановления доступа", font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 8)
        )

        form = ttk.Frame(self)
        form.pack(anchor="w", padx=16)
        self.request_var = tk.StringVar()
        add_field(
            form,
            0,
            "Запрос восстановления:",
            self.request_var,
            hint="формат: media_id:слот:контрольная_сумма",
            width=60,
        )

        actions = ttk.Frame(self)
        actions.pack(anchor="w", padx=16, pady=8)
        ttk.Button(actions, text="Выдать код", command=self._issue).pack(side="left")
        ttk.Button(actions, text="Назад", command=self.app.show_dashboard).pack(side="left", padx=(8, 0))

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16)

    def _issue(self) -> None:
        request_text = self.request_var.get().strip()
        if not request_text:
            self.message_bar.error("Введите запрос восстановления.")
            return
        service = RecoveryService(self.app.session)
        with error_guard(self.message_bar):
            code = service.issue_code(request_text)
            self.app.save_container()
            self.message_bar.success(f"Код восстановления: {code} — передайте его пользователю.")
