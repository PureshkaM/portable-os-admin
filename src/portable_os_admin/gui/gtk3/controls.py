"""GTK3 form controls used by the administrator screens."""

from weakref import WeakKeyDictionary

from gi.repository import GLib, Gtk, Pango

from .base import TclError, Widget


_ALIGNMENT = {"left": 0.0, "center": 0.5, "right": 1.0}
_JUSTIFY = {
    "left": Gtk.Justification.LEFT,
    "center": Gtk.Justification.CENTER,
    "right": Gtk.Justification.RIGHT,
}
_ORIENTATIONS = {"horizontal": Gtk.Orientation.HORIZONTAL, "vertical": Gtk.Orientation.VERTICAL}


def _font_attributes(font):
    attributes = Pango.AttrList()
    if font:
        family, size, *styles = font
        description = Pango.FontDescription()
        description.set_family({"TkDefaultFont": "Sans", "TkFixedFont": "Monospace"}.get(family, family))
        description.set_size(int(size * Pango.SCALE))
        if "bold" in styles:
            description.set_weight(Pango.Weight.BOLD)
        attributes.insert(Pango.attr_font_desc_new(description))
    return attributes


class _BoundWidget(Widget):
    def __init__(self, master, widget, **options):
        self._variable = None
        self._trace = None
        self._sync_depth = 0
        super().__init__(master, widget, **options)
        self._connect_native(widget, "destroy", self._unbind_variable)

    def _bind_variable(self, variable):
        self._unbind_variable()
        self._variable = variable
        if variable is not None:
            self._trace = variable.trace_add("write", self._sync_variable)
            self._sync_variable()

    def _unbind_variable(self, *_args):
        if self._trace is not None:
            self._variable.trace_remove("write", self._trace)
        self._variable = None
        self._trace = None

    def _sync_variable(self, *_args):
        if self._variable is None:
            return
        self._sync_depth += 1
        try:
            self._show_value(self._variable.get())
        finally:
            self._sync_depth -= 1

    def _write_variable(self, value):
        if self._variable is not None and self._variable.get() != value:
            self._variable.set(value)


class Label(_BoundWidget):
    _class_name = "TLabel"

    def __init__(self, master=None, **options):
        self._wraplength = 0
        self._width = -1
        super().__init__(master, Gtk.Label(xalign=0.0), **options)

    def cget(self, name):
        if name == "text":
            return self.widget.get_text()
        return super().cget(name)

    __getitem__ = cget

    def _show_value(self, value):
        self.widget.set_text(str(value))

    def _update_width(self):
        self.widget.set_width_chars(self._width)
        self.widget.set_line_wrap(self._wraplength > 0)
        limit = -1
        if self._wraplength > 0:
            measure = self.widget.create_pango_layout("0000000000")
            measure.set_attributes(self.widget.get_attributes())
            width, _ = measure.get_pixel_size()
            limit = max(1, int(self._wraplength / max(1, width / 10)))
        self.widget.set_max_width_chars(limit)

    def _configure_option(self, name, value):
        if name == "text":
            if self._variable is None:
                self._show_value(value)
        elif name == "textvariable":
            self._bind_variable(value)
        elif name == "font":
            self.widget.set_attributes(_font_attributes(value))
            self._update_width()
        elif name == "width":
            self._width = int(value)
            self._update_width()
        elif name == "wraplength":
            self._wraplength = int(value)
            self.widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            self._update_width()
        elif name == "anchor":
            self.widget.set_xalign({"w": 0.0, "e": 1.0}.get(value[-1:], 0.5))
            self.widget.set_yalign({"n": 0.0, "s": 1.0}.get(value[:1], 0.5))
        elif name == "justify":
            self.widget.set_justify(_JUSTIFY[value])
        else:
            return super()._configure_option(name, value)
        return True


class _CommandWidget(_BoundWidget):
    def __init__(self, master, widget, **options):
        self._command = None
        super().__init__(master, widget, **options)
        self._connect_native(widget, "clicked", self._clicked)

    def _clicked(self, _widget):
        if self._command is not None and self.widget.is_sensitive() and not self._sync_depth:
            self._command()

    def invoke(self):
        if self.widget.is_sensitive():
            self.widget.clicked()

    def _configure_option(self, name, value):
        if name == "command":
            self._command = value
        elif name == "text":
            self.widget.set_label(str(value))
        else:
            return super()._configure_option(name, value)
        return True


