"""Small reusable building blocks shared by every GUI screen.

Kept deliberately minimal (no third-party widget toolkit) so the GUI adds no
new dependency beyond the stdlib `tkinter` -- see README, раздел «Архитектура»
"Упаковка" for why that matters: it means the existing deb/rpm packaging only
needs a system `python3-tk` dependency, not another pip package pulled into
the private venv at install time.
"""
from __future__ import annotations

import gc
import queue
import threading
import tkinter as tk
from contextlib import contextmanager
from tkinter import ttk
from typing import Callable, NamedTuple

from ..config.validator import ConfigValidationUnavailableError
from ..errors import PortableOSAdminError
from . import preferences, theme

# Every exception type any backend call in this project is documented to
# raise on an expected failure (bad password, device not eligible, repo
# unreachable, ...). Anything outside this tuple is unexpected, and still
# gets caught by error_guard's final `except Exception` -- but as a labeled
# "непредвиденная ошибка" rather than silently treated the same as a normal,
# anticipated failure.
KNOWN_ERRORS = (PortableOSAdminError, ConfigValidationUnavailableError, OSError, ValueError)


class MessageBar(ttk.Frame):
    """Plain-language status line: green/blue/red text instead of the TUI's
    Rich markup, always in the same place on every screen so the user learns
    one spot to look at rather than hunting for where an error landed."""

    COLORS = {"info": theme.INFO, "success": theme.SUCCESS, "error": theme.DANGER}

    def __init__(self, parent: tk.Widget, on_card: bool = False):
        """`on_card=True` where the bar sits inside a white card rather than on
        the window behind it -- otherwise it draws a grey band across it."""
        frame_style = "Card.TFrame" if on_card else "Window.TFrame"
        self._label_style = "Card.TLabel" if on_card else "Window.TLabel"
        super().__init__(parent, style=frame_style)
        self.var = tk.StringVar()
        self.label = ttk.Label(
            self, textvariable=self.var, style=self._label_style, justify="left"
        )
        wrap_to_width(self.label)
        self.label.pack(anchor="w", pady=theme.SPACE_XS)

    def clear(self) -> None:
        self.var.set("")

    def info(self, text: str) -> None:
        self._set(text, "info")

    def success(self, text: str) -> None:
        self._set(text, "success")

    def error(self, text: str) -> None:
        self._set(text, "error")

    def _set(self, text: str, kind: str) -> None:
        self.var.set(text)
        self.label.configure(foreground=self.COLORS[kind])


def wrap_to_width(label: ttk.Label, padding: int = 0) -> ttk.Label:
    """Перенос по фактической ширине, а не по числу, вписанному на глаз.

    До 04.09.2026 длина строки задавалась числом в каждом месте отдельно:
    860 в строке сообщений, 760 на одном шаге мастера, 740 на соседнем. Числа
    подбирались под окно 1280 и в окне 1040 оказывались шире доступного
    места, поэтому сообщение об ошибке обрывалось на полуслове ровно там,
    где его важнее всего дочитать: «...доступен только на носителе, созданном
    этой програм|».

    Ширина берётся у родителя при каждом его изменении, так что одна и та же
    подпись верна в обоих размерах окна и переживёт третий.
    """
    parent = label.master

    def _resize(event: "tk.Event") -> None:
        width = max(event.width - padding, 160)
        if label.cget("wraplength") != width:
            label.configure(wraplength=width)

    parent.bind("<Configure>", _resize, add="+")
    return label


class Column(NamedTuple):
    """Одна колонка таблицы: всё, что о ней нужно знать, в одном месте.

    `minimum` -- ширина, ниже которой колонка не сжимается. Без неё узкое
    окно съедало заголовок до нечитаемого («Идентификат» вместо
    «Идентификатор»), а значение обрезалось молча.

    `stretch` -- кому достаётся лишнее место. Растягивать имеет смысл ровно
    одну колонку на таблицу, ту, где текст длиннее любой ширины: иначе
    свободное место делится поровну и отбирается у соседей.

    `numeric` -- выравнивание вправо. Числа сравнивают столбиком, по
    последней цифре, и левое выравнивание эту возможность отнимает.
    """

    key: str
    title: str
    width: int
    minimum: int = 80
    stretch: bool = False
    numeric: bool = False


