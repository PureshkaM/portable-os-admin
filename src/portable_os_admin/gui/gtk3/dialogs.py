"""Модальные диалоги администратора на GTK 3."""
from __future__ import annotations

import os
from types import SimpleNamespace

from gi.repository import Gtk


def _parent_window(parent):
    widget = parent if isinstance(parent, Gtk.Widget) else getattr(parent, "widget", None)
    if isinstance(widget, Gtk.Widget):
        window = widget.get_toplevel()
        if isinstance(window, Gtk.Window):
            return window
    return None


def _message(title, message, kind, buttons, parent, default):
    window = _parent_window(parent)
    dialog = Gtk.MessageDialog(transient_for=window, modal=True, destroy_with_parent=True,
                               message_type=kind, buttons=buttons, text=title)
    dialog.set_title(title)
    dialog.format_secondary_text(message)
    dialog.set_default_response(default)
    try:
        return dialog.run()
    finally:
        dialog.destroy()


def askyesno(title, message, *, parent=None, icon="question", default="no"):
    kind = {"warning": Gtk.MessageType.WARNING, "error": Gtk.MessageType.ERROR,
            "info": Gtk.MessageType.INFO}.get(icon, Gtk.MessageType.QUESTION)
    selected = Gtk.ResponseType.YES if default == "yes" else Gtk.ResponseType.NO
    return _message(title, message, kind, Gtk.ButtonsType.YES_NO, parent, selected) == Gtk.ResponseType.YES


def showinfo(title, message, *, parent=None):
    _message(title, message, Gtk.MessageType.INFO, Gtk.ButtonsType.OK, parent, Gtk.ResponseType.OK)
    return "ok"


def showerror(title, message, *, parent=None):
    _message(title, message, Gtk.MessageType.ERROR, Gtk.ButtonsType.OK, parent, Gtk.ResponseType.OK)
    return "ok"


def _choose(action, *, parent=None, title="", filetypes=(), initialdir=None, initialfile=None,
            defaultextension="", multiple=False):
    window = _parent_window(parent)
    dialog = Gtk.FileChooserDialog(title=title, transient_for=window, action=action)
    dialog.set_modal(True)
    dialog.set_destroy_with_parent(True)
    dialog.set_local_only(True)
    dialog.set_select_multiple(multiple)
    dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
    saving = action == Gtk.FileChooserAction.SAVE
    accept_label = "Сохранить" if saving else "Выбрать" if action == Gtk.FileChooserAction.SELECT_FOLDER else "Открыть"
    dialog.add_button(accept_label, Gtk.ResponseType.ACCEPT)
    dialog.set_default_response(Gtk.ResponseType.ACCEPT)
    if initialdir:
        dialog.set_current_folder(os.fspath(initialdir))
    if initialfile and saving:
        dialog.set_current_name(os.fspath(initialfile))
    for name, patterns in filetypes:
        filter_ = Gtk.FileFilter()
        filter_.set_name(name)
        for pattern in patterns.split() if isinstance(patterns, str) else patterns:
            filter_.add_pattern(pattern)
        dialog.add_filter(filter_)
    try:
        while dialog.run() == Gtk.ResponseType.ACCEPT:
            if multiple:
                return tuple(dialog.get_filenames())
            path = dialog.get_filename()
            if not path:
                continue
            if saving:
                if defaultextension and not os.path.splitext(path)[1]:
                    path += defaultextension if defaultextension.startswith(".") else "." + defaultextension
                # Проверяется окончательное имя, включая расширение из параметров диалога.
                if os.path.lexists(path) and not askyesno(
                    "Заменить файл?", f"Файл «{os.path.basename(path)}» уже существует.",
                    parent=dialog, icon="warning",
                ):
                    continue
            return path
        return () if multiple else ""
    finally:
        dialog.destroy()


def askopenfilename(**options):
    return _choose(Gtk.FileChooserAction.OPEN, **options)


def askopenfilenames(**options):
    return _choose(Gtk.FileChooserAction.OPEN, multiple=True, **options)


def asksaveasfilename(**options):
    return _choose(Gtk.FileChooserAction.SAVE, **options)


def askdirectory(**options):
    return _choose(Gtk.FileChooserAction.SELECT_FOLDER, **options)


def _ask(title, prompt, convert, parent, initialvalue):
    dialog = Gtk.Dialog(title=title, transient_for=_parent_window(parent),
                        modal=True, destroy_with_parent=True)
    dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
    dialog.add_button("OK", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    content = dialog.get_content_area()
    content.set_spacing(8)
    content.set_border_width(12)
    label = Gtk.Label(label=prompt, xalign=0, wrap=True, max_width_chars=65)
    content.pack_start(label, False, False, 0)
    entry = Gtk.Entry(activates_default=True)
    entry.set_text("" if initialvalue is None else str(initialvalue))
    content.pack_start(entry, False, False, 0)
    error = Gtk.Label(xalign=0, wrap=True)
    error.get_style_context().add_class("error")
    content.pack_start(error, False, False, 0)
    content.show_all()
    error.hide()
    entry.grab_focus()
    entry.select_region(0, -1)
    try:
        while dialog.run() == Gtk.ResponseType.OK:
            try:
                return convert(entry.get_text())
            except ValueError as exc:
                error.set_text(str(exc))
                error.show()
                entry.grab_focus()
        return None
    finally:
        dialog.destroy()


def askstring(title, prompt, *, parent=None, initialvalue=None):
    return _ask(title, prompt, str, parent, initialvalue)


def askinteger(title, prompt, *, parent=None, initialvalue=None, minvalue=None, maxvalue=None):
    def convert(text):
        try:
            result = int(text)
        except ValueError as exc:
            raise ValueError("Введите целое число.") from exc
        if minvalue is not None and result < minvalue:
            raise ValueError(f"Введите число не меньше {minvalue}.")
        if maxvalue is not None and result > maxvalue:
            raise ValueError(f"Введите число не больше {maxvalue}.")
        return result

    return _ask(title, prompt, convert, parent, initialvalue)


filedialog = SimpleNamespace(askopenfilename=askopenfilename, askopenfilenames=askopenfilenames,
                             asksaveasfilename=asksaveasfilename, askdirectory=askdirectory)
simpledialog = SimpleNamespace(askstring=askstring, askinteger=askinteger)
messagebox = SimpleNamespace(askyesno=askyesno, showinfo=showinfo, showerror=showerror)
