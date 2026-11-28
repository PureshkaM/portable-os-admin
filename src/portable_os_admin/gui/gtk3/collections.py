"""Виджеты таблиц, списков и многострочного ввода в GTK 3."""
from __future__ import annotations

from itertools import count

from .base import TclError, Widget
from gi.repository import Gdk, GObject, Gtk, Pango


class _Scrolled(Widget):
    def __init__(self, master, view, *, overlay=False, **options):
        self.view = view
        self._scrolled = Gtk.ScrolledWindow()
        self._scrolled.set_propagate_natural_height(False)
        self._scrolled.set_propagate_natural_width(False)
        self._adjustments = {"x": self._scrolled.get_hadjustment(),
                             "y": self._scrolled.get_vadjustment()}
        self._scroll_callbacks = {}
        if view is not None:
            self._event_widget = view
            self._scrolled.add(view)
            view.show()
        widget = self._scrolled
        if overlay:
            self._overlay = widget = Gtk.Overlay()
            widget.add(self._scrolled)
            self._scrolled.show()
        super().__init__(master, widget=widget, **options)
        for axis, adjustment in self._adjustments.items():
            self._connect_native(adjustment, "changed", self._scroll_changed, axis)
            self._connect_native(adjustment, "value-changed", self._scroll_changed, axis)

    def _configure_option(self, name, value):
        if name not in ("xscrollcommand", "yscrollcommand"):
            return super()._configure_option(name, value)
        axis = name[0]
        target = getattr(getattr(value, "__self__", None), "widget", None)
        if isinstance(target, Gtk.Scrollbar):
            target.set_adjustment(self._adjustments[axis])
        self._scroll_callbacks[axis] = None if isinstance(target, Gtk.Scrollbar) else value
        policies = list(self._scrolled.get_policy())
        policies[0 if axis == "x" else 1] = Gtk.PolicyType.EXTERNAL
        self._scrolled.set_policy(*policies)
        self._scroll_changed(self._adjustments[axis], axis)
        return True

    def _scroll_changed(self, _adjustment, axis):
        callback = self._scroll_callbacks.get(axis)
        if callback:
            callback(*self._view(axis))

    def _view(self, axis, operation=None, amount=0, unit="units"):
        adjustment = self._adjustments[axis]
        lower, upper = adjustment.get_lower(), adjustment.get_upper()
        span = upper - lower
        offset = adjustment.get_value() - lower
        page = adjustment.get_page_size()
        if operation is None:
            return (offset / span, min((offset + page) / span, 1.0)) if span > page else (0.0, 1.0)
        if operation == "moveto":
            value = lower + float(amount) * span
        elif operation == "scroll":
            step = adjustment.get_page_increment() if unit == "pages" else adjustment.get_step_increment()
            value = adjustment.get_value() + int(amount) * (step or 24)
        else:
            raise TclError(f"Неизвестная операция прокрутки: {operation}")
        adjustment.set_value(max(lower, min(value, upper - page)))

    def xview(self, *args):
        return self._view("x", *args)

    def yview(self, *args):
        return self._view("y", *args)

    def yview_moveto(self, fraction):
        self.yview("moveto", fraction)

    def yview_scroll(self, amount, units):
        self.yview("scroll", amount, units)