def data_table(
    parent: tk.Widget,
    columns: "tuple[Column, ...]",
    *,
    height: int = 10,
    min_rows: int = 1,
    selectmode: str = "browse",
    empty_text: str = "",
    expand: bool = True,
    pady: "tuple[int, int] | int" = 0,
) -> "tuple[ttk.Treeview, TablePlaceholder]":
    """Таблица со всем, что у таблицы должно быть одинаково.

    Третий помощник рядом с `section_header` и `section_card`, и по той же
    причине: разъезжается то, что каждый экран набирает сам. До этого девять
    таблиц задавали ширины, выравнивание и прокрутку каждая по-своему -- одна
    знала про `minwidth`, остальные нет, а размер в байтах выравнивался
    вправо ровно в одной из трёх.

    Что здесь собрано:

    * ширины, минимумы и растяжение -- из описания колонок;
    * вертикальная прокрутка -- всегда, горизонтальная -- когда сумма
      минимумов перестаёт помещаться, и она же убирается, когда место
      возвращается. Полоса, висящая под таблицей всё время, съедает строку
      там, где в ней нет нужды;
    * пустое состояние -- если сказано, чем его объяснить;
    * видимый фокус. ttk не рисует его у `Treeview` вовсе: пользователь,
      дошедший до таблицы табом, не понимал, где он. Приход фокуса выделяет
      первую строку, если ничего не выбрано, -- и видно, и стрелки с
      кнопками над строкой сразу работают.

    Возвращает саму таблицу и её пустое состояние: экраны работают со
    строками, а не с обёрткой.
    """
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=expand, pady=pady)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)

    # Растягивающаяся таблица просит одну строку, а не `height`, и место
    # получает от упаковщика.
    #
    # Иначе она отнимает его у соседей: запрошенная высота учитывается
    # первой, а `expand` делит только то, что осталось. При окне наименьшего
    # размера (900x600) суммарный запрос экрана превышал окно, и рядам кнопок
    # под таблицей доставался остаток -- на экране обмена кнопки «Куда
    # сохранить…», «Прочитать» и «Удалить» сжимались до одного пикселя, то
    # есть исчезали, а на главном экране рисовались пустыми прямоугольниками
    # без подписей. Снято в приложении 07.09.2026.
    #
    # На окне побольше разницы нет: лишнее место всё равно достаётся
    # единственному растягивающемуся виджету, то есть таблице.
    # ...но одна строка годится только там, где у таблицы есть соперники за
    # место. Внутри прокручиваемого шага мастера их нет, и добавлять ей высоту
    # некому: таблица остаётся ростом в строку, а всё, кроме первой записи,
    # уходит под прокрутку, которую почти не видно.
    #
    # Найдено 14.09.2026 и стоило получаса: сохранённая конфигурация «не
    # появлялась» в мастере сборки -- в списке конфигураций она есть, в мастере
    # нет. Проверены источник данных, запись на диск, чтение контейнера
    # напрямую, журнал приложения и перезапуск; всё это время вторая строка
    # просто была за нижним краем таблицы. Поэтому минимум задаётся тем, кто
    # знает своё окружение, а умолчание остаётся прежним.
    table = ttk.Treeview(
        frame, columns=[c.key for c in columns], show="headings",
        height=(max(1, min_rows) if expand else height), selectmode=selectmode,
    )
    for column in columns:
        anchor = "e" if column.numeric else "w"
        table.heading(column.key, text=column.title, anchor=anchor)
        table.column(
            column.key,
            width=column.width,
            minwidth=column.minimum,
            stretch=column.stretch,
            anchor=anchor,
        )
    table.grid(row=0, column=0, sticky="nsew")

    vertical = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal = ttk.Scrollbar(frame, orient="horizontal", command=table.xview)
    table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

    shown = False

    def _fit(_event: "tk.Event | None" = None) -> None:
        """Полоса нужна ровно тогда, когда правый край перестал помещаться.

        Считается по текущим ширинам колонок, а не по сумме минимумов, и это
        разница не арифметическая: минимум -- предел сжатия, а прячет правый
        край Tk гораздо раньше, как только колонок в сумме больше, чем места.
        Первая версия сравнивала с минимумами, и на снимке штатного окна
        1040x700 колонка «Создан» была обрезана, а полосы не было.
        """
        nonlocal shown
        occupied = sum(int(table.column(column.key, "width")) for column in columns)
        needed = table.winfo_width() < occupied
        if needed and not shown:
            horizontal.grid(row=1, column=0, sticky="ew")
            shown = True
        elif not needed and shown:
            horizontal.grid_remove()
            shown = False

    table.bind("<Configure>", _fit, add="+")

    def _show_focus(_event: "tk.Event") -> None:
        if table.selection():
            return
        rows = table.get_children()
        if rows:
            table.selection_set(rows[0])
            table.focus(rows[0])

    table.bind("<FocusIn>", _show_focus, add="+")

    placeholder = TablePlaceholder(table, empty_text)
    return table, placeholder


def button_row(
    parent: tk.Widget,
    buttons: "tuple[tuple[str, Callable[[], None]], ...]",
    *,
    style: str = "",
    accent_first: bool = False,
    side: str = "top",
    pady: "tuple[int, int] | int" = 0,
) -> "tuple[ttk.Frame, list[ttk.Button]]":
    """Ряд кнопок, который переносится, когда перестаёт помещаться.

    Кнопка, вышедшая за край, не сжимается и не сокращает подпись -- Tk
    просто перестаёт её рисовать: на экране конфигураций «Назад» оставалось
    37 пикселей от нужных 126, а на экране журналов «Сохранить в файл…» не
    хватало восьми. Дефект появляется только в узком окне, поэтому ловится
    последним -- и в этом проекте ловился уже трижды, каждый раз одинаково
    (кнопки обмена, шаг пакетов, действия над носителем).

    Лечится не сокращением подписей и не подбором ширин под один размер
    окна, а переносом: не поместившееся уходит на следующую строку. Тогда
    ряд верен при любой ширине, а не при той, под которую подбирали.
    """
    row = ttk.Frame(parent, style=style) if style else ttk.Frame(parent)
    row.pack(side=side, fill="x", pady=pady)
    widgets: list[ttk.Button] = []
    for index, (text, command) in enumerate(buttons):
        button_style = "Accent.TButton" if (accent_first and index == 0) else ""
        widgets.append(
            ttk.Button(row, text=text, command=command, style=button_style)
            if button_style
            else ttk.Button(row, text=text, command=command)
        )

    gap = theme.SPACE_SM
    placed_for = -1

    def _layout(_event: "tk.Event | None" = None) -> None:
        nonlocal placed_for
        available = row.winfo_width()
        # Раскладка только при изменившейся ширине: сама она меняет высоту
        # ряда, а это опять `<Configure>` -- и без этой проверки перекладка
        # вызывала бы себя без конца.
        if available == placed_for or available <= 1:
            return
        placed_for = available
        line = column = used = 0
        for button in widgets:
            needed = button.winfo_reqwidth() + gap
            if column and used + needed > available:
                line += 1
                column = used = 0
            button.grid(
                row=line, column=column, sticky="w",
                padx=(0, gap), pady=(gap if line else 0, 0),
            )
            used += needed
            column += 1

    row.bind("<Configure>", _layout, add="+")
    return row, widgets


