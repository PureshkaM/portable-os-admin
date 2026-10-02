"""Вкладка «Доступ» окна свойств носителя (LIVE-2.0, этап Э7).

Что администратор делает с доступом одного носителя на сервере:

* выдаёт код активации -- он показывается один раз и передаётся пользователю
  другим каналом, не вместе с носителем;
* задаёт рабочие места: тип (RDP, VNC, SSH), адрес, порт, пользователь, домен,
  отпечаток (сертификат RDP, CA VNC, ключ хоста SSH), нужна ли смарт-карта;
* снимает блокировку (после клона или исчерпанных попыток TOTP);
* видит события носителя с сервера, уже расшифрованные.

Всё -- через `admin.access_actions`, сеть -- в фоне.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...admin import access_actions
from .. import theme
from ..widgets import Column, data_table, run_in_background

TYPES = ("rdp", "vnc", "ssh")
DEFAULT_PORTS = {"rdp": 3389, "vnc": 5900, "ssh": 22}
COLUMNS = (
    Column("name", "Имя", 160, 100, stretch=True),
    Column("type", "Тип", 50, 45),
    Column("host", "Адрес", 140, 100),
    Column("port", "Порт", 55, 50, numeric=True),
    Column("user", "Пользователь", 110, 90),
    Column("smartcard", "Карта", 55, 50),
)


class AccessTab:
    def __init__(self, window, page: ttk.Frame) -> None:
        self.window = window
        self.container = window.app.session.container
        self.media_id = window.media_id
        self.targets: list[dict] = []
        if not access_actions.is_connected(self.container):
            ttk.Label(page, text="Сервер доступа не подключён (Обслуживание → Подключиться).",
                      style="Card.TLabel").pack(anchor="w")
            return

        activation = ttk.LabelFrame(page, text="Активация", padding=theme.SPACE_SM)
        activation.pack(fill="x")
        ttk.Button(activation, text="Выдать код активации", command=self._issue).pack(anchor="w")
        self.code_var = tk.StringVar()
        ttk.Entry(activation, textvariable=self.code_var, state="readonly", width=16,
                  font=theme.FONT_SECTION).pack(anchor="w", pady=(theme.SPACE_XS, 0))
        ttk.Label(activation, style="Hint.TLabel", wraplength=theme.TEXT_WRAP,
                  text="Код действует 72 часа и один раз. Передайте его другим каналом, "
                       "не вместе с носителем.").pack(anchor="w")

        places = ttk.LabelFrame(page, text="Рабочие места", padding=theme.SPACE_SM)
        places.pack(fill="both", expand=True, pady=(theme.SPACE_SM, 0))
        self.tree, self.placeholder = data_table(places, COLUMNS, height=5,
                                                 empty_text="Рабочих мест нет.")
        row = ttk.Frame(places)
        row.pack(fill="x", pady=(theme.SPACE_XS, 0))
        ttk.Button(row, text="Добавить…", command=self._add).pack(side="left")
        ttk.Button(row, text="Изменить…", command=self._edit).pack(side="left", padx=theme.SPACE_XS)
        ttk.Button(row, text="Удалить", command=self._remove).pack(side="left")
        ttk.Button(row, text="Сохранить на сервере", command=self._save).pack(side="right")

        service = ttk.Frame(page)
        service.pack(fill="x", pady=(theme.SPACE_SM, 0))
        ttk.Button(service, text="Снять блокировку", command=self._unblock).pack(side="left")
        ttk.Button(service, text="События с сервера", command=self._load_events).pack(
            side="left", padx=theme.SPACE_XS)
        self.events = tk.Text(page, height=7, state="disabled")
        self.events.pack(fill="both", expand=True, pady=(theme.SPACE_XS, 0))
        run_in_background(
            window, lambda: access_actions.get_targets(self.container, self.media_id),
            on_success=lambda targets: (self.targets.extend(targets), self._render()),
            on_error=lambda exc: window.message_bar.error(f"Рабочие места не получены: {exc}"))

    # -- действия ---------------------------------------------------------
    def _issue(self) -> None:
        bar = self.window.message_bar
        run_in_background(
            self.window, lambda: access_actions.issue_activation(self.container, self.media_id),
            on_success=lambda code: (self.code_var.set(code), self.window.app.save_container(),
                                     bar.success("Код выдан. Прежняя привязка телефона погашена.")),
            on_error=lambda exc: bar.error(f"Код не выдан: {exc}"))

    def _render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for target in self.targets:
            self.tree.insert("", "end", values=[
                target.get(c.key, "") if c.key != "smartcard" else ("да" if target.get(c.key) else "")
                for c in COLUMNS])
        self.placeholder.update(not self.targets)

    def _add(self) -> None:
        TargetDialog(self.window, None, self._accept)

    def _edit(self) -> None:
        selected = self.tree.selection()
        if selected:
            index = self.tree.index(selected[0])
            TargetDialog(self.window, self.targets[index], lambda t: self._accept(t, index))

    def _accept(self, target: dict, index: int | None = None) -> None:
        if index is None:
            self.targets.append(target)
        else:
            self.targets[index] = target
        self._render()

    def _remove(self) -> None:
        selected = self.tree.selection()
        if selected:
            del self.targets[self.tree.index(selected[0])]
            self._render()

    def _save(self) -> None:
        bar = self.window.message_bar
        targets = [dict(t) for t in self.targets]
        run_in_background(
            self.window, lambda: access_actions.set_targets(self.container, self.media_id, targets),
            on_success=lambda n: (self.window.app.save_container(),
                                  bar.success(f"Рабочих мест на сервере: {n}.")),
            on_error=lambda exc: bar.error(f"Не сохранено: {exc}"))

    def _unblock(self) -> None:
        bar = self.window.message_bar
        run_in_background(
            self.window, lambda: access_actions.unblock(self.container, self.media_id),
            on_success=lambda _r: (self.window.app.save_container(),
                                   bar.success("Блокировка снята.")),
            on_error=lambda exc: bar.error(f"Не снята: {exc}"))

    def _load_events(self) -> None:
        def show(lines):
            self.events.configure(state="normal")
            self.events.delete("1.0", "end")
            self.events.insert("end", "\n".join(lines) or "Событий нет.")
            self.events.configure(state="disabled")
        run_in_background(
            self.window, lambda: access_actions.media_events(self.container, self.media_id),
            on_success=show,
            on_error=lambda exc: self.window.message_bar.error(f"Не получено: {exc}"))


class TargetDialog(tk.Toplevel):
    """Одно рабочее место."""

    def __init__(self, parent, target: dict | None, on_accept) -> None:
        super().__init__(parent)
        self.title("Рабочее место")
        self.transient(parent)
        self.on_accept = on_accept
        target = target or {"type": "rdp", "port": 3389}
        self.vars = {key: tk.StringVar(value=str(target.get(key, ""))) for key in
                     ("id", "name", "type", "host", "port", "user", "domain", "pin")}
        self.smartcard = tk.BooleanVar(value=bool(target.get("smartcard")))
        self.tls_only = tk.BooleanVar(value=target.get("security") == "tls")
        form = ttk.Frame(self, padding=theme.SPACE_SM)
        form.pack(fill="both", expand=True)
        labels = (("name", "Имя"), ("type", "Тип"), ("host", "Адрес"), ("port", "Порт"),
                  ("user", "Пользователь"), ("domain", "Домен"),
                  ("pin", "Отпечаток (RDP: sha256:…; SSH: ssh-ed25519 …; VNC: CA в PEM)"))
        for row, (key, title) in enumerate(labels):
            ttk.Label(form, text=title).grid(row=row, column=0, sticky="w")
            if key == "type":
                widget = ttk.Combobox(form, textvariable=self.vars[key], values=TYPES,
                                      state="readonly", width=8)
                widget.bind("<<ComboboxSelected>>", self._default_port)
            else:
                widget = ttk.Entry(form, textvariable=self.vars[key], width=48)
            widget.grid(row=row, column=1, sticky="we", pady=2)
        ttk.Checkbutton(form, text="Вход по смарт-карте (токен рабочего места)",
                        variable=self.smartcard).grid(row=len(labels), column=1, sticky="w")
        ttk.Checkbutton(form, text="RDP без NLA (сервер xrdp)",
                        variable=self.tls_only).grid(row=len(labels) + 1, column=1, sticky="w")
        self.error = ttk.Label(form, foreground=theme.DANGER)
        self.error.grid(row=len(labels) + 2, column=0, columnspan=2, sticky="w")
        ttk.Button(form, text="Готово", command=self._ok).grid(row=len(labels) + 3, column=1, sticky="e")

    def _default_port(self, _event=None) -> None:
        self.vars["port"].set(str(DEFAULT_PORTS[self.vars["type"].get()]))

    def _ok(self) -> None:
        values = {k: v.get().strip() for k, v in self.vars.items()}
        if not values["host"] or not values["pin"] or not values["port"].isdigit():
            self.error.configure(text="Нужны адрес, порт и отпечаток.")
            return
        values["port"] = int(values["port"])
        values["id"] = values["id"] or f"{values['type']}-{values['host']}".replace(":", "-")[:60]
        values["name"] = values["name"] or values["host"]
        values["smartcard"] = self.smartcard.get()
        values["security"] = "tls" if self.tls_only.get() and values["type"] == "rdp" else "nla"
        self.on_accept(values)
        self.destroy()


def build_access_tab(window) -> AccessTab:
    return AccessTab(window, window.tab("Доступ"))
