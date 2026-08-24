"""Small reusable building blocks shared by every GUI screen.

Kept deliberately minimal (no third-party widget toolkit) so the GUI adds no
new dependency beyond the stdlib `tkinter` -- see docs/ARCHITECTURE.md
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
from typing import Callable

from ..config.validator import ConfigValidationUnavailableError
from ..errors import PortableOSAdminError
from . import theme

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
            self, textvariable=self.var, style=self._label_style, wraplength=860, justify="left"
        )
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
        ttk.Label(parent, text=hint, style=hint_style, wraplength=theme.TEXT_WRAP, justify="left").grid(
            row=next_row, column=1, sticky="w", pady=(0, theme.SPACE_XS)
        )
        next_row += 1
    return entry, next_row


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
    if on_change is not None:
        combo.bind("<<ComboboxSelected>>", lambda _event: on_change())
    next_row = row + 1
    if hint:
        ttk.Label(parent, text=hint, style=hint_style, wraplength=theme.TEXT_WRAP, justify="left").grid(
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
        footer = ttk.Frame(self, style="Window.TFrame")
        footer.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_LG))
        # Leaving, back, forward -- and only the one that continues the work is
        # accented. Three equally weighted buttons make the user read all three
        # every time.
        self.cancel_btn = ttk.Button(footer, text="Отмена", style="Quiet.TButton", command=self._cancel)
        self.cancel_btn.pack(side="left")
        self.next_btn = ttk.Button(footer, text="Далее", style="Accent.TButton", command=self._go_next)
        self.next_btn.pack(side="right")
        self.back_btn = ttk.Button(footer, text="Назад", command=self._go_back)
        self.back_btn.pack(side="right", padx=(0, theme.SPACE_SM))

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
        ttk.Label(header, textvariable=self.step_title_var, style="Title.TLabel").pack(anchor="w")
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
