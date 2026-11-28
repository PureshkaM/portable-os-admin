"""Desktop GUI entry point.

Same window flow as the Textual TUI (`portable_os_admin.tui.app`) -- a
startup screen to open/create the admin container, then a dashboard giving
access to configuration management, installation, log viewing, the exchange
partition, and recovery-code issuance -- but the multi-field flows
(configuration authoring, installation) are wizards here instead of single
crowded forms. See `gui/__init__.py` for why this presentation layer exists
alongside the TUI rather than replacing it.
"""
from __future__ import annotations

import gc
import os
import signal
from .ui import tk
from pathlib import Path
from .ui import ttk

from .ui import messagebox

from .. import ADMIN_APP_NAME, SYSTEM_NAME
from ..config import ConfigManager
from ..container import AdminContainer, ContainerManager
from ..session import AdminSession
from . import theme
from .elevate import elevate_or_exit


class PortableOSAdminGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{ADMIN_APP_NAME} — {SYSTEM_NAME}")
        # Wider than before: the wizard now shows its step list alongside the
        # content instead of a bare progress bar, and the form needs the same
        # room it always did next to it.
        self.geometry("1040x700")
        self.minsize(900, 600)

        # One call decides colours, fonts and spacing everywhere -- see
        # gui/theme.py. It selects "clam" for the same reason this code always
        # did: it is the one built-in theme that renders consistently across
        # the Linux desktops this application targets, and the only one that
        # honours the colours set on it.
        try:
            theme.apply(self)
        except tk.TclError:
            # A Tk without the clam theme still has to run, just plainer.
            pass

        self.container_manager = ContainerManager()
        self.config_manager = ConfigManager()
        self.session: AdminSession | None = None
        self.container_path: Path | None = None

        self._content = ttk.Frame(self)
        self._content.pack(fill="both", expand=True)
        self._current_frame: ttk.Frame | None = None
        #: Оболочка с боковой навигацией. Живёт, пока открыт сеанс: разделы
        #: сменяются внутри неё, а мастера занимают окно целиком и убирают её.
        self._shell = None
        self._status: tk.StringVar | None = None

        self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        # Крестик -- не единственное нештатное закрытие. Выход из системы и
        # `kill` присылают SIGTERM, и без обработчика процесс умирает мгновенно:
        # секреты остаются незатёртыми, а служебные тома -- открытыми
        # устройствами /dev/mapper, то есть доступом к журналам и разделу
        # обмена носителя без всякого пароля.
        #
        # Молча мирится с невозможностью: обработчик ставится только из
        # главного потока, и в чужом окружении (встроенный интерпретатор,
        # тестовый запуск в потоке) это законный отказ, а не повод не
        # открыться. Во время записи носителя свои обработчики ставит
        # `TerminationGuardMixin` и возвращает эти на место, когда закончит.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, self._on_termination)
            except (ValueError, OSError, AttributeError):
                pass
        self.show_startup()

    @property
    def container(self) -> "AdminContainer | None":
        """Открытый контейнер -- единственное хранилище всего, что ведёт

        администратор: носителей, записей восстановления и конфигураций.
        Каталога `configs/` рядом с файлом больше нет; до 09.09.2026
        конфигурации лежали там открытым текстом.
        """
        return self.session.container if self.session is not None else None

    def show_frame(self, frame_cls, **kwargs) -> None:
        """Экран во весь размер окна: начальный экран и мастера.

        Мастер занимает окно целиком намеренно: у него свой список шагов
        слева, и вторая навигация рядом предлагала бы уйти из середины работы,
        которую нельзя бросить на полпути.
        """
        self._teardown_current()
        if self._shell is not None:
            self._shell.destroy()
            self._shell = None
        self._current_frame = frame_cls(self._content, self, **kwargs)
        self._current_frame.pack(fill="both", expand=True)

    def show_section(self, key: str, **kwargs) -> None:
        """Раздел внутри оболочки с боковой навигацией.

        Разделы -- это «где я нахожусь»: список носителей, конфигурации,
        журналы, обмен, восстановление, обслуживание. Оболочка при переходе не
        пересоздаётся, поэтому список слева не мигает и не теряет прокрутку.
        """
        from .shell import NavigationShell

        # Журналы и обмен здесь больше не значатся: они стали вкладками окна
        # свойств носителя (17.09.2026), а не экранами во весь размер окна.
        screens = {
            "media": ("dashboard", "DashboardFrame"),
            "configs": ("configs_list", "ConfigsListFrame"),
            "recovery": ("recovery", "RecoveryFrame"),
            "maintenance": ("maintenance", "MaintenanceFrame"),
        }
        module_name, class_name = screens[key]
        module = __import__(
            f"portable_os_admin.gui.screens.{module_name}", fromlist=[class_name]
        )
        frame_cls = getattr(module, class_name)

        self._teardown_current()
        if self._shell is None:
            self._shell = NavigationShell(self._content, self)
            self._shell.pack(fill="both", expand=True)
        self._current_frame = frame_cls(self._shell.content, self, **kwargs)
        self._current_frame.pack(fill="both", expand=True)
        self._shell.mark_active(key)

    def _teardown_current(self) -> None:
        if self._current_frame is not None:
            self._current_frame.destroy()
            self._current_frame = None
            # On the UI thread, deliberately: a screen leaves tk.Variable
            # objects behind, and finalising one from a worker thread calls
            # Tcl from the wrong thread and hangs that thread for good. See
            # gui/widgets.py run_in_background.
            gc.collect()

    def set_status(self, text: str) -> None:
        """Строка пояснения к разделу, на который наведён курсор.

        Пусто -- законное значение: убрать пояснение так же важно, как
        показать, иначе оно останется от раздела, который уже не под курсором.
        """
        if self._status is not None:
            self._status.set(text)

    def show_startup(self) -> None:
        from .screens.startup import StartupFrame

        self.show_frame(StartupFrame)

    def show_dashboard(self) -> None:
        """Список носителей -- он же раздел, с которого начинается работа."""
        self.show_section("media")

    def open_session(self, container: AdminContainer, path: Path) -> None:
        self.session = AdminSession(container)
        self.container_path = path
        self.show_dashboard()

    def save_container(self) -> None:
        if self.session is not None:
            self.container_manager.save(self.session.container)

    def end_session(self) -> None:
        """Закрыть сеанс: сохранить, освободить тома, затереть секреты.

        Отделено от выхода намеренно, и это не лазейка мимо `close_session`:
        закрытие сеанса -- настоящая часть работы, а выход добавляется сверху.

        Порядок здесь имеет значение. Сначала сохраняется контейнер -- после
        затирания сохранить уже нельзя, и это правильно. Потом уничтожается
        экран: именно он держит открытые тома обмена и журналов и закрывает их
        в своём `<Destroy>`. И только затем затираются секреты.
        """
        if self.session is not None:
            self.save_container()
        # Экран -- до затирания: он закрывает служебные тома, которые держит.
        self._teardown_current()
        if self._shell is not None:
            self._shell.destroy()
            self._shell = None
        if self.session is not None:
            self.session.close()
        self.session = None
        self.container_path = None
        self.show_startup()

    def close_session(self) -> None:
        """Закрыть сеанс и завершить программу.

        Программа завершается, а не возвращается на начальный экран, и это
        решение о безопасности, а не об удобстве.

        Ключ контейнера затирается на месте, но секрет администратора -- тот,
        из которого выводятся коды восстановления **всех** выданных
        носителей, -- живёт в памяти строкой. Строку в Python затереть нельзя:
        она неизменяема, сборщик мусора копирует объекты между поколениями, а
        каждое обращение к секрету создаёт новые байты. Пока процесс жив, всё
        это лежит в его куче -- читается через `/proc/PID/mem`, попадает в
        дамп при падении, может уйти в своп. Единственная надёжная гарантия --
        смерть процесса: страницы возвращаются ядру и обнуляются перед выдачей
        другому.

        Цена известна и принята: сменить контейнер можно только перезапуском
        программы. Смена контейнера -- редкость (администратор работает со
        своим), а гарантия иначе недостижима.
        """
        if not self._may_stop_now():
            return
        self.end_session()
        self._quit()

    def _may_stop_now(self) -> bool:
        """Спрашивает, если прерывание испортит носитель.

        Вопрос задаётся на обоих путях выхода. До 17.09.2026 его задавал
        только крестик, а «Закрыть сеанс» посреди записи убивало программу
        молча -- носитель оставался непригодным, и человек узнавал об этом от
        флешки, а не от программы.
        """
        if not self.install_in_progress():
            return True
        return bool(
            messagebox.askyesno(
                "Идёт запись на носитель",
                "Сейчас идёт запись на носитель. Если закрыть программу, работа "
                "будет прервана, а носитель останется в непригодном состоянии — его "
                "придётся размечать заново.\n\nВсё равно закрыть?",
                icon="warning",
                default="no",
                parent=self,
            )
        )

    def _on_close_window(self) -> None:
        """Крестик -- такой же конец работы, как кнопка, и ведёт туда же.

        Раньше он сохранял контейнер и уничтожал окно, оставляя секреты в
        памяти умирающего процесса на волю случая. Теперь оба пути сходятся в
        одном месте: закрыть сеанс, затереть, выйти.
        """
        if not self._may_stop_now():
            return
        self.end_session()
        self._quit()

    def _on_termination(self, _signum, _frame):
        """Выход из системы и `kill` -- то же нештатное закрытие, что крестик.

        Без обработчика процесс умирает мгновенно, не затерев ничего и не
        закрыв служебные тома: они остались бы открытыми устройствами
        `/dev/mapper`, то есть доступом к журналам и разделу обмена носителя
        без всякого пароля.
        """
        self.end_session()
        self._quit()

    def _quit(self) -> None:
        """Завершить процесс, а не только закрыть окно.

        `os._exit`, а не `sys.exit`: выход из обработчика Tk до конца
        `mainloop` не доходит, а финализаторы на пути к нормальному выходу
        создали бы новые копии того, что только что затёрто. Контейнер к этому
        моменту сохранён, файлы закрыты и отданы ядру -- терять нечего.
        """
        try:
            self.destroy()
        except tk.TclError:
            pass
        os._exit(0)

    def install_in_progress(self) -> bool:
        frame = self._current_frame
        return bool(getattr(frame, "_running", False))


def run() -> None:
    # Before any window: the program administers portable media and erases
    # disks, so it opens only for someone who can prove they are the machine's
    # administrator. This also removes the old failure mode where installation
    # was attempted unprivileged and died inside parted.
    elevate_or_exit()
    PortableOSAdminGUI().mainloop()


if __name__ == "__main__":
    run()