def _is_disabled(widget: tk.Widget) -> bool:
    """Выключен ли виджет -- одинаково для ttk и обычного tk.

    Спрашивается по-разному: у ttk состояние это флаги (`state()`), у tk --
    опция (`cget("state")`), и у `Treeview` опции `state` нет вовсе -- запрос
    к ней падает с «unknown option "-state"».
    """
    try:
        return "disabled" in widget.state()  # type: ignore[attr-defined]
    except (AttributeError, tk.TclError):
        pass
    try:
        return str(widget.cget("state")) == "disabled"
    except tk.TclError:
        return False


class TablePlaceholder:
    """Объяснение поверх пустой таблицы.

    Пустая сетка со шапкой читается как экран, который не загрузился, а не
    как контейнер, в котором пока ничего нет. Раньше это лечили по-разному:
    на главном экране -- синей строкой внизу, на остальных никак, и человек
    смотрел на пустую таблицу, не понимая, сломалось что-то или так и надо.

    Объяснение стоит там, где смотрят -- в самой таблице, -- и говорит, что
    сделать, а не только что пусто.
    """

    def __init__(self, table: ttk.Treeview, text: str):
        self.label = ttk.Label(table, text=text, style="Hint.TLabel", justify="center")
        self._visible = False

    def show(self, text: str = "") -> None:
        if text:
            self.label.configure(text=text)
        if not self._visible:
            self.label.place(relx=0.5, rely=0.4, anchor="center")
            self._visible = True

    def hide(self) -> None:
        if self._visible:
            self.label.place_forget()
            self._visible = False

    def update(self, empty: bool, text: str = "") -> None:
        """`empty` -- пуста ли таблица; текст можно уточнить по обстоятельствам."""
        if empty:
            self.show(text)
        else:
            self.hide()


def section_card(parent: tk.Widget, grow: bool = True) -> ttk.Frame:
    """Белое поле раздела -- то, что стоит под заголовком на каждом экране.

    Пара к `section_header`. Разделять их незачем: заголовок на фоне окна и
    содержимое на белом поле -- это одна раскладка, и разъезжались они
    ровно тогда, когда экран набирал её сам.

    Внутри карточки обычные `ttk.Label` и `ttk.Frame` уже белые, поэтому
    содержимому не нужно называть стиль -- и лишний повод ошибиться пропадает.
    """
    card = ttk.Frame(parent, style="Card.TFrame", padding=theme.SPACE_MD)
    card.pack(
        side="top",
        # `grow=False` -- для экранов из одного поля: белое полотно во весь
        # рост под тремя строчками выглядит не спокойно, а недогруженным.
        fill="both" if grow else "x",
        expand=grow,
        padx=theme.SPACE_LG,
        pady=(0, theme.SPACE_LG),
    )
    return card


def section_header(parent: tk.Widget, title: str, hint: str = "") -> ttk.Frame:
    """Заголовок раздела: одинаковый на всех экранах.

    До 04.09.2026 каждый экран набирал его сам, и разница была видна ровно
    там, где её и замечают, -- при переходе между разделами: где-то отступ
    16 пикселей, где-то из шкалы темы; где-то подпись серая, где-то чёрная.

    Возвращается сам заголовок -- на случай, если экрану нужно добавить в ту
    же строку что-то своё.
    """
    header = ttk.Frame(parent, style="Window.TFrame")
    header.pack(side="top", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM))
    ttk.Label(header, text=title, style="Window.TLabel", font=theme.FONT_TITLE).pack(anchor="w")
    if hint:
        ttk.Label(
            header,
            text=hint,
            style="Window.TLabel",
            foreground=theme.TEXT_MUTED,
            # Перенос, а не обрезание: подпись объясняет раздел, и обрыв на
            # полуслове хуже её отсутствия.
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, 0))
    return header


@contextmanager
def error_guard(message_bar: MessageBar):
    """Wraps a block of backend calls so nothing ever reaches the user as a
    raw traceback -- the actual complaint from live RedOS testing was that
    the old TUI "тыкал в лицо ошибками"; this is the one place that rule is
    enforced, so every screen gets it just by using `with error_guard(...)`
    instead of repeating its own try/except ladder."""
    try:
        yield
    except KNOWN_ERRORS as exc:
        message_bar.error(str(exc))
    except Exception as exc:  # noqa: BLE001 -- deliberate last-resort net
        message_bar.error(f"Непредвиденная ошибка: {exc}")


