"""Свойства носителя: всё об одной флешке в одном окне.

Открывается двойным щелчком по строке списка. Так устроены списки везде, где
их делают всерьёз, и причина не в привычке: свойства объекта -- это взгляд на
одну вещь, а не ещё один раздел программы. Уводить ради него с экрана со
списком значит терять место, где человек находился.

До 17.09.2026 действия над носителем стояли рядом кнопок под таблицей:
«Проверить целостность», «Журналы носителя», «Обмен носителя»,
«Переименовать», «Отзыв…». Ряд рос вместе с числом действий, трижды переставал
помещаться в окно и заставлял переводить взгляд и руку от выделенной строки
вниз, чтобы там вспомнить, к чему эти кнопки относятся. Теперь действия
принадлежат объекту: правая кнопка на строке и это окно.

Вкладки добавляются по одной, а не все разом: журналы, обмен и отзыв -- от
двухсот до четырёхсот строк каждый, и переселение их одним заходом оставило бы
приложение неработающим надолго. Пока вкладки нет, действие открывается со
своего экрана из меню строки.
"""
from __future__ import annotations

import datetime
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk

from ...admin import ExchangeManager, MediaActions, media_revoke, revocation_publish
from ...admin.service_volumes import ServiceVolumeError, open_exchange
from ...errors import (
    ExchangeError,
    InsufficientSpaceError,
    LogsCorruptedError,
    LogsUnavailableError,
    PortableOSAdminError,
)
from ...install import integrity
from .. import theme
from ..widgets import (
    Column,
    PropertiesWindow,
    ScrollableArea,
    add_field,
    button_row,
    data_table,
    error_guard,
    facts_table,
    publish_media_state,
    run_in_background,
    widget_alive,
)
from .device_step import DANGER_COLOR, NOT_ROOT_MESSAGE, is_root


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


