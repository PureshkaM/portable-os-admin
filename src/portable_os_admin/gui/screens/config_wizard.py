"""UC-3 "Управление файлом конфигурации установки", as a guided wizard.

Direct response to live RedOS testing feedback: the TUI's config screen let
a user click "Создать" and land on a single crowded form, with no
unmissable "what are you actually installing?" moment and no repository
package browsing wired into the same flow. Here, picking the target OS is
step 1 of 7 and cannot be skipped, and every later step's fields (release,
mirror defaults, sudo group name used elsewhere) follow from that choice.
"""
from __future__ import annotations

import platform
import re
import tkinter as tk
from tkinter import ttk

import json

from ...config.repo_browser import RepoPackage, get_package_source
from ...admin import read_image_facts
from ...config.schema import (
    LUKS_CIPHERS,
    LUKS_KDFS,
    InstallConfig,
)
from ...config.validator import ConfigValidationUnavailableError, boot_warnings
from ...install import machine_binding
from ...install.emulation import probe as probe_emulation
from .. import theme
from ..widgets import (
    KNOWN_ERRORS,
    Column,
    WizardFrame,
    add_choice,
    add_field,
    data_table,
    run_in_background,
)

def _distributions() -> "dict[str, type]":
    """Семейства, которые умеет ставить программа, из реестра бэкендов.

    Экран больше не хранит про них ничего своего. До 02.09.2026 здесь лежали
    четыре словаря -- подписи, происхождение, выпуски и зеркала, -- и третий
    дистрибутив потребовал бы правки в мастере, хотя знает про себя только
    бэкенд. Теперь добавление семейства -- это один класс, а список в мастере
    складывается сам.
    """
    from ...install.backends import BACKENDS

    return {name: cls for name, cls in BACKENDS.items() if cls.TITLE}


def _os_label(target_os: str) -> str:
    cls = _distributions().get(target_os)
    return cls.TITLE if cls else target_os


def _os_description(target_os: str) -> str:
    cls = _distributions().get(target_os)
    return cls.PROVENANCE if cls else ""


def _releases_of(target_os: str) -> "tuple[str, ...]":
    cls = _distributions().get(target_os)
    return cls.RELEASES if cls else ()


def _default_release(target_os: str) -> str:
    cls = _distributions().get(target_os)
    return cls.DEFAULT_RELEASE if cls else ""


def _default_mirror(target_os: str, release: str = "", arch: str = "") -> str:
    cls = _distributions().get(target_os)
    return cls().default_mirror(release, arch) if cls else ""
CATALOG_DISPLAY_LIMIT = 200

#: Shown instead of an empty architecture field, which is what "same as this
#: machine" is stored as. An empty combobox reads as "not filled in yet"
#: rather than as a decision.
ARCH_SAME_AS_HOST = "как у этой машины"
#: Offered spellings. One per architecture rather than both synonyms: the
#: schema accepts amd64/x86_64 and arm64/aarch64 alike because users copy
#: whichever they saw, but a list offering the same thing twice is a list that
#: makes people wonder what the difference is.
ARCH_CHOICES = ("amd64", "arm64")


