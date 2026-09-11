"""Main window after a session is open ("Интерфейсы пользователя-2")."""
from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk

from ...admin import media_detect
from .. import theme
from ..widgets import Column, MessageBar, button_row, data_table, section_card, section_header


class DashboardFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        # Bottom-up, so the actions and the message line keep their space and
        # the table gives up its own when the window is short.
        self.message_bar = MessageBar(self)
        self.message_bar.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_MD))

        # Ряд действий, и только действий.
        #
        # Разделы («Журналы», «Обмен», «Восстановление», «Обслуживание») ушли
        # в боковую навигацию: кнопка отвечает на вопрос «что сделать», раздел
        # -- на вопрос «где я», и в одном ряду они не отвечали ни на один. Тот
        # ряд к тому же дважды не помещался в окно.
        actions = ttk.Frame(self, style="Window.TFrame")
        actions.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, 0))

        # Установка -- то, ради чего программу открывают; одна акцентная
        # кнопка говорит об этом без единого слова пояснения.
        ttk.Button(
            actions, text="Установка ОС", style="Accent.TButton", command=self._open_install
        ).pack(side="left")
        # Порядок повторяет порядок работы: сначала собрать дистрибутив, потом
        # разворачивать его на носители -- по одному или тиражом.
        for text, command in (
            ("Собрать дистрибутив", self._open_build),
            ("Развернуть дистрибутив", self._open_deploy),
        ):
            ttk.Button(actions, text=text, command=command).pack(
                side="left", padx=(theme.SPACE_SM, 0)
            )

        # Заголовок раздела, а не программы: имя программы уже стоит над
        # списком разделов слева, и повторять его здесь значит занимать
        # строку тем, что читатель только что прочитал.
        #
        # Двойной щелчок сам себя не объясняет, и это тот случай, когда
        # подпись несёт новое, а не пересказывает очевидное.
        section_header(
            self,
            "Носители",
            "Носители, зарегистрированные в этом контейнере администратора. "
            "Двойной щелчок — проверка загрузчика и initramfs.",
        )

        card = section_card(self)

        # Первым -- имя, которое дал администратор, а не идентификатор.
        #
        # `46aed677` отвечает на вопрос «тот ли это носитель», но не на вопрос
        # «чей он», а список из десяти строк читают именно ради второго.
        # Столбец «Подключён» заполняется сам: носитель, воткнутый в порт,
        # узнаётся по UUID своих томов, без пароля и без монтирования.
        # Ширина проверяется по заголовку, а не по значению: идентификатор --
        # восемь знаков и в сотню пикселей помещается, а слово
        # «Идентификатор» над ним обрезалось до «Идентификат».
        #
        # Сумма ширин влезает в область содержимого при окне минимальной
        # ширины: до 02.09.2026 она была больше, и колонка «Создан» уходила за
        # край -- дата создания носителя не читалась вовсе. Минимумы держат
        # то же обещание в окне уже минимального: ниже них колонки не
        # сжимаются, а таблица получает горизонтальную прокрутку.
        # Состояние стоит вторым, сразу за именем: список читают ради вопроса
        # «чей носитель», а отозванный носитель -- это ответ «уже ничей», и
        # узнавать его надо тем же взглядом, а не доходя до конца строки.
        self.table, self.placeholder = data_table(
            card,
            (
                Column("name", "Носитель", 120, 120, stretch=True),
                # Владелец -- второй вопрос после «что это за носитель», и
                # первый, который задают, когда сотрудник уволился или звонит
                # за кодом восстановления. Столбец добавлен 11.09.2026; сумма
                # ширин от этого перестала помещаться в окно наименьшего
                # размера, и это не дефект, а тот случай, ради которого у
                # таблицы есть горизонтальная прокрутка: колонки не сжимаются
                # ниже минимума, а уезжают под прокрутку целиком.
                Column("owner", "Владелец", 150, 130),
                Column("status", "Состояние", 130, 110),
                Column("connected", "Подключён", 110, 105),
                Column("media_id", "Идентификатор", 145, 140),
                Column("os", "ОС", 60, 55),
                Column("config", "Конфигурация", 130, 130),
                Column("created", "Создан", 125, 120),
            ),
            height=10,
            empty_text="Носителей пока нет.\nНачните с «Установка ОС».",
        )
        # Отозванная строка гаснет, а не исчезает: отзыв -- факт учёта, и
        # история выданных носителей не должна редеть оттого, что часть из них
        # уже отозвана. Приглушённый цвет отличает её с одного взгляда, не
        # заставляя читать колонку.
        self.table.tag_configure("revoked", foreground=theme.TEXT_MUTED)
        # Проверка целостности относится к одному носителю, а не к контейнеру
        # вообще, поэтому она открывается со строки, а не кнопкой: седьмая
        # кнопка в ряд уже не помещается, и это ловит тест ширины.
        self.table.bind("<Double-1>", self._open_selected_integrity)
        # Действия над конкретным носителем стоят под таблицей, а не в общем
        # ряду: они относятся к выбранной строке, и место рядом с ней говорит
        # об этом лучше любой подписи.
        #
        # Ряд переносимый, а не сколоченный из `pack(side="left")`: четвёртая
        # кнопка в окне 900x600 уже не помещается, а Tk её не сжимает -- он
        # просто перестаёт её рисовать, и «Отзыв…» получал 77 пикселей от
        # нужных 126. Этот дефект проект ловил трижды, каждый раз одинаково,
        # и ровно ради него заведён `button_row`.
        row_host = ttk.Frame(self, style="Window.TFrame")
        row_host.pack(side="top", fill="x", padx=theme.SPACE_LG)
        button_row(
            row_host,
            (
                ("Проверить целостность", self._open_selected_integrity),
                ("Журналы носителя", self._open_selected_logs),
                ("Переименовать", self._rename_selected),
                ("Отзыв…", self._open_selected_revoke),
            ),
            style="Window.TFrame",
            pady=(theme.SPACE_SM, 0),
        )

        self._poll_job = None
        self._poll_connected()
        # Опрос прекращается вместе с экраном: `after` переживает виджет и
        # продолжал бы дёргать уничтоженную таблицу.
        self.bind("<Destroy>", self._stop_polling)


    def _refresh_table(self) -> None:
        # Выбор переживает перерисовку: таблица обновляется сама каждые пять
        # секунд, и без этого строка, на которую человек нажал, теряла
        # выделение у него под рукой -- ровно между «выбрал» и «нажал кнопку».
        # Идентификатор строки -- это `media_id`, поэтому вернуть выбор можно
        # по имени, а не по номеру: номер после перерисовки означал бы другой
        # носитель.
        selected = self.table.selection()
        for item in self.table.get_children():
            self.table.delete(item)
        records = self.app.session.container.data.media_records
        self.placeholder.update(not records)
        connected = media_detect.detect_map(records)
        for record in records:
            found = connected.get(record.media_id)
            self.table.insert(
                "",
                "end",
                iid=record.media_id,
                values=(
                    record.display_name,
                    # Прочерк, а не пустота: пустая ячейка читается как «здесь
                    # ничего не должно быть», тогда как здесь должно -- просто
                    # носитель записан до того, как поле появилось.
                    record.owner or "—",
                    record.status_text,
                    found.device_path if found else "",
                    record.media_id,
                    record.target_os,
                    record.config_name,
                    record.created_at,
                ),
                tags=("revoked",) if record.is_revoked else (),
            )
        restored = [item for item in selected if self.table.exists(item)]
        if restored:
            self.table.selection_set(restored)

    def _poll_connected(self) -> None:
        """Опрашивает порты, пока экран открыт.

        Носитель втыкают уже после того, как список открыт, -- и если не
        опрашивать, администратору придётся догадаться нажать «обновить».
        Пять секунд -- достаточно редко, чтобы не мешать, и достаточно часто,
        чтобы флешка «появилась» пока рука ещё на ней.
        """
        if not self.winfo_exists():
            return
        try:
            self._refresh_table()
        finally:
            self._poll_job = self.after(5000, self._poll_connected)

    def _rename_selected(self) -> None:
        """Имя носителя живёт в контейнере, а не на флешке.

        Поэтому переименование ничего не пишет на носитель: его можно дать
        уже выданному, потерянному и даже сломанному -- лишь бы запись о нём
        была здесь.
        """
        media_id = self._selected_media_id()
        if not media_id:
            self.message_bar.error("Выберите носитель в списке.")
            return
        record = next(
            (r for r in self.app.session.container.data.media_records if r.media_id == media_id),
            None,
        )
        if record is None:
            self.message_bar.error("Запись о носителе не найдена.")
            return
        chosen = simpledialog.askstring(
            "Имя носителя",
            "Как называть этот носитель? Например: «Иванов, бухгалтерия».\n"
            "Имя хранится в контейнере администратора и на носитель не пишется.",
            initialvalue=record.name or record.label,
            parent=self,
        )
        if chosen is None:
            return
        record.name = chosen.strip()
        self.app.save_container()
        self._refresh_table()
        self.message_bar.success(
            f"Носитель {media_id} теперь называется «{record.display_name}»."
        )

    def _stop_polling(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        job = getattr(self, "_poll_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001 -- окно уже уничтожено
                pass
            self._poll_job = None

    def _selected_media_id(self) -> str:
        """Идентификатор выбранного носителя -- это и есть имя строки.

        Раньше он читался из колонки по номеру, и номер приходилось править в
        трёх местах при каждой новой колонке. Имя строки от порядка колонок не
        зависит вовсе.
        """
        selection = self.table.selection()
        return selection[0] if selection else ""

    def _open_selected_integrity(self, _event=None) -> None:
        from .integrity_check import IntegrityFrame

        media_id = self._selected_media_id()
        if not media_id:
            return
        self.app.show_frame(IntegrityFrame, media_id=media_id)

    def _open_selected_revoke(self) -> None:
        """Отзыв носителя -- отдельный экран, а не вопрос в диалоге.

        Потому что ответов два, разной силы: пометить запись отозванной, если
        носитель не вернулся, и стереть заголовки, если вернулся. Разницу между
        ними надо показать рядом, а в диалоге с двумя кнопками её не объяснить.
        """
        from .revoke import RevokeFrame

        media_id = self._selected_media_id()
        if not media_id:
            self.message_bar.error("Выберите носитель в списке.")
            return
        self.app.show_frame(RevokeFrame, media_id=media_id)

    def _open_selected_logs(self) -> None:
        """Журналы того носителя, который выбран в таблице.

        Раздел «Журналы» спрашивает носитель сам; отсюда ответ уже есть, и
        спрашивать второй раз незачем.
        """
        self.app.show_section("logs", media_id=self._selected_media_id())

    def _open_build(self) -> None:
        from .build_wizard import BuildWizardFrame

        self.app.show_frame(BuildWizardFrame)

    def _open_deploy(self) -> None:
        from .deploy_wizard import DeployWizardFrame

        self.app.show_frame(DeployWizardFrame)

    def _open_install(self) -> None:
        from .install_wizard import InstallWizardFrame

        self.app.show_frame(InstallWizardFrame)

