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

import tkinter as tk
from tkinter import simpledialog, ttk

from ...admin import MediaActions
from ...errors import PortableOSAdminError
from ...install import integrity
from .. import theme
from ..widgets import PropertiesWindow, facts_table, run_in_background, widget_alive


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

        self._build_facts()
        self._build_integrity()

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


def open_media_properties(parent: tk.Widget, app, media_id: str) -> MediaPropertiesWindow:
    window = MediaPropertiesWindow(parent, app, media_id)
    window.take_focus()
    return window
