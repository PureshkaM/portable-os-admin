"""Обмен конфигурациями с другим администратором -- отдельным окном.

Зачем отдельным. На экране конфигураций стояло девять кнопок в трёх рядах, и
четыре из них были про обмен: «Мой код обмена…», «Отправить…», «Отправить
все…», «Принять…». Они не действия над выбранной строкой, а работа с чужим
контейнером, и стояли рядом с «Удалить» -- то есть предлагались как равные по
последствиям. Администратор, открывший экран впервые, выбирал из девяти
кнопок раньше, чем понимал разницу между ними.

Обмен к тому же делают редко: конфигурацию пишут один раз и потом ей
пользуются. Редкое действие, занимающее треть экрана, -- это и есть
переусложнение, на которое указал пользователь 16.09.2026.

Что здесь не изменилось: сам обмен. Упаковка, распаковка и опознание
отправителя лежат в `config/exchange.py` и не тронуты -- переехали только
диалоги, то есть та часть, которая спрашивает файл и показывает ответ.
"""
from __future__ import annotations

from ..ui import tk
from pathlib import Path
from ..ui import ttk

from ...config import exchange
from .. import theme
from ..widgets import PropertiesWindow, button_row, error_guard


class ConfigsShareWindow(PropertiesWindow):
    """Код администратора, отправка конфигураций и приём присланных.

    Окно, а не экран (17.09.2026). Обмен с другим администратором -- действие,
    а не место в программе: `show_frame` уносил боковую навигацию, и экран
    открывался прижатым к углу окна, без отступов и без списка разделов слева.
    Своя кнопка закрытия и Esc делают «Назад» ненужной.
    """

    def __init__(self, parent: tk.Widget, app, config_name: str = ""):
        self.app = app
        #: Конфигурация, выбранная на списке до входа сюда. Выбор -- уже
        #: ответ, и спрашивать его второй раз значит искать в списке строку,
        #: которую администратор только что выделил.
        self.config_name = config_name

        super().__init__(parent, "Обмен конфигурациями", width=700, height=560)
        card = self.tab("Обмен")
        ttk.Label(
            card,
            text="Посылка шифруется кодом получателя: открыть её сможет только он.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))

        ttk.Label(card, text="Мой код", font=theme.FONT_SECTION).pack(anchor="w")
        ttk.Label(
            card,
            # Подпись несёт то, чего без неё не понять: почему код можно
            # диктовать вслух. Без этого его начнут беречь как пароль и
            # передавать окольными путями.
            text="Код публичный: он только шифрует присланное вам, но ничего не "
            "расшифровывает. Его можно продиктовать по телефону.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))
        ttk.Button(card, text="Показать мой код…", command=self._show_my_code).pack(anchor="w")

        ttk.Separator(card).pack(fill="x", pady=theme.SPACE_MD)

        ttk.Label(card, text="Отправить", font=theme.FONT_SECTION).pack(anchor="w")
        self.selected_label = ttk.Label(card, text="", style="Hint.TLabel", wraplength=theme.TEXT_WRAP)
        self.selected_label.pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))
        self._say_what_is_selected()

        # Имя конфигурации -- в строке выше, а не в подписи кнопки: имена
        # бывают длинными, и подпись вместе с ними разъезжается (снимок
        # 17.09.2026). Кнопка говорит, что делает, строка -- над чем.
        send_buttons: "list[tuple[str, object]]" = []
        if self.config_name:
            send_buttons.append(("Отправить выбранную…", self._send_selected))
        send_buttons.append(("Отправить все…", self._send_all))
        button_row(card, tuple(send_buttons), pady=(0, theme.SPACE_SM))

        ttk.Separator(card).pack(fill="x", pady=theme.SPACE_MD)

        ttk.Label(card, text="Принять", font=theme.FONT_SECTION).pack(anchor="w")
        ttk.Label(
            card,
            text="Одноимённые конфигурации в контейнере будут заменены — имя "
            "отправителя называется до того, как что-то попадёт внутрь.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))
        button_row(card, (("Принять посылку…", self._receive),), pady=(0, theme.SPACE_SM))

    def _say_what_is_selected(self) -> None:
        if self.config_name:
            self.selected_label.config(
                text=f"Выбрана «{self.config_name}». Отправить можно её одну или все сразу."
            )
        else:
            self.selected_label.config(
                text="В списке ничего не было выбрано — отправить можно все сразу, "
                "либо вернуться и выделить нужную."
            )

    # -- обмен с другим администратором -------------------------------------
    def _show_my_code(self) -> None:
        """Свой код -- то, что администратор диктует или пересылает коллеге.

        Код публичный: перехват не даёт ничего, расшифровать присланное может
        только этот контейнер. Поэтому он показывается прямо на экране, а не
        под подтверждением.
        """
        from ..ui import messagebox

        with error_guard(self.message_bar):
            identity = exchange.identity_of(self.app.container)
            self.app.save_container()
            self.clipboard_clear()
            self.clipboard_append(identity.code)
            messagebox.showinfo(
                "Мой код обмена",
                f"Отпечаток: {identity.fingerprint}\n\n{identity.code}\n\n"
                "Код скопирован в буфер обмена. Его можно передавать открыто: "
                "он только шифрует присланное вам, но не расшифровывает.",
                parent=self,
            )

    def _send_selected(self) -> None:
        if not self.config_name:
            self.message_bar.error("Конфигурация не выбрана.")
            return
        self._send([self.config_name])

    def _send_all(self) -> None:
        names = self.app.config_manager.list_configs(self.app.container)
        if not names:
            self.message_bar.error("Отправлять нечего: в контейнере нет конфигураций.")
            return
        self._send(names)

    def _send(self, names: "list[str]") -> None:
        from ..ui import filedialog, simpledialog

        code = simpledialog.askstring(
            "Код получателя",
            "Код администратора, которому предназначена посылка:",
            parent=self,
        )
        if not code:
            return
        with error_guard(self.message_bar):
            payload = [
                (name, self.app.container.find_config(name).data)
                for name in names
                if self.app.container.find_config(name) is not None
            ]
            blob = exchange.pack(self.app.container, payload, code)
            self.app.save_container()
            target = filedialog.asksaveasfilename(
                parent=self,
                title="Куда сохранить посылку",
                defaultextension=exchange.EXCHANGE_SUFFIX,
                initialfile=("configs" if len(payload) > 1 else payload[0][0])
                + exchange.EXCHANGE_SUFFIX,
            )
            if not target:
                return
            Path(target).write_bytes(blob)
            self.message_bar.success(
                f"Посылка сохранена: {target}. Конфигураций внутри: {len(payload)}. "
                "Открыть её сможет только владелец указанного кода."
            )

    def _receive(self) -> None:
        from ..ui import filedialog, messagebox

        source = filedialog.askopenfilename(
            parent=self,
            title="Посылка от другого администратора",
            filetypes=[("Посылка", "*" + exchange.EXCHANGE_SUFFIX), ("Все файлы", "*")],
        )
        if not source:
            return
        with error_guard(self.message_bar):
            sender, configs = exchange.unpack(self.app.container, Path(source).read_bytes())
            names = ", ".join(name for name, _ in configs)
            # Отправитель называется до того, как что-то попадёт в контейнер:
            # по присланной конфигурации собирают носитель, и «от кого» здесь
            # такой же вопрос, как «что внутри».
            if not messagebox.askyesno(
                "Принять конфигурации",
                f"Отправитель, отпечаток {sender.fingerprint}.\n\n"
                f"В посылке: {names}.\n\n"
                "Одноимённые конфигурации в контейнере будут заменены. Принять?",
                parent=self,
            ):
                return
            for name, data in configs:
                self.app.container.put_config(name, data)
            self.app.save_container()
            self.message_bar.success(
                f"Принято конфигураций: {len(configs)} (от {sender.fingerprint})."
            )


def open_configs_share(parent: tk.Widget, app, config_name: str = "") -> ConfigsShareWindow:
    window = ConfigsShareWindow(parent, app, config_name)
    window.take_focus()
    return window