class Treeview(_Scrolled):
    _class_name = "Treeview"

    def __init__(self, master=None, *, columns=(), show="headings", height=10,
                 selectmode="extended", **options):
        self._rows = {}
        self._tags = {}
        self._ids = count(1)
        self._keys = tuple(columns)
        self._columns = {}
        self._model = Gtk.ListStore(GObject.TYPE_STRING, GObject.TYPE_PYOBJECT,
                                    GObject.TYPE_PYOBJECT)
        view = Gtk.TreeView(model=self._model, headers_visible="headings" in show)
        super().__init__(master, view, overlay=True, columns=self._keys, height=height, **options)
        self._selection = view.get_selection()
        self._selection.set_mode(_selection_mode(selectmode))
        self._connect_native(self._selection, "changed", self._selection_changed)
        for index, key in enumerate(self._keys):
            renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            native = Gtk.TreeViewColumn(key, renderer)
            native.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            native.set_cell_data_func(renderer, self._render_cell, index)
            heading = {"text": key, "anchor": "w", "command": None}
            config = {"width": 100, "minwidth": 1, "stretch": True, "anchor": "w"}
            self._columns[key] = (native, renderer, heading, config)
            self._connect_native(native, "clicked", self._heading_clicked, key)
            view.append_column(native)
        self._connect_native(self.widget, "destroy", self._clear_column_renderers)

    def _selection_changed(self, _selection):
        self._dispatch("<<TreeviewSelect>>")

    def _clear_column_renderers(self, _widget):
        for column, renderer, _heading, _config in self._columns.values():
            column.set_cell_data_func(renderer, None)

    def _configure_option(self, name, value):
        if name == "height":
            self._scrolled.set_min_content_height(max(int(value), 1) * 24 + 30)
            return True
        return super()._configure_option(name, value)

    def _heading_clicked(self, _column, key):
        command = self._columns[key][2]["command"]
        if command:
            command()

    def _render_cell(self, _column, cell, model, row, index):
        values, tags = model.get(row, 1, 2)
        cell.set_property("text", str(values[index]) if index < len(values) else "")
        color = next((self._tags[tag] for tag in tags if tag in self._tags), None)
        if color:
            cell.set_property("foreground", color)
        cell.set_property("foreground-set", bool(color))

    def heading(self, key, option=None, **options):
        native, _, heading, _ = self._columns[key]
        if options:
            heading.update(options)
            native.set_title(heading["text"])
            native.set_alignment(_alignment(heading["anchor"]))
            native.set_clickable(bool(heading["command"]))
        return heading[option] if option is not None else dict(heading)

    def column(self, key, option=None, **options):
        native, renderer, _, config = self._columns[key]
        if options:
            config.update(options)
            native.set_min_width(config["minwidth"])
            native.set_fixed_width(config["width"])
            native.set_expand(bool(config["stretch"]))
            renderer.set_property("xalign", _alignment(config["anchor"]))
        return config[option] if option is not None else dict(config)

    def tag_configure(self, tag, *, foreground=None):
        self._tags[tag] = foreground
        self.view.queue_draw()

    def insert(self, parent, index, iid=None, *, values=(), tags=()):
        if parent:
            raise TclError("Таблица принимает строки без вложенности")
        if iid is None:
            iid = f"I{next(self._ids):03X}"
            while iid in self._rows:
                iid = f"I{next(self._ids):03X}"
        iid = str(iid)
        if iid in self._rows:
            raise TclError(f"Строка уже есть: {iid}")
        row = (iid, tuple(values), (tags,) if isinstance(tags, str) else tuple(tags))
        self._rows[iid] = (self._model.append(row) if index == "end" else
                           self._model.insert(int(index), row))
        return iid

    def _row(self, iid):
        try:
            return self._rows[iid]
        except KeyError as exc:
            raise TclError(f"Строка не найдена: {iid}") from exc

    def delete(self, *items):
        for iid in items:
            row = self._row(iid)
            del self._rows[iid]
            self._model.remove(row)

    def get_children(self, item=""):
        if item:
            self._row(item)
            return ()
        return tuple(row[0] for row in self._model)

    def exists(self, iid):
        return iid in self._rows

    def index(self, iid):
        return self._model.get_path(self._row(iid)).get_indices()[0]

    def item(self, iid, option=None):
        values, tags = self._model.get(self._row(iid), 1, 2)
        record = {"values": values, "tags": tags}
        return record[option] if option is not None else record

    def _id_at(self, path):
        return self._model.get_value(self._model.get_iter(path), 0)

    def selection(self):
        _, paths = self._selection.get_selected_rows()
        return tuple(self._id_at(path) for path in paths)

    def selection_set(self, *items):
        if len(items) == 1 and isinstance(items[0], (list, tuple)):
            items = items[0]
        rows = [self._row(iid) for iid in items]
        self._selection.unselect_all()
        for row in rows:
            self._selection.select_iter(row)

    def focus(self, iid=None):
        if iid is not None:
            self.view.set_cursor(self._model.get_path(self._row(iid)), None, False)
        path, _ = self.view.get_cursor()
        return self._id_at(path) if path is not None else ""

    def see(self, iid):
        self.view.scroll_to_cell(self._model.get_path(self._row(iid)), None, False, 0, 0)

    def identify_row(self, y):
        # Событие по ячейке GTK уже передаёт координату внутри области строк.
        location = self.view.get_path_at_pos(0, int(y))
        return self._id_at(location[0]) if location else ""