def run_in_background(
    widget: tk.Widget,
    fn: Callable[[], object],
    on_success: Callable[[object], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    poll_ms: int = 80,
) -> None:
    """Runs `fn` in a worker thread and marshals its result back onto
    Tkinter's main thread via `widget.after`.

    Tkinter, like Textual, is not thread-safe -- no widget may be touched
    from a background thread. This is what a synchronous, on-thread
    `source.list_packages(...)` call did to the Textual TUI during live
    RedOS testing (froze/garbled the screen for as long as the fetch+parse
    of tens of thousands of packages took); every slow call in the GUI
    (device listing, repository browsing, the real install run) goes through
    this helper for exactly that reason.
    """
    # Collect now, on the UI thread, before another thread exists.
    #
    # A tk.Variable finalises by calling into Tcl. Python's cyclic collector
    # runs on whichever thread happens to trip the allocation threshold, so an
    # orphaned Variable left over from a destroyed screen can be finalised on a
    # *worker* thread -- which calls Tcl from the wrong thread and blocks
    # forever. Observed exactly that way: an install thread parked in
    # tkinter/__init__.py __del__ while the window kept running, so the install
    # neither finished nor failed.
    #
    # Sweeping here means the worker starts with nothing of Tk's left to
    # collect. _render_step and show_frame sweep for the same reason, right
    # after they destroy widgets.
    gc.collect()

    result_queue: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 -- forwarded to on_error, not swallowed
            result_queue.put(("error", exc))
        else:
            result_queue.put(("ok", value))

    def poll() -> None:
        # The user can navigate away while the work is still running, which
        # destroys the widgets the callbacks were going to update. Delivering
        # the result then raises TclError from inside the event loop, where it
        # surfaces as a traceback rather than as anything the user can act on.
        if not widget_alive(widget):
            return
        try:
            kind, payload = result_queue.get_nowait()
        except queue.Empty:
            widget.after(poll_ms, poll)
            return
        if kind == "ok":
            if on_success is not None:
                on_success(payload)
        else:
            if on_error is not None:
                on_error(payload)  # type: ignore[arg-type]

    threading.Thread(target=worker, daemon=True).start()
    widget.after(poll_ms, poll)


def publish_media_state(frame, media_id: str) -> None:
    """Сообщает ресурсу отзыва состояние носителя, не задерживая экран.

    Ничего не делает, пока контейнер не знаком с ресурсом: отзыв через сеть --
    возможность, а не обязанность, и организация, которая её не подключала, не
    должна видеть ошибок соединения после каждого действия.

    Неудача отправки не отменяет того, что уже записано в контейнере, и
    сообщается отдельной строкой: местное решение принято, а сервер узнает о
    нём при следующей попытке.
    """
    from ..admin import revocation_publish

    container = frame.app.session.container
    if not revocation_publish.is_connected(container):
        return

    def work():
        return revocation_publish.publish_record(container, media_id)

    def done(said) -> None:
        # Сохранение и при успехе, и при отказе: номер команды увеличился в
        # обоих случаях и обязан пережить закрытие программы.
        frame.app.save_container()
        if said:
            frame.message_bar.success(said)

    def failed(exc: BaseException) -> None:
        frame.app.save_container()
        frame.message_bar.error(
            f"{exc}. Запись в контейнере уже изменена — повторите отправку позже."
        )

    run_in_background(frame, work, done, failed)


def widget_alive(widget) -> bool:
    """True while the widget still exists.

    Needed wherever a background result lands on a widget the user may have
    navigated away from: a wizard step's widgets are destroyed when the step is
    left, and touching them afterwards raises TclError inside the event loop,
    where it becomes a traceback instead of anything actionable.
    """
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


class ScrollableArea(ttk.Frame):
    """A frame whose contents scroll vertically when they outgrow it.

    Exists because several wizard steps (the package picker, the review step
    with the configuration text) are taller than a modest screen, and without
    this the whole window simply grew until the navigation buttons sat below
    the bottom edge of the display with no way to reach them.

    Callers put their widgets into `.content`, not into the ScrollableArea
    itself.
    """

    def __init__(self, parent: tk.Widget, background: str = theme.SURFACE):
        super().__init__(parent, style="Card.TFrame")
        # The canvas draws its own background, and left at Tk's default grey it
        # showed as a rectangle behind the card the step is drawn on.
        self._canvas = tk.Canvas(
            self, highlightthickness=0, borderwidth=0, background=background
        )
        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)

        self.content = ttk.Frame(self._canvas, style="Card.TFrame")
        self._window = self._canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.content.bind("<Configure>", self._on_content_resize)
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        # Wheel events go to the widget under the pointer, which is usually a
        # label or an entry rather than the canvas, so they are bound while the
        # pointer is inside this area instead of on the canvas alone.
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)

    def _on_content_resize(self, _event) -> None:
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._sync_scrollbar()

    def _on_canvas_resize(self, event) -> None:
        # Keep the inner frame exactly as wide as the viewport, so wraplength
        # and fill="x" behave the way they would without the canvas.
        self._canvas.itemconfigure(self._window, width=event.width)
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        needed = self.content.winfo_reqheight() > self._canvas.winfo_height()
        if needed and not self._scrollbar.winfo_ismapped():
            self._scrollbar.pack(side="right", fill="y")
        elif not needed and self._scrollbar.winfo_ismapped():
            self._scrollbar.pack_forget()
            self._canvas.yview_moveto(0)

    def _bind_wheel(self, _event=None) -> None:
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Button-4>", self._on_wheel)
        self.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.unbind_all("<MouseWheel>")
        self.unbind_all("<Button-4>")
        self.unbind_all("<Button-5>")

    def _on_wheel(self, event) -> None:
        if not self._scrollbar.winfo_ismapped():
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            # Windows reports multiples of 120; X11/macOS report small numbers.
            delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta, "units")


