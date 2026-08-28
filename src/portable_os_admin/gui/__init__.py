"""Tkinter desktop GUI: the guided, wizard-driven interface.

Added after live RedOS testing of the Textual TUI (see README, раздел «Архитектура»
"Известные ограничения") surfaced that raw, single-page forms with
after-the-fact validation errors were confusing to operate, and that a
blocking network call could make the whole interface appear to hang. This
package is a second, independent presentation layer on top of the exact same
business logic used by `portable_os_admin.tui` (container/session/config/
install/recovery/admin) -- nothing under those packages changed to support
it, so nothing here can regress TUI behavior.

Every screen in `gui.screens` is one step or one guided multi-step wizard,
built on the primitives in `gui.widgets`: a plain-language MessageBar instead
of Rich markup, and `run_in_background` so slow calls (device listing,
repository fetches, the real install run) never block Tkinter's event loop
the way the equivalent TUI calls once did.
"""
