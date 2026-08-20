"""Small reusable building blocks shared by every GUI screen.

Kept deliberately minimal (no third-party widget toolkit) so the GUI adds no
new dependency beyond the stdlib `tkinter` -- see docs/ARCHITECTURE.md
"Упаковка" for why that matters: it means the existing deb/rpm packaging only
needs a system `python3-tk` dependency, not another pip package pulled into
the private venv at install time.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from contextlib import contextmanager
from tkinter import ttk
from typing import Callable

from ..config.validator import ConfigValidationUnavailableError
from ..errors import PortableOSAdminError

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

    COLORS = {"info": "#2b5fad", "success": "#1a7f37", "error": "#c0362c"}

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self.var = tk.StringVar()
        self.label = ttk.Label(self, textvariable=self.var, wraplength=760, justify="left")
        self.label.pack(anchor="w", pady=(4, 4))

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

    def __init__(self, parent: tk.Widget):
        super().__init__(parent)
        self._canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self._scrollbar = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)

        self.content = ttk.Frame(self._canvas)
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
) -> tuple[ttk.Entry, int]:
    """Places one label+entry(+hint) pair on `parent`'s grid starting at
    `row`; returns the entry and the next free row so callers can chain
    several fields without hand-tracking row numbers."""
    ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="ne", padx=(0, 10), pady=4)
    entry = ttk.Entry(parent, textvariable=var, show=show or "", width=width)
    entry.grid(row=row, column=1, sticky="w", pady=4)
    next_row = row + 1
    if hint:
        ttk.Label(parent, text=hint, foreground="#777777", font=("TkDefaultFont", 9)).grid(
            row=next_row, column=1, sticky="w"
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
) -> tuple[ttk.Combobox, int]:
    """Like `add_field`, but the value is picked from a list.

    `editable=True` still allows typing something that is not on the list --
    used where the list covers the realistic cases but cannot be exhaustive
    (distribution releases, cipher key sizes), so an unusual but valid value
    is not made impossible by the UI.
    """
    ttk.Label(parent, text=label_text).grid(row=row, column=0, sticky="ne", padx=(0, 10), pady=4)
    combo = ttk.Combobox(
        parent,
        textvariable=var,
        values=list(values),
        width=width,
        state="normal" if editable else "readonly",
    )
    combo.grid(row=row, column=1, sticky="w", pady=4)
    if on_change is not None:
        combo.bind("<<ComboboxSelected>>", lambda _event: on_change())
    next_row = row + 1
    if hint:
        ttk.Label(parent, text=hint, foreground="#777777", font=("TkDefaultFont", 9), wraplength=520,
                  justify="left").grid(row=next_row, column=1, sticky="w")
        next_row += 1
    return combo, next_row


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
        super().__init__(parent)
        self.app = app
        self.step_titles = step_titles
        self.step_hints = step_hints or [""] * len(step_titles)
        self.finish_label = finish_label
        self.step_index = 0

        # Three layers, packed in this order on purpose. Tk's packer hands out
        # space to whatever was packed first, so the footer and the message bar
        # claim theirs before the body does. A step tall enough to overflow the
        # screen then scrolls inside its own area instead of pushing the
        # navigation buttons past the bottom edge, where they became
        # unreachable.
        footer = ttk.Frame(self)
        footer.pack(side="bottom", fill="x", padx=16, pady=16)
        self.cancel_btn = ttk.Button(footer, text="Отмена", command=self._cancel)
        self.cancel_btn.pack(side="left")
        self.back_btn = ttk.Button(footer, text="< Назад", command=self._go_back)
        self.back_btn.pack(side="left", padx=(8, 0))
        self.next_btn = ttk.Button(footer, text="Далее >", command=self._go_next)
        self.next_btn.pack(side="right")

        self.message_bar = MessageBar(self)
        self.message_bar.pack(side="bottom", fill="x", padx=16)

        header = ttk.Frame(self)
        header.pack(side="top", fill="x", padx=16, pady=(16, 4))
        self.step_title_var = tk.StringVar()
        ttk.Label(header, textvariable=self.step_title_var, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        self.hint_var = tk.StringVar()
        ttk.Label(header, textvariable=self.hint_var, foreground="#555555", wraplength=760, justify="left").pack(
            anchor="w", pady=(2, 0)
        )
        self.progress = ttk.Progressbar(header, maximum=max(len(step_titles) - 1, 1))
        self.progress.pack(fill="x", pady=(8, 0))

        self._scroller = ScrollableArea(self)
        self._scroller.pack(side="top", fill="both", expand=True, padx=16, pady=8)
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
        self.message_bar.clear()
        total = len(self.step_titles)
        self.step_title_var.set(f"Шаг {self.step_index + 1} из {total}: {self.step_titles[self.step_index]}")
        self.hint_var.set(self.step_hints[self.step_index])
        self.progress.configure(value=self.step_index)
        self.build_step(self.step_index, self.body)
        self.back_btn.configure(state=("normal" if self.step_index > 0 else "disabled"))
        is_last = self.step_index == total - 1
        self.next_btn.configure(text=(self.finish_label if is_last else "Далее >"))

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