def _alignment(anchor):
    return {"e": 1.0, "center": 0.5}.get(anchor, 0.0)


def _selection_mode(name):
    return {"browse": Gtk.SelectionMode.BROWSE, "single": Gtk.SelectionMode.SINGLE,
            "extended": Gtk.SelectionMode.MULTIPLE, "multiple": Gtk.SelectionMode.MULTIPLE,
            "none": Gtk.SelectionMode.NONE}[name]


class Menu(Widget):
    _class_name = "Menu"

    def __init__(self, master=None, *, tearoff=0):
        super().__init__(master, widget=Gtk.Menu())

    def add_command(self, *, label, command):
        item = Gtk.MenuItem(label=label)
        self._connect_native(item, "activate", lambda _item: command())
        self.widget.append(item)
        item.show()

    def add_separator(self):
        item = Gtk.SeparatorMenuItem()
        self.widget.append(item)
        item.show()

    def tk_popup(self, x_root, y_root):
        event = Gtk.get_current_event()
        if event is not None:
            self.widget.popup_at_pointer(event)
        else:
            rectangle = Gdk.Rectangle()
            rectangle.x, rectangle.y = x_root, y_root
            rectangle.width = rectangle.height = 1
            self.widget.popup_at_rect(Gdk.get_default_root_window(), rectangle,
                                      Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def grab_release(self):
        # GTK освобождает захват при закрытии выпадающего меню.
        pass


class Text(_Scrolled):
    _class_name = "Text"

    def __init__(self, master=None, *, height=10, width=0, wrap="char", undo=False, **options):
        view = Gtk.TextView()
        view.set_wrap_mode({"none": Gtk.WrapMode.NONE, "word": Gtk.WrapMode.WORD_CHAR,
                            "char": Gtk.WrapMode.CHAR}[wrap])
        self._buffer = view.get_buffer()
        super().__init__(master, view, **options)
        self._scrolled.set_min_content_height(max(height, 1) * 18)
        if width:
            # Широкий текст редактора прокручивается, размер окна задаёт экран.
            self._scrolled.set_min_content_width(min(width, 56) * 8)
        if undo:
            self._undo, self._redo = [], []
            self._before_action = ""
            self._connect_native(self._buffer, "begin-user-action", self._begin_edit)
            self._connect_native(self._buffer, "end-user-action", self._end_edit)
            self._connect_native(view, "key-press-event", self._undo_key)

    def _configure_option(self, name, value):
        if name == "state":
            editable = value == "normal"
            self.view.set_editable(editable)
            self.view.set_cursor_visible(editable)
            return True
        if name == "font" and isinstance(value, (tuple, list)) and value[0] == "TkFixedFont":
            self.view.set_monospace(True)
            return True
        return super()._configure_option(name, value)

    def _iter(self, index):
        if index == "end":
            return self._buffer.get_end_iter()
        if index == "insert":
            return self._buffer.get_iter_at_mark(self._buffer.get_insert())
        line, column = map(int, str(index).split(".", 1))
        iterator = self._buffer.get_iter_at_line(line - 1)
        if isinstance(iterator, tuple):
            iterator = iterator[-1]
        for _ in range(column):
            if iterator.ends_line() or not iterator.forward_char():
                break
        return iterator

    def get(self, start, end):
        content = self._buffer.get_text(self._iter(start), self._iter(end), True)
        return content + "\n" if end == "end" else content

    def insert(self, index, text):
        self._buffer.insert(self._iter(index), str(text))

    def delete(self, start, end):
        self._buffer.delete(self._iter(start), self._iter(end))

    def see(self, index):
        self.view.scroll_to_iter(self._iter(index), 0, True, 0, 1)

    def _plain_text(self):
        start, end = self._buffer.get_bounds()
        return self._buffer.get_text(start, end, True)

    def _begin_edit(self, _buffer):
        self._before_action = self._plain_text()

    def _end_edit(self, _buffer):
        if self._before_action != self._plain_text():
            self._undo.append(self._before_action)
            del self._undo[:-50]
            self._redo.clear()

    def _undo_key(self, _view, event):
        if not self.view.get_editable() or not (event.state & Gdk.ModifierType.CONTROL_MASK):
            return False
        key = Gdk.keyval_to_lower(event.keyval)
        if key not in (Gdk.KEY_z, Gdk.KEY_y):
            return False
        redo = key == Gdk.KEY_y or event.state & Gdk.ModifierType.SHIFT_MASK
        source, destination = (self._redo, self._undo) if redo else (self._undo, self._redo)
        if source:
            destination.append(self._plain_text())
            self._buffer.set_text(source.pop())
        return True


class Listbox(_Scrolled):
    _class_name = "Listbox"

    def __init__(self, master=None, *, height=10, width=0, selectmode="browse", **options):
        view = Gtk.ListBox(selection_mode=_selection_mode(selectmode))
        super().__init__(master, view, **options)
        self._scrolled.set_min_content_height(max(height, 1) * 21)
        if width:
            self._scrolled.set_min_content_width(width * 8)

    def _index(self, index):
        return len(self.view.get_children()) - 1 if index == "end" else int(index)

    def insert(self, index, *items):
        position = -1 if index == "end" else int(index)
        for item in items:
            label = Gtk.Label(label=str(item), xalign=0, margin_start=5, margin_end=5)
            self.view.insert(label, position)
            label.get_parent().show_all()
            if position >= 0:
                position += 1

    def get(self, first, last=None):
        rows = self.view.get_children()
        start = self._index(first)
        if last is None:
            return rows[start].get_child().get_text() if 0 <= start < len(rows) else ""
        return tuple(row.get_child().get_text() for row in rows[max(start, 0):self._index(last) + 1])

    def delete(self, first, last=None):
        start, end = self._index(first), self._index(first if last is None else last)
        for row in self.view.get_children()[max(start, 0):end + 1]:
            self.view.remove(row)

    def curselection(self):
        return tuple(sorted(row.get_index() for row in self.view.get_selected_rows()))

    def selection_set(self, first, last=None):
        start, end = self._index(first), self._index(first if last is None else last)
        for row in self.view.get_children()[max(start, 0):end + 1]:
            self.view.select_row(row)


class Canvas(_Scrolled):
    """Область мастера с одним прокручиваемым блоком содержимого."""

    _class_name = "Canvas"

    def __init__(self, master=None, **options):
        self._content = None
        super().__init__(master, None, **options)
        self._scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)

    def create_window(self, _coordinates, *, window, anchor="nw"):
        self._content = window
        window.widget.set_hexpand(True)
        self._scrolled.add(window.widget)
        window.widget.show()
        return 1

    def bbox(self, _item):
        if self._content is None:
            return (0, 0, 0, 0)
        return (0, 0, self._content.winfo_reqwidth(), self._content.winfo_reqheight())

    def itemconfigure(self, _item, *, width):
        # Родной viewport уже расширяет вложенный блок до текущей ширины.
        pass

    def _configure_option(self, name, value):
        if name in ("scrollregion", "highlightthickness", "borderwidth"):
            return True
        return super()._configure_option(name, value)
