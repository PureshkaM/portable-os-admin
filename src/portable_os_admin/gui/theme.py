"""One place that decides how the interface looks.

Before this existed, colours and font sizes were written inline wherever a
widget was created -- "#777777" here, ("TkDefaultFont", 13, "bold") there --
so the same idea (a hint, a heading, a warning) was spelled slightly
differently on each screen, and changing any of it meant hunting through five
files.

Three rules the values below follow, and the reasons, since they are what
makes an interface readable rather than merely colourful:

**A scale, not arbitrary numbers.** Spacing comes from a 4-pixel step and font
sizes from four fixed roles. Two elements are either the same distance apart or
visibly different, never three pixels off -- the near-miss is what reads as
sloppy.

**Contrast carries meaning.** Body text is nearly black on near-white; muted
text is grey enough to recede but still passes contrast for reading; the accent
colour appears on exactly one thing per screen -- the button that continues the
work. A second accent would mean neither is the answer.

**Surfaces separate content from chrome.** The step content sits on a white
card against a grey window, so where the form begins and ends is visible
without drawing a single border.

Everything is plain ttk, since the packaging constraint (see
README, раздел «Архитектура» "Упаковка") rules out any widget toolkit from PyPI.
"""
from __future__ import annotations

import sys

from .ui import tk
from .ui import ttk
from .ui import using_gtk

# ----------------------------------------------------------------------
# Palette
# ----------------------------------------------------------------------
#: Window background: the area *around* the content.
BACKGROUND = "#eceef1"
#: Content background: the card the current step is drawn on.
SURFACE = "#ffffff"
#: A slightly recessed surface, for grouped sub-sections inside a card.
SURFACE_SUNKEN = "#f5f6f8"
#: Hairlines. Light enough to separate without becoming a grid of boxes.
BORDER = "#d3d7dd"

TEXT = "#1b1f24"
#: Secondary text: hints, units, explanations. Grey enough to recede, dark
#: enough to read comfortably against both surfaces above.
TEXT_MUTED = "#5b6470"
#: Text on the accent colour.
TEXT_ON_ACCENT = "#ffffff"
#: Disabled controls.
TEXT_DISABLED = "#9aa1ab"

#: Боковая навигация: своя поверхность, чуть темнее содержимого.
SIDEBAR = "#e2e5ea"
#: Раздел, в котором администратор сейчас находится.
SIDEBAR_ACTIVE = "#2c5fb8"

#: Used on exactly one control per screen: the one that continues the work.
ACCENT = "#2c5fb8"
ACCENT_ACTIVE = "#24509d"

DANGER = "#b42318"
SUCCESS = "#1a7f37"
WARNING = "#8a5a00"
INFO = "#2b5fad"

# ----------------------------------------------------------------------
# Spacing, on a 4px step
# ----------------------------------------------------------------------
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 16
SPACE_LG = 24
SPACE_XL = 32

#: Longest line of body text, in pixels. Beyond roughly this width the eye
#: loses the start of the next line, which is why prose here wraps well before
#: the window edge instead of filling it.
TEXT_WRAP = 620
#: For text in the second column of a form, which already starts well to the
#: right of the card's edge. Using the full width there pushed the last words
#: of a long hint off the card entirely.
# 440, а не 520: на экранах с длинными подписями («Мин. длина пароля
# пользователя:», «Разных машин на носитель:») колонка полей начинается
# примерно с x=510, и подсказка шириной 520 уезжала за правый край карточки --
# обрезанными оказывались все длинные подсказки шага «Диск и шифрование», а не
# какая-то одна. Замерено на окне 1040x700, том самом, которым приложение
# открывается.
HINT_WRAP = 440

# ----------------------------------------------------------------------
# Type scale: four roles, no more
# ----------------------------------------------------------------------
FONT_TITLE = ("TkDefaultFont", 15, "bold")
FONT_SECTION = ("TkDefaultFont", 10, "bold")
FONT_BODY = ("TkDefaultFont", 10)
FONT_SMALL = ("TkDefaultFont", 9)
FONT_MONO = ("TkFixedFont", 10)


