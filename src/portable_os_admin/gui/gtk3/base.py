"""GTK objects beneath the layout and event calls shared by the screens."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import count, groupby
import re

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


class TclError(RuntimeError):
    """The shared screens use this exception for unavailable widgets."""


_IDS = count(1)


class Variable:
    def __init__(self, master=None, value=None, name=None):
        self._name = name or f"var{next(_IDS)}"
        self._value = self._convert(value)
        self._traces = {}

    def _convert(self, value):
        return value

    def __str__(self):
        return self._name

    def get(self):
        return self._value

    def set(self, value):
        self._value = self._convert(value)
        for callback in tuple(self._traces.values()):
            callback(self._name, "", "write")

    def trace_add(self, mode, callback):
        token = f"trace{next(_IDS)}"
        if mode == "write":
            self._traces[token] = callback
        return token

    def trace_remove(self, mode, token):
        self._traces.pop(token, None)


class StringVar(Variable):
    def _convert(self, value):
        return "" if value is None else str(value)


class BooleanVar(Variable):
    def _convert(self, value):
        if isinstance(value, str):
            return value.lower() not in ("", "0", "false", "no", "off")
        return bool(value)


@dataclass
class Event:
    widget: Widget | None = None
    x: int = 0
    y: int = 0
    x_root: int = 0
    y_root: int = 0
    keycode: int = 0
    keysym: str = ""
    char: str = ""
    num: int = 0
    delta: int = 0
    state: int = 0
    width: int = 0
    height: int = 0


_POINTER = {
    "button-press-event": Gdk.EventMask.BUTTON_PRESS_MASK,
    "enter-notify-event": Gdk.EventMask.ENTER_NOTIFY_MASK,
    "leave-notify-event": Gdk.EventMask.LEAVE_NOTIFY_MASK,
    "scroll-event": Gdk.EventMask.SCROLL_MASK | Gdk.EventMask.SMOOTH_SCROLL_MASK,
}


def _signals(sequence):
    if sequence.startswith("<<") or sequence == "<Destroy>":
        return ()
    if sequence == "<Configure>":
        return ("size-allocate",)
    if sequence in ("<FocusIn>", "<FocusOut>"):
        return ("focus-in-event" if sequence == "<FocusIn>" else "focus-out-event",)
    if sequence in ("<Enter>", "<Leave>"):
        return ("enter-notify-event" if sequence == "<Enter>" else "leave-notify-event",)
    if sequence == "<MouseWheel>" or sequence in ("<Button-4>", "<Button-5>"):
        return ("scroll-event",)
    if sequence.startswith(("<Button", "<Double-")):
        return ("button-press-event",)
    return ("key-press-event",)


def _pair(value):
    if isinstance(value, (tuple, list)):
        return int(value[0]), int(value[-1])
    return int(value or 0), int(value or 0)


def _padding(value):
    if not isinstance(value, (tuple, list)):
        value = (value,)
    values = tuple(int(item) for item in value)
    if len(values) == 4:
        return values
    if len(values) == 3:
        return values[0], values[1], values[2], values[1]
    horizontal, vertical = values if len(values) == 2 else (values[0], values[0])
    return horizontal, vertical, horizontal, vertical


def _alignment(start, end):
    if start and end:
        return Gtk.Align.FILL
    if start:
        return Gtk.Align.START
    if end:
        return Gtk.Align.END
    return Gtk.Align.CENTER


class Widget:
    _class_name = ""
    _default_root = None

    def __init__(self, master=None, widget=None, **options):
        self.master = master if master is not None else Widget._default_root
        self.widget = widget if widget is not None else Gtk.EventBox()
        self._children = []
        self._managed = []
        self._options = {}
        self._states = set()
        self._bindings = {}
        self._gtk_bindings = set()
        self._native_connections = []
        self._global_tokens = []
        self._sources = set()
        self._destroyed = False
        self._name = str(next(_IDS))
        self._manager = None
        self._geometry_options = {}
        self._layout_pending = False
        self._focus_map_handler = None
        self._body = None
        self._mounted = []
        self._row_weights = {}
        self._column_weights = {}
        self._embedded = getattr(self, "_overlay", None) is not None
        self._overlay = getattr(self, "_overlay", None)
        self._event_box = None
        self._css_provider = None
        self.widget._poa_wrapper = self
        if self.master is not None:
            self.master._children.append(self)
        self._native_connections.append((self.widget, self.widget.connect_after("destroy", self._dispose_gtk_widget)))
        self.configure(**options)
        if not isinstance(self.widget, Gtk.Window):
            self.widget.show()

    def __str__(self):
        if self.master is None:
            return "."
        return str(self.master).rstrip(".") + "." + self._name

    def cget(self, name):
        return self._options.get(name, "")

    __getitem__ = cget

    def keys(self):
        return list(self._options)

    def __setitem__(self, name, value):
        self.configure(**{name: value})

    def configure(self, cnf=None, **options):
        if isinstance(cnf, str):
            return self.cget(cnf)
        for name, value in {**(cnf or {}), **options}.items():
            previous = self._options.get(name)
            self._options[name] = value
            if self._configure_option(name, value):
                continue
            if name == "style":
                context = self.widget.get_style_context()
                if previous:
                    context.remove_class(previous.replace(".", "-"))
                if value:
                    context.add_class(value.replace(".", "-"))
            elif name in ("background", "bg", "foreground", "fg"):
                self._apply_colors()
            elif name == "padding" and self._overlay is not None:
                self._queue_layout()
            elif name in ("width", "height"):
                self.widget.set_size_request(
                    int(self.cget("width") or -1), int(self.cget("height") or -1)
                )
            elif name == "cursor":
                self._set_cursor(value)
            elif name == "state":
                self._states.difference_update(("disabled", "readonly"))
                if value in ("disabled", "readonly"):
                    self._states.add(value)
                self.widget.set_sensitive(value != "disabled")

    config = configure

    def _configure_option(self, name, value):
        return False

    def state(self, flags=None):
        if flags is None:
            return tuple(sorted(self._states))
        previous = self._states.copy()
        for flag in flags:
            if flag.startswith("!"):
                self._states.discard(flag[1:])
            else:
                self._states.add(flag)
        if self._states != previous:
            current = "disabled" if "disabled" in self._states else "readonly" if "readonly" in self._states else "normal"
            retained = self._states.copy()
            self.configure(state=current)
            self._states = retained
        return tuple(sorted(previous ^ self._states))

    def instate(self, flags):
        return all((flag[1:] not in self._states) if flag.startswith("!") else flag in self._states for flag in flags)

    def _apply_colors(self):
        context = self.widget.get_style_context()
        if self._css_provider is None:
            self._css_provider = Gtk.CssProvider()
            context.add_provider(self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        background = self.cget("background") or self.cget("bg")
        foreground = self.cget("foreground") or self.cget("fg")
        rules = []
        if background:
            rules.append(f"background-color: {background}")
        if foreground:
            rules.append(f"color: {foreground}")
        self._css_provider.load_from_data(("* {" + ";".join(rules) + "}").encode())

    def _set_cursor(self, value):
        target = self._event_target()
        window = target.get_window()
        if window is not None:
            cursor = Gdk.Cursor.new_from_name(window.get_display(), "pointer" if value == "hand2" else value)
            window.set_cursor(cursor)

    def winfo_class(self):
        return self._class_name or type(self).__name__

    def winfo_exists(self):
        return not self._destroyed

    def winfo_children(self):
        return list(self._children)

    def winfo_parent(self):
        return str(self.master) if self.master is not None else ""

    def winfo_manager(self):
        return self._manager or ""

    def winfo_toplevel(self):
        return self if isinstance(self, _Window) or self.master is None else self.master.winfo_toplevel()

    def winfo_ismapped(self):
        return not self._destroyed and self.widget.get_mapped()

    def winfo_width(self):
        return self.widget.get_allocated_width()

    def winfo_height(self):
        return self.widget.get_allocated_height()

    def winfo_reqwidth(self):
        widget = self.widget
        return max(0, widget.get_preferred_width()[1] - widget.get_margin_start() - widget.get_margin_end())

    def winfo_reqheight(self):
        widget = self.widget
        if not widget.get_visible():
            return 0
        width = self.winfo_width() + widget.get_margin_start() + widget.get_margin_end()
        height = widget.get_preferred_height_for_width(max(1, width))[1]
        # GTK's preferred size includes external margins, its allocation excludes them.
        return max(0, height - widget.get_margin_top() - widget.get_margin_bottom())

    def _root_position(self):
        top = self.winfo_toplevel()
        window = top.widget.get_window()
        if window is None:
            return (0, 0)
        root_x, root_y = window.get_origin()[-2:]
        local = self.widget.translate_coordinates(top.widget, 0, 0)
        return (root_x + local[0], root_y + local[1]) if local else (root_x, root_y)

    def winfo_rootx(self):
        return self._root_position()[0]

    def winfo_rooty(self):
        return self._root_position()[1]

    def _show_for_layout(self):
        self.widget.show()

    def _manage(self, manager, options):
        if self.master is None or self._destroyed:
            return
        before = options.pop("before", None)
        if before is None and self._manager == manager and all(
            self._geometry_options.get(k) == v for k, v in options.items()
        ):
            return
        siblings = self.master._managed
        if before is not None and before is not self:
            if before not in siblings:
                raise TclError("положение задано относительно другого контейнера")
            if self._manager is not None:
                siblings.remove(self)
            siblings.insert(siblings.index(before), self)
        elif self._manager is None:
            siblings.append(self)
        if self._manager != manager:
            self._geometry_options.clear()
        self._manager = manager
        self._geometry_options.update(options)
        self._show_for_layout()
        self.master._queue_layout()

    def pack(self, **options):
        self._manage("pack", options)

    def grid(self, **options):
        self._manage("grid", options)

    def place(self, **options):
        self._manage("place", options)

    def _forget(self):
        if self._manager is not None:
            self._manager = None
            self._geometry_options.clear()
            self.master._managed.remove(self)
            self.widget.hide()
            self.master._queue_layout()

    pack_forget = grid_remove = place_forget = _forget

    def pack_propagate(self, flag):
        # GTK still measures the contents; the frame width is a minimum request.
        self._options["pack_propagate"] = bool(flag)

    def rowconfigure(self, index, weight=0, **options):
        self._row_weights[int(index)] = weight
        self._queue_layout()

    def columnconfigure(self, index, weight=0, **options):
        self._column_weights[int(index)] = weight
        self._queue_layout()

    def _render_widget(self):
        return self.widget if self._event_box is None else self._event_box

    def _queue_layout(self):
        if self._destroyed or self._layout_pending:
            return
        self._layout_pending = True
        self.after_idle(self._layout)

    def _layout(self):
        self._layout_pending = False
        active = self._managed
        if self._overlay is None:
            if not active:
                return
            self._overlay = Gtk.Overlay()
            self.widget.add(self._overlay)
            self._overlay.show()

        top = self.winfo_toplevel()
        focus = top._clear_focus_in(self._mounted) if isinstance(top, _Window) else None
        for child in self._mounted:
            parent = child.get_parent()
            if parent is not None:
                parent.remove(child)
        self._mounted.clear()
        if self._body is not None:
            self._overlay.remove(self._body)
            self._body = None

        pack = [child for child in active if child._manager == "pack"]
        grid = [child for child in active if child._manager == "grid"]
        if pack or grid:
            self._body = self._grid_layout(grid) if grid else self._pack_layout(pack)
            left, upper, right, bottom = _padding(self.cget("padding") or 0)
            self._body.set_margin_start(left)
            self._body.set_margin_top(upper)
            self._body.set_margin_end(right)
            self._body.set_margin_bottom(bottom)
            self._overlay.add(self._body)
            self._body.show()
        for child in active:
            if child._manager == "place":
                widget = child._render_widget()
                options = child._geometry_options
                widget.set_halign(Gtk.Align.CENTER if options.get("relx", 0) >= 0.5 else Gtk.Align.START)
                widget.set_valign(Gtk.Align.CENTER if options.get("rely", 0) > 0 else Gtk.Align.START)
                self._overlay.add_overlay(widget)
                self._mounted.append(widget)
                widget.show()
        if focus is not None and focus.get_mapped() and focus.get_can_focus() and top._focused is None:
            focus.grab_focus()

    def _layout_child(self, child):
        widget = child._render_widget()
        padx, pady = (_pair(child._geometry_options.get(name, 0)) for name in ("padx", "pady"))
        widget.set_margin_start(padx[0])
        widget.set_margin_end(padx[1])
        widget.set_margin_top(pady[0])
        widget.set_margin_bottom(pady[1])
        widget.set_hexpand(False)
        widget.set_vexpand(False)
        self._mounted.append(widget)
        widget.show()
        return widget

    def _grid_layout(self, children):
        grid = Gtk.Grid()
        for child in children:
            opts = child._geometry_options
            col, row = opts.get("column", 0), opts.get("row", 0)
            width, height = opts.get("columnspan", 1), opts.get("rowspan", 1)
            sticky = opts.get("sticky", "")
            widget = self._layout_child(child)
            widget.set_halign(_alignment("w" in sticky, "e" in sticky))
            widget.set_valign(_alignment("n" in sticky, "s" in sticky))
            widget.set_hexpand(any(self._column_weights.get(i, 0) for i in range(col, col + width)))
            widget.set_vexpand(any(self._row_weights.get(i, 0) for i in range(row, row + height)))
            grid.attach(widget, col, row, width, height)
        return grid

    def _pack_layout(self, children):
        def vertical(child):
            return child._geometry_options.get("side", "top") in ("top", "bottom")

        first = previous = None
        for is_vertical, items in groupby(children, vertical):
            orientation = Gtk.Orientation.VERTICAL if is_vertical else Gtk.Orientation.HORIZONTAL
            box = Gtk.Box(orientation=orientation)
            if previous is None:
                first = box
            else:
                previous.pack_start(box, True, True, 0)
            box.show()
            for child in items:
                widget = self._layout_child(child)
                opts = child._geometry_options
                fill, anchor = opts.get("fill", ""), opts.get("anchor", "")
                if anchor == "center":
                    anchor = ""
                fill_x, fill_y = fill in ("x", "both"), fill in ("y", "both")
                widget.set_halign(Gtk.Align.FILL if fill_x else _alignment("w" in anchor, "e" in anchor))
                widget.set_valign(Gtk.Align.FILL if fill_y else _alignment("n" in anchor, "s" in anchor))
                side = opts.get("side", "top")
                pack = box.pack_end if side in ("bottom", "right") else box.pack_start
                pack(widget, bool(opts.get("expand")), fill_y if is_vertical else fill_x, 0)
            previous = box
        return first

    def _event_target(self):
        return getattr(self, "_event_widget", self._render_widget())

    def _connect_native(self, target, signal, callback, *args):
        handler = target.connect(signal, callback, *args)
        self._native_connections.append((target, handler))
        return handler

    def _connect_binding(self, signal):
        if signal in self._gtk_bindings:
            return
        target = self.widget
        if signal in _POINTER:
            target = self._event_target()
        elif signal != "size-allocate":
            target = getattr(self, "_event_widget", self.widget)
        if signal in _POINTER and not target.get_has_window() and target is self.widget:
            self._make_event_box()
            target = self._event_target()
        if signal in _POINTER:
            target.add_events(_POINTER[signal])
        self._connect_native(target, signal, self._native_event, signal)
        self._gtk_bindings.add(signal)

    def _make_event_box(self):
        old_parent = self.widget.get_parent()
        had_focus = False
        if old_parent is not None:
            top = self.winfo_toplevel()
            had_focus = isinstance(top, _Window) and top._clear_focus_in((self.widget,)) is not None
            old_parent.remove(self.widget)
        self._event_box = Gtk.EventBox()
        self._event_box.set_visible_window(False)
        self._event_box.add(self.widget)
        if self._manager and self.master is not None:
            self.master._queue_layout()
        if had_focus:
            self.focus_set()

    def bind(self, sequence, callback, add=None):
        if add != "+":
            self._bindings[sequence] = {}
        token = f"bind{next(_IDS)}"
        self._bindings.setdefault(sequence, {})[token] = callback
        for signal in _signals(sequence):
            self._connect_binding(signal)
        return token

    def unbind(self, sequence, token=None):
        if token is None:
            self._bindings.pop(sequence, None)
        else:
            self._bindings.get(sequence, {}).pop(token, None)

    def bind_all(self, sequence, callback, add=None):
        top = self.winfo_toplevel()
        if add != "+":
            top._global_bindings[sequence] = {}
        token = f"bind{next(_IDS)}"
        top._global_bindings.setdefault(sequence, {})[token] = callback
        self._global_tokens.append((sequence, token))
        for signal in _signals(sequence):
            top._connect_binding(signal)
        return token

    def unbind_all(self, sequence):
        self.winfo_toplevel()._global_bindings.pop(sequence, None)
        self._global_tokens[:] = [entry for entry in self._global_tokens if entry[0] != sequence]

    def _dispatch(self, sequence, event=None):
        event = event or Event(widget=self)
        for callback in tuple(self._bindings.get(sequence, {}).values()):
            if callback(event) == "break":
                return "break"
        return None

    def event_generate(self, sequence, **values):
        event = Event(widget=self, **values)
        if self._dispatch(sequence, event) != "break":
            self.winfo_toplevel()._dispatch_global(sequence, event)

    def _native_event(self, _target, native, signal):
        event = Event(widget=self)
        if signal == "size-allocate":
            event.width, event.height = native.width, native.height
            self._dispatch("<Configure>", event)
            return None
        if signal == "key-press-event":
            key = Gdk.keyval_name(native.keyval) or ""
            event.keysym = key
            event.keycode = native.hardware_keycode
            char = Gdk.keyval_to_unicode(native.keyval)
            event.char = chr(char) if char else ""
            event.state = int(native.state)
            sequence = f"<{'Return' if key == 'KP_Enter' else key}>"
        elif signal == "focus-in-event":
            sequence = "<FocusIn>"
        elif signal == "focus-out-event":
            sequence = "<FocusOut>"
        else:
            x, y = native.get_coords()[-2:]
            rx, ry = native.get_root_coords()[-2:]
            event.x, event.y, event.x_root, event.y_root = map(int, (x, y, rx, ry))
            if signal == "button-press-event":
                event.num = native.button
                if native.type == Gdk.EventType.DOUBLE_BUTTON_PRESS:
                    sequence = f"<Double-{event.num}>"
                else:
                    sequence = f"<Button-{event.num}>"
            elif signal == "enter-notify-event":
                sequence = "<Enter>"
            elif signal == "leave-notify-event":
                sequence = "<Leave>"
            else:
                if native.direction == Gdk.ScrollDirection.SMOOTH:
                    dy = native.get_scroll_deltas()[-1]
                elif native.direction == Gdk.ScrollDirection.UP:
                    dy = -1
                elif native.direction == Gdk.ScrollDirection.DOWN:
                    dy = 1
                else:
                    return False
                if not dy:
                    return False
                event.num, event.delta = (4, 120) if dy < 0 else (5, -120)
                sequence = "<MouseWheel>" if "<MouseWheel>" in self._bindings else f"<Button-{event.num}>"
        if self._dispatch(sequence, event) == "break":
            return True
        if self is self.winfo_toplevel():
            return self._dispatch_global(sequence, event)
        return False

    def _dispatch_global(self, sequence, event):
        callbacks = getattr(self, "_global_bindings", {}).get(sequence, {})
        return any(callback(event) == "break" for callback in tuple(callbacks.values()))

    def focus_set(self):
        target = getattr(self, "_event_widget", self.widget)
        top = self.winfo_toplevel()
        if self._destroyed or not isinstance(top, _Window) or not target.get_can_focus():
            return
        if target.get_mapped() and target.get_toplevel() is top.widget:
            top._focus_request = None
            target.grab_focus()
            return
        # A wizard can request focus while creating the next step, before the
        # layout has parented the field. GTK requires a real window ancestor.
        top._focus_request = self
        if self._focus_map_handler is None:
            self._focus_map_handler = self._connect_native(target, "map", self._focus_when_mapped)

    def _focus_when_mapped(self, target):
        target.disconnect(self._focus_map_handler)
        self._native_connections.remove((target, self._focus_map_handler))
        self._focus_map_handler = None
        if self.winfo_toplevel()._focus_request is self:
            self.focus_set()

    def focus_get(self):
        top = self.winfo_toplevel()
        if not isinstance(top, _Window):
            return None
        if top._focus_request is not None:
            return top._focus_request
        node = top._focused
        while node is not None:
            owner = getattr(node, "_poa_wrapper", None)
            if owner is not None:
                return owner
            node = node.get_parent()
        return None

    def lift(self):
        if self.master is not None:
            self.master._children.remove(self)
            self.master._children.append(self)
        if self._manager == "place" and self._render_widget().get_parent() is self.master._overlay:
            self.master._overlay.reorder_overlay(self._render_widget(), -1)

    def after(self, delay, callback, *args):
        return self._schedule(lambda invoke: GLib.timeout_add(delay, invoke), callback, args)

    def after_idle(self, callback, *args):
        return self._schedule(GLib.idle_add, callback, args)

    def _schedule(self, register, callback, args):
        def invoke():
            self._sources.discard(source)
            if not self._destroyed:
                callback(*args)
            return False

        source = register(invoke)
        self._sources.add(source)
        return source

    def after_cancel(self, source):
        if source in self._sources:
            self._sources.remove(source)
            GLib.source_remove(source)

    def update(self):
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    update_idletasks = update

    def clipboard_clear(self):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).clear()

    def clipboard_append(self, text):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(str(text), -1)
        clipboard.store()

    def destroy(self):
        if not self._destroyed:
            top = self.winfo_toplevel()
            if isinstance(top, _Window):
                top._clear_focus_in((self.widget,))
            self._render_widget().destroy()

    def _dispose_gtk_widget(self, _widget):
        if self._destroyed:
            return
        self._destroyed = True
        top = self.winfo_toplevel()
        if isinstance(top, _Window) and top._focus_request is self:
            top._focus_request = None
        for child in tuple(self._children):
            child.destroy()
        for source in tuple(self._sources):
            self.after_cancel(source)
        if self.master is not None:
            self.master._children.remove(self)
            if self._manager is not None:
                self.master._managed.remove(self)
            self.master._queue_layout()
        try:
            self._dispatch("<Destroy>")
        finally:
            self._disconnect_native(top)
            if Widget._default_root is self:
                Widget._default_root = None
                if Gtk.main_level():
                    Gtk.main_quit()

    def _disconnect_native(self, top):
        # GTK signal closures retain Python callbacks outside the cyclic collector.
        for target, handler in self._native_connections:
            if target.handler_is_connected(handler):
                target.disconnect(handler)
        self._native_connections.clear()
        if isinstance(top, _Window):
            for sequence, token in self._global_tokens:
                top._global_bindings.get(sequence, {}).pop(token, None)
            if top is self:
                top._global_bindings.clear()
                top._focused = None
                top._delete = None
        self._global_tokens.clear()
        self._gtk_bindings.clear()
        self._bindings.clear()
        self._options.clear()
        self._mounted.clear()
        if getattr(self.widget, "_poa_wrapper", None) is self:
            del self.widget._poa_wrapper


class Frame(Widget):
    _class_name = "TFrame"

    def __init__(self, master=None, **options):
        super().__init__(master, Gtk.EventBox(), **options)


class LabelFrame(Widget):
    _class_name = "TLabelframe"

    def __init__(self, master=None, **options):
        super().__init__(master, Gtk.Frame(), **options)

    def _configure_option(self, name, value):
        if name == "text":
            self.widget.set_label(str(value))
            return True
        return False


class _Window(Widget):
    def __init__(self, master=None, **options):
        self._global_bindings = {}
        self._delete = None
        self._withdrawn = False
        self._focus_request = None
        self._focused = None
        super().__init__(master, Gtk.Window(), **options)
        self._connect_native(self.widget, "delete-event", self._on_delete)
        self._connect_native(self.widget, "set-focus", self._on_set_focus)

    def _on_set_focus(self, _window, focus):
        self._focused = focus

    def _clear_focus_in(self, widgets):
        focus = self._focused
        if not self._destroyed and focus is not None and any(
            focus is widget or focus.is_ancestor(widget) for widget in widgets
        ):
            self.widget.set_focus(None)
            return focus
        return None

    def _on_delete(self, _window, _event):
        if self._delete is not None:
            self._delete()
            return True
        return False

    def protocol(self, name, callback):
        if name == "WM_DELETE_WINDOW":
            self._delete = callback

    def title(self, text=None):
        if text is None:
            return self.widget.get_title()
        self.widget.set_title(text)

    def geometry(self, spec):
        match = re.fullmatch(r"(?:(\d+)x(\d+))?(?:([+-]\d+)([+-]\d+))?", spec)
        if match is None:
            raise TclError(f"неверная геометрия окна {spec!r}")
        width, height, x, y = match.groups()
        if width is not None:
            self.widget.set_default_size(int(width), int(height))
            if self.widget.get_mapped():
                self.widget.resize(int(width), int(height))
        if x is not None:
            self.widget.move(int(x), int(y))

    def minsize(self, width=None, height=None):
        if width is None and height is None:
            return tuple(max(1, value) for value in self.widget.get_size_request())
        self.widget.set_size_request(width, height)

    def withdraw(self):
        self._withdrawn = True
        self.widget.hide()

    def deiconify(self):
        self._withdrawn = False
        self.widget.present()

    def transient(self, owner):
        self.widget.set_transient_for(owner.winfo_toplevel().widget)
        self.widget.set_destroy_with_parent(True)

    def grab_set(self):
        self.widget.set_modal(True)

    def grab_release(self):
        self.widget.set_modal(False)

    def lift(self):
        self.widget.present()

    focus_force = lift

    def wait_window(self, other):
        if not other.winfo_exists():
            return
        loop = GLib.MainLoop()
        other._connect_native(other.widget, "destroy", lambda _widget: loop.quit())
        if other.winfo_exists():
            loop.run()


class Tk(_Window):
    def __init__(self, **options):
        initialized, _arguments = Gtk.init_check()
        if not initialized:
            raise TclError("GTK не может открыть дисплей")
        super().__init__(**options)
        Widget._default_root = self
        self._group = Gtk.WindowGroup()
        self._group.add_window(self.widget)
        self.widget.show()

    def mainloop(self):
        self.update_idletasks()
        if self.winfo_exists():
            Gtk.main()


class Toplevel(_Window):
    def __init__(self, master=None, **options):
        super().__init__(master, **options)
        if self.master is not None:
            top = self.master.winfo_toplevel()
            root = Widget._default_root
            if root is not None:
                root._group.add_window(self.widget)
            self.transient(top)
        self.after_idle(self._show_window)

    def _show_window(self):
        if not self._withdrawn:
            self.widget.show()
