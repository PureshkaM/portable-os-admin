"""Widget backend selected at launch, before importing any screens."""
from importlib import import_module

_MODULES = ("tk", "ttk", "font", "filedialog", "messagebox", "simpledialog")
_gtk_backend = None


def select_gtk() -> None:
    """Use the GTK implementation for all subsequently loaded screens."""
    global _gtk_backend
    _gtk_backend = import_module(".gtk3", __package__)


def using_gtk() -> bool:
    return _gtk_backend is not None


def __getattr__(name: str):
    if name not in _MODULES:
        raise AttributeError(name)
    if _gtk_backend is not None:
        return getattr(_gtk_backend, name)
    module = "tkinter" if name == "tk" else f"tkinter.{name}"
    return import_module(module)