def apply(root: tk.Misc):
    """Apply the desktop theme for the selected interface."""
    if using_gtk():
        from .gtk3 import theme as gtk_theme

        return gtk_theme.apply(root, sys.modules[__name__])

    style = ttk.Style(root)
    style.theme_use("clam")

    try:
        root.configure(background=BACKGROUND)
    except tk.TclError:
        # A frame, not a toplevel. Harmless: its own background is set below.
        pass

    # The default is the *content* surface, not the window's.
    #
    # Deliberately this way round. Almost every widget in this application is
    # drawn inside a card -- forms, tables, checkboxes, the lot -- while only
    # the wizard's own chrome sits on the window behind it. Defaulting to the
    # window colour meant every one of those had to name a style, and the ones
    # that were missed showed as grey rectangles on white. Now the exceptions
    # are the few pieces of chrome, which are named explicitly and are all in
    # one file.
    style.configure(".", background=SURFACE, foreground=TEXT, font=FONT_BODY)

    style.configure("TFrame", background=SURFACE)
    style.configure("TLabel", background=SURFACE, foreground=TEXT, font=FONT_BODY)
    style.configure("TCheckbutton", background=SURFACE, foreground=TEXT, font=FONT_BODY)
    style.configure("TRadiobutton", background=SURFACE, foreground=TEXT, font=FONT_BODY)

    # The chrome around the content: the window itself, the step rail, the
    # footer holding the navigation buttons.
    style.configure("Window.TFrame", background=BACKGROUND)
    style.configure("Window.TLabel", background=BACKGROUND, foreground=TEXT)

    # -- боковая навигация --------------------------------------------
    #
    # Своя поверхность, темнее содержимого: так видно, где кончается «где я» и
    # начинается «над чем я работаю», без единой разделительной линии. Активный
    # раздел отмечен не цветом текста, а заливкой -- цвет текста в списке уже
    # занят различием «раздел» и «выход из сеанса».
    style.configure("Sidebar.TFrame", background=SIDEBAR)
    style.configure(
        "SidebarTitle.TLabel", background=SIDEBAR, foreground=TEXT, font=FONT_SECTION
    )
    style.configure("SidebarItem.TLabel", background=SIDEBAR, foreground=TEXT, font=FONT_BODY)
    style.configure(
        "SidebarItemActive.TLabel",
        background=SIDEBAR_ACTIVE,
        foreground=TEXT_ON_ACCENT,
        font=FONT_BODY,
    )
    style.configure(
        "SidebarQuiet.TLabel", background=SIDEBAR, foreground=TEXT_MUTED, font=FONT_BODY
    )
    # Конец работы отмечен цветом опасности, потому что он необратим: с
    # 17.09.2026 закрытие сеанса завершает программу -- только смерть процесса
    # убирает из памяти секрет, из которого выводятся коды восстановления всех
    # носителей. Цвет предупреждает, а подпись при наведении объясняет: одного
    # цвета мало, чтобы понять, что будет.
    style.configure(
        "SidebarDanger.TLabel", background=SIDEBAR, foreground=DANGER, font=FONT_BODY
    )

    # -- вкладки -------------------------------------------------------
    #
    # Заведены вместе с окном свойств (17.09.2026). Без своего стиля «clam»
    # рисует вкладки в собственных серо-клетчатых тонах поверх нашей темы --
    # окно выглядело бы вставленным из другой программы.
    style.configure("TNotebook", background=BACKGROUND, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab",
        background=BACKGROUND,
        foreground=TEXT_MUTED,
        padding=(SPACE_MD, SPACE_SM),
        font=FONT_BODY,
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", SURFACE)],
        foreground=[("selected", TEXT)],
        expand=[("selected", (0, 0, 0, 0))],
    )

    # -- surfaces ------------------------------------------------------
    for suffix, colour in (("Card", SURFACE), ("Sunken", SURFACE_SUNKEN)):
        style.configure(f"{suffix}.TFrame", background=colour)
        style.configure(f"{suffix}.TLabel", background=colour, foreground=TEXT)
        style.configure(f"{suffix}.TCheckbutton", background=colour, foreground=TEXT)
        style.configure(f"{suffix}.TRadiobutton", background=colour, foreground=TEXT)

    # Disabled controls keep their surface and only lose contrast.
    #
    # clam substitutes its own grey background when a widget goes disabled,
    # which on a white card drew a grey block behind every greyed-out label --
    # so a section switched off (the kiosk options on a console system) looked
    # like a rendering fault rather than like something inactive.
    disabled_bg = [("disabled", SURFACE)]
    disabled_fg = [("disabled", TEXT_DISABLED)]
    for name in ("TLabel", "TCheckbutton", "TRadiobutton"):
        style.map(name, background=disabled_bg, foreground=disabled_fg)
        style.map(f"Card.{name}", background=disabled_bg, foreground=disabled_fg)
    for name in ("Hint.TLabel", "Muted.TLabel", "Section.TLabel"):
        style.map(name, background=disabled_bg, foreground=disabled_fg)
    style.map(
        "Sunken.TLabel",
        background=[("disabled", SURFACE_SUNKEN)],
        foreground=disabled_fg,
    )
    for name in ("Sunken.TCheckbutton", "Sunken.TRadiobutton", "HintSunken.TLabel"):
        style.map(
            name, background=[("disabled", SURFACE_SUNKEN)], foreground=disabled_fg
        )

    # -- text roles ----------------------------------------------------
    style.configure("Title.TLabel", font=FONT_TITLE, background=SURFACE)
    style.configure("Section.TLabel", font=FONT_SECTION, background=SURFACE)
    style.configure("SectionSunken.TLabel", font=FONT_SECTION, background=SURFACE_SUNKEN)
    style.configure("Hint.TLabel", font=FONT_SMALL, foreground=TEXT_MUTED, background=SURFACE)
    style.configure(
        "HintSunken.TLabel", font=FONT_SMALL, foreground=TEXT_MUTED, background=SURFACE_SUNKEN
    )
    style.configure("Muted.TLabel", foreground=TEXT_MUTED, background=SURFACE)
    style.configure("Warning.TLabel", foreground=WARNING, background=SURFACE, font=FONT_SMALL)
    style.configure("WarningSunken.TLabel", foreground=WARNING, background=SURFACE_SUNKEN, font=FONT_SMALL)

    # -- buttons -------------------------------------------------------
    style.configure(
        "TButton",
        background="#e4e7ec",
        foreground=TEXT,
        bordercolor=BORDER,
        focuscolor=ACCENT,
        padding=(SPACE_MD, SPACE_SM),
        font=FONT_BODY,
    )
    style.map(
        "TButton",
        background=[("active", "#d8dce3"), ("disabled", "#eef0f3")],
        foreground=[("disabled", TEXT_DISABLED)],
    )

    #: The primary action. Exactly one of these is visible at a time.
    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground=TEXT_ON_ACCENT,
        bordercolor=ACCENT,
        # Рамка фокуса -- светлая, иначе её нет вовсе: производные стили
        # наследуют `focuscolor` от `TButton`, а там он равен ACCENT, то есть
        # фону этой самой кнопки. Проверено на живом Tk: до правки
        # `lookup("Accent.TButton", "focuscolor")` возвращал тот же #2c5fb8, и
        # пользователь, дошедший до главной кнопки табом, не видел, что стоит
        # на ней.
        focuscolor=TEXT_ON_ACCENT,
        padding=(SPACE_MD, SPACE_SM),
        font=FONT_SECTION,
    )
    style.map(
        "Accent.TButton",
        background=[("active", ACCENT_ACTIVE), ("disabled", "#b9c4d8")],
        foreground=[("disabled", "#eef1f6")],
    )

    #: A destructive or leaving action: present, deliberately quiet.
    style.configure(
        "Quiet.TButton", background=BACKGROUND, foreground=TEXT_MUTED, bordercolor=BORDER,
        padding=(SPACE_MD, SPACE_SM),
    )
    style.map("Quiet.TButton", background=[("active", "#e0e3e8")], foreground=[("active", TEXT)])

    # -- inputs --------------------------------------------------------
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(
            name,
            fieldbackground=SURFACE,
            background=SURFACE,
            foreground=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            insertcolor=TEXT,
            padding=SPACE_XS,
        )
        style.map(
            name,
            bordercolor=[("focus", ACCENT)],
            lightcolor=[("focus", ACCENT)],
            fieldbackground=[("disabled", SURFACE_SUNKEN), ("readonly", SURFACE_SUNKEN)],
            foreground=[("disabled", TEXT_DISABLED)],
        )

    style.configure(
        "Treeview",
        background=SURFACE,
        fieldbackground=SURFACE,
        foreground=TEXT,
        bordercolor=BORDER,
        rowheight=24,
    )
    style.configure("Treeview.Heading", background="#e4e7ec", foreground=TEXT, font=FONT_SECTION)
    style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", TEXT_ON_ACCENT)])

    style.configure("TLabelframe", background=SURFACE, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=SURFACE, foreground=TEXT, font=FONT_SECTION)

    style.configure(
        "TProgressbar", background=ACCENT, troughcolor="#dde1e7", bordercolor=BORDER, thickness=6
    )
    style.configure("TSeparator", background=BORDER)
    style.configure("TScrollbar", background="#dfe3e8", troughcolor=BACKGROUND, bordercolor=BORDER)

    return style


def fixed_font(size: int = 10):
    """A monospaced font for anything that must line up column by column."""
    if using_gtk():
        return ("TkFixedFont", size)

    from .ui import font as tkfont

    font = tkfont.nametofont("TkFixedFont").copy()
    font.configure(size=size)
    return font
