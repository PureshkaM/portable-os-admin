"""Semantic colours and text roles for the GTK interface."""
from gi.repository import Gdk, Gtk

_provider = None


def apply(root, colours):
    global _provider
    root.widget.get_style_context().add_class("poa-window")
    if _provider is not None:
        return _provider

    css = f"""
    .poa-window, .Window-TFrame, .Window-TLabel {{
        background-color: {colours.BACKGROUND};
    }}
    .Card-TFrame, .Card-TLabel, .Card-TCheckbutton, .Card-TRadiobutton {{
        background-color: {colours.SURFACE};
    }}
    .Sunken-TFrame, .Sunken-TLabel, .Sunken-TCheckbutton, .Sunken-TRadiobutton {{
        background-color: {colours.SURFACE_SUNKEN};
    }}
    .Sidebar-TFrame {{
        background-color: {colours.SIDEBAR};
    }}
    .SidebarTitle-TLabel, .Section-TLabel, .SectionSunken-TLabel {{
        font-weight: bold;
    }}
    .SidebarItemActive-TLabel, button.Accent-TButton {{
        background-color: {colours.ACCENT};
        background-image: none;
        color: {colours.TEXT_ON_ACCENT};
    }}
    button.Accent-TButton:hover {{
        background-color: {colours.ACCENT_ACTIVE};
    }}
    button.Accent-TButton:disabled {{
        background-color: {colours.BORDER};
        color: {colours.TEXT_MUTED};
    }}
    button.Accent-TButton:focus {{
        outline-color: {colours.TEXT_ON_ACCENT};
    }}
    .Hint-TLabel, .HintSunken-TLabel, .Muted-TLabel, .SidebarQuiet-TLabel {{
        color: {colours.TEXT_MUTED};
    }}
    .Warning-TLabel, .WarningSunken-TLabel {{
        color: {colours.WARNING};
    }}
    .SidebarDanger-TLabel {{
        color: {colours.DANGER};
    }}
    .Title-TLabel {{
        font-size: 15pt;
        font-weight: bold;
    }}
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _provider = provider
    return provider
