"""UC-3 "Управление файлом конфигурации установки", as a guided wizard.

Direct response to live RedOS testing feedback: the TUI's config screen let
a user click "Создать" and land on a single crowded form, with no
unmissable "what are you actually installing?" moment and no repository
package browsing wired into the same flow. Here, picking the target OS is
step 1 of 7 and cannot be skipped, and every later step's fields (release,
mirror defaults, sudo group name used elsewhere) follow from that choice.
"""
from __future__ import annotations

import re
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import json

from ...config.repo_browser import RepoPackage, get_package_source
from ...config.schema import (
    KNOWN_RELEASES,
    LUKS_CIPHERS,
    LUKS_KDFS,
    SUPPORTED_TARGET_OS,
    InstallConfig,
)
from ...config.validator import ConfigValidationUnavailableError
from ...errors import RepoBrowseError
from ..widgets import KNOWN_ERRORS, WizardFrame, add_choice, add_field, run_in_background

OS_LABELS = {
    "debian": "Debian (стандартный, debootstrap)",
    "redos": "RedOS (dnf --installroot)",
    "astra": "Astra Linux Common Edition (патченный debootstrap)",
}
OS_DESCRIPTIONS = {
    "debian": "Проверено против deb.debian.org.",
    "redos": "Проверено против repo1.red-soft.ru (релизы 7.3 / 8.0).",
    "astra": "Требует пакет debootstrap с патчем Astra на этом хосте; адрес зеркала не проверен вживую — укажите свой.",
}
DEFAULT_RELEASE = {"debian": "bookworm", "redos": "8.0", "astra": "orel"}
DEFAULT_MIRROR = {
    "debian": "https://deb.debian.org/debian",
    "redos": "https://repo1.red-soft.ru/redos/8.0/x86_64/os",
    "astra": "",
}
CATALOG_DISPLAY_LIMIT = 200