class MediaPropertiesWindow(PropertiesWindow):
    """Окно свойств одного носителя."""

    def __init__(self, parent: tk.Widget, app, media_id: str):
        self.app = app
        self._actions = MediaActions(app.session)
        self.record = self._actions.find(media_id)
        self.media_id = media_id

        title = self.record.display_name if self.record is not None else media_id
        # Шире и выше умолчания: во вкладке «Целостность» лежит список
        # расхождений, а в «Сведениях» -- дюжина строк. В окне поменьше
        # подвал с кнопкой «Закрыть» уходил за нижний край.
        super().__init__(parent, f"Носитель: {title}", width=820, height=700)

        if self.record is None:
            page = self.tab("Сведения")
            ttk.Label(
                page,
                text="Запись о носителе не найдена в контейнере.",
                style="Card.TLabel",
            ).pack(anchor="w")
            return

        #: Открытый том раздела обмена. Живёт, пока открыта вкладка «Обмен»:
        #: открытие LUKS считает функцию вывода ключа, и делать это на каждую
        #: операцию значило бы подвешивать окно на секунды.
        self._volume = None
        self.manager = ExchangeManager()
        #: Идёт ли стирание заголовков: пока идёт, кнопка недоступна.
        self._erasing = False

        # Порядок вкладок -- от «что это» к «что с ним сделать», и самое
        # разрушительное последним: отзыв уничтожает ключи безвозвратно.
        self._build_facts()
        self._build_integrity()
        self._build_logs()
        self._build_exchange()
        self._build_revoke()

        # Том закрывается вместе с окном, и это не мелочь: окно свойств
        # закрывают и кнопкой, и Esc, а оставленный открытым том держит
        # носитель занятым устройством /dev/mapper -- флешку нельзя извлечь, и
        # причина этого снаружи ничем себя не выдаёт.
        self.bind("<Destroy>", self._on_destroy)

    # -- вкладка «Сведения» -------------------------------------------------
    def _build_facts(self) -> None:
        page = self.tab("Сведения")
        record = self.record

        # Подключён ли носитель -- спрашивается один раз при открытии окна, а
        # не по таймеру: окно показывает состояние на момент, когда его
        # открыли, и мигающая строка «подключён/не подключён» под рукой
        # администратора только мешала бы читать остальное.
        found = self._actions.connected([record]).get(record.media_id)

        # `fill="both"` и растяжение: без них вкладка отдаёт содержимому ровно
        # столько ширины, сколько оно запросило, и кнопка ниже отрисовывалась
        # пустым прямоугольником -- подпись не помещалась (найдено снимком
        # 17.09.2026).
        self._facts_host = ttk.Frame(page, style="Card.TFrame")
        self._facts_host.pack(fill="both", expand=True, anchor="w")
        self._render_facts(found)

        ttk.Separator(page).pack(fill="x", pady=theme.SPACE_MD)

        ttk.Label(
            page,
            text="Имя носителя живёт в контейнере, а не на флешке: его можно дать "
            "уже выданному, потерянному и даже сломанному.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))
        ttk.Button(page, text="Переименовать…", command=self._rename).pack(anchor="w")

    def _render_facts(self, found) -> None:
        for child in self._facts_host.winfo_children():
            child.destroy()
        record = self.record
        rows = [
            ("Имя", record.display_name),
            ("Владелец", record.owner),
            ("Состояние", record.status_text),
            ("Подключён", found.device_path if found else "нет"),
            ("Идентификатор", record.media_id),
            ("Система", f"{record.target_os}"),
            ("Конфигурация", record.config_name),
            ("Создан", record.created_at),
        ]
        if record.is_revoked:
            rows.append(("Отозван", record.revoked_at))
            rows.append(("Причина отзыва", record.revoked_reason))
        # Опись загрузчика -- не факт «да/нет», а ответ на вопрос, можно ли
        # вообще проверить целостность этого носителя.
        rows.append(
            (
                "Опись загрузчика",
                f"снята, файлов: {len(record.boot_manifest)}"
                if record.boot_manifest
                else "нет — носитель записан версией, которая её не снимала",
            )
        )
        facts_table(self._facts_host, tuple(rows))

    def _rename(self) -> None:
        chosen = simpledialog.askstring(
            "Имя носителя",
            "Как называть этот носитель? Например: «Иванов, бухгалтерия».\n"
            "Имя хранится в контейнере администратора и на носитель не пишется.",
            initialvalue=self.record.name or self.record.label,
            parent=self,
        )
        if chosen is None:
            return
        try:
            self.record = self._actions.rename(self.media_id, chosen)
        except PortableOSAdminError as exc:
            self.message_bar.error(str(exc))
            return
        self.title(f"Носитель: {self.record.display_name}")
        self._render_facts(self._actions.connected([self.record]).get(self.media_id))
        self.message_bar.success(f"Теперь называется «{self.record.display_name}».")

    # -- вкладка «Целостность» ----------------------------------------------
    def _build_integrity(self) -> None:
        """Проверка незашифрованной части носителя.

        Это обнаружение, а не защита: подменённый initramfs спрашивает пароль
        так же, как настоящий, и если человек уже загрузился с такого
        носителя, пароль украден до всякой проверки. Помешать может только
        Secure Boot со своим ключом или TPM, а их у носителя, который несут на
        чужую машину, нет по условию задачи.
        """
        page = self.tab("Целостность")
        ttk.Label(
            page,
            text="Суммы файлов на ESP и /boot сверяются с описью из контейнера. "
            "Носитель ищется в портах по UUID своих томов, а не по пути "
            "устройства: та же флешка на этой машине /dev/sdb, на соседней "
            "/dev/sdc.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w")

        self.check_btn = ttk.Button(page, text="Проверить", command=self._check)
        self.check_btn.pack(anchor="w", pady=(theme.SPACE_SM, theme.SPACE_SM))

        self.integrity_verdict = tk.StringVar(
            value="Проверка не запускалась. Это обнаружение, а не защита: "
            "подменённый initramfs спрашивает пароль так же, как настоящий."
        )
        ttk.Label(
            page,
            textvariable=self.integrity_verdict,
            style="Card.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w")

        # Список расхождений появляется вместе с расхождениями.
        #
        # Пустое поле на пол-вкладки до первой проверки не несёт ничего и
        # выглядит как место, где что-то потерялось (снимок 17.09.2026).
        # Поэтому оно создаётся, но не показывается, пока сравнение не прошло.
        self.details = tk.Text(page, height=10, state="disabled")

    def _check(self) -> None:
        self.check_btn.configure(state="disabled")
        self.message_bar.info("Ищу носитель среди подключённых…")
        # Монтирование и подсчёт сумм -- операции блокирующие, а носитель может
        # быть медленной флешкой: на потоке интерфейса это замерший экран.
        run_in_background(self, self._compare, on_success=self._show_result, on_error=self._failed)

    def _compare(self) -> "dict[str, list[str]]":
        return self._actions.check_integrity(self.record)

    def _show_result(self, difference: "dict[str, list[str]]") -> None:
        if not widget_alive(self):
            return
        self.check_btn.configure(state="normal")
        text = integrity.describe(difference)
        self.integrity_verdict.set(text)
        if integrity.is_intact(difference):
            self.message_bar.success(text)
        else:
            self.message_bar.error(text)
        self._show_details(difference)

    def _failed(self, exc: BaseException) -> None:
        if not widget_alive(self):
            return
        self.check_btn.configure(state="normal")
        if isinstance(exc, PortableOSAdminError):
            self.message_bar.error(str(exc))
            self.integrity_verdict.set(str(exc))
        else:
            self.message_bar.error(f"Не удалось проверить носитель: {exc}")

    def _show_details(self, difference: "dict[str, list[str]]") -> None:
        """Поимённо, а не числом.

        Администратору важно не «десять отличий», а то, что среди них
        `boot:initramfs-*.img`.
        """
        lines = []
        for title, key in (
            ("Изменено", "changed"),
            ("Появилось лишнего", "added"),
            ("Пропало", "removed"),
        ):
            for path in difference[key]:
                lines.append(f"{title}: {path}")
        if not self.details.winfo_ismapped():
            self.details.pack(fill="both", expand=True, pady=(theme.SPACE_SM, 0))
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n".join(lines) or "Расхождений нет.")
        self.details.configure(state="disabled")


    # -- вкладка «Журналы» --------------------------------------------------
    def _build_revoke(self) -> None:
        """Отзыв носителя: два ответа разной силы.

        Носитель не вернулся -- только учёт: пометка с причиной уходит на
        сервер, и носитель, выйдя в сеть, уничтожит себя сам. Носитель
        вернулся -- настоящий отзыв: заголовки LUKS всех томов уничтожаются, и
        данные не открыть ничем.

        Разницу между ними надо показать рядом, поэтому здесь два блока, а не
        диалог с двумя кнопками: в диалоге её не объяснить.
        """
        # Единственная вкладка с прокруткой, и она здесь по делу.
        #
        # Остальные четыре помещаются целиком, а тут два блока с пояснениями,
        # два поля с повтором пути, кнопка стирания и журнал событий -- в
        # штатном окне кнопка и журнал уходят за нижний край (снимок
        # 17.09.2026). На прежнем экране прокрутка была ровно поэтому, и,
        # перенося вкладку, я её снял, решив, что места хватит. Не хватило.
        host = self.tab("Отзыв")
        scroller = ScrollableArea(host, background=theme.SURFACE)
        scroller.pack(fill="both", expand=True)
        page = scroller.content
        self._server_connected = revocation_publish.is_connected(self.app.session.container)

        self.revoke_status = tk.StringVar()
        ttk.Label(page, textvariable=self.revoke_status, font=theme.FONT_SECTION,
                  style="Card.TLabel").pack(anchor="w")
        # Последняя связь -- только когда ресурс отзыва подключён: без него
        # спрашивать некого, и строка «не узнать» была бы шумом.
        self.contact_var = tk.StringVar()
        if self._server_connected:
            ttk.Label(page, textvariable=self.contact_var, style="Card.TLabel").pack(
                anchor="w", pady=(theme.SPACE_XS, 0)
            )

        ttk.Separator(page).pack(fill="x", pady=theme.SPACE_MD)
        ttk.Label(page, text="Носитель не вернулся", font=theme.FONT_SECTION,
                  style="Card.TLabel").pack(anchor="w")
        ttk.Label(
            page,
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

        form = ttk.Frame(page, style="Card.TFrame")
        form.pack(anchor="w", fill="x")
        self.reason_var = tk.StringVar(value=self.record.revoked_reason)
        add_field(form, 0, "Причина отзыва:", self.reason_var,
                  hint="например: сотрудник уволен 11.09.2026", width=40)

        marks = ttk.Frame(page, style="Card.TFrame")
        marks.pack(anchor="w", pady=(theme.SPACE_XS, 0))
        self.mark_btn = ttk.Button(marks, text="Пометить отозванным", command=self._mark)
        self.mark_btn.pack(side="left")
        self.restore_btn = ttk.Button(marks, text="Снять отметку", command=self._restore)
        self.restore_btn.pack(side="left", padx=(theme.SPACE_SM, 0))

        ttk.Separator(page).pack(fill="x", pady=theme.SPACE_MD)
        ttk.Label(page, text="Носитель вернулся", font=theme.FONT_SECTION,
                  style="Card.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Заголовки LUKS всех томов носителя будут уничтожены вместе с "
            "мастер-ключом. После этого данные не открыть ничем — ни паролем "
            "пользователя, ни служебным ключом из контейнера, ни кодом "
            "восстановления. Запись станет отозванной автоматически.",
            foreground=DANGER_COLOR,
            background=theme.SURFACE,
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))

        erase_form = ttk.Frame(page, style="Card.TFrame")
        erase_form.pack(anchor="w", fill="x")
        # Подставляется то, что программа увидела сама: носитель узнаётся в
        # порту по UUID своих томов. Подставленное всё равно повторяется руками
        # ниже -- подсказка не заменяет подтверждения.
        found = self._actions.connected([self.record]).get(self.media_id)
        self.device_var = tk.StringVar(value=found.device_path if found else "")
        add_field(erase_form, 0, "Устройство носителя:", self.device_var,
                  hint=("узнан в порту по UUID томов" if found else "например /dev/sdb"),
                  width=32)
        self.confirm_var = tk.StringVar()
        add_field(erase_form, 2, "Повторите путь к устройству:", self.confirm_var,
                  hint="это последняя защита от стирания не того носителя", width=32)

        self.erase_btn = ttk.Button(
            page, text="Стереть заголовки LUKS", command=self._erase, state="disabled"
        )
        self.erase_btn.pack(anchor="w", pady=(theme.SPACE_SM, 0))
        # Недоступна, пока путь не повторён верно -- как на шаге записи
        # носителя: ошибка в устройстве здесь стоит чужих данных.
        self.confirm_var.trace_add("write", self._sync_erase_button)
        self.device_var.trace_add("write", self._sync_erase_button)

        ttk.Separator(page).pack(fill="x", pady=theme.SPACE_MD)
        ttk.Label(page, text="Что происходило с этой записью", font=theme.FONT_SECTION,
                  style="Card.TLabel").pack(anchor="w")
        self.journal_table, self.journal_placeholder = data_table(
            page,
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

        self._refresh_revoke()
        if self._server_connected:
            self._ask_server()

    def _ask_server(self) -> None:
        """Спрашивает ресурс отзыва в фоне: сеть не должна держать окно."""
        container = self.app.session.container
        media_id = self.media_id
        run_in_background(
            self,
            lambda: revocation_publish.query_record(container, media_id),
            on_success=lambda status: (
                self.contact_var.set(contact_text(status)) if status is not None else None
            ),
            on_error=lambda exc: self.contact_var.set(f"Последняя связь: не узнать — {exc}"),
        )

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
            self._refresh_revoke()
            self._render_facts(self._actions.connected([self.record]).get(self.media_id))
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
            self._refresh_revoke()
            self._render_facts(self._actions.connected([self.record]).get(self.media_id))
            self.message_bar.success(
                f"Носитель «{self.record.display_name}» снова числится действующим."
            )
            publish_media_state(self, self.media_id)

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
        container = self.app.session.container
        media_id = self.media_id
        run_in_background(
            self,
            lambda: media_revoke.erase_medium(
                container, media_id, device, confirm, reason=reason, dry_run=False
            ),
            on_success=self._erased,
            on_error=self._erase_failed,
        )

    def _erased(self, result) -> None:
        self._erasing = False
        # Запись пометил отозванной сам `erase_medium`: стёртый носитель,
        # оставшийся в списке действующим, -- худшее из состояний.
        self.app.save_container()
        if not widget_alive(self):
            return
        self.confirm_var.set("")
        self._refresh_revoke()
        self._render_facts(self._actions.connected([self.record]).get(self.media_id))
        self.message_bar.success(
            result.describe() + ". Данные на этом носителе больше не восстановить."
        )

    def _erase_failed(self, exc: BaseException) -> None:
        self._erasing = False
        if not widget_alive(self):
            return
        self._sync_erase_button()
        self.message_bar.error(f"{exc} Запись осталась действующей.")

    def _refresh_revoke(self) -> None:
        record = self.record
        if record.is_revoked:
            reason = f" — {record.revoked_reason}" if record.revoked_reason else ""
            self.revoke_status.set(f"Состояние: {record.status_text}{reason}")
        else:
            self.revoke_status.set("Состояние: действует")
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

    def _build_logs(self) -> None:
        """Журналы носителя -- ключом из контейнера, без пароля пользователя.

        Том журналов зашифрован своим ключом, который лежит в контейнере
        администратора и больше нигде: пользователь переносной системы своих
        журналов изменить не может, а администратор читает их, не зная его
        парольной фразы.

        Выбора носителя здесь нет: окно открыто на конкретной флешке. Прежний
        экран журналов первым делом спрашивал, чьи журналы читать, -- при том
        что ответ был дан выделением строки.
        """
        page = self.tab("Журналы")

        top = ttk.Frame(page, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="Фильтр:", style="Card.TLabel").pack(side="left")
        self.log_filter = tk.StringVar()
        ttk.Entry(top, textvariable=self.log_filter, width=28).pack(
            side="left", padx=(theme.SPACE_SM, theme.SPACE_MD)
        )
        self.load_logs_btn = ttk.Button(top, text="Загрузить", command=self._load_logs)
        self.load_logs_btn.pack(side="left")
        ttk.Button(top, text="Сохранить в файл…", command=self._save_logs).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

        ttk.Label(
            page,
            text="Том журналов открывается ключом из контейнера: пароль пользователя "
            "не нужен, а сам пользователь свои журналы изменить не может.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_SM, 0))

        # Поле журнала появляется вместе с журналом: пустой белый прямоугольник
        # на всю вкладку до первой загрузки ничего не сообщает (снимок
        # 17.09.2026, та же болезнь, что была на вкладке целостности).
        self.log_view = tk.Text(page, height=16, state="disabled")

    def _load_logs(self) -> None:
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")
        filter_text = self.log_filter.get().strip() or None
        self.load_logs_btn.configure(state="disabled")
        self.message_bar.info("Открывается том журналов…")
        # Открытие тома и чтение блокируют поток, а носитель может быть
        # медленной флешкой: на потоке интерфейса это замершее окно.
        run_in_background(
            self,
            lambda: self._actions.read_logs(self.record, filter_text),
            on_success=self._show_logs,
            on_error=self._logs_failed,
        )

    def _show_logs(self, lines) -> None:
        if not widget_alive(self):
            return
        self.load_logs_btn.configure(state="normal")
        if not self.log_view.winfo_ismapped():
            self.log_view.pack(fill="both", expand=True, pady=(theme.SPACE_SM, 0))
        self.log_view.configure(state="normal")
        for line in lines:
            self.log_view.insert("end", line + "\n")
        self.log_view.configure(state="disabled")
        self.message_bar.success(f"Прочитано строк: {len(lines)}. Том закрыт.")

    def _logs_failed(self, exc: BaseException) -> None:
        if not widget_alive(self):
            return
        self.load_logs_btn.configure(state="normal")
        if isinstance(exc, (ServiceVolumeError, LogsUnavailableError, LogsCorruptedError)):
            self.message_bar.error(str(exc))
        else:
            self.message_bar.error(f"Не удалось прочитать журналы: {exc}")

    def _save_logs(self) -> None:
        """Журнал нужен не только на экране: его прикладывают к заявке.

        Пересказывать его словами или снимать с экрана -- худший из способов.
        """
        lines = self.log_view.get("1.0", "end").strip()
        if not lines:
            self.message_bar.error("Нечего сохранять: журнал не загружен.")
            return
        chosen = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить журнал",
            initialfile=f"journal-{self.media_id}.txt",
            defaultextension=".txt",
        )
        if not chosen:
            return
        try:
            with open(chosen, "w", encoding="utf-8") as handle:
                handle.write(lines + "\n")
        except OSError as exc:
            self.message_bar.error(f"Не удалось сохранить: {exc}")
            return
        self.message_bar.success(f"Журнал сохранён: {chosen}")

    # -- вкладка «Обмен» ----------------------------------------------------
    def _build_exchange(self) -> None:
        """Раздел обмена -- папка, общая с пользователем носителя.

        Том подключается явно, а не на каждую операцию: открытие LUKS считает
        функцию вывода ключа, и это заметное время.
        """
        page = self.tab("Обмен")

        top = ttk.Frame(page, style="Card.TFrame")
        top.pack(fill="x")
        self.connect_btn = ttk.Button(top, text="Подключить", command=self._connect_exchange)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(
            top, text="Отключить", command=self._disconnect_exchange, state="disabled"
        )
        self.disconnect_btn.pack(side="left", padx=(theme.SPACE_SM, 0))
        ttk.Button(top, text="Обновить", command=self._after_exchange_change).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )
        self.space_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.space_var, style="Muted.TLabel").pack(side="right")

        self.files_table, self.files_placeholder = data_table(
            page,
            (
                Column("name", "Файл", 320, 160, stretch=True),
                Column("size", "Размер (байт)", 150, 120, numeric=True),
            ),
            height=8,
            pady=(theme.SPACE_SM, theme.SPACE_SM),
            empty_text="Подключите носитель, чтобы увидеть файлы.",
        )
        self.files_table.bind("<<TreeviewSelect>>", self._on_file_selected)
        self.files_placeholder.show()

        write_row = ttk.Frame(page, style="Card.TFrame")
        write_row.pack(fill="x")
        ttk.Label(write_row, text="Записать:", style="Card.TLabel").pack(side="left")
        self.write_path = tk.StringVar()
        ttk.Entry(write_row, textvariable=self.write_path).pack(
            side="left", fill="x", expand=True, padx=(theme.SPACE_SM, 0)
        )
        button_row(
            page,
            (
                ("Выбрать файл…", lambda: self._choose_file()),
                ("Выбрать каталог…", lambda: self._choose_dir()),
                ("Записать", lambda: self._write_file()),
            ),
            pady=(theme.SPACE_SM, 0),
        )

        # Поля отдельно, кнопки отдельным переносимым рядом.
        #
        # Шесть элементов в одну строку через `pack(side="left")` не поместились:
        # «Удалить» обрезалось до буквы «У» (снимок 17.09.2026). Это ровно тот
        # дефект, ради которого в проекте заведён `button_row` -- он переносит
        # не поместившееся на следующую строку, -- и я повторил его вручную,
        # набрав ряд заново.
        read_row = ttk.Frame(page, style="Card.TFrame")
        read_row.pack(fill="x", pady=(theme.SPACE_SM, 0))
        ttk.Label(read_row, text="Выбрано:", style="Card.TLabel").pack(side="left")
        self.selected_name = tk.StringVar()
        ttk.Entry(read_row, textvariable=self.selected_name, width=18, state="readonly").pack(
            side="left", padx=(theme.SPACE_SM, theme.SPACE_MD)
        )
        ttk.Label(read_row, text="Куда:", style="Card.TLabel").pack(side="left")
        self.read_dest = tk.StringVar()
        ttk.Entry(read_row, textvariable=self.read_dest).pack(
            side="left", fill="x", expand=True, padx=(theme.SPACE_SM, 0)
        )
        button_row(
            page,
            (
                ("Куда сохранить…", lambda: self._choose_destination()),
                ("Прочитать", lambda: self._read_file()),
                ("Удалить из раздела", lambda: self._delete_file()),
            ),
            pady=(theme.SPACE_SM, 0),
        )

    def _connect_exchange(self) -> None:
        if self._volume is not None:
            self.message_bar.info("Носитель уже подключён.")
            return
        self.connect_btn.configure(state="disabled")
        self.message_bar.info("Открывается зашифрованный раздел обмена…")
        record = self.record
        run_in_background(
            self,
            lambda: open_exchange(record).__enter__(),
            on_success=self._exchange_connected,
            on_error=self._exchange_failed,
        )

    def _exchange_connected(self, volume) -> None:
        if not widget_alive(self):
            # Окно закрыли, пока том открывался: оставить его открытым значит
            # занять носитель, и закрыть его будет некому.
            volume.close()
            return
        self._volume = volume
        self.connect_btn.configure(state="disabled")
        self.disconnect_btn.configure(state="normal")
        self.message_bar.success("Раздел обмена подключён.")
        self._after_exchange_change()

    def _exchange_failed(self, exc: BaseException) -> None:
        if not widget_alive(self):
            return
        self.connect_btn.configure(state="normal")
        self.message_bar.error(
            str(exc) if isinstance(exc, ServiceVolumeError)
            else f"Не удалось подключить раздел: {exc}"
        )

    def _disconnect_exchange(self) -> None:
        if self._volume is None:
            return
        self._volume.close()
        self._volume = None
        for item in self.files_table.get_children():
            self.files_table.delete(item)
        self.files_placeholder.show("Подключите носитель, чтобы увидеть файлы.")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.space_var.set("")
        self.message_bar.success("Раздел обмена закрыт, носитель можно извлекать.")

    def _on_destroy(self, event) -> None:
        if event.widget is self and self._volume is not None:
            self._volume.close()
            self._volume = None

    def _mount_point(self) -> "str | None":
        if self._volume is None:
            self.message_bar.error("Сначала подключите носитель.")
            return None
        return self._volume.mountpoint

    def _on_file_selected(self, _event) -> None:
        selection = self.files_table.selection()
        if selection:
            self.selected_name.set(self.files_table.item(selection[0], "values")[0])

    def _choose_file(self) -> None:
        chosen = filedialog.askopenfilename(parent=self, title="Файл для раздела обмена")
        if chosen:
            self.write_path.set(chosen)

    def _choose_dir(self) -> None:
        """Каталог целиком: через обмен едут настройки программ, а это чаще
        дерево, чем один файл."""
        chosen = filedialog.askdirectory(parent=self, title="Каталог для раздела обмена")
        if chosen:
            self.write_path.set(chosen)

    def _choose_destination(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Куда сохранить")
        if not chosen:
            return
        name = self.selected_name.get().strip().rstrip("/")
        self.read_dest.set(f"{chosen}/{name}" if name else chosen)

    def _after_exchange_change(self) -> None:
        mount_point = self._volume.mountpoint if self._volume is not None else None
        if mount_point is None:
            return
        for item in self.files_table.get_children():
            self.files_table.delete(item)
        try:
            for info in self.manager.list_files(mount_point):
                self.files_table.insert("", "end", values=(info.name, info.size_bytes))
        except ExchangeError as exc:
            self.message_bar.error(str(exc))
        self.files_placeholder.update(
            not self.files_table.get_children(), "На разделе обмена пока нет файлов."
        )
        # Сколько места осталось -- до записи, а не отказом после неё.
        try:
            free, total = self.manager.free_space(mount_point)
        except (ExchangeError, OSError):
            return
        self.space_var.set(
            f"Свободно {free / 1024 / 1024:.0f} МБ из {total / 1024 / 1024:.0f} МБ"
        )

    def _write_file(self) -> None:
        mount_point = self._mount_point()
        if mount_point is None:
            return
        source = self.write_path.get().strip()
        if not source:
            self.message_bar.error("Укажите, что записать.")
            return
        try:
            self.manager.write_file(mount_point, Path(source))
        except (ExchangeError, InsufficientSpaceError, OSError) as exc:
            self.message_bar.error(str(exc))
            return
        self.message_bar.success(f"Записано: {source}")
        self._after_exchange_change()

    def _read_file(self) -> None:
        mount_point = self._mount_point()
        if mount_point is None:
            return
        name = self.selected_name.get().strip()
        dest = self.read_dest.get().strip()
        if not name or not dest:
            self.message_bar.error("Выберите файл в списке и укажите, куда его сохранить.")
            return
        try:
            self.manager.read_file(mount_point, name, Path(dest))
        except (ExchangeError, OSError) as exc:
            self.message_bar.error(str(exc))
            return
        self.message_bar.success(f"Файл «{name}» скопирован в {dest}")

    def _delete_file(self) -> None:
        mount_point = self._mount_point()
        if mount_point is None:
            return
        name = self.selected_name.get().strip()
        if not name:
            self.message_bar.error("Выберите файл для удаления.")
            return
        try:
            self.manager.delete_file(mount_point, name)
        except (ExchangeError, OSError) as exc:
            self.message_bar.error(str(exc))
            return
        self.message_bar.success(f"Файл «{name}» удалён из раздела обмена.")
        self._after_exchange_change()


def open_media_properties(parent: tk.Widget, app, media_id: str) -> MediaPropertiesWindow:
    window = MediaPropertiesWindow(parent, app, media_id)
    window.take_focus()
    return window
