"""Каркас главного окна: разделы слева, работа справа.

Почему так, а не рядом кнопок. До 02.09.2026 главный экран был девятью
кнопками в два ряда, где вперемешку стояли действия («Установка ОС», «Собрать
дистрибутив») и разделы («Журналы», «Обмен», «Восстановление»). Ряд уже
однажды не поместился в окно: «Восстановление» обрезалось краем, «Состояние»
не было видно вовсе, и лечили это вторым рядом, а не разбором причины.

Причина же простая: кнопка отвечает на вопрос «что сделать», а раздел -- на
вопрос «где я нахожусь», и складывать их в один ряд означает не отвечать ни на
один. Разделы уходят влево постоянным списком -- так у администратора всегда
видно, из чего состоит программа и где он сейчас, -- а действия остаются на
своём экране, рядом с тем, над чем они совершаются.

Это же снимает и потолок: список слева растёт вниз, и следующий раздел не
придётся втискивать в строку. Рекомендация GNOME HIG ровно об этом -- боковая
панель уместна, когда разделов больше, чем помещается в переключатель видов.

Почему разделов четыре, а не шесть. «Журналы» и «Обмен» ушли отсюда
16.09.2026: оба относятся к одному носителю, а не к программе. Разделом каждый
из них первым делом спрашивал носитель выпадающим списком -- то есть заново
задавал вопрос, на который администратор уже ответил, выделив строку в списке
носителей. Теперь они открываются действием над выбранной строкой и приходят
с готовым ответом. Экраны остались прежними: список разделов слева и список
открываемых экранов -- разные вещи, и убранное отсюда никуда не делось.

«Обслуживание» осталось разделом, хотя решение 16.09.2026 называло его вместе
с «Обменом». На него это решение не легло: там пароль контейнера, следы
прерванных установок и ресурс отзыва -- три вещи о самой программе, и ни одной
о носителе. Переносить в действия над строкой было нечего.

Мастера (установка, сборка, развёртывание) сюда не попадают и занимают окно
целиком. Они не место, а последовательность шагов: у них свой список шагов
слева, и вторая навигация рядом с ним предлагала бы уйти из середины работы,
которую нельзя бросить на полпути.
"""
from __future__ import annotations

from .ui import tk
from .ui import ttk

from .. import ADMIN_APP_NAME, SYSTEM_NAME
from . import theme

#: Разделы в порядке работы, а не по алфавиту: сначала то, ради чего программу
#: открывают, потом то, к чему возвращаются по случаю.
#:
#: `key` -- имя раздела для подсветки, `title` -- подпись, `hint` -- строка,
#: объясняющая раздел до того, как в него зашли.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("media", "Носители", "Собрать, записать и выдать носитель"),
    ("configs", "Конфигурации", "Из чего собирается система"),
    ("recovery", "Восстановление", "Код доступа, когда пароль забыт"),
    ("maintenance", "Обслуживание", "Пароль контейнера и следы прерванных установок"),
)


class NavigationShell(ttk.Frame):
    """Боковая навигация плюс область, в которой живёт текущий раздел."""

    def __init__(self, parent: tk.Widget, app) -> None:
        super().__init__(parent, style="Window.TFrame")
        self.app = app
        self._buttons: dict[str, ttk.Label] = {}
        self._active = ""

        sidebar = ttk.Frame(self, style="Sidebar.TFrame", padding=(0, theme.SPACE_MD))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        sidebar.configure(width=228)

        ttk.Label(
            sidebar,
            text=f"{ADMIN_APP_NAME} · {SYSTEM_NAME}",
            style="SidebarTitle.TLabel",
        ).pack(anchor="w", padx=theme.SPACE_MD, pady=(0, theme.SPACE_MD))

        for key, title, hint in SECTIONS:
            item = ttk.Label(sidebar, text=title, style="SidebarItem.TLabel", padding=(theme.SPACE_MD, theme.SPACE_SM))
            item.pack(fill="x")
            # Курсор и щелчок на подписи, а не кнопка: кнопка в списке разделов
            # выглядит как действие, которое что-то запустит, а здесь она лишь
            # переводит взгляд.
            item.bind("<Button-1>", lambda _event, k=key: self.app.show_section(k))
            item.bind("<Enter>", lambda _event, w=item: w.configure(cursor="hand2"))
            self._buttons[key] = item
            if hint:
                item.bind("<Enter>", lambda _event, h=hint: self.app.set_status(h), add="+")
                item.bind("<Leave>", lambda _event: self.app.set_status(""), add="+")

        # Выход из сеанса -- внизу и отдельно: это не раздел, а конец работы.
        #
        # Цветом опасности и с пояснением при наведении, потому что действие
        # необратимо: программа завершается целиком. Иначе и нельзя -- ключ
        # контейнера затирается на месте, а секрет администратора живёт
        # строкой, которую в Python не затереть, и убирает его из памяти
        # только смерть процесса. Человек должен узнать это до нажатия.
        quit_item = ttk.Label(
            sidebar,
            text="Закрыть сеанс",
            style="SidebarDanger.TLabel",
            padding=(theme.SPACE_MD, theme.SPACE_SM),
        )
        quit_item.pack(side="bottom", fill="x")
        quit_item.bind("<Button-1>", lambda _event: self.app.close_session())
        quit_item.bind("<Enter>", lambda _event: quit_item.configure(cursor="hand2"))
        quit_item.bind(
            "<Enter>",
            lambda _event: self.app.set_status(
                "Программа закроется целиком: иначе ключи не убрать из памяти"
            ),
            add="+",
        )
        quit_item.bind("<Leave>", lambda _event: self.app.set_status(""), add="+")

        self.content = ttk.Frame(self, style="Window.TFrame")
        self.content.pack(side="left", fill="both", expand=True)

    def mark_active(self, key: str) -> None:
        """Подсветить раздел, в котором администратор сейчас находится."""
        self._active = key
        for name, widget in self._buttons.items():
            widget.configure(
                style="SidebarItemActive.TLabel" if name == key else "SidebarItem.TLabel"
            )


__all__ = ["NavigationShell", "SECTIONS"]