class ConfigWizardFrame(WizardFrame):
    def __init__(self, parent: tk.Widget, app, config_path: Path | None = None):
        self.config_path = config_path
        if config_path is not None:
            self.config = app.config_manager.open(config_path)
        else:
            self.config = InstallConfig.create_default(name="", target_os="debian")
            self.config.mirror = DEFAULT_MIRROR["debian"]
        self._catalog: list[RepoPackage] = []
        self._catalog_view: list[RepoPackage] = []
        self._sort_descending: dict[str, bool] = {}

        super().__init__(
            parent,
            app,
            step_titles=[
                "Имя и целевая ОС",
                "Учётная запись пользователя",
                "Диск и шифрование",
                "Удалённый доступ",
                "Пакеты для предустановки",
                "Проверка и сохранение",
            ],
            step_hints=[
                "Что вы устанавливаете на носитель? Это определяет всё, что будет дальше.",
                "Учётная запись, которая будет создана на установленной системе.",
                "Размеры служебных разделов и параметры шифрования системного раздела (LUKS).",
                "Необязательно: доступ по SSH на установленной системе.",
                "Необязательно: подключитесь к репозиторию выбранной ОС и выберите пакеты.",
                "Проверьте конфигурацию целиком, при необходимости поправьте вручную, затем сохраните.",
            ],
            finish_label="Сохранить конфигурацию",
        )

    # -- step 0: name + target OS ---------------------------------------
    def _build_step_basics(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        self.name_var = tk.StringVar(value=self.config.name)
        name_entry, row = add_field(form, 0, "Имя конфигурации:", self.name_var, width=36)
        if self.config_path is not None:
            name_entry.configure(state="disabled")

        ttk.Label(form, text="Целевая ОС:").grid(row=row, column=0, sticky="ne", padx=(0, 10), pady=(10, 4))
        os_frame = ttk.Frame(form)
        os_frame.grid(row=row, column=1, sticky="w", pady=(10, 4))
        self.target_os_var = tk.StringVar(value=self.config.target_os)
        self.os_description_var = tk.StringVar(value=OS_DESCRIPTIONS.get(self.config.target_os, ""))
        for target_os in SUPPORTED_TARGET_OS:
            ttk.Radiobutton(
                os_frame,
                text=OS_LABELS[target_os],
                value=target_os,
                variable=self.target_os_var,
                command=self._on_target_os_changed,
            ).pack(anchor="w")
        row += 1
        ttk.Label(form, textvariable=self.os_description_var, foreground="#777777", wraplength=480, justify="left").grid(
            row=row, column=1, sticky="w"
        )
        row += 1

        self.release_var = tk.StringVar(value=self.config.release or DEFAULT_RELEASE[self.config.target_os])
        # Editable rather than a closed list: the list covers the releases this
        # project has actually checked against real repositories, but a site can
        # run an internal mirror of something else, and the UI should not make
        # that impossible -- only unnecessary to remember.
        self.release_combo, row = add_choice(
            form,
            row,
            "Релиз/версия:",
            self.release_var,
            KNOWN_RELEASES.get(self.config.target_os, ()),
            hint="выберите из списка проверенных выпусков или впишите свой",
            width=22,
            editable=True,
        )
        self.mirror_var = tk.StringVar(value=self.config.mirror)
        add_field(
            form,
            row,
            "Зеркало репозитория:",
            self.mirror_var,
            hint="используется и для установки, и для просмотра пакетов на шаге 5",
            width=48,
        )

    def _on_target_os_changed(self) -> None:
        target_os = self.target_os_var.get()
        self.os_description_var.set(OS_DESCRIPTIONS.get(target_os, ""))
        # The release list belongs to the chosen OS: leaving Debian suite names
        # on offer after picking RedOS would invite exactly the mistake the list
        # exists to prevent.
        releases = KNOWN_RELEASES.get(target_os, ())
        self.release_combo.configure(values=list(releases))
        # Only replace release/mirror if they still hold another OS's default
        # (or are empty) -- never clobber something the user already typed.
        known_defaults = {r for group in KNOWN_RELEASES.values() for r in group}
        if self.release_var.get() in ("", *known_defaults):
            self.release_var.set(DEFAULT_RELEASE.get(target_os, ""))
        if self.mirror_var.get() in ("", *DEFAULT_MIRROR.values()):
            self.mirror_var.set(DEFAULT_MIRROR.get(target_os, ""))

    def _save_step_basics(self) -> None:
        self.config.name = self.name_var.get().strip()
        self.config.target_os = self.target_os_var.get()
        self.config.release = self.release_var.get().strip()
        self.config.mirror = self.mirror_var.get().strip()

    def _validate_step_basics(self) -> bool:
        if not self.config.name:
            self.message_bar.error("Укажите имя конфигурации.")
            return False
        if not self.config.release:
            self.message_bar.error("Укажите релиз/версию устанавливаемой ОС.")
            return False
        return True

    # -- step 1: user account --------------------------------------------
    def _build_step_account(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        account = self.config.account
        self.username_var = tk.StringVar(value=account.username)
        add_field(
            form,
            0,
            "Имя пользователя:",
            self.username_var,
            hint="строчные латинские буквы/цифры/подчёркивание/дефис, не начинается с цифры",
        )
        self.full_name_var = tk.StringVar(value=account.full_name)
        _, row = add_field(form, 2, "Полное имя (необязательно):", self.full_name_var)
        self.shell_var = tk.StringVar(value=account.shell)
        _, row = add_field(form, row, "Оболочка входа:", self.shell_var)
        self.min_password_var = tk.StringVar(value=str(account.min_password_length))
        _, row = add_field(form, row, "Мин. длина пароля пользователя:", self.min_password_var, width=6)
        self.sudo_var = tk.BooleanVar(value=account.sudo)
        ttk.Checkbutton(form, text="Разрешить администрирование (sudo)", variable=self.sudo_var).grid(
            row=row, column=1, sticky="w", pady=4
        )

    def _save_step_account(self) -> None:
        account = self.config.account
        account.username = self.username_var.get().strip()
        account.full_name = self.full_name_var.get().strip()
        account.shell = self.shell_var.get().strip() or "/bin/bash"
        account.sudo = self.sudo_var.get()
        try:
            account.min_password_length = int(self.min_password_var.get().strip())
        except ValueError:
            account.min_password_length = 0  # caught as invalid by validate_step below

    def _validate_step_account(self) -> bool:
        if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", self.config.account.username):
            self.message_bar.error(
                "Недопустимое имя пользователя (строчные латинские буквы/цифры/подчёркивание/дефис, "
                "не начинается с цифры)."
            )
            return False
        if self.config.account.min_password_length < 8:
            self.message_bar.error("Минимальная длина пользовательского пароля должна быть не менее 8.")
            return False
        return True

    # -- step 2: disk + LUKS ----------------------------------------------
    def _build_step_disk(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        disk = self.config.disk
        luks = self.config.luks
        self.esp_mb_var = tk.StringVar(value=str(disk.esp_partition_mb))
        _, row = add_field(
            form, 0, "EFI-раздел (МБ):", self.esp_mb_var, hint="не менее 128 МБ; сюда ставится загрузчик", width=10
        )
        self.boot_mb_var = tk.StringVar(value=str(disk.boot_partition_mb))
        _, row = add_field(
            form,
            row,
            "Раздел /boot (МБ):",
            self.boot_mb_var,
            hint="не менее 512 МБ; незашифрованный, хранит ядро и initramfs",
            width=10,
        )
        self.log_mb_var = tk.StringVar(value=str(disk.log_partition_mb))
        _, row = add_field(form, row, "Раздел логов (МБ):", self.log_mb_var, hint="не менее 64 МБ", width=10)
        self.exchange_mb_var = tk.StringVar(value=str(disk.exchange_partition_mb))
        _, row = add_field(
            form, row, "Раздел обмена (МБ):", self.exchange_mb_var, hint="не менее 64 МБ", width=10
        )
        ttk.Separator(form, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=8)
        row += 1
        self.cipher_var = tk.StringVar(value=luks.cipher)
        self.cipher_combo, row = add_choice(
            form,
            row,
            "Шифр LUKS:",
            self.cipher_var,
            LUKS_CIPHERS.keys(),
            hint="aes-xts-plain64 — то, что используют штатные установщики Debian и RHEL",
            width=24,
            on_change=self._on_cipher_changed,
        )
        self.key_size_var = tk.StringVar(value=str(luks.key_size_bits))
        self.key_size_combo, row = add_choice(
            form,
            row,
            "Размер ключа (бит):",
            self.key_size_var,
            [str(s) for s in LUKS_CIPHERS.get(luks.cipher, (512, 256))],
            hint=(
                "Список зависит от выбранного шифра — другие значения cryptsetup не примет. "
                "В режиме XTS ключ делится пополам, поэтому 512 бит — это AES-256, "
                "самый стойкий вариант AES; более длинного ключа для AES не существует. "
                "Стойкость к подбору пароля задаётся не этим полем, а функцией ниже."
            ),
            width=10,
            editable=True,
        )
        self.kdf_var = tk.StringVar(value=luks.kdf)
        self.kdf_combo, row = add_choice(
            form,
            row,
            "Формирование ключа:",
            self.kdf_var,
            LUKS_KDFS,
            hint=(
                "argon2id требует много памяти на каждую попытку и потому резко удорожает "
                "перебор пароля; pbkdf2 слабее и нужен только для совместимости"
            ),
            width=12,
        )
        self.attempts_var = tk.StringVar(value=str(luks.failed_attempts_limit))
        add_field(
            form, row, "Лимит попыток пароля:", self.attempts_var, hint="от 1 до 10", width=6
        )

    def _on_cipher_changed(self) -> None:
        allowed = LUKS_CIPHERS.get(self.cipher_var.get(), ())
        self.key_size_combo.configure(values=[str(s) for s in allowed])
        # A size the new cipher cannot take would fail inside cryptsetup halfway
        # through an install, so it is corrected here instead.
        if allowed and self.key_size_var.get().strip() not in {str(s) for s in allowed}:
            self.key_size_var.set(str(allowed[0]))

    def _save_step_disk(self) -> None:
        disk = self.config.disk
        luks = self.config.luks
        for var, attr, default in (
            (self.esp_mb_var, "esp_partition_mb", 0),
            (self.boot_mb_var, "boot_partition_mb", 0),
            (self.log_mb_var, "log_partition_mb", 0),
            (self.exchange_mb_var, "exchange_partition_mb", 0),
        ):
            try:
                setattr(disk, attr, int(var.get().strip()))
            except ValueError:
                setattr(disk, attr, default)
        luks.cipher = self.cipher_var.get().strip() or "aes-xts-plain64"
        luks.kdf = self.kdf_var.get().strip() or "argon2id"
        try:
            luks.key_size_bits = int(self.key_size_var.get().strip())
        except ValueError:
            luks.key_size_bits = 0
        try:
            luks.failed_attempts_limit = int(self.attempts_var.get().strip())
        except ValueError:
            luks.failed_attempts_limit = 0

    def _validate_step_disk(self) -> bool:
        disk = self.config.disk
        luks = self.config.luks
        if disk.esp_partition_mb < 128:
            self.message_bar.error("Размер EFI-раздела должен быть не менее 128 МБ.")
            return False
        if disk.boot_partition_mb < 512:
            self.message_bar.error("Размер раздела /boot должен быть не менее 512 МБ.")
            return False
        if disk.log_partition_mb < 64:
            self.message_bar.error("Размер раздела логов должен быть не менее 64 МБ.")
            return False
        if disk.exchange_partition_mb < 64:
            self.message_bar.error("Размер раздела обмена должен быть не менее 64 МБ.")
            return False
        if luks.cipher not in LUKS_CIPHERS:
            self.message_bar.error(
                f"Шифр {luks.cipher} не поддерживается. Доступны: {', '.join(LUKS_CIPHERS)}."
            )
            return False
        allowed = LUKS_CIPHERS[luks.cipher]
        if luks.key_size_bits not in allowed:
            self.message_bar.error(
                f"Шифр {luks.cipher} не принимает ключ {luks.key_size_bits} бит. "
                f"Допустимо: {', '.join(str(s) for s in allowed)}."
            )
            return False
        if luks.kdf not in LUKS_KDFS:
            self.message_bar.error(f"Функция формирования ключа {luks.kdf} не поддерживается.")
            return False
        if not (1 <= luks.failed_attempts_limit <= 10):
            self.message_bar.error("Лимит попыток пароля должен быть от 1 до 10.")
            return False
        return True

    # -- step 3: remote access --------------------------------------------
    def _build_step_remote(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        remote = self.config.remote_access
        self.remote_enabled_var = tk.BooleanVar(value=remote.enabled)
        ttk.Checkbutton(
            form, text="Включить удалённый доступ", variable=self.remote_enabled_var
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        # Only openssh is offered because only openssh is implemented: the
        # install puts openssh-server on the medium and enables the service.
        # A "vpn" choice used to sit here, but nothing acted on it -- it merely
        # got written into the configuration file, so a medium built with it
        # had no remote access at all.
        self.remote_method_var = tk.StringVar(value="openssh")
        ttk.Label(form, text="Метод:").grid(row=1, column=0, sticky="ne", padx=(0, 10))
        ttk.Label(form, text="OpenSSH (ставится openssh-server, служба включается)").grid(
            row=1, column=1, sticky="w"
        )
        self.remote_port_var = tk.StringVar(value=str(remote.port))
        add_field(form, 2, "Порт:", self.remote_port_var, width=8)

    def _save_step_remote(self) -> None:
        remote = self.config.remote_access
        remote.enabled = self.remote_enabled_var.get()
        remote.method = self.remote_method_var.get()
        try:
            remote.port = int(self.remote_port_var.get().strip())
        except ValueError:
            remote.port = 0

    def _validate_step_remote(self) -> bool:
        remote = self.config.remote_access
        if not remote.enabled:
            return True
        if not (0 < remote.port <= 65535):
            self.message_bar.error("Некорректный порт удалённого доступа (1-65535).")
            return False
        return True

    # -- step 4: packages ----------------------------------------------------
    def _build_step_packages(self, body: ttk.Frame) -> None:
        top = ttk.Frame(body)
        top.pack(fill="x")
        ttk.Label(top, text=f"Зеркало: {self.config.mirror or '(не задано)'}   Релиз: {self.config.release}").pack(
            anchor="w"
        )
        self.connect_btn = ttk.Button(top, text="Подключиться к репозиторию", command=self._connect_repo)
        self.connect_btn.pack(anchor="w", pady=(4, 8))

        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        filter_row = ttk.Frame(body)
        filter_row.pack(fill="x")
        ttk.Label(filter_row, text="Фильтр по имени/описанию:").pack(side="left")
        ttk.Entry(filter_row, textvariable=self.filter_var, width=40).pack(side="left", padx=(6, 0))

        self.catalog_label_var = tk.StringVar(value="Нажмите «Подключиться к репозиторию», чтобы увидеть пакеты.")
        ttk.Label(body, textvariable=self.catalog_label_var, foreground="#555555").pack(anchor="w", pady=(6, 2))

        lists = ttk.Frame(body)
        lists.pack(fill="both", expand=True)
        left = ttk.Frame(lists)
        left.pack(side="left", fill="both", expand=True)
        # selectmode="extended" is what makes Ctrl-click and Shift-click work;
        # the default "browse" allows exactly one row, which meant adding
        # twenty packages took twenty round trips through the button.
        self.catalog_table = ttk.Treeview(
            left, columns=("name", "version", "summary"), show="headings", height=10, selectmode="extended"
        )
        for col, text, width in (("name", "пакет", 140), ("version", "версия", 100), ("summary", "описание", 260)):
            self.catalog_table.heading(col, text=text, command=lambda c=col: self._sort_catalog(c))
            self.catalog_table.column(col, width=width, anchor="w")
        self.catalog_table.pack(fill="both", expand=True)
        ttk.Label(
            left,
            text="Несколько пакетов: Ctrl — по одному, Shift — диапазоном. Щелчок по заголовку столбца сортирует.",
            foreground="#777777",
            font=("TkDefaultFont", 9),
            wraplength=420,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Button(left, text="Добавить выбранные ->", command=self._add_package).pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(lists)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        ttk.Label(right, text="Выбрано для установки:").pack(anchor="w")
        self.selected_list = tk.Listbox(right, height=10, selectmode="extended")
        self.selected_list.pack(fill="both", expand=True)
        for name in self.config.packages:
            self.selected_list.insert("end", name)
        ttk.Button(right, text="<- Убрать выбранные", command=self._remove_package).pack(anchor="w", pady=(4, 0))

    def _sort_catalog(self, column: str) -> None:
        """Sorts the whole fetched catalogue, not just the rows on screen.

        Sorting only the visible page would be misleading: the table shows the
        first N of possibly tens of thousands of packages, so "sort by name
        descending" has to reach into the full list to mean anything.
        """
        if not self._catalog:
            return
        ascending = not self._sort_descending.get(column, False)
        self._sort_descending[column] = ascending
        key = {"name": lambda p: p.name.lower(),
               "version": lambda p: p.version,
               "summary": lambda p: p.summary.lower()}[column]
        self._catalog.sort(key=key, reverse=not ascending)
        for col, text in (("name", "пакет"), ("version", "версия"), ("summary", "описание")):
            marker = ""
            if col == column:
                marker = "  ↑" if ascending else "  ↓"
            self.catalog_table.heading(col, text=text + marker)
        self._apply_filter()

    def _connect_repo(self) -> None:
        target_os = self.config.target_os
        mirror = self.config.mirror
        release = self.config.release
        self.connect_btn.configure(state="disabled")
        self.message_bar.info(
            "Подключение к репозиторию… для больших репозиториев это может занять до минуты, окно останется отзывчивым."
        )

        def fetch():
            source = get_package_source(target_os)
            return source.list_packages(mirror, release)

        run_in_background(self, fetch, on_success=self._on_catalog_loaded, on_error=self._on_catalog_error)

    def _on_catalog_loaded(self, catalog: list[RepoPackage]) -> None:
        self.connect_btn.configure(state="normal")
        self._catalog = catalog
        self.message_bar.success(f"Загружено пакетов из репозитория: {len(catalog)}")
        self._refresh_catalog(catalog)

    def _on_catalog_error(self, exc: BaseException) -> None:
        self.connect_btn.configure(state="normal")
        if isinstance(exc, KNOWN_ERRORS):
            self.message_bar.error(str(exc))
        else:
            self.message_bar.error(f"Непредвиденная ошибка: {exc}")

    def _refresh_catalog(self, items: list[RepoPackage]) -> None:
        for row in self.catalog_table.get_children():
            self.catalog_table.delete(row)
        self._catalog_view = items[:CATALOG_DISPLAY_LIMIT]
        for pkg in self._catalog_view:
            self.catalog_table.insert("", "end", values=(pkg.name, pkg.version, pkg.summary))
        if len(items) > CATALOG_DISPLAY_LIMIT:
            self.catalog_label_var.set(
                f"Найдено в репозитории: {len(items)} — показаны первые {CATALOG_DISPLAY_LIMIT}, "
                "уточните фильтр, чтобы найти нужный пакет."
            )
        else:
            self.catalog_label_var.set(f"Найдено в репозитории: {len(items)}.")

    def _apply_filter(self) -> None:
        if not self._catalog:
            return
        query = self.filter_var.get().strip().lower()
        items = self._catalog if not query else [
            p for p in self._catalog if query in p.name.lower() or query in p.summary.lower()
        ]
        self._refresh_catalog(items)

    def _add_package(self) -> None:
        selection = self.catalog_table.selection()
        if not selection:
            self.message_bar.error("Выберите пакеты в списке репозитория (Ctrl или Shift — несколько сразу).")
            return
        added, already = [], 0
        for row_id in selection:
            pkg = self._catalog_view[self.catalog_table.index(row_id)]
            if pkg.name in self.config.packages:
                already += 1
                continue
            self.config.packages.append(pkg.name)
            self.selected_list.insert("end", pkg.name)
            added.append(pkg.name)
        if not added:
            self.message_bar.info("Все выбранные пакеты уже в конфигурации.")
            return
        note = f" (ещё {already} уже были в списке)" if already else ""
        if len(added) == 1:
            self.message_bar.success(f"Добавлен пакет «{added[0]}»{note}.")
        else:
            self.message_bar.success(f"Добавлено пакетов: {len(added)}{note}.")

    def _remove_package(self) -> None:
        selection = self.selected_list.curselection()
        if not selection:
            self.message_bar.error("Выберите пакеты в списке справа (Ctrl или Shift — несколько сразу).")
            return
        # Deleted back to front so the earlier indices stay valid.
        removed = []
        for index in sorted(selection, reverse=True):
            name = self.selected_list.get(index)
            self.selected_list.delete(index)
            if name in self.config.packages:
                self.config.packages.remove(name)
            removed.append(name)
        if len(removed) == 1:
            self.message_bar.success(f"Убран пакет «{removed[0]}».")
        else:
            self.message_bar.success(f"Убрано пакетов: {len(removed)}.")

    def _save_step_packages(self) -> None:
        return  # config.packages is kept in sync live by _add_package/_remove_package

    # -- step 5: review + edit + save --------------------------------------
    def _build_step_review(self, body: ttk.Frame) -> None:
        """The whole configuration, editable.

        This replaces the old read-only summary and the separate "system tasks"
        step. An administrator who wants to set something the wizard does not
        expose -- a system task, an unusual option -- edits it here, where the
        full document is visible, instead of through a step dedicated to typing
        shell commands into a list box.
        """
        ttk.Label(
            body,
            text="Файл конфигурации целиком. Можно править прямо здесь — изменения применятся по кнопке ниже.",
            foreground="#555555",
            wraplength=740,
            justify="left",
        ).pack(anchor="w")

        editor_frame = ttk.Frame(body)
        editor_frame.pack(fill="both", expand=True, pady=(6, 0))
        self.config_text = tk.Text(editor_frame, height=14, width=88, wrap="none", undo=True)
        y_scroll = ttk.Scrollbar(editor_frame, orient="vertical", command=self.config_text.yview)
        x_scroll = ttk.Scrollbar(editor_frame, orient="horizontal", command=self.config_text.xview)
        self.config_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        y_scroll.pack(side="right", fill="y")
        x_scroll.pack(side="bottom", fill="x")
        self.config_text.pack(side="left", fill="both", expand=True)
        self.config_text.insert("1.0", self._config_as_text())

        buttons = ttk.Frame(body)
        buttons.pack(anchor="w", pady=(8, 0))
        ttk.Button(buttons, text="Применить правки", command=self._apply_edits).pack(side="left")
        ttk.Button(buttons, text="Вернуть как было", command=self._reset_edits).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Проверить конфигурацию", command=self._run_validation).pack(side="left", padx=(6, 0))

        status = "не проверена"
        if self.config.validation_status == "valid":
            status = "корректна"
        elif self.config.validation_status == "invalid":
            status = "содержит ошибки: " + "; ".join(self.config.validation_errors)
        self.validation_status_var = tk.StringVar(value=f"Статус проверки: {status}")
        ttk.Label(body, textvariable=self.validation_status_var, wraplength=740, justify="left").pack(
            anchor="w", pady=(6, 0)
        )

    def _config_as_text(self) -> str:
        data = self.config.to_dict()
        # Validation state is derived, not authored: showing it invites editing
        # it, and a hand-set "valid" would let an unchecked configuration reach
        # the installer.
        for derived in ("validation_status", "validation_errors"):
            data.pop(derived, None)
        return json.dumps(data, ensure_ascii=False, indent=2)

    def _apply_edits(self) -> None:
        raw = self.config_text.get("1.0", "end").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.message_bar.error(f"Не разобрать JSON (строка {exc.lineno}, позиция {exc.colno}): {exc.msg}")
            return
        if not isinstance(data, dict):
            self.message_bar.error("Ожидается объект JSON верхнего уровня.")
            return
        data.setdefault("name", self.config.name)
        try:
            edited = InstallConfig.from_dict(data)
        except (TypeError, ValueError) as exc:
            self.message_bar.error(f"Конфигурация не принята: {exc}")
            return
        self.config = edited
        # Edits invalidate any earlier verdict -- the installer refuses configs
        # that are not marked valid, so this cannot smuggle an unchecked one
        # through.
        self.config.validation_status = "unchecked"
        self.config.validation_errors = []
        self.validation_status_var.set("Статус проверки: не проверена (после правки)")
        self.message_bar.success("Правки применены. Нажмите «Проверить конфигурацию».")

    def _has_unapplied_edits(self) -> bool:
        editor = getattr(self, "config_text", None)
        if editor is None or not editor.winfo_exists():
            return False
        try:
            shown = json.loads(editor.get("1.0", "end").strip())
        except json.JSONDecodeError:
            return True  # unparseable text is certainly not what is stored
        return shown != json.loads(self._config_as_text())

    def _reset_edits(self) -> None:
        self.config_text.delete("1.0", "end")
        self.config_text.insert("1.0", self._config_as_text())
        self.message_bar.info("Текст возвращён к сохранённому состоянию.")

    def _run_validation(self) -> None:
        try:
            self.app.config_manager.validate(self.config)
        except ConfigValidationUnavailableError as exc:
            self.message_bar.error(f"Проверка недоступна: {exc}")
            return
        if self.config.validation_status == "valid":
            self.validation_status_var.set("Статус проверки: корректна")
            self.message_bar.success("Конфигурация корректна.")
        else:
            self.validation_status_var.set("Статус проверки: содержит ошибки: " + "; ".join(self.config.validation_errors))
            self.message_bar.error("Конфигурация содержит ошибки, см. выше.")

    def _save_step_review(self) -> None:
        return

    # -- WizardFrame hooks ------------------------------------------------
    _STEP_BUILDERS = [
        _build_step_basics,
        _build_step_account,
        _build_step_disk,
        _build_step_remote,
        _build_step_packages,
        _build_step_review,
    ]
    _STEP_SAVERS = [
        _save_step_basics,
        _save_step_account,
        _save_step_disk,
        _save_step_remote,
        _save_step_packages,
        _save_step_review,
    ]
    _STEP_VALIDATORS = [
        _validate_step_basics,
        _validate_step_account,
        _validate_step_disk,
        _validate_step_remote,
        None,
        None,
    ]

    def build_step(self, index: int, body: ttk.Frame) -> None:
        self._STEP_BUILDERS[index](self, body)

    def save_step(self, index: int) -> None:
        self._STEP_SAVERS[index](self)

    def validate_step(self, index: int) -> bool:
        validator = self._STEP_VALIDATORS[index]
        if validator is None:
            return True
        return validator(self)

    def on_finish(self) -> None:
        # Text typed into the editor but never applied would otherwise be
        # dropped silently on save -- the user would watch their edit vanish
        # with a success message.
        if self._has_unapplied_edits():
            self.message_bar.error(
                "В тексте конфигурации есть неприменённые правки. Нажмите «Применить правки» "
                "или «Вернуть как было» — иначе они не сохранятся."
            )
            return
        if self.config_path is not None:
            path = self.config_path
        else:
            path = self.app.config_manager._path_for(self.app.configs_dir, self.config.name)
        with_error = False
        try:
            self.app.config_manager.save(self.config, path)
        except KNOWN_ERRORS as exc:
            self.message_bar.error(str(exc))
            with_error = True
        if with_error:
            return
        from .configs_list import ConfigsListFrame

        self.app.show_frame(ConfigsListFrame)

    def on_cancel(self) -> None:
        from .configs_list import ConfigsListFrame

        self.app.show_frame(ConfigsListFrame)
