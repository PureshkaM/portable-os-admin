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

        header = ttk.Frame(self)
        header.pack(fill="x", padx=16, pady=(16, 4))
        self.step_title_var = tk.StringVar()
        ttk.Label(header, textvariable=self.step_title_var, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        self.hint_var = tk.StringVar()
        ttk.Label(header, textvariable=self.hint_var, foreground="#555555", wraplength=760, justify="left").pack(
            anchor="w", pady=(2, 0)
        )
        self.progress = ttk.Progressbar(header, maximum=max(len(step_titles) - 1, 1))
        self.progress.pack(fill="x", pady=(8, 0))

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=16, pady=8)

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16)

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=16, pady=16)
        self.cancel_btn = ttk.Button(footer, text="Отмена", command=self._cancel)
        self.cancel_btn.pack(side="left")
        self.back_btn = ttk.Button(footer, text="< Назад", command=self._go_back)
        self.back_btn.pack(side="left", padx=(8, 0))
        self.next_btn = ttk.Button(footer, text="Далее >", command=self._go_next)
        self.next_btn.pack(side="right")

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
