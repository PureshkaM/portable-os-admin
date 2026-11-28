"""GTK 3 widgets used by the shared Ткач screens on РЕД ОС."""
from types import SimpleNamespace

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from . import base, collections, controls, dialogs

filedialog = dialogs.filedialog
messagebox = dialogs.messagebox
simpledialog = dialogs.simpledialog

tk = SimpleNamespace(
    Tk=base.Tk,
    Toplevel=base.Toplevel,
    Misc=base.Widget,
    Widget=base.Widget,
    Frame=base.Frame,
    Label=controls.Label,
    StringVar=base.StringVar,
    BooleanVar=base.BooleanVar,
    Variable=base.Variable,
    Event=base.Event,
    TclError=base.TclError,
    Canvas=collections.Canvas,
    Listbox=collections.Listbox,
    Menu=collections.Menu,
    Text=collections.Text,
)
ttk = SimpleNamespace(
    Frame=base.Frame,
    LabelFrame=base.LabelFrame,
    Label=controls.Label,
    Button=controls.Button,
    Checkbutton=controls.Checkbutton,
    Radiobutton=controls.Radiobutton,
    Entry=controls.Entry,
    Combobox=controls.Combobox,
    Progressbar=controls.Progressbar,
    Scrollbar=controls.Scrollbar,
    Separator=controls.Separator,
    Notebook=controls.Notebook,
    Treeview=collections.Treeview,
)


def run() -> None:
    from ..ui import select_gtk

    select_gtk()
    from ..app import run as run_application

    run_application()