class Button(_CommandWidget):
    _class_name = "TButton"

    def __init__(self, master=None, **options):
        super().__init__(master, Gtk.Button(), **options)


class Checkbutton(_CommandWidget):
    _class_name = "TCheckbutton"

    def __init__(self, master=None, **options):
        super().__init__(master, Gtk.CheckButton(), **options)

    def _show_value(self, value):
        self.widget.set_active(bool(value))

    def _clicked(self, widget):
        if not self._sync_depth and widget.is_sensitive():
            self._write_variable(widget.get_active())
            super()._clicked(widget)

    def _configure_option(self, name, value):
        if name == "variable":
            self._bind_variable(value)
            return True
        return super()._configure_option(name, value)


class Radiobutton(_CommandWidget):
    _class_name = "TRadiobutton"

    # GTK requires an active item in each group. Keep a separate, unpacked
    # button selected when the variable has no corresponding form choice.
    _groups = WeakKeyDictionary()

    def __init__(self, master=None, **options):
        self._value = ""
        self._unselected = None
        super().__init__(master, Gtk.RadioButton(), **options)

    def _show_value(self, value):
        if value == self._value:
            self.widget.set_active(True)
        elif self.widget.get_active() and self._unselected is not None:
            self._unselected.set_active(True)

    def _clicked(self, widget):
        if widget.get_active() and widget.is_sensitive() and not self._sync_depth:
            self._write_variable(self._value)
            super()._clicked(widget)

    def _configure_option(self, name, value):
        if name == "value":
            self._value = value
            if self._variable is not None:
                self._sync_variable()
        elif name == "variable":
            self._unselected = self._groups.get(value)
            if self._unselected is None:
                self._unselected = Gtk.RadioButton()
                if value is not None:
                    self._groups[value] = self._unselected
            self._sync_depth += 1
            try:
                self.widget.join_group(self._unselected)
                self._bind_variable(value)
            finally:
                self._sync_depth -= 1
        else:
            return super()._configure_option(name, value)
        return True


class Entry(_BoundWidget):
    _class_name = "TEntry"

    def __init__(self, master=None, **options):
        super().__init__(master, Gtk.Entry(), **options)
        self._connect_native(self.widget, "changed", self._changed)
        self._connect_native(self.widget, "destroy", self._clear_hidden_text)

    def _show_value(self, value):
        text = str(value)
        if self.widget.get_text() != text:
            self.widget.set_text(text)

    def _changed(self, widget):
        if not self._sync_depth:
            self._write_variable(widget.get_text())

    def _clear_hidden_text(self, widget):
        if not widget.get_visibility():
            widget.set_text("")

    def get(self):
        return self.widget.get_text()

    def insert(self, index, text):
        if self.widget.is_sensitive() and self.widget.get_editable():
            position = len(self.get()) if index == "end" else int(index)
            self.widget.insert_text(text, position)

    def _configure_option(self, name, value):
        if name == "textvariable":
            self._bind_variable(value)
        elif name == "width":
            self.widget.set_width_chars(int(value))
        elif name == "show":
            self.widget.set_visibility(not value)
            self.widget.set_input_purpose(Gtk.InputPurpose.PASSWORD if value else Gtk.InputPurpose.FREE_FORM)
            if value:
                self.widget.set_invisible_char(value[0])
        elif name == "font":
            self.widget.set_attributes(_font_attributes(value))
        elif name == "justify":
            self.widget.set_alignment(_ALIGNMENT[value])
        elif name == "state":
            self.widget.set_editable(value == "normal")
            return super()._configure_option(name, value)
        else:
            return super()._configure_option(name, value)
        return True


class Combobox(_BoundWidget):
    _class_name = "TCombobox"

    def __init__(self, master=None, **options):
        widget = Gtk.ComboBoxText.new_with_entry()
        self._entry = widget.get_child()
        self._values = ()
        super().__init__(master, widget, **options)
        self._connect_native(widget, "changed", self._changed)

    def _show_value(self, value):
        text = str(value)
        if text in self._values:
            self.widget.set_active(self._values.index(text))
        else:
            self.widget.set_active(-1)
            self._entry.set_text(text)

    def _changed(self, widget):
        if self._sync_depth:
            return
        selected = widget.get_active()
        if selected == -1:
            self._write_variable(self._entry.get_text())
        else:
            self._write_variable(self._values[selected])
            self._dispatch("<<ComboboxSelected>>")

    def _replace_values(self, values):
        current = self._entry.get_text() if self._variable is None else self._variable.get()
        self._sync_depth += 1
        try:
            self.widget.remove_all()
            self._values = tuple(map(str, values))
            for value in self._values:
                self.widget.append_text(value)
            self._show_value(current)
        finally:
            self._sync_depth -= 1

    def _configure_option(self, name, value):
        if name == "textvariable":
            self._bind_variable(value)
        elif name == "values":
            self._replace_values(value)
        elif name == "width":
            self._entry.set_width_chars(int(value))
        elif name == "state":
            self._entry.set_editable(value == "normal")
            return super()._configure_option(name, value)
        else:
            return super()._configure_option(name, value)
        return True


