"""Вкладка «Выпуски и группы» в «Обслуживании» (LIVE-2.0, Э7).

Как программы в групповых политиках: дистрибутив назначается группе, носитель
получает ближайшее назначение вверх по дереву, корень -- назначение по
умолчанию. Здесь администратор:

* публикует собранный дистрибутив выпуском на сервер доступа;
* задаёт минимальный номер выпуска -- ниже него сервер отвечает 426 и носитель
  обновляется до входа;
* заводит группы, назначает им дистрибутив и политику виртуальных машин.

Всё читается одним запросом `overview`, сеть -- в фоне.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, simpledialog, ttk

from ...admin import access_actions
from .. import theme
from ..widgets import Column, data_table, run_in_background, widget_alive

INHERIT = "по наследству"
DIST_COLUMNS = (
    Column("id", "Дистрибутив", 160, 110, stretch=True),
    Column("system", "Система", 120, 90),
    Column("latest", "Выпуск", 110, 90, numeric=True),
    Column("min_serial", "Не ниже", 110, 80, numeric=True),
)
GROUP_COLUMNS = (
    Column("id", "Группа", 100, 80, stretch=True),
    Column("parent", "Входит в", 80, 70),
    Column("assigned", "Назначено", 110, 90),
    Column("effective", "Действует", 110, 90),
    Column("vm", "ВМ", 90, 80),
    Column("count", "Носителей", 95, 95, numeric=True),
)


class ChoiceDialog(tk.Toplevel):
    """Выбор одного значения переключателями; `on_accept(значение)`.

    Переключатели, а не выпадающий список: вариантов единицы, и все они
    видны сразу -- в том числе текущий.
    """

    def __init__(self, parent, title: str, label: str, values: list[str], current: str,
                 on_accept) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.on_accept = on_accept
        body = ttk.Frame(self, padding=theme.SPACE_MD)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=label).pack(anchor="w", pady=(0, theme.SPACE_XS))
        self.var = tk.StringVar(value=current)
        for value in values:
            ttk.Radiobutton(body, text=value, value=value, variable=self.var).pack(anchor="w")
        ttk.Button(body, text="Готово", command=self._ok).pack(anchor="e", pady=(theme.SPACE_SM, 0))
        # Над окном программы, а не где придётся: без оконного менеджера (и
        # у некоторых с ним) окно вставало в левый верхний угол экрана.
        top = parent.winfo_toplevel()
        self.geometry(f"+{top.winfo_rootx() + 120}+{top.winfo_rooty() + 120}")
        self.grab_set()

    def _ok(self) -> None:
        value = self.var.get()
        self.destroy()
        self.on_accept(value)


class ReleasesTab:
    def __init__(self, page: ttk.Frame, frame) -> None:
        self.page = page
        self.frame = frame
        self.app = frame.app
        self.bar = frame.message_bar
        self.data: dict = {"groups": [], "distributions": [], "media": []}
        self._busy = False
        if not access_actions.is_connected(self.container):
            ttk.Label(page, text="Сервер доступа не подключён (вкладка «Ресурс отзыва»).",
                      style="Card.TLabel").pack(anchor="w")
            return

        dists = ttk.LabelFrame(page, text="Дистрибутивы", padding=theme.SPACE_SM)
        dists.pack(fill="both", expand=True)
        self.dist_tree, self.dist_empty = data_table(
            dists, DIST_COLUMNS, height=4, empty_text="Выпусков на сервере нет.")
        row = ttk.Frame(dists)
        row.pack(fill="x", pady=(theme.SPACE_XS, 0))
        self.publish_btn = ttk.Button(row, text="Опубликовать выпуск…", command=self._publish)
        self.publish_btn.pack(side="left")
        ttk.Button(row, text="Не ниже…", command=self._min_serial).pack(
            side="left", padx=theme.SPACE_XS)
        ttk.Button(row, text="Обновить", command=self.refresh).pack(side="right")
        self.progress_text = tk.StringVar()
        ttk.Label(dists, textvariable=self.progress_text, style="Hint.TLabel").pack(anchor="w")

        groups = ttk.LabelFrame(page, text="Группы", padding=theme.SPACE_SM)
        groups.pack(fill="both", expand=True, pady=(theme.SPACE_SM, 0))
        self.group_tree, self.group_empty = data_table(groups, GROUP_COLUMNS, height=5)
        row = ttk.Frame(groups)
        row.pack(fill="x", pady=(theme.SPACE_XS, 0))
        ttk.Button(row, text="Новая группа…", command=self._new_group).pack(side="left")
        ttk.Button(row, text="Назначить дистрибутив…", command=self._assign).pack(
            side="left", padx=theme.SPACE_XS)
        ttk.Button(row, text="Виртуальные машины…", command=self._vm_policy).pack(side="left")
        #: Поток выгрузки пишет сюда «файл i из n»; окно забирает по таймеру.
        self._upload_state = ""
        self.refresh()

    @property
    def container(self):
        return self.app.session.container

    # -- чтение -----------------------------------------------------------
    def refresh(self) -> None:
        run_in_background(self.page, lambda: access_actions.overview(self.container),
                          on_success=self._render,
                          on_error=lambda exc: self.bar.error(f"Сервер не ответил: {exc}"))

    def _render(self, data: dict) -> None:
        if not widget_alive(self.page):
            return
        self.data = data
        self.dist_tree.delete(*self.dist_tree.get_children())
        for dist in data["distributions"]:
            self.dist_tree.insert("", "end", iid=dist["id"], values=(
                dist["id"], f"{dist['family']}, {dist['arch']}",
                dist["releases"][0] if dist["releases"] else "—",
                dist["min_serial"] or "—"))
        self.dist_empty.update(not data["distributions"])
        counts: dict[str, int] = {}
        for media in data["media"]:
            counts[media["group"]] = counts.get(media["group"], 0) + 1
        self.group_tree.delete(*self.group_tree.get_children())
        for group in self._ordered(data["groups"]):
            if group["allow_vm"] is None:
                vm = ("да" if group["effective_vm"] else "нет") + ", насл."
            else:
                vm = "да" if group["allow_vm"] else "нет"
            self.group_tree.insert("", "end", iid=group["id"], values=(
                group["id"], group["parent"] or "", group["distribution"] or "",
                group["effective"] or "не назначено", vm, counts.get(group["id"], 0)))
        self.group_empty.update(False)

    @staticmethod
    def _ordered(groups: list[dict]) -> list[dict]:
        """Дерево обходом в глубину: подгруппа -- сразу под родителем."""
        children: dict = {}
        for group in groups:
            children.setdefault(group["parent"], []).append(group)
        out: list[dict] = []

        def walk(parent):
            for group in sorted(children.get(parent, []), key=lambda g: g["id"]):
                if group in out:
                    continue
                out.append(group)
                walk(group["id"])

        walk(None)
        out += [g for g in groups if g not in out]
        return out

    def _selected(self, tree) -> str:
        selection = tree.selection()
        return selection[0] if selection else ""

    def _act(self, fn, done: str) -> None:
        def ok(_result):
            self.app.save_container()
            self.bar.success(done)
            self.refresh()
        run_in_background(self.page, fn, on_success=ok,
                          on_error=lambda exc: self.bar.error(f"Не выполнено: {exc}"))

    # -- дистрибутивы -----------------------------------------------------
    def _publish(self) -> None:
        if self._busy:
            return
        directory = filedialog.askdirectory(parent=self.page, title="Каталог дистрибутива")
        if not directory:
            return
        from ...install import distribution

        try:
            metadata = distribution.read_metadata(directory)
        except Exception as exc:  # noqa: BLE001 - показать, почему каталог не подходит
            self.bar.error(f"Это не каталог дистрибутива: {exc}")
            return
        name = simpledialog.askstring(
            "Публикация", "Дистрибутив на сервере (линейка выпусков):",
            initialvalue=self._selected(self.dist_tree) or metadata.name, parent=self.page)
        if not name:
            return
        self._busy = True
        self.publish_btn.configure(state="disabled")

        def progress(index: int, total: int, file: str) -> None:
            self._upload_state = f"Выгружаю {file} ({index + 1} из {total})…"

        def finished(serial) -> None:
            self._busy = False
            self.app.save_container()
            if widget_alive(self.page):
                self.publish_btn.configure(state="normal")
                self.progress_text.set("")
                self.bar.success(f"Выпуск {serial} опубликован в {name.strip()}.")
                self.refresh()

        def failed(exc) -> None:
            self._busy = False
            if widget_alive(self.page):
                self.publish_btn.configure(state="normal")
                self.progress_text.set("")
                self.bar.error(f"Не опубликовано: {exc}")

        run_in_background(self.page, lambda: access_actions.publish_distribution(
            self.container, directory, name.strip(), progress), on_success=finished, on_error=failed)
        self._poll_upload()

    def _poll_upload(self) -> None:
        if not self._busy or not widget_alive(self.page):
            return
        self.progress_text.set(self._upload_state)
        self.page.after(500, self._poll_upload)

    def _min_serial(self) -> None:
        dist_id = self._selected(self.dist_tree)
        if not dist_id:
            self.bar.error("Выберите дистрибутив.")
            return
        line = next(d for d in self.data["distributions"] if d["id"] == dist_id)
        serial = simpledialog.askinteger(
            "Минимальный выпуск",
            f"{dist_id}: носители с выпуском ниже этого номера обновятся до входа "
            "(0 — без требования).",
            initialvalue=line["releases"][0] if line["releases"] else 0, minvalue=0,
            parent=self.page)
        if serial is None:
            return
        self._act(lambda: access_actions.set_min_serial(self.container, dist_id, serial),
                  f"{dist_id}: не ниже {serial}." if serial else f"{dist_id}: без требования.")

    # -- группы -----------------------------------------------------------
    def _new_group(self) -> None:
        parent = self._selected(self.group_tree) or "root"
        name = simpledialog.askstring("Новая группа", f"Имя группы (войдёт в «{parent}»):",
                                      parent=self.page)
        if name:
            self._act(lambda: access_actions.create_group(self.container, name.strip(), parent),
                      f"Группа {name.strip()} создана.")

    def _assign(self) -> None:
        group_id = self._selected(self.group_tree)
        if not group_id:
            self.bar.error("Выберите группу.")
            return
        group = next(g for g in self.data["groups"] if g["id"] == group_id)
        values = [INHERIT] + [d["id"] for d in self.data["distributions"]]

        def accept(value: str) -> None:
            dist = None if value == INHERIT else value
            self._act(lambda: access_actions.assign(self.container, group_id, dist),
                      f"{group_id}: {dist or 'по наследству'}.")

        ChoiceDialog(self.page, "Назначение", f"Дистрибутив группы «{group_id}»:", values,
                     group["distribution"] or INHERIT, accept)

    def _vm_policy(self) -> None:
        group_id = self._selected(self.group_tree)
        if not group_id:
            self.bar.error("Выберите группу.")
            return
        group = next(g for g in self.data["groups"] if g["id"] == group_id)
        choices = {"разрешён": True, "запрещён": False, INHERIT: None}
        current = next(k for k, v in choices.items() if v == group["allow_vm"])

        def accept(value: str) -> None:
            allow = choices[value]
            self._act(lambda: access_actions.set_vm_policy(self.container, group_id, allow),
                      f"{group_id}: вход с виртуальной машины {value}.")

        ChoiceDialog(self.page, "Виртуальные машины",
                     f"Вход с виртуальной машины для «{group_id}»:", list(choices), current, accept)


def build_releases_tab(notebook: ttk.Notebook, frame) -> ReleasesTab:
    page = ttk.Frame(notebook, style="Card.TFrame", padding=theme.SPACE_MD)
    notebook.add(page, text="Выпуски и группы")
    return ReleasesTab(page, frame)
