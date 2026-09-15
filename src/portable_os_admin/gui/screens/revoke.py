"""Отзыв носителя: учёт для потерянного, стирание для вернувшегося.

Два действия на одном экране, потому что администратор приходит сюда с одним
вопросом -- «сотрудник уволился, что делать с его флешкой» -- и ответ зависит
от того, лежит она перед ним или нет. Разводить это по разным разделам значило
бы заставить выбирать раздел до того, как вопрос задан.

Главное здесь -- не кнопки, а то, что между ними написано. Отметка отзыва
ничего не запирает: носитель, которого нет у администратора, остаётся ровно
таким же, каким был, и «отозвать» здесь означает снять с учёта, а не закрыть
доступ. Кнопка, которая молча делает вид, что заперла носитель, опаснее
отсутствия кнопки -- по ней человек решит, что дело закрыто, и не станет ни
менять пароли, ни считать носитель утекшим.

Это тот случай, когда подпись несёт неочевидное, и потому она здесь есть,
вопреки общему правилу не объяснять очевидного: без неё разница между двумя
действиями не видна вовсе -- обе кнопки выглядят одинаково решительно.
"""
from __future__ import annotations

import datetime
import tkinter as tk
from tkinter import ttk

from ...admin import media_detect, media_revoke, revocation_publish
from .. import theme
from ..widgets import (
    Column,
    MessageBar,
    add_field,
    data_table,
    error_guard,
    publish_media_state,
    section_card,
    section_header,
    run_in_background,
    widget_alive,
)
from .device_step import DANGER_COLOR, is_root

NOT_ROOT_MESSAGE = (
    "Стирание требует прав root: заголовок LUKS правит cryptsetup, обычному "
    "пользователю это недоступно. Пометить запись отозванной можно и без них."
)


def contact_text(status) -> str:
    """Строка о последней связи носителя с ресурсом отзыва.

    Время сервер хранит в UTC; показывается местное -- администратор сверяет
    его со своими часами и с тем, когда носитель видели в последний раз.
    """
    if not status.last_seen:
        return "Последняя связь: не выходил на связь"
    try:
        moment = datetime.datetime.fromisoformat(status.last_seen)
        when = moment.astimezone().strftime("%d.%m.%Y %H:%M")
    except ValueError:
        when = status.last_seen
    return (
        f"Последняя связь: {when}, версия {status.version or '—'}, "
        f"машина {status.machine or '—'}"
    )


class RevokeFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app, media_id: str = ""):
        super().__init__(parent, style="Window.TFrame")
        self.app = app
        self.media_id = media_id
        self._erasing = False

        record = app.session.container.find_media(media_id) if media_id else None
        self.record = record

        section_header(
            self,
            f"Отзыв носителя: {record.display_name}" if record else "Отзыв носителя",
            (
                "Отметка отзыва уходит на сервер: носитель, выйдя в сеть, получит "
                "«отозван» и уничтожит себя. Без связи с сервером он продолжает "
                "работать, пока не выйдет на связь."
                if revocation_publish.is_connected(app.session.container)
                else "Отметка отзыва — это учёт: сервер отзыва не подключён, и "
                "отметка не запирает носитель и ничего на нём не меняет. Запирает "
                "только стирание, и для него носитель должен быть в руках у "
                "администратора."
            ),
        )

        card = section_card(self)

        if record is None:
            bar = MessageBar(card, on_card=True)
            bar.pack(fill="x")
            bar.error("Носитель не найден в контейнере.")
            ttk.Button(card, text="Назад", command=app.show_dashboard).pack(
                anchor="w", pady=(theme.SPACE_SM, 0)
            )
            return

        self.status_var = tk.StringVar()
        ttk.Label(card, textvariable=self.status_var, font=theme.FONT_SECTION).pack(anchor="w")
        # Последняя связь -- только когда ресурс отзыва подключён: без него
        # спрашивать некого, и строка «не узнать» была бы шумом.
        self.contact_var = tk.StringVar()
        self._server_connected = revocation_publish.is_connected(app.session.container)
        if self._server_connected:
            ttk.Label(card, textvariable=self.contact_var).pack(
                anchor="w", pady=(theme.SPACE_XS, 0)
            )

        # -- носитель не вернулся: только учёт ------------------------------
        ttk.Separator(card).pack(fill="x", pady=theme.SPACE_MD)
        ttk.Label(card, text="Носитель не вернулся", font=theme.FONT_SECTION).pack(anchor="w")
        ttk.Label(
            card,
            text=(
                "Потерян, украден или не сдан. Отметка уходит на сервер отзыва: "
                "выйдя в сеть, носитель уничтожит себя, а приложения в нём без "
                "подтверждения сервера не запустятся. Тот, кто открывает том в "
                "обход носителя, этим не останавливается — закройте его учётную "
                "запись и доступ в компании."
                if self._server_connected
                else "Потерян, украден или не сдан. Сервер отзыва не подключён, и "
                "программа до носителя не дотянется: пометка снимает его с учёта, но "
                "не закрывает доступ к данным. От чужой машины носитель защищает "
                "привязка к машинам, а не эта отметка."
            ),
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))

        form = ttk.Frame(card)
        form.pack(anchor="w")
        self.reason_var = tk.StringVar(value=record.revoked_reason)
        add_field(
            form,
            0,
            "Причина отзыва:",
            self.reason_var,
            hint="например: сотрудник уволен 11.09.2026",
            width=40,
        )

        mark_row = ttk.Frame(card)
        mark_row.pack(anchor="w", pady=(theme.SPACE_XS, 0))
        self.mark_btn = ttk.Button(mark_row, text="Пометить отозванным", command=self._mark)
        self.mark_btn.pack(side="left")
        self.restore_btn = ttk.Button(mark_row, text="Снять отметку", command=self._restore)
        self.restore_btn.pack(side="left", padx=(theme.SPACE_SM, 0))

        # -- носитель вернулся: настоящий отзыв -----------------------------
        ttk.Separator(card).pack(fill="x", pady=theme.SPACE_MD)
        ttk.Label(card, text="Носитель вернулся", font=theme.FONT_SECTION).pack(anchor="w")
        ttk.Label(
            card,
            text="Заголовки LUKS всех томов носителя будут уничтожены вместе с "
            "мастер-ключом. После этого данные не открыть ничем — ни паролем "
            "пользователя, ни служебным ключом из контейнера, ни кодом "
            "восстановления. Запись станет отозванной автоматически.",
            foreground=DANGER_COLOR,
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))

        erase_form = ttk.Frame(card)
        erase_form.pack(anchor="w")
        # Подставляется то, что программа увидела сама: носитель узнаётся в
        # порту по UUID своих томов, без пароля и монтирования. Подставленное
        # всё равно придётся повторить руками ниже -- подсказка не заменяет
        # подтверждения.
        found = media_detect.detect_map([record]).get(record.media_id)
        self.device_var = tk.StringVar(value=found.device_path if found else "")
        add_field(
            erase_form,
            0,
            "Устройство носителя:",
            self.device_var,
            hint=(
                "узнан в порту по UUID томов" if found else "например /dev/sdb"
            ),
            width=32,
        )
        self.confirm_var = tk.StringVar()
        confirm_entry, _ = add_field(
            erase_form,
            2,
            "Повторите путь к устройству:",
            self.confirm_var,
            hint="это последняя защита от стирания не того носителя",
            width=32,
        )

        self.erase_btn = ttk.Button(
            card,
            text="Стереть заголовки LUKS",
            command=self._erase,
            state="disabled",
        )
        self.erase_btn.pack(anchor="w", pady=(theme.SPACE_SM, 0))
        # Недоступна, пока путь не повторён верно -- как на шаге записи
        # носителя, и по той же причине: ошибка в устройстве здесь стоит
        # чужих данных, а не сообщения об ошибке.
        self.confirm_var.trace_add("write", self._sync_erase_button)
        self.device_var.trace_add("write", self._sync_erase_button)

        # -- что уже происходило --------------------------------------------
        ttk.Separator(card).pack(fill="x", pady=theme.SPACE_MD)
        ttk.Label(card, text="Что происходило с этой записью", font=theme.FONT_SECTION).pack(
            anchor="w"
        )
        self.journal_table, self.journal_placeholder = data_table(
            card,
            (
                Column("at", "Когда", 130, 110),
                Column("what", "Что произошло", 200, 180, stretch=True),
                Column("details", "Причина", 220, 120),
            ),
            height=5,
            expand=False,
            pady=(theme.SPACE_SM, 0),
            empty_text="Отзыв этого носителя пока не трогали.",
        )

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x", pady=(theme.SPACE_SM, 0))

        ttk.Button(card, text="Назад", command=app.show_dashboard).pack(
            anchor="w", pady=(theme.SPACE_SM, 0)
        )

        self._refresh()
        if self._server_connected:
            self._ask_server()

    # -- последняя связь ----------------------------------------------------
    def _ask_server(self) -> None:
        """Спрашивает ресурс отзыва в фоне: сеть не должна держать экран."""
        container = self.app.session.container
        media_id = self.media_id

        def work():
            return revocation_publish.query_record(container, media_id)

        run_in_background(self, work, on_success=self._show_contact,
                          on_error=self._contact_failed)

    def _show_contact(self, status) -> None:
        if status is not None:
            self.contact_var.set(contact_text(status))

    def _contact_failed(self, exc: BaseException) -> None:
        self.contact_var.set(f"Последняя связь: не узнать — {exc}")

    # -- учёт ---------------------------------------------------------------
    def _mark(self) -> None:
        reason = self.reason_var.get().strip()
        if not reason:
            # Причина обязательна: через полгода «отозван» без неё не отвечает
            # ни на один вопрос, ради которого в этот список смотрят.
            self.message_bar.error("Назовите причину отзыва — она останется в записи.")
            return
        with error_guard(self.message_bar):
            self.app.session.container.revoke_media(self.media_id, reason)
            self.app.save_container()
            self._refresh()
            self.message_bar.success(
                f"Носитель «{self.record.display_name}» больше не числится действующим. "
                "Данные на нём это не закрывает: если он у постороннего, он работает "
                "как работал."
            )
            # Вот это как раз закрывает -- если носитель дойдёт до сети:
            # получив «отозван», он уничтожает заголовок LUKS сам.
            publish_media_state(self, self.media_id)

    def _restore(self) -> None:
        with error_guard(self.message_bar):
            self.app.session.container.restore_media(self.media_id)
            self.app.save_container()
            self.reason_var.set("")
            self._refresh()
            self.message_bar.success(
                f"Носитель «{self.record.display_name}» снова числится действующим."
            )
            publish_media_state(self, self.media_id)

    # -- стирание -----------------------------------------------------------
    def _sync_erase_button(self, *_args) -> None:
        """Кнопка стирания доступна ровно тогда, когда повтор пути совпал."""
        button = getattr(self, "erase_btn", None)
        if button is None or not widget_alive(button):
            return
        device = self.device_var.get().strip()
        matches = bool(device) and self.confirm_var.get().strip() == device
        button.configure(state=("normal" if matches and not self._erasing else "disabled"))

    def _erase(self) -> None:
        if self._erasing:
            return
        if not is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)
            return
        device = self.device_var.get().strip()
        confirm = self.confirm_var.get().strip()
        reason = self.reason_var.get().strip()

        self._erasing = True
        self.erase_btn.configure(state="disabled")
        self.message_bar.info(f"Стираю заголовки на {device}…")

        def work():
            return media_revoke.erase_medium(
                self.app.session.container,
                self.media_id,
                device,
                confirm,
                reason=reason,
                dry_run=False,
            )

        run_in_background(self, work, on_success=self._erased, on_error=self._erase_failed)

    def _erased(self, result) -> None:
        self._erasing = False
        # Запись пометил отозванной сам `erase_medium`: стёртый носитель,
        # оставшийся в списке действующим, -- худшее из состояний.
        self.app.save_container()
        if not widget_alive(self):
            return
        self.confirm_var.set("")
        self._refresh()
        self.message_bar.success(
            result.describe() + ". Данные на этом носителе больше не восстановить."
        )

    def _erase_failed(self, exc: BaseException) -> None:
        self._erasing = False
        if not widget_alive(self):
            return
        self._sync_erase_button()
        self.message_bar.error(f"{exc} Запись осталась действующей.")

    # -- состояние экрана ----------------------------------------------------
    def _refresh(self) -> None:
        record = self.record
        if record.is_revoked:
            reason = f" — {record.revoked_reason}" if record.revoked_reason else ""
            self.status_var.set(f"Состояние: {record.status_text}{reason}")
        else:
            self.status_var.set("Состояние: действует")
        # Доступно ровно одно из двух: пометить нечего, если уже помечено.
        self.mark_btn.configure(state=("disabled" if record.is_revoked else "normal"))
        self.restore_btn.configure(state=("normal" if record.is_revoked else "disabled"))
        self._sync_erase_button()

        for item in self.journal_table.get_children():
            self.journal_table.delete(item)
        entries = self.app.session.container.journal_for(self.media_id)
        for entry in entries:
            self.journal_table.insert(
                "", "end", values=(entry.at[:19].replace("T", " "), entry.title, entry.details)
            )
        self.journal_placeholder.update(not entries)