class Progressbar(Widget):
    _class_name = "TProgressbar"

    def __init__(self, master=None, **options):
        self._mode = "determinate"
        self._maximum = 100.0
        self._value = 0.0
        self._timer = None
        super().__init__(master, Gtk.ProgressBar(pulse_step=0.02), **options)
        self._connect_native(self.widget, "destroy", self.stop)

    def _update_fraction(self):
        fraction = self._value / self._maximum if self._maximum > 0 else 0.0
        self.widget.set_fraction(max(0.0, min(1.0, fraction)))

    def _tick(self):
        if self._mode == "indeterminate":
            self.widget.pulse()
        else:
            self._value = (self._value + 1) % max(1.0, self._maximum)
            self._update_fraction()
        return GLib.SOURCE_CONTINUE

    def start(self, interval=50):
        if self._timer is None:
            self._timer = GLib.timeout_add(max(1, int(interval)), self._tick)

    def stop(self, *_args):
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None
        if self._mode == "indeterminate":
            self.widget.set_fraction(0.0)

    def _configure_option(self, name, value):
        if name == "mode":
            self._mode = value
            self._update_fraction()
        elif name == "length":
            self.widget.set_size_request(int(value), -1)
        elif name == "maximum":
            self._maximum = float(value)
            self._update_fraction()
        elif name == "value":
            self._value = float(value)
            self._update_fraction()
        else:
            return super()._configure_option(name, value)
        return True


class Scrollbar(Widget):
    _class_name = "TScrollbar"

    def __init__(self, master=None, **options):
        self._command = None
        self._setting = False
        self._adjustment = Gtk.Adjustment(upper=1.0, page_size=1.0)
        super().__init__(master, Gtk.Scrollbar(
            orientation=Gtk.Orientation.VERTICAL, adjustment=self._adjustment,
        ), **options)
        self._connect_native(self._adjustment, "value-changed", self._changed)

    def _changed(self, adjustment):
        if not self._setting and self._command is not None:
            self._command("moveto", adjustment.get_value())

    def set(self, first, last):
        first, last = float(first), float(last)
        page = max(0.0, last - first)
        self._setting = True
        try:
            self._adjustment.configure(first, 0.0, 1.0, 0.05, page * 0.9, page)
        finally:
            self._setting = False

    def _configure_option(self, name, value):
        if name == "orient":
            self.widget.set_orientation(_ORIENTATIONS[value])
        elif name == "command":
            self._command = value
        else:
            return super()._configure_option(name, value)
        return True


class Separator(Widget):
    _class_name = "TSeparator"

    def __init__(self, master=None, **options):
        super().__init__(master, Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), **options)

    def _configure_option(self, name, value):
        if name == "orient":
            self.widget.set_orientation(_ORIENTATIONS[value])
            return True
        return super()._configure_option(name, value)


class Notebook(Widget):
    _class_name = "TNotebook"

    def __init__(self, master=None, **options):
        super().__init__(master, Gtk.Notebook(scrollable=True), **options)

    def add(self, child, *, text):
        label = Gtk.Label(label=text)
        self.widget.append_page(child.widget, label)
        label.show()
        child.widget.show()

    def tabs(self):
        return tuple(
            str(self.widget.get_nth_page(index)._poa_wrapper)
            for index in range(self.widget.get_n_pages())
        )

    def select(self, tab=None):
        tabs = self.tabs()
        if tab is None:
            selected = self.widget.get_current_page()
            return tabs[selected] if selected >= 0 else ""

        try:
            index = tab if isinstance(tab, int) else tabs.index(str(tab))
        except ValueError as exc:
            raise TclError(f"Вкладка не найдена: {tab}") from exc
        if not 0 <= index < len(tabs):
            raise TclError(f"Вкладка не найдена: {tab}")
        self.widget.set_current_page(index)