class UiMailbox:
    """Carries updates from a worker thread to the UI thread.

    Not `widget.after(...)` called from the worker: `after` is itself a Tcl
    call, and Tkinter is not thread-safe, so scheduling from another thread
    corrupts the interpreter -- observed as "invalid command name ...poll"
    followed by the event loop dropping callbacks. A queue plus polling from
    the main thread is the same pattern `run_in_background` already uses.
    """

    def __init__(self, widget: tk.Widget, deliver: Callable[..., None], poll_ms: int = 100):
        self._widget = widget
        self._deliver = deliver
        self._poll_ms = poll_ms
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._running = False

    def post(self, *args) -> None:
        """Safe to call from any thread."""
        self._queue.put(args)

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._drain()

    def stop(self) -> None:
        self._running = False

    def _drain(self) -> None:
        if not widget_alive(self._widget):
            self._running = False
            return
        while True:
            try:
                args = self._queue.get_nowait()
            except queue.Empty:
                break
            self._deliver(*args)
        if self._running:
            self._widget.after(self._poll_ms, self._drain)


def add_field(
    parent: tk.Widget,
    row: int,
    label_text: str,
    var: tk.Variable,
    hint: str | None = None,
    show: str | None = None,
    width: int = 34,
    label_style: str = "Card.TLabel",
    hint_style: str = "Hint.TLabel",
) -> tuple[ttk.Entry, int]:
    """Places one label+entry(+hint) pair on `parent`'s grid starting at
    `row`; returns the entry and the next free row so callers can chain
    several fields without hand-tracking row numbers."""
    # sticky="e" rather than "ne": the label sits on the same baseline as the
    # field it names. Anchored to the top it floated above the entry, which
    # read as a caption for the group rather than a name for the field.
    ttk.Label(parent, text=label_text, style=label_style).grid(
        row=row, column=0, sticky="e", padx=(0, theme.SPACE_MD), pady=theme.SPACE_XS
    )
    entry = ttk.Entry(parent, textvariable=var, show=show or "", width=width)
    entry.grid(row=row, column=1, sticky="w", pady=theme.SPACE_XS)
    next_row = row + 1
    if hint:
        ttk.Label(parent, text=hint, style=hint_style, wraplength=theme.HINT_WRAP, justify="left").grid(
            row=next_row, column=1, sticky="w", pady=(0, theme.SPACE_XS)
        )
        next_row += 1
    return entry, next_row


def _wheel_scrolls_the_form(combo: ttk.Combobox) -> None:
    """Колесо над списком прокручивает форму, а не меняет выбранное значение.

    ttk::combobox по умолчанию листает колесом свои значения, а прокрутка
    формы висит на `bind_all`, то есть на теге `all` -- он в цепочке
    последний. Поэтому на длинной форме колесо сперва меняло настройку, над
    которой прошёл курсор, и только потом прокручивало страницу.

    Найдено живым прогоном 28.08.2026: при листании шага «Диск и шифрование»
    шифр LUKS молча съехал с aes-xts-plain64 на serpent-xts-plain64, и
    увидеть это удалось лишь на сводке последнего шага. Цена ошибки -- том,
    зашифрованный не тем алгоритмом, который выбирал администратор.

    Обработчик висит на самом виджете, а привязка виджета срабатывает раньше
    классовой: "break" не даёт списку увидеть событие вовсе. Прокрутку при
    этом делаем сами, поднявшись до ближайшего предка, который умеет
    прокручиваться, -- иначе форма перестала бы листаться над каждым списком.
    """
    def handler(event):
        holder = combo.master
        while holder is not None and not hasattr(holder, "_on_wheel"):
            holder = getattr(holder, "master", None)
        if holder is not None:
            holder._on_wheel(event)
        return "break"

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        combo.bind(sequence, handler)