class ConfigWizardFrame(WizardFrame):
    def __init__(self, parent: tk.Widget, app, config_name: str | None = None):
        #: Имя правимой конфигурации, если открыт не мастер создания. Имя, а
        #: не путь: конфигурации живут в контейнере, и адресуются именем.
        self.config_name = config_name
        if config_name is not None:
            self.config = app.config_manager.open(app.container, config_name)
        else:
            # A new configuration starts as the locked-down minimum, not as a
            # blank system. What the wizard opens on is a statement about what
            # this tool builds, and every later step is the administrator
            # opening something on purpose.
            # Зеркало приходит вместе с базовой конфигурацией: его знает
            # семейство, а не этот экран.
            self.config = InstallConfig.create_baseline(name="", target_os="debian")
        # Kept for the change list on the last step: what the administrator
        # started from, so "what did I actually alter?" has an answer.
        self._baseline = InstallConfig.create_baseline(
            name=self.config.name, target_os=self.config.target_os
        )

        self._catalog: list[RepoPackage] = []
        self._catalog_view: list[RepoPackage] = []
        self._sort_descending: dict[str, bool] = {}

        super().__init__(
            parent,
            app,
            # Заголовки всё равно пересобираются по плану шагов сразу после
            # конструктора; здесь -- полный набор, чтобы каркас знал их число
            # до первой отрисовки.
            step_titles=[title for _key, title, _hint in self._expert_plan()],
            step_hints=[hint for _key, _title, hint in self._expert_plan()],
            finish_label="Сохранить конфигурацию",
        )

    supports_modes = True
    mode_preference_key = "config_wizard_expert"

    #: Полный набор шагов -- то, что видно в экспертном режиме.
    @staticmethod
    def _expert_plan() -> "list[tuple[str, str, str]]":
        return [
            ("basics", "Имя и целевая ОС", ""),
            ("account", "Учётная запись пользователя", ""),
            ("disk", "Диск и шифрование", ""),
            ("remote", "Удалённый доступ", "Необязательно."),
            ("packages", "Пакеты для предустановки", "Необязательно."),
            ("profile", "Режим системы и обновления", ""),
            ("review", "Проверка и сохранение", ""),
        ]

    def step_plan(self) -> "list[tuple[str, str, str]]":
        """Какие шаги показывать в текущем режиме.

        Упрощённый режим -- не «мастер попроще», а ответ на вопрос, от чего
        действительно зависит судьба собираемого образа. Программа начинает с
        минимальной защищённой системы и сама знает, каким должен быть шифр,
        как разбит диск и что удалённый доступ выключен; спрашивать об этом
        того, кому нужен обычный киоск, значит требовать решения там, где
        решение уже принято и обосновано.

        Поэтому здесь исчезает целый шаг -- удалённый доступ, выключенный по
        умолчанию, -- а внутри оставшихся показывается только то, что меняет
        результат: какая система, на каком языке, с какими приложениями, для
        скольких машин и с какой средой.

        Экспертный режим ничего не переставляет, а добавляет: NN/g советует
        ровно один уровень раскрытия, а не лестницу, -- поэтому шаги и поля
        стоят на прежних местах, просто их становится больше.
        """
        plan = self._expert_plan()
        if self.expert_mode:
            return plan
        return [step for step in plan if step[0] != "remote"]

    def _expert_block(self, parent, title: str) -> "ttk.Frame | None":
        """Рамка для полей, которые видны только в экспертном режиме.

        Возвращает None в упрощённом -- вызывающему достаточно проверить
        результат, и ни одного `if self.expert_mode` в теле шага не появится.
        """
        if not self.expert_mode:
            return None
        block = ttk.Frame(parent)
        if title:
            ttk.Label(block, text=title, font=theme.FONT_SECTION).pack(anchor="w", pady=(theme.SPACE_MD, theme.SPACE_XS))
        return block

    # -- step 0: name + target OS ---------------------------------------
    def _build_step_basics(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        self.name_var = tk.StringVar(value=self.config.name)
        name_entry, row = add_field(form, 0, "Имя конфигурации:", self.name_var, width=36)
        if self.config_name is not None:
            name_entry.configure(state="disabled")

        # Образ спрашивается первым и один раз: из него читаются семейство,
        # выпуск, архитектура, языки, пояса, раскладки и приложения. До
        # 16.09.2026 эти значения брались из кортежей в коде, а образ
        # предлагалось прочитать кнопкой на шаге пакетов -- то есть правда о
        # дистрибутиве была делом добровольным и лежала не на своём месте.
        ttk.Label(form, text="Установочный образ:").grid(
            row=row, column=0, sticky="ne", padx=(0, 10), pady=(10, 4)
        )
        self.image_var = tk.StringVar(value=getattr(self, "_image_path", ""))
        image_row_frame = ttk.Frame(form)
        image_row_frame.grid(row=row, column=1, sticky="w", pady=(10, 4))
        ttk.Entry(
            image_row_frame, textvariable=self.image_var, width=34, state="readonly"
        ).pack(side="left")
        ttk.Button(
            image_row_frame, text="Выбрать…", command=self._choose_image
        ).pack(side="left", padx=(theme.SPACE_XS, 0))
        row += 1
        self.image_status_var = tk.StringVar(value=self._image_status_text())
        ttk.Label(
            form,
            textvariable=self.image_status_var,
            style="Hint.TLabel",
            wraplength=theme.HINT_WRAP,
            justify="left",
        ).grid(row=row, column=1, sticky="w", pady=(0, theme.SPACE_XS))
        row += 1

        ttk.Label(form, text="Целевая ОС:").grid(row=row, column=0, sticky="ne", padx=(0, 10), pady=(10, 4))
        os_frame = ttk.Frame(form)
        os_frame.grid(row=row, column=1, sticky="w", pady=(10, 4))
        self.target_os_var = tk.StringVar(value=self.config.target_os)
        self.os_description_var = tk.StringVar(value=_os_description(self.config.target_os))
        for target_os in _distributions():
            ttk.Radiobutton(
                os_frame,
                text=_os_label(target_os),
                value=target_os,
                variable=self.target_os_var,
                command=self._on_target_os_changed,
            ).pack(anchor="w")
        row += 1
        ttk.Label(form, textvariable=self.os_description_var, foreground=theme.TEXT_MUTED, wraplength=480, justify="left").grid(
            row=row, column=1, sticky="w"
        )
        row += 1

        self.release_var = tk.StringVar(value=self.config.release or _default_release(self.config.target_os))
        # Editable rather than a closed list: the list covers the releases this
        # project has actually checked against real repositories, but a site can
        # run an internal mirror of something else, and the UI should not make
        # that impossible -- only unnecessary to remember.
        self.release_combo, row = add_choice(
            form,
            row,
            "Релиз/версия:",
            self.release_var,
            _releases_of(self.config.target_os),
            hint="выберите из списка проверенных выпусков или впишите свой",
            width=22,
            editable=True,
        )
        self.mirror_var = tk.StringVar(value=self.config.mirror)
        if self.expert_mode:
            # Адрес репозитория знает само семейство, и по умолчанию он верный.
            # Спрашивать его у того, кто собирает обычный носитель, значит
            # предлагать испортить рабочее умолчание.
            _, row = add_field(
                form,
                row,
                "Зеркало репозитория:",
                self.mirror_var,
                hint="используется и для установки, и для просмотра пакетов на шаге «Пакеты»",
                width=48,
            )

        # Architecture is a first-class choice, not a field only reachable by
        # editing the JSON. An administrator on an arm64 laptop building sticks
        # for amd64 desktops is the normal case, not an exotic one -- and left
        # empty this means "same as this machine", so the installer refuses an
        # image of any other architecture before it starts.
        self.arch_var = tk.StringVar(value=self.config.arch or ARCH_SAME_AS_HOST)
        self.arch_combo, row = add_choice(
            form,
            row,
            "Архитектура носителя:",
            self.arch_var,
            self._arch_values(),
            width=22,
            on_change=self._on_arch_changed,
        )
        self.arch_status_var = tk.StringVar()
        self.arch_status = ttk.Label(
            form,
            textvariable=self.arch_status_var,
            style="Hint.TLabel",
            # HINT_WRAP, not TEXT_WRAP: this sits in the form's second column,
            # which already starts well to the right, and the full width pushed
            # the end of the sentence off the card.
            wraplength=theme.HINT_WRAP,
            justify="left",
        )
        self.arch_status.grid(row=row, column=1, sticky="w", pady=(0, theme.SPACE_XS))
        self._on_arch_changed()
        row += 1

        self.hostname_var = tk.StringVar(value=self.config.hostname)
        if self.expert_mode:
            _, row = add_field(
                form, row, "Имя в сети:", self.hostname_var,
                hint="у всех носителей организации оно не должно быть одинаковым",
                width=24,
            )

        loc = self.config.localisation
        self.locale_var = tk.StringVar(value=loc.locale)
        self.locale_combo, row = add_choice(
            form, row, "Язык системы:", self.locale_var,
            self._choice_values("locales"), width=22, editable=True,
        )
        self.keymap_var = tk.StringVar(value=loc.keymap)
        if self.expert_mode:
            # Консоли у пользователя нет: текстовые входы замаскированы, а
            # переключение на них запрещено. Раскладка консоли остаётся
            # осмысленной только для того, кто собирает консольную систему.
            self.keymap_combo, row = add_choice(
                form, row, "Раскладка консоли:", self.keymap_var,
                self._choice_values("keyboard_layouts"), width=22, editable=True,
            )
        self.x11_layouts_var = tk.StringVar(value=loc.x11_layouts)
        _, row = add_field(
            form, row, "Раскладки рабочего стола:", self.x11_layouts_var,
            hint="через запятую; пусто — как в консоли",
            width=22,
        )
        self.kernel_params_var = tk.StringVar(value=self.config.boot.kernel_parameters)
        if self.expert_mode:
            _, row = add_field(
                form, row, "Параметры ядра:", self.kernel_params_var,
                hint="добавляются к тем, что задаёт программа; nomodeset отключает KMS — сеанс не запустится",
                width=36,
            )
        # Подробная загрузка -- в обоих режимах: это первое, о чём просят, когда
        # носитель не грузится, и искать её в экспертном незачем.
        self.verbose_boot_var = tk.BooleanVar(value=self.config.boot.verbose)
        ttk.Checkbutton(
            form, text="Подробная загрузка",
            variable=self.verbose_boot_var,
        ).grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        self.serial_console_var = tk.BooleanVar(value=self.config.boot.serial_console)
        if self.expert_mode:
            # Только для стенда. Экран остаётся главной консолью -- в отличие
            # от `console=` в параметрах ядра, из-за которого пароль вводили
            # вслепую.
            ttk.Checkbutton(
                form, text="Дублировать вывод в последовательный порт (ttyS0)",
                variable=self.serial_console_var,
            ).grid(row=row, column=1, sticky="w", pady=4)
            row += 1
        self.timezone_var = tk.StringVar(value=loc.timezone)
        self.timezone_combo, row = add_choice(
            form, row, "Часовой пояс:", self.timezone_var,
            self._choice_values("timezones"),
            width=22, editable=True,
        )

    def _on_arch_changed(self) -> None:
        """Says now whether this machine can build for the chosen architecture.

        Building for a foreign architecture works -- every chroot step runs
        through the emulator the kernel dispatches to -- but only where one is
        registered. Answering here, while the administrator is choosing, beats
        refusing at install time: by then they have picked an image, a medium
        and a passphrase.
        """
        chosen = self.arch_var.get()
        if chosen == ARCH_SAME_AS_HOST:
            self.arch_status_var.set(
                f"Как у этой машины ({platform.machine()}). Образ другой архитектуры "
                "будет отклонён."
            )
            self.arch_status.configure(style="Hint.TLabel")
            return

        emulation = probe_emulation(chosen)
        if emulation.native:
            self.arch_status_var.set("Родная архитектура этой машины — эмуляция не нужна.")
            self.arch_status.configure(style="Hint.TLabel")
        elif emulation.available:
            self.arch_status_var.set(
                f"Чужая архитектура. Эмулятор найден ({emulation.registration}), сборка "
                "возможна: шаги внутри chroot пойдут через него и займут заметно больше "
                "времени. Загрузку такого носителя проверьте на машине нужной архитектуры."
            )
            self.arch_status.configure(style="Hint.TLabel")
        else:
            self.arch_status_var.set(f"Собрать не получится: {emulation.reason}")
            self.arch_status.configure(style="Warning.TLabel")

    def _on_target_os_changed(self) -> None:
        target_os = self.target_os_var.get()
        self.os_description_var.set(_os_description(target_os))
        # The release list belongs to the chosen OS: leaving Debian suite names
        # on offer after picking RedOS would invite exactly the mistake the list
        # exists to prevent.
        releases = _releases_of(target_os)
        self.release_combo.configure(values=list(releases))
        # Only replace release/mirror if they still hold another OS's default
        # (or are empty) -- never clobber something the user already typed.
        distributions = _distributions()
        known_defaults = {r for cls in distributions.values() for r in cls.RELEASES}
        if self.release_var.get() in ("", *known_defaults):
            self.release_var.set(_default_release(target_os))
        known_mirrors = {cls().default_mirror() for cls in distributions.values()}
        known_mirrors |= {
            cls().default_mirror(release)
            for cls in distributions.values()
            for release in cls.RELEASES
        }
        if self.mirror_var.get() in ("", *known_mirrors):
            self.mirror_var.set(_default_mirror(target_os, self.release_var.get()))

    def _save_step_basics(self) -> None:
        self.config.name = self.name_var.get().strip()
        self.config.target_os = self.target_os_var.get()
        self.config.release = self.release_var.get().strip()
        self.config.mirror = self.mirror_var.get().strip()
        # "как у этой машины" is the label for an empty value, which is what
        # the schema means by "same architecture as the host".
        chosen_arch = self.arch_var.get()
        self.config.arch = "" if chosen_arch == ARCH_SAME_AS_HOST else chosen_arch
        self.config.hostname = self.hostname_var.get().strip() or "portable-os"
        self.config.localisation.locale = self.locale_var.get().strip()
        self.config.localisation.keymap = self.keymap_var.get().strip()
        self.config.localisation.x11_layouts = self.x11_layouts_var.get().strip()
        self.config.localisation.timezone = self.timezone_var.get().strip()
        self.config.boot.kernel_parameters = self.kernel_params_var.get().strip()
        self.config.boot.verbose = self.verbose_boot_var.get()
        self.config.boot.serial_console = self.serial_console_var.get()

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
        self.shell_var = tk.StringVar(value=account.shell)
        self.min_password_var = tk.StringVar(value=str(account.min_password_length))
        self.sudo_var = tk.BooleanVar(value=account.sudo)
        if not self.expert_mode:
            # В упрощённом режиме учётная запись -- это только её имя.
            #
            # Пароль ей всё равно заперт (вход автоматический), оболочка не
            # используется -- текстовые входы замаскированы, -- а полное имя
            # никто не увидит. Права администратора в минимальной защищённой
            # системе не выдаются вовсе: это ровно та дверь, ради закрытия
            # которой всё и строится.
            ttk.Label(
                form,
                text="Пароль этой записи заперт, вход в сеанс автоматический, "
                "прав администратора у неё нет.",
                style="Hint.TLabel",
                wraplength=theme.TEXT_WRAP,
                justify="left",
            ).grid(row=2, column=1, sticky="w", pady=(theme.SPACE_SM, 0))
            return
        _, row = add_field(form, 2, "Полное имя (необязательно):", self.full_name_var)
        _, row = add_field(form, row, "Оболочка входа:", self.shell_var)
        _, row = add_field(form, row, "Мин. длина пароля пользователя:", self.min_password_var, width=6)
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
        self.boot_mb_var = tk.StringVar(value=str(disk.boot_partition_mb))
        self.log_mb_var = tk.StringVar(value=str(disk.log_partition_mb))
        row = 0
        if self.expert_mode:
            # Размеры служебных разделов -- решение, которое программа уже
            # приняла и обосновала: загрузчику хватает 512 МБ, ядру с initramfs
            # -- гигабайта, журналам -- 512 МБ. Менять их осмысленно разве что
            # под очень маленький носитель, и тогда это делает тот, кто знает
            # зачем.
            _, row = add_field(
                form, 0, "EFI-раздел (МБ):", self.esp_mb_var,
                hint="не менее 128 МБ; сюда ставится загрузчик", width=10,
            )
            _, row = add_field(
                form,
                row,
                "Раздел /boot (МБ):",
                self.boot_mb_var,
                hint="не менее 512 МБ; незашифрованный, хранит ядро и initramfs",
                width=10,
            )
            _, row = add_field(
                form, row, "Раздел логов (МБ):", self.log_mb_var, hint="не менее 64 МБ", width=10
            )
        self.exchange_mb_var = tk.StringVar(value=str(disk.exchange_partition_mb))
        _, row = add_field(
            form, row, "Раздел обмена (МБ):", self.exchange_mb_var,
            hint="через него администратор передаёт пользователю файлы и настройки "
            "доустановленных программ; не менее 64 МБ",
            width=10,
        )
        self.cipher_var = tk.StringVar(value=luks.cipher)
        self.key_size_var = tk.StringVar(value=str(luks.key_size_bits))
        self.kdf_var = tk.StringVar(value=luks.kdf)
        self.attempts_var = tk.StringVar(value=str(luks.failed_attempts_limit))
        if self.expert_mode:
            # Шифр, длина ключа и функция формирования ключа -- то, за что
            # отвечает программа. Умолчания (aes-xts-plain64, 512 бит,
            # argon2id) -- это то, что ставят штатные установщики Debian и
            # RHEL, и менять их без причины значит только ослаблять носитель.
            ttk.Separator(form, orient="horizontal").grid(
                row=row, column=0, columnspan=2, sticky="ew", pady=8
            )
            row += 1
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
            self.key_size_combo, row = add_choice(
                form,
                row,
                "Размер ключа (бит):",
                self.key_size_var,
                [str(size) for size in LUKS_CIPHERS.get(luks.cipher, (512, 256))],
                hint=(
                    "Список зависит от выбранного шифра — другие значения cryptsetup не примет. "
                    "В режиме XTS ключ делится пополам, поэтому 512 бит — это AES-256, "
                    "самый стойкий вариант AES; более длинного ключа для AES не существует. "
                    "Стойкость к подбору пароля задаётся не этим полем, а функцией ниже."
                ),
                width=10,
                editable=True,
            )
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
            _, row = add_field(
                form, row, "Лимит попыток пароля:", self.attempts_var,
                hint="от 1 до 10", width=6,
            )
        self.binding_slots_var = tk.StringVar(value=str(self.config.machine_binding.slots))
        add_field(
            form,
            row,
            "Разных машин на носитель:",
            self.binding_slots_var,
            hint=(
                "0 — ограничения нет. Пока свободные слоты есть, фраза открывает носитель "
                "на любой машине; запрет начинает действовать, когда занят последний. "
                "Освободить слот можно только через администратора"
            ),
            width=6,
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
        try:
            self.config.machine_binding.slots = int(self.binding_slots_var.get().strip())
        except ValueError:
            # -1, а не 0: 0 -- это осмысленный выбор «без ограничения», и молча
            # выдать его за набранную ерунду значило бы отключить защиту вместо
            # того, чтобы сказать об ошибке. Валидация ниже отвергает -1.
            self.config.machine_binding.slots = -1

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
        slots = self.config.machine_binding.slots
        if not (0 <= slots <= machine_binding.MAX_SLOTS):
            self.message_bar.error(
                f"Число машин должно быть от 0 до {machine_binding.MAX_SLOTS}."
            )
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
        # Образ -- основной источник, зеркало -- запасной: в закрытом контуре
        # сети нет, и кнопка, требующая её, была единственной на шаге.
        sources = ttk.Frame(top)
        sources.pack(anchor="w", pady=(4, 8))
        # Каталог читается сам, когда шаг открывается: образ уже выбран, и
        # требовать нажатия ради того, что программа может сделать без спроса,
        # значит тратить внимание администратора впустую. Кнопка остаётся для
        # обратного случая -- перечитать, если образ подменили.
        self.image_packages_btn = ttk.Button(
            sources, text="Перечитать из образа", command=self._load_packages_from_image
        )
        self.image_packages_btn.pack(side="left")
        self.connect_btn = ttk.Button(sources, text="Подключиться к репозиторию", command=self._connect_repo)
        self.connect_btn.pack(side="left", padx=(theme.SPACE_SM, 0))

        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        filter_row = ttk.Frame(body)
        filter_row.pack(fill="x")
        ttk.Label(filter_row, text="Фильтр по имени/описанию:").pack(side="left")
        ttk.Entry(filter_row, textvariable=self.filter_var, width=40).pack(side="left", padx=(6, 0))

        self.catalog_label_var = tk.StringVar(value="Читаю пакеты из образа…")
        ttk.Label(body, textvariable=self.catalog_label_var, style="Hint.TLabel").pack(
            anchor="w", pady=(theme.SPACE_XS, 2)
        )

        # Шаг открылся -- каталог читается сам, если образ выбран и ещё не
        # прочитан. Стоит именно здесь, после подписи: блок её и правит, а
        # порядок построения виджетов -- не деталь оформления, а условие
        # работоспособности (пять тестов интерфейса падали ровно на этом).
        # `after` вместо прямого вызова: фоновая задача не должна трогать
        # виджеты, пока шаг ещё строится.
        if getattr(self, "_image_path", "") and not getattr(self, "_catalog", None):
            self.after(100, self._load_packages_from_image)
        elif not getattr(self, "_image_path", ""):
            self.catalog_label_var.set(
                "Образ не выбран на первом шаге: список пакетов взять неоткуда."
            )

        lists = ttk.Frame(body)
        lists.pack(fill="both", expand=True)
        left = ttk.Frame(lists)
        left.pack(side="left", fill="both", expand=True)
        # selectmode="extended" is what makes Ctrl-click and Shift-click work;
        # the default "browse" allows exactly one row, which meant adding
        # twenty packages took twenty round trips through the button.
        self.catalog_table, _catalog_empty = data_table(
            left,
            (
                Column("name", "пакет", 130, 90),
                Column("version", "версия", 90, 70),
                Column("summary", "описание", 170, 120, stretch=True),
            ),
            height=10,
            selectmode="extended",
        )
        # Щелчок по заголовку сортирует. Ширины и минимумы заданы выше, в
        # описании колонок: они подобраны так, чтобы обе половины шага
        # помещались в карточку при окне наименьшего размера (1040x700, с
        # которым программа открывается). До 04.09.2026 сумма была больше:
        # правая половина с кнопкой «Добавить выбранные» уходила за край, и
        # шаг оказывался неработоспособным, пока окно не растянут вручную.
        for col in ("name", "version", "summary"):
            self.catalog_table.heading(col, command=lambda c=col: self._sort_catalog(c))
        ttk.Label(
            left,
            text="Ctrl — по одному, Shift — диапазоном.",
            style="Hint.TLabel",
            wraplength=theme.HINT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, 0))
        ttk.Button(left, text="Добавить выбранные ->", command=self._add_package).pack(anchor="w", pady=(4, 0))

        right = ttk.Frame(lists)
        # Не expand: место сначала получает таблица каталога -- в ней ищут, а
        # справа лежит короткий список выбранного.
        right.pack(side="left", fill="y", padx=(theme.SPACE_MD, 0))
        ttk.Label(right, text="Выбрано для установки:").pack(anchor="w")
        # Ширина задана явно: без неё список растёт по длиннейшему имени
        # пакета и выталкивает свою же кнопку за край карточки.
        self.selected_list = tk.Listbox(right, height=10, width=24, selectmode="extended")
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

    def _choose_image(self) -> None:
        """Выбор образа и чтение всего, что он о себе знает, одним разом."""
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            parent=self,
            title="Установочный образ дистрибутива",
            filetypes=[("Образы", "*.iso *.img"), ("Все файлы", "*")],
        )
        if not chosen:
            return
        self._image_path = chosen
        self.image_var.set(chosen)
        self.image_status_var.set("Читаю образ…")
        run_in_background(
            self,
            lambda: read_image_facts(chosen, packages=list(self.config.packages)),
            on_success=self._on_image_facts,
            on_error=self._on_image_facts_error,
        )

    def _on_image_facts(self, facts) -> None:
        """Подставляет всё, что образ рассказал о себе, и запирает поля.

        Правило простое (решение 16.09.2026): дал образ список -- поле
        ограничено этим списком, потому что носитель с чужим значением не
        соберётся; не дал -- поле открыто, и рядом стоит причина.
        """
        self._image_facts = facts
        if facts.launchers:
            self._package_launchers = facts.launchers
            if hasattr(self, "launcher_box"):
                self._refresh_launchers()
        info = facts.info
        if info is not None:
            # Семейство и выпуск образ знает точнее любого списка в коде.
            if hasattr(self, "target_os_var") and info.family:
                self.target_os_var.set(info.family)
                self._on_target_os_changed()
            if hasattr(self, "release_var") and info.release:
                self.release_var.set(info.release)
        self._apply_image_choices()
        self.image_status_var.set(self._image_status_text())

    def _on_image_facts_error(self, exc: BaseException) -> None:
        self._image_facts = None
        self.image_status_var.set(f"Образ прочитать не удалось: {exc}")

    def _facts(self):
        return getattr(self, "_image_facts", None)

    def _choice_values(self, name: str) -> "list[str]":
        """Значения для поля: из образа, если он их дал, иначе пусто."""
        facts = self._facts()
        if facts is None:
            return []
        choices = getattr(facts, name, None)
        return list(choices.values) if choices is not None and choices.known else []

    def _arch_values(self) -> "list[str]":
        facts = self._facts()
        if facts is None or not facts.architectures.known:
            return [ARCH_SAME_AS_HOST]
        return [ARCH_SAME_AS_HOST, *facts.architectures.values]

    def _apply_image_choices(self) -> None:
        """Закрывает поля списками образа, а без списка -- открывает их."""
        facts = self._facts()
        if facts is None:
            return
        pairs = (
            ("locale_combo", facts.locales),
            ("timezone_combo", facts.timezones),
            ("keymap_combo", facts.keyboard_layouts),
        )
        for attr, choices in pairs:
            combo = getattr(self, attr, None)
            if combo is None:
                continue
            if choices.known:
                combo.configure(values=list(choices.values), state="readonly")
            else:
                combo.configure(values=[], state="normal")
        arch_combo = getattr(self, "arch_combo", None)
        if arch_combo is not None:
            arch_combo.configure(values=self._arch_values())

    def _image_status_text(self) -> str:
        """Одна строка о том, что дал образ и чего не дал -- и почему."""
        facts = self._facts()
        if facts is None:
            return "Образ не выбран: значения вводятся вручную."
        if not facts.usable:
            return f"Образ прочитать не удалось: {facts.failure}"
        info = facts.info
        parts = [f"{info.label}: {info.family} {info.release}"]
        missing = []
        for name, choices in (
            ("языки", facts.locales),
            ("пояса", facts.timezones),
            ("раскладки", facts.keyboard_layouts),
        ):
            if choices.known:
                parts.append(f"{name}: {len(choices.values)}")
            else:
                missing.append(choices.reason)
        line = ", ".join(parts)
        if missing:
            line += ". " + "; ".join(missing) + " -- эти поля открыты для ввода"
        return line

    def _ask_image_path(self) -> str:
        """Образ, выбранный на первом шаге. Второй раз не спрашивается.

        До 16.09.2026 здесь открывался диалог: образ выбирался лениво, из шага
        пакетов или из шага киоска, и администратор указывал один и тот же файл
        дважды. Теперь образ -- поле первого шага, и всё, что от него нужно
        дальше, уже прочитано; остаётся вернуть путь.
        """
        return getattr(self, "_image_path", "")

    def _load_packages_from_image(self) -> None:
        """Каталог пакетов из установочного образа, без сети.

        Монтирование и разбор идут в фоне, как и загрузка с зеркала: primary
        РЕД ОС -- десятки мегабайт XML. Если образ прочитать не удалось, а
        зеркало задано, `load_packages` пробует его сам -- сообщение скажет,
        откуда список на самом деле.
        """
        chosen = self._ask_image_path()
        if not chosen:
            return
        from ...config.repo_browser import load_packages
        from ...install.image_source import MountedImage

        target_os = self.config.target_os
        release = self.config.release
        mirror = self.config.mirror
        arch = self.config.arch or platform.machine()
        self.image_packages_btn.configure(state="disabled")
        self.connect_btn.configure(state="disabled")
        self.message_bar.info("Чтение списка пакетов из образа…")

        def fetch():
            with MountedImage(chosen) as image:
                return load_packages(
                    target_os, release, image_mountpoint=image.mountpoint, mirror=mirror, arch=arch
                )

        def loaded(result):
            catalog, origin = result
            self._on_catalog_loaded(catalog, origin)

        def failed(exc):
            # Выбранный файл забывается: следующая попытка спросит заново, а
            # не будет раз за разом монтировать тот же неподходящий образ.
            self._image_path = ""
            self._on_catalog_error(exc)

        run_in_background(self, fetch, on_success=loaded, on_error=failed)

    def _on_catalog_loaded(self, catalog: list[RepoPackage], origin: str = "network") -> None:
        self.connect_btn.configure(state="normal")
        if hasattr(self, "image_packages_btn"):
            self.image_packages_btn.configure(state="normal")
        self._catalog = catalog
        if origin == "image":
            self.message_bar.success(f"Прочитано пакетов из образа: {len(catalog)}")
        else:
            self.message_bar.success(f"Загружено пакетов из репозитория: {len(catalog)}")
        self._refresh_catalog(catalog)

    def _on_catalog_error(self, exc: BaseException) -> None:
        self.connect_btn.configure(state="normal")
        if hasattr(self, "image_packages_btn"):
            self.image_packages_btn.configure(state="normal")
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
                f"Найдено: {len(items)} — показаны первые {CATALOG_DISPLAY_LIMIT}, "
                "уточните фильтр, чтобы найти нужный пакет."
            )
        else:
            self.catalog_label_var.set(f"Найдено: {len(items)}.")

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

    # -- step 5: system profile and updates ----------------------------------
    def _build_step_profile(self, body: ttk.Frame) -> None:
        """Placed after the package step on purpose.

        The kiosk whitelist names desktop entries, and which ones exist depends
        on what was chosen there -- so the administrator arrives here already
        knowing what is going onto the medium, and can be offered the selected
        packages as candidates rather than a blank field.
        """
        kiosk = self.config.kiosk

        self.profile_var = tk.StringVar(value=self.config.profile)
        if self.expert_mode:
            mode = ttk.LabelFrame(
                body, text="Режим работы установленной системы", padding=theme.SPACE_SM
            )
            mode.pack(fill="x", pady=(0, theme.SPACE_MD))
            ttk.Radiobutton(
                mode,
                text="Консольная система — текстовый вход, без графики (как было раньше)",
                value="console",
                variable=self.profile_var,
                command=self._on_profile_changed,
            ).pack(anchor="w", pady=(theme.SPACE_XS, 0))
            ttk.Radiobutton(
                mode,
                text="Режим киоска — урезанный рабочий стол только с разрешёнными приложениями",
                value="kiosk",
                variable=self.profile_var,
                command=self._on_profile_changed,
            ).pack(anchor="w", pady=(0, theme.SPACE_XS))
        else:
            # Упрощённый режим строит киоск и только киоск: минимальная
            # защищённая система -- это то, ради чего программа существует, а
            # консольная сборка осталась от её прошлой жизни и нужна тому, кто
            # придёт за ней сознательно.
            self.profile_var.set("kiosk")

        backend_supported = self.config.target_os in ("debian", "redos")
        if not backend_supported:
            ttk.Label(
                body,
                text=f"Режим киоска недоступен для {self.config.target_os}: "
                "реализован и проверен только для debian и redos.",
                style="Warning.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w", pady=(0, theme.SPACE_XS))

        self.kiosk_frame = ttk.LabelFrame(
            body, text="Рабочая среда, приложения и ограничения", padding=theme.SPACE_SM
        )
        self.kiosk_frame.pack(fill="both", expand=True, pady=(0, theme.SPACE_MD))
        inner = ttk.Frame(self.kiosk_frame)
        inner.pack(fill="both", expand=True)

        self.desktop_var = tk.StringVar(value=kiosk.desktop_environment or "mate")
        desktop_row = ttk.Frame(inner)
        desktop_row.pack(fill="x", pady=(0, theme.SPACE_SM))
        ttk.Label(desktop_row, text="Рабочая среда:").pack(side="left", padx=(0, theme.SPACE_MD))
        ttk.Radiobutton(
            desktop_row, text="MATE", value="mate", variable=self.desktop_var
        ).pack(side="left")
        ttk.Radiobutton(
            desktop_row, text="KDE Plasma", value="kde", variable=self.desktop_var
        ).pack(side="left", padx=(theme.SPACE_MD, 0))
        ttk.Label(
            inner,
            text="KDE ограничивает строже (запрет проверяют сами приложения), "
            "но тяжелее и дольше ставится.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))

        self.session_var = tk.StringVar(value=kiosk.session_type or "x11")
        if self.expert_mode:
            # X11 -- то, на чём проверены все четыре комбинации; Wayland есть
            # только у Plasma и только в Debian. Выбор осмысленный, но не для
            # того, кто собирает обычный носитель.
            session_row = ttk.Frame(inner)
            session_row.pack(fill="x")
            ttk.Label(session_row, text="Графический протокол:").pack(
                side="left", padx=(0, theme.SPACE_MD)
            )
            ttk.Radiobutton(
                session_row, text="X11", value="x11", variable=self.session_var
            ).pack(side="left")
            ttk.Radiobutton(
                session_row,
                text="Wayland (только KDE)",
                value="wayland",
                variable=self.session_var,
            ).pack(side="left", padx=(theme.SPACE_MD, 0))
            ttk.Label(
                inner,
                text="В образе РЕД ОС Wayland-сеанса Plasma нет — только X11.",
                style="Hint.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w", pady=(0, theme.SPACE_SM))

        ttk.Label(
            inner,
            text="Доступно пользователю ровно то, что установлено пакетами на шаге 5. "
            "Здесь можно снять лишние запускатели: один пакет нередко ставит несколько.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))

        # Галочки, а не список имён.
        #
        # До 02.09.2026 здесь вводились имена desktop-записей руками, и ошибку
        # это давало неотличимую от исправной настройки: прогон того же дня
        # разрешил «atril», пакета с ним не поставил, и меню оказалось пустым.
        # Теперь список приходит из пакетов, а рукам оставлено обратное
        # действие -- снять лишнее.
        self.launcher_vars: dict[str, tk.BooleanVar] = {}
        self.launcher_box = ttk.Frame(inner)
        self.launcher_box.pack(fill="both", expand=True, pady=(0, theme.SPACE_SM))

        list_actions = ttk.Frame(inner)
        list_actions.pack(fill="x", pady=(0, theme.SPACE_SM))

        self._refresh_launchers()

        options = ttk.Frame(inner)
        options.pack(fill="x", pady=(theme.SPACE_SM, 0))
        self.autostart_var = tk.StringVar(value=kiosk.autostart_app)
        self.blank_var = tk.StringVar(value=str(kiosk.screen_blank_minutes))
        self.allow_media_var = tk.BooleanVar(value=kiosk.allow_removable_media)
        self.firejail_var = tk.BooleanVar(value=kiosk.firejail)
        self.firejail_opts_var = tk.StringVar(value=kiosk.firejail_options)
        blank_row = 0
        if self.expert_mode:
            add_field(
                options, 0, "Запускать при входе:", self.autostart_var, width=26,
                hint="Необязательно. Имя запускателя, который не снят выше.",
            )
            blank_row = 2
        add_field(
            options,
            blank_row,
            "Гасить экран через, мин:",
            self.blank_var,
            width=8,
            hint="0 — не гасить. Экран именно гаснет, а не блокируется: пароль учётной записи "
            "заблокирован, вход автоматический, и разблокировать экран было бы нечем.",
        )

        self.allow_shutdown_var = tk.BooleanVar(value=kiosk.allow_shutdown)
        ttk.Checkbutton(
            inner,
            text="Разрешить выключение и перезагрузку машины",
            variable=self.allow_shutdown_var,
        ).pack(anchor="w", pady=(theme.SPACE_SM, 0))

        # Что из «Параметров системы» открыто пользователю.
        #
        # Поля здесь не было вовсе -- найдено 12.09.2026. Механизм в коде есть
        # с обеих сторон (`SETTINGS_MODULES`, запреты модулей KDE), а задать
        # его администратору было нечем: носитель с разрешёнными настройками
        # приложением не собирался. Тот же класс дефекта, что был с полем
        # привязки и со своими пакетами -- код есть, выхода в интерфейс нет.
        #
        # Показывается в обоих режимах: это решение о том, что пользователю
        # позволено менять в системе, и прятать его за «экспертностью» значит
        # прятать выбор, который администратор обязан сделать осознанно.
        # Пусто по умолчанию -- отправная точка проекта самая закрытая.
        self.settings_vars: "dict[str, tk.BooleanVar]" = {}
        allowed_now = set(kiosk.allowed_settings or ())
        settings_box = ttk.LabelFrame(
            inner, text=" Разрешённые настройки ", padding=theme.SPACE_SM
        )
        settings_box.pack(fill="x", pady=(theme.SPACE_SM, 0))
        for key, label in (
            ("network", "Сеть"),
            ("sound", "Звук"),
            ("datetime", "Дата и время"),
            ("localisation", "Язык и форматы"),
            ("keyboard", "Раскладка клавиатуры"),
        ):
            variable = tk.BooleanVar(value=key in allowed_now)
            self.settings_vars[key] = variable
            ttk.Checkbutton(settings_box, text=label, variable=variable).pack(anchor="w")
        if self.expert_mode:
            # Съёмные носители закрыты в минимальной системе намеренно:
            # принесённая флешка обходит раздел обмена с его `noexec`.
            # Открывать эту дверь -- решение, а не настройка по умолчанию.
            ttk.Checkbutton(
                inner,
                text="Разрешить подключение съёмных носителей",
                variable=self.allow_media_var,
            ).pack(anchor="w")
            ttk.Checkbutton(
                inner,
                text="Запускать приложения в песочнице firejail, если она есть в образе",
                variable=self.firejail_var,
            ).pack(anchor="w")
            firejail_row = ttk.Frame(inner)
            firejail_row.pack(fill="x", padx=(theme.SPACE_LG, 0))
            add_field(firejail_row, 0, "Параметры firejail:", self.firejail_opts_var, width=26)

        settings = self.config.auto_update
        self.update_enabled_var = tk.BooleanVar(value=settings.enabled)
        self.update_kernel_var = tk.BooleanVar(value=settings.include_kernel)
        self.update_delay_var = tk.StringVar(value=str(settings.delay_minutes))
        if self.expert_mode:
            # Обновление на носителе -- отдельное решение со своей ценой:
            # оно требует сети, времени на загрузке и рабочего репозитория.
            # В минимальной системе выключено.
            updates = ttk.LabelFrame(body, text="Автоматические обновления", padding=theme.SPACE_SM)
            updates.pack(fill="x")
            ttk.Checkbutton(
                updates,
                text="Обновлять систему при каждой загрузке носителя",
                variable=self.update_enabled_var,
            ).pack(anchor="w")
            ttk.Label(
                updates,
                text="Обновления берутся из репозитория, указанного на шаге 1. Без него "
                "обновляться неоткуда: установочный образ на носителе недоступен.",
                style="Hint.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w", padx=(theme.SPACE_LG, 0))
            ttk.Checkbutton(
                updates,
                text="Обновлять ядро и загрузчик",
                variable=self.update_kernel_var,
            ).pack(anchor="w", pady=(theme.SPACE_XS, 0))
            update_opts = ttk.Frame(updates)
            update_opts.pack(anchor="w", pady=(theme.SPACE_XS, 0))
            add_field(
                update_opts,
                0,
                "Начинать через, мин:",
                self.update_delay_var,
                width=8,
                hint="Пауза после загрузки, чтобы обновление не соперничало с рабочим столом "
                "за ту же флешку.",
            )

        # Отзыв показывается в обоих режимах, в отличие от обновлений: это не
        # удобство, а единственное, что администратор может сделать с
        # носителем, которого у него нет в руках.
        revocation = self.config.revocation
        self.revocation_enabled_var = tk.BooleanVar(value=revocation.enabled)
        box = ttk.LabelFrame(body, text="Отзыв и авторизация сеанса", padding=theme.SPACE_SM)
        box.pack(fill="x", pady=(theme.SPACE_MD, 0))
        ttk.Checkbutton(
            box,
            text="Спрашивать сервер отзыва раз в 5 минут и при подключении к сети",
            variable=self.revocation_enabled_var,
        ).pack(anchor="w")
        server = self.app.session.container.data.revocation_server_url
        ttk.Label(
            box,
            text=(
                f"Сервер: {server}. В режиме киоска приложения запускаются только "
                "после его подтверждения. Ответ «отозван» уничтожает носитель "
                "целиком — необратимо."
                if server
                else "Контейнер не знаком ни с одним ресурсом отзыва: «Обслуживание» → "
                "«Подключиться». Без этого носитель проверять ответ нечем."
            ),
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", padx=(theme.SPACE_LG, 0))

        self._on_profile_changed()

    def _on_profile_changed(self) -> None:
        """Greys out the kiosk options when a console system is selected."""
        enabled = self.profile_var.get() == "kiosk"
        state = "normal" if enabled else "disabled"
        for child in self._all_children(self.kiosk_frame):
            try:
                child.configure(state=state)
            except tk.TclError:
                # Frames and labels have no -state option; nothing to grey out.
                pass

    def _all_children(self, widget: tk.Widget) -> list[tk.Widget]:
        found: list[tk.Widget] = []
        for child in widget.winfo_children():
            found.append(child)
            found += self._all_children(child)
        return found

    def _refresh_launchers(self) -> None:
        """Перерисовывает галочки по выбранным пакетам.

        Источник -- каталог образа: он единственный не устареет вместе с
        дистрибутивом. Для Debian состав пакета из установочного DVD узнать
        нельзя вовсе -- файла `Contents` там нет, -- и об этом сказано прямо,
        а не изображено пустым списком: администратор увидит найденные
        запускатели в журнале установки и сможет снять лишние следующей
        сборкой.
        """
        for child in self.launcher_box.winfo_children():
            child.destroy()
        self.launcher_vars = {}

        packages = list(self.config.packages)
        if not packages:
            ttk.Label(
                self.launcher_box,
                text="Пакеты не выбраны: вернитесь на шаг «Пакеты для предустановки». "
                "Без них пользователю нечего будет запустить.",
                style="Hint.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w")
            return

        known = getattr(self, "_package_launchers", {}) or {}
        hidden = set(self.config.kiosk.hidden_launchers)
        shown = 0
        for package in packages:
            for name in known.get(package, ()):
                variable = tk.BooleanVar(value=name not in hidden)
                self.launcher_vars[name] = variable
                ttk.Checkbutton(
                    self.launcher_box,
                    text=f"{name}  —  из пакета {package}",
                    variable=variable,
                ).pack(anchor="w")
                shown += 1
        if shown:
            return

        ttk.Label(
            self.launcher_box,
            text="Какие запускатели принесут эти пакеты, станет видно при сборке: "
            "для Debian состав пакета из установочного образа не читается "
            "(файла Contents на DVD нет). Установка перечислит найденное в журнале.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w")

    def _save_step_profile(self) -> None:
        self.config.profile = self.profile_var.get()
        kiosk = self.config.kiosk
        kiosk.enabled = self.config.profile == "kiosk"
        kiosk.desktop_environment = self.desktop_var.get()
        kiosk.session_type = self.session_var.get()
        kiosk.hidden_launchers = sorted(
            name for name, variable in self.launcher_vars.items() if not variable.get()
        )
        kiosk.autostart_app = self.autostart_var.get().strip()
        kiosk.allow_shutdown = self.allow_shutdown_var.get()
        kiosk.allowed_settings = sorted(
            key for key, variable in self.settings_vars.items() if variable.get()
        )
        kiosk.allow_removable_media = self.allow_media_var.get()
        kiosk.firejail = self.firejail_var.get()
        kiosk.firejail_options = self.firejail_opts_var.get().strip()
        try:
            kiosk.screen_blank_minutes = int(self.blank_var.get().strip() or 0)
        except ValueError:
            kiosk.screen_blank_minutes = -1  # reported by _validate_step_profile

        # Адрес не спрашивается здесь: он один на контейнер, а не на
        # конфигурацию, и берётся оттуда, где администратор познакомился с
        # ресурсом. Иначе носители одной организации разошлись бы по разным
        # адресам из-за опечатки в одной конфигурации.
        self.config.revocation.enabled = self.revocation_enabled_var.get()
        self.config.revocation.url = self.app.session.container.data.revocation_server_url

        settings = self.config.auto_update
        settings.enabled = self.update_enabled_var.get()
        settings.include_kernel = self.update_kernel_var.get()
        try:
            settings.delay_minutes = int(self.update_delay_var.get().strip() or 0)
        except ValueError:
            settings.delay_minutes = -1

    def _validate_step_profile(self) -> bool:
        if self.config.revocation.enabled and not self.config.revocation.url:
            self.message_bar.error(
                "Отзыв включён, но контейнер не знаком ни с одним ресурсом: "
                "«Обслуживание» → «Подключиться». Носитель, собранный без этого, "
                "проверять ответ не сможет."
            )
            return False
        kiosk = self.config.kiosk
        if self.config.profile == "kiosk":
            if self.config.target_os not in ("debian", "redos"):
                self.message_bar.error(
                    f"Режим киоска не поддерживается для {self.config.target_os}. "
                    "Выберите консольную систему или другую целевую ОС."
                )
                return False
            if not self.config.packages:
                self.message_bar.error(
                    "Выберите хотя бы один пакет с приложением на шаге «Пакеты для "
                    "предустановки» — система собирается минимальной, и без них "
                    "пользователю нечего будет запустить."
                )
                return False
            if kiosk.session_type == "wayland":
                if kiosk.desktop_environment == "mate":
                    self.message_bar.error(
                        "MATE не умеет работать под Wayland — выберите X11 или KDE."
                    )
                    return False
                if self.config.target_os == "redos":
                    self.message_bar.error(
                        "В образе РЕД ОС нет Wayland-сеанса Plasma — для РЕД ОС выберите X11."
                    )
                    return False
            if kiosk.autostart_app and kiosk.autostart_app in kiosk.hidden_launchers:
                self.message_bar.error(
                    f"Запускатель «{kiosk.autostart_app}» снят из показа — приложение "
                    "запускалось бы, но открыть его заново было бы нечем."
                )
                return False
            if kiosk.screen_blank_minutes < 0:
                self.message_bar.error("Время до гашения экрана — целое число минут, 0 или больше.")
                return False
            if kiosk.firejail_options and not kiosk.firejail:
                self.message_bar.error(
                    "Заданы параметры firejail, но сама песочница выключена."
                )
                return False

        settings = self.config.auto_update
        if settings.enabled:
            if not self.config.mirror:
                self.message_bar.error(
                    "Для автообновления нужен адрес репозитория — задайте его на первом шаге. "
                    "Установочный образ на носителе недоступен, обновляться будет неоткуда."
                )
                return False
            if settings.delay_minutes < 0:
                self.message_bar.error("Задержка перед обновлением — целое число минут, 0 или больше.")
                return False
        return True

    # -- step 5: review + edit + save --------------------------------------
    def _validation_status_text(self) -> str:
        return {
            "valid": "Конфигурация проверена: ошибок нет.",
            "invalid": "Конфигурация не прошла проверку — см. сообщение ниже.",
        }.get(self.config.validation_status, "Конфигурация ещё не проверена.")

    def _build_step_review(self, body: ttk.Frame) -> None:
        """The whole configuration, editable.

        This replaces the old read-only summary and the separate "system tasks"
        step. An administrator who wants to set something the wizard does not
        expose -- a system task, an unusual option -- edits it here, where the
        full document is visible, instead of through a step dedicated to typing
        shell commands into a list box.
        """
        self._build_change_list(body)

        if not self.expert_mode:
            # Текст конфигурации в упрощённом режиме не показывается, и это не
            # утаивание, а ответ на вопрос «что я собрал»: список изменений
            # выше отвечает на него по-русски, а шестьдесят полей JSON --
            # только тому, кто и так знает, что ищет. Ничего не теряется:
            # переключатель наверху открывает и редактор, и всё прочее.
            ttk.Label(
                body,
                text="Остальное — размеры разделов, шифрование, обновления, удалённый "
                "доступ — программа задала сама. Экспертный режим наверху показывает "
                "и их, и текст конфигурации целиком.",
                style="Hint.TLabel",
                wraplength=theme.TEXT_WRAP,
                justify="left",
            ).pack(anchor="w", pady=(theme.SPACE_MD, theme.SPACE_SM))
            buttons = ttk.Frame(body)
            buttons.pack(anchor="w")
            ttk.Button(
                buttons, text="Проверить конфигурацию", command=self._run_validation
            ).pack(side="left")
            self.validation_var = tk.StringVar(value=self._validation_status_text())
            ttk.Label(
                body, textvariable=self.validation_var, style="Hint.TLabel"
            ).pack(anchor="w", pady=(theme.SPACE_SM, 0))
            return

        ttk.Label(
            body,
            text="Можно править прямо здесь — изменения применятся по кнопке ниже.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_MD, 0))

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
        ttk.Label(body, textvariable=self.validation_status_var, wraplength=theme.TEXT_WRAP, justify="left").pack(
            anchor="w", pady=(6, 0)
        )

    def _build_change_list(self, body: ttk.Frame) -> None:
        """What this configuration changes against the locked-down starting point.

        The finished file answers "what will be installed"; this answers "what
        did I do", which in a configuration whose purpose is restricting a
        system is the more useful question -- particularly for the entries that
        *open* something up.
        """
        changes = self.config.diff_from(self._baseline)

        frame = ttk.LabelFrame(
            body, text="Отличия от исходной минимальной конфигурации", padding=theme.SPACE_SM
        )
        frame.pack(fill="x", pady=(0, theme.SPACE_SM))

        if not changes:
            ttk.Label(
                frame,
                text="Ничего не изменено: конфигурация в точности такая, какой создаётся "
                "по умолчанию — режим киоска с минимальными возможностями.",
                style="Hint.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w")
            return

        table, _empty = data_table(
            frame,
            (
                Column("field", "Параметр", 260, 150),
                Column("before", "Было", 150, 110),
                Column("after", "Стало", 220, 130, stretch=True),
            ),
            height=min(len(changes), 8),
            expand=False,
        )
        for label, before, after in changes:
            table.insert("", "end", values=(label, before, after))
        ttk.Label(
            frame,
            text=f"Изменений: {len(changes)}.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(theme.SPACE_XS, 0))

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
        self._set_validation_status("Статус проверки: не проверена (после правки)")
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

    def _set_validation_status(self, text: str) -> None:
        """Пишет вердикт туда, где он показан в текущем режиме.

        В экспертном это строка под редактором, в упрощённом -- строка под
        кнопкой; общий метод избавляет от `if` в каждом месте вызова и от
        падения, когда переменной другого режима просто нет.
        """
        for name in ("validation_status_var", "validation_var"):
            variable = getattr(self, name, None)
            if variable is not None:
                variable.set(text)

    def _run_validation(self) -> None:
        try:
            self.app.config_manager.validate(self.config)
        except ConfigValidationUnavailableError as exc:
            self.message_bar.error(f"Проверка недоступна: {exc}")
            return
        # Say which checks actually ran. "Корректна" means something different
        # when the distribution's own validator was unavailable, and hiding that
        # would overstate what was verified.
        note = getattr(self.app.config_manager, "native_validation_note", "")
        # Предупреждения менеджер не хранит -- они не меняют вердикт, -- поэтому
        # спрашиваются здесь, чтобы «корректна» не заслонила их.
        warnings = boot_warnings(self.config)
        if warnings:
            note = "\n".join(filter(None, [note, *("Внимание: " + w for w in warnings)]))
        if self.config.validation_status == "valid":
            self._set_validation_status(
                "Статус проверки: корректна" + (f"\n{note}" if note else "")
            )
            if note:
                self.message_bar.info("Конфигурация прошла внутренние правила. " + note)
            else:
                self.message_bar.success(
                    "Конфигурация корректна: пройдены и внутренние правила, и штатное "
                    "средство проверки дистрибутива."
                )
        else:
            self._set_validation_status("Статус проверки: содержит ошибки: " + "; ".join(self.config.validation_errors))
            self.message_bar.error("Конфигурация содержит ошибки, см. выше.")

    def _save_step_review(self) -> None:
        return

    # -- WizardFrame hooks ------------------------------------------------
    def build_step(self, index: int, body: ttk.Frame) -> None:
        getattr(self, f"_build_step_{self.step_keys[index]}")(body)

    def save_step(self, index: int) -> None:
        saver = getattr(self, f"_save_step_{self.step_keys[index]}", None)
        if saver is not None:
            saver()

    def validate_step(self, index: int) -> bool:
        validator = getattr(self, f"_validate_step_{self.step_keys[index]}", None)
        return True if validator is None else validator()

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
        with_error = False
        try:
            self.app.config_manager.save(self.app.container, self.config)
            self.app.save_container()
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
