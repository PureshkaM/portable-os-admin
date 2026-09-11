"""UC-7 "Выдача кода восстановления доступа"."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...errors import MediaNotRegisteredError
from ...recovery import RecoveryService
from ...recovery.request import KIND_MACHINES, RecoveryRequest
from .. import theme
from ..widgets import MessageBar, add_field, error_guard, section_card, section_header


class RecoveryFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        section_header(
            self,
            "Восстановление",
            "Код выдаётся по запросу пользователя, когда пароль забыт.",
        )

        card = section_card(self, grow=False)

        form = ttk.Frame(card)
        form.pack(anchor="w")
        self.request_var = tk.StringVar()
        add_field(
            form,
            0,
            "Запрос от пользователя:",
            self.request_var,
            hint="три группы по четыре знака, например CSJ0-J20E-VEC3",
            width=30,
        )

        actions = ttk.Frame(card)
        actions.pack(fill="x", pady=theme.SPACE_SM)
        ttk.Button(actions, text="Выдать код", command=self._issue).pack(side="left")
        ttk.Button(actions, text="Назад", command=self.app.show_dashboard).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

    def _issue(self) -> None:
        request_text = self.request_var.get().strip()
        if not request_text:
            self.message_bar.error("Введите запрос восстановления.")
            return

        # Первое нажатие называет владельца, второе выдаёт код.
        #
        # До 11.09.2026 код уходил по первому нажатию, и проверялись ровно три
        # вещи: разбирается ли запрос, есть ли запись, свободен ли слот. О том,
        # кто звонит, не спрашивалось ничего -- а звонить может любой, кто нашёл
        # чужую флешку: носитель показывает код запроса сам, стоит трижды
        # ошибиться паролем. Выдача кода тогда обходит и LUKS, и привязку к
        # машинам, и длину фразы -- телефонным звонком.
        #
        # Здесь не «подтвердите действие» -- такое подтверждение нажимают не
        # глядя. Здесь называется имя владельца, которого администратор должен
        # услышать от звонящего, то есть шаг несёт сведения, а не вопрос.
        # Полностью дыру это не закрывает: опознание процедурное и остаётся на
        # администраторе. Но спросить ему теперь есть что.
        if getattr(self, "_confirmed_request", "") != request_text:
            with error_guard(self.message_bar):
                request = RecoveryRequest.parse(request_text)
                record = self.app.session.container.find_media(request.media_id)
                if record is None:
                    raise MediaNotRegisteredError(
                        f"сведения о переносной системе не найдены (media_id={request.media_id})"
                    )
                self._confirmed_request = request_text
                owner = record.owner.strip()
                whose = (
                    f"Владелец: {owner}."
                    if owner
                    else "Владелец не указан — это носитель, выданный до того, как поле появилось."
                )
                self.message_bar.info(
                    f"Носитель «{record.display_name}». {whose} "
                    "Убедитесь, что говорите именно с ним, и нажмите «Выдать код» ещё раз."
                )
            return

        service = RecoveryService(self.app.session)
        with error_guard(self.message_bar):
            # Вид запроса разбирается отдельно и до выдачи: администратор
            # должен видеть, что у него просят. Забытый пароль и исчерпанный
            # носитель -- разные события с разными последствиями: во втором
            # случае носитель забудет все машины и наберёт их заново, и
            # сказать об этом надо до того, как код уйдёт пользователю.
            request = RecoveryRequest.parse(request_text)
            code = service.issue_code(request_text)
            self.app.save_container()
            # Выдача кода -- единственное действие администратора, которое
            # отдаёт наружу доступ к носителю, и единственное, о котором потом
            # спрашивают «а кто это выдал». Записывается носитель и владелец,
            # но не сам код: код открывает носитель, а журнал лежит на диске
            # открытым текстом.
            from .. import app_journal

            record = self.app.session.container.find_media(request.media_id)
            app_journal.record(
                app_journal.RECOVERY_CODE_ISSUED,
                details=f"носитель {request.media_id}, владелец: "
                + ((record.owner or "не указан") if record else "запись не найдена"),
            )
            if request.kind == KIND_MACHINES:
                self.message_bar.success(
                    f"Носитель {request.media_id} исчерпал список машин. "
                    f"Код сброса: {code} — передайте его пользователю. "
                    "После сброса носитель забудет все машины и начнёт набирать их заново."
                )
            else:
                self.message_bar.success(
                    f"Пользователь носителя {request.media_id} забыл пароль. "
                    f"Код восстановления: {code} — передайте его пользователю."
                )