def add_choice(
    parent: tk.Widget,
    row: int,
    label_text: str,
    var: tk.Variable,
    values,
    hint: str | None = None,
    width: int = 32,
    editable: bool = False,
    on_change=None,
    label_style: str = "Card.TLabel",
    hint_style: str = "Hint.TLabel",
) -> tuple[ttk.Combobox, int]:
    """Like `add_field`, but the value is picked from a list.

    `editable=True` still allows typing something that is not on the list --
    used where the list covers the realistic cases but cannot be exhaustive
    (distribution releases, cipher key sizes), so an unusual but valid value
    is not made impossible by the UI.
    """
    ttk.Label(parent, text=label_text, style=label_style).grid(
        row=row, column=0, sticky="e", padx=(0, theme.SPACE_MD), pady=theme.SPACE_XS
    )
    combo = ttk.Combobox(
        parent,
        textvariable=var,
        values=list(values),
        width=width,
        state="normal" if editable else "readonly",
    )
    combo.grid(row=row, column=1, sticky="w", pady=theme.SPACE_XS)
    _wheel_scrolls_the_form(combo)
    if on_change is not None:
        combo.bind("<<ComboboxSelected>>", lambda _event: on_change())
    next_row = row + 1
    if hint:
        ttk.Label(parent, text=hint, style=hint_style, wraplength=theme.HINT_WRAP, justify="left").grid(
            row=next_row, column=1, sticky="w", pady=(0, theme.SPACE_XS)
        )
        next_row += 1
    return combo, next_row


class _StepRail(ttk.Frame):
    """The list of steps down the left-hand side.

    Answers three questions a progress bar cannot: which step is this, what has
    been done, and what is still coming. The last one is the reason it exists
    -- a wizard that only says "step 3 of 7" makes the user click forward to
    find out whether the thing they are looking for is in it.

    Drawn with plain tk widgets rather than ttk: each row needs its own
    background colour to show state, and ttk would need a named style per
    state per row.
    """

    #: Wide enough for the longest step name at the wrapping width below.
    WIDTH = 220

    def __init__(self, parent: tk.Widget, step_titles: list[str]):
        super().__init__(parent, width=self.WIDTH, style="Window.TFrame")
        # Without this the frame collapses to whatever the labels need, and the
        # content column shifts sideways as the user moves between steps.
        self.pack_propagate(False)
        self._rows: list[tuple[tk.Frame, tk.Label, tk.Label]] = []
        self.set_titles(step_titles)

    def set_titles(self, step_titles: list[str]) -> None:
        """Перерисовывает список шагов.

        Нужно затем, что переключатель режима меняет сам состав шагов: в
        упрощённом их меньше. Рельс, оставшийся от прежнего режима, показывал
        бы шаги, которых больше нет.
        """
        for row, _marker, _label in self._rows:
            row.destroy()
        self._rows = []

        for index, title in enumerate(step_titles):
            row = tk.Frame(self, background=theme.BACKGROUND)
            row.pack(fill="x", pady=(0, 2))
            marker = tk.Label(
                row,
                text=str(index + 1),
                width=2,
                background=theme.BACKGROUND,
                foreground=theme.TEXT_MUTED,
                font=theme.FONT_SMALL,
            )
            marker.pack(side="left", padx=(theme.SPACE_SM, theme.SPACE_SM), pady=theme.SPACE_SM)
            label = tk.Label(
                row,
                text=title,
                background=theme.BACKGROUND,
                foreground=theme.TEXT_MUTED,
                font=theme.FONT_BODY,
                anchor="w",
                justify="left",
                wraplength=self.WIDTH - 56,
            )
            label.pack(side="left", fill="x", expand=True, pady=theme.SPACE_SM)
            self._rows.append((row, marker, label))

    def set_current(self, current: int) -> None:
        for index, (row, marker, label) in enumerate(self._rows):
            if index < current:
                # Done. The tick is the whole point: it distinguishes "behind
                # me" from "ahead of me", which grey alone does not.
                background, foreground, text = theme.BACKGROUND, theme.SUCCESS, "✓"
                font = theme.FONT_BODY
            elif index == current:
                background, foreground, text = theme.SURFACE, theme.ACCENT, str(index + 1)
                font = theme.FONT_SECTION
            else:
                background, foreground, text = theme.BACKGROUND, theme.TEXT_DISABLED, str(index + 1)
                font = theme.FONT_BODY
            row.configure(background=background)
            marker.configure(background=background, foreground=foreground, text=text)
            label.configure(
                background=background,
                foreground=theme.TEXT if index == current else foreground,
                font=font,
            )


class WizardFrame(ttk.Frame):
    """Base class for every guided, multi-step screen.

    Subclasses declare `step_titles` (and optionally `step_hints`) in
    `__init__` before calling `super().__init__`, then implement
    `build_step(index, body)`, `validate_step(index) -> bool` (return False
    and call `self.message_bar.error(...)` to block advancing), and
    `on_finish()`. `on_cancel()` may be overridden to navigate elsewhere;
    the default just calls it once with no extra effect on the frame itself.

    Only the fields belonging to the *current* step are ever validated, so a
    mistake on step 2 doesn't hide behind five other unrelated fields the
    user hasn't reached yet -- the concrete complaint about the old
    single-page forms.
    """

    #: Умеет ли этот мастер два объёма вопросов. Подклассу достаточно
    #: поставить True и переопределить `step_plan()`.
    supports_modes = False

    #: Под каким именем режим запоминается между запусками.
    mode_preference_key = "wizard_expert_mode"

    def __init__(
        self,
        parent: tk.Widget,
        app,
        step_titles: list[str],
        step_hints: list[str] | None = None,
        finish_label: str = "Готово",
    ):
        super().__init__(parent, style="Window.TFrame")
        self.app = app
        self.step_titles = step_titles
        self.step_hints = step_hints or [""] * len(step_titles)
        self.finish_label = finish_label
        self.step_index = 0
        # Режим запоминается: тот, кто работает в экспертном, работает в нём
        # постоянно, и переключать его при каждом запуске означало бы не давать
        # выбора, а требовать его заново.
        self.expert_mode = bool(preferences.get(self.mode_preference_key, False))

        # Layout, outermost first:
        #
        #   +----------------------------------------------------+
        #   | rail  |  card: title, hint, ------------- separator |
        #   | 1 ... |        the step's own widgets (scrolls)     |
        #   | 2 ... |                                             |
        #   +----------------------------------------------------+
        #   | message bar                                        |
        #   | Отмена            < Назад  [ Далее > ]             |
        #   +----------------------------------------------------+
        #
        # The rail replaced a bare progress bar. A bar says how far along the
        # user is; it does not say what the remaining steps are, so "can I
        # finish this now, or is there a repository to connect to first?" had
        # no answer until they got there.
        #
        # Footer and message bar are packed before the body on purpose. Tk's
        # packer serves whoever asks first, so a step too tall for the window
        # scrolls inside its own area instead of pushing the navigation buttons
        # off the bottom edge, where they became unreachable.
        self._footer = ttk.Frame(self, style="Window.TFrame")
        self._footer.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_LG))
        # Leaving, back, forward -- and only the one that continues the work is
        # accented. Three equally weighted buttons make the user read all three
        # every time.
        self.cancel_btn = ttk.Button(self._footer, text="Отмена", style="Quiet.TButton", command=self._cancel)
        self.cancel_btn.pack(side="left")
        self.next_btn = ttk.Button(self._footer, text="Далее", style="Accent.TButton", command=self._go_next)
        self.next_btn.pack(side="right")
        self.back_btn = ttk.Button(self._footer, text="Назад", command=self._go_back)
        self.back_btn.pack(side="right", padx=(0, theme.SPACE_SM))
        # Tab внутри подвала идёт слева направо, как читают: «Отмена», «Назад»,
        # «Далее». Порядок обхода в Tk -- это порядок виджетов у родителя, а
        # не расположение на экране, и «Далее» создаётся раньше «Назад», чтобы
        # упаковаться правее. Поднятие возвращает его в конец.
        self.next_btn.lift()

        self.message_bar = MessageBar(self)
        self.message_bar.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_SM))

        columns = ttk.Frame(self, style="Window.TFrame")
        columns.pack(side="top", fill="both", expand=True, padx=theme.SPACE_LG, pady=theme.SPACE_LG)

        self._rail = _StepRail(columns, step_titles)
        self._rail.pack(side="left", fill="y", padx=(0, theme.SPACE_LG))

        card = ttk.Frame(columns, style="Card.TFrame")
        card.pack(side="left", fill="both", expand=True)

        header = ttk.Frame(card, style="Card.TFrame")
        header.pack(side="top", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM))
        self.step_title_var = tk.StringVar()
        title_row = ttk.Frame(header, style="Card.TFrame")
        title_row.pack(fill="x")
        ttk.Label(title_row, textvariable=self.step_title_var, style="Title.TLabel").pack(side="left")
        if self.supports_modes:
            # Переключатель стоит в шапке шага, а не в отдельном окне
            # настроек: он меняет то, что видно прямо сейчас, и место рядом с
            # заголовком говорит об этом без подписи.
            self._mode_var = tk.BooleanVar(value=self.expert_mode)
            self._mode_toggle = ttk.Checkbutton(
                title_row,
                text="Экспертный режим",
                variable=self._mode_var,
                style="Card.TCheckbutton",
                command=self._on_mode_toggled,
            )
            self._mode_toggle.pack(side="right")
        self.hint_var = tk.StringVar()
        ttk.Label(
            header,
            textvariable=self.hint_var,
            style="Muted.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, 0))
        ttk.Separator(card, orient="horizontal").pack(fill="x", padx=theme.SPACE_LG)

        self._scroller = ScrollableArea(card)
        self._scroller.pack(
            side="top", fill="both", expand=True, padx=theme.SPACE_LG, pady=theme.SPACE_MD
        )
        self.body = self._scroller.content

        # Подвал создан раньше содержимого -- иначе слишком высокий шаг
        # выталкивал бы кнопки за нижний край, -- и по обходу Tab он оказывался
        # впереди: первым, что получал фокус в мастере, была кнопка «Отмена», а
        # поля, ради которых шаг существует, -- последними. Поднятие ставит
        # навигацию туда, где её ждут: после содержимого шага.
        self._footer.lift()

        self.step_keys = [str(i) for i in range(len(self.step_titles))]
        if self.supports_modes:
            self._apply_step_plan()
        self._render_step()

    # -- subclasses implement these ---------------------------------
    def build_step(self, index: int, body: ttk.Frame) -> None:
        raise NotImplementedError

    def validate_step(self, index: int) -> bool:
        return True

    def on_finish(self) -> None:
        raise NotImplementedError

    def on_cancel(self) -> None:
        raise NotImplementedError

    def save_step(self, index: int) -> None:
        """Optional hook: persist widget values from the current step into
        backing state before navigating away, forward or backward, since the
        step's widgets are destroyed either way. Default no-op; only
        validate_step is required to persist on the forward path if a
        subclass doesn't override this."""
        return

    # -- wizard chrome -------------------------------------------------
    def _render_step(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        # Finalise what those widgets left behind while still on the UI thread
        # -- see the note in run_in_background about tk.Variable.__del__.
        gc.collect()
        self.message_bar.clear()
        total = len(self.step_titles)
        # Just the name: which number it is, and how many remain, is what the
        # rail beside it shows. Repeating "Шаг 6 из 7" here spent the most
        # prominent line on the screen restating the navigation.
        self.step_title_var.set(self.step_titles[self.step_index])
        self.hint_var.set(self.step_hints[self.step_index])
        self._rail.set_current(self.step_index)
        self.build_step(self.step_index, self.body)
        self.back_btn.configure(state=("normal" if self.step_index > 0 else "disabled"))
        is_last = self.step_index == total - 1
        self.next_btn.configure(text=(self.finish_label if is_last else "Далее"))
        self._focus_safely()

    def _focus_safely(self) -> None:
        """Фокус при открытии шага -- на его содержимом, а не на кнопке.

        Пробелом и Enter мастер пролистывают, и это делает важным, на чём
        стоит фокус в первый момент. На кнопке подвала он означает, что
        случайное нажатие уводит с шага, а на последнем шаге -- запускает
        работу; на поле он не делает ничего.

        Поэтому берётся первый принимающий фокус виджет содержимого. Если шаг
        состоит из одних кнопок, фокус идёт на «Назад»: это единственное
        действие мастера, которое ничего не портит и всегда обратимо. Шаг,
        который сам знает, куда поставить фокус (поле повтора пути перед
        стиранием), делает это в `build_step` и перекрывает выбор здесь --
        своё знание точнее общего правила.
        """
        # Шаг, который сам знает, куда поставить фокус, уже сделал это в
        # `build_step`, и его знание точнее общего правила: на шаге стирания
        # это поле повтора пути, а не первое поле сверху. Проверяется по
        # тому, где фокус оказался, а не по флагу, который шаг мог бы забыть
        # выставить.
        current = self.focus_get()
        if current is not None and str(current).startswith(str(self.body)):
            return
        target = self._first_focusable(self.body)
        if target is None:
            target = self.back_btn if self.step_index > 0 else self.cancel_btn
        target.focus_set()

    @staticmethod
    def _first_focusable(parent: tk.Widget) -> "tk.Widget | None":
        """Первый виджет содержимого, который вообще берёт фокус.

        Обход в порядке размещения, вглубь: `takefocus` у ttk-виджетов бывает
        пустой строкой -- «решает сам виджет», -- поэтому отдельно
        перечислены те классы, которые принимают ввод.
        """
        takes = ("TEntry", "TCombobox", "TSpinbox", "TCheckbutton", "TRadiobutton", "Treeview", "Text")
        for child in parent.winfo_children():
            if child.winfo_class() in takes and not _is_disabled(child):
                return child
            found = WizardFrame._first_focusable(child)
            if found is not None:
                return found
        return None

    def _on_mode_toggled(self) -> None:
        """Переключение объёма вопросов без потери введённого.

        Значения текущего шага сохраняются перед пересборкой -- иначе
        переключатель стирал бы то, что администратор только что набрал, и
        пользоваться им было бы страшно.

        Шаг после пересборки остаётся тем же по смыслу, а не по номеру: в
        упрощённом режиме шагов меньше, и номер указывал бы уже на другой.
        """
        self.save_step(self.step_index)
        current_key = self.step_keys[self.step_index] if self.step_keys else ""
        self.expert_mode = bool(self._mode_var.get())
        preferences.set_value(self.mode_preference_key, self.expert_mode)
        self._apply_step_plan()
        if current_key in self.step_keys:
            self.step_index = self.step_keys.index(current_key)
        else:
            # Шаг был экспертным и исчез: возвращаемся на предыдущий по
            # порядку, а не на первый -- начинать сначала из-за переключателя
            # никто не просил.
            self.step_index = min(self.step_index, len(self.step_keys) - 1)
        self._render_step()

    def _apply_step_plan(self) -> None:
        """Пересобирает список шагов под текущий режим."""
        plan = self.step_plan()
        if not plan:
            return
        self.step_keys = [key for key, _title, _hint in plan]
        self.step_titles = [title for _key, title, _hint in plan]
        self.step_hints = [hint for _key, _title, hint in plan]
        self._rail.set_titles(self.step_titles)

    def step_plan(self) -> "list[tuple[str, str, str]]":
        """(ключ, заголовок, подсказка) для каждого шага текущего режима.

        Пустой список означает «мастер не умеет режимов»: тогда работают
        `step_titles`, переданные в конструктор.
        """
        return []

    def _go_back(self) -> None:
        if self.step_index == 0:
            return
        self.save_step(self.step_index)
        self.step_index -= 1
        self._render_step()

    def _go_next(self) -> None:
        self.save_step(self.step_index)
        if not self.validate_step(self.step_index):
            return
        if self.step_index == len(self.step_titles) - 1:
            self.on_finish()
            return
        self.step_index += 1
        self._render_step()

    def _cancel(self) -> None:
        self.on_cancel()
