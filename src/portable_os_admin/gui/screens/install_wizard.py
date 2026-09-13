"""UC-4 "Установка ОС на внешний носитель", as a guided wizard.

Direct response to live RedOS testing: a real USB stick did not appear in
the auto-detected list (lsblk's RM flag is unreliable, fixed separately in
install/devices.py) and the manual-entry fallback wasn't obvious enough to
find. Here both are always shown together on the same step, with explicit
text explaining when/why to use the manual field.

The real (dry_run=False) install run is a single, already-tested, unchanged
call into OSInstaller.run() -- this wizard does not touch installer.py or
command_runner.py at all, it only moves that blocking call off the UI
thread (see gui/widgets.py run_in_background) so the window doesn't appear
to hang for however long partitioning/LUKS/debootstrap take.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from tkinter import filedialog

from ...errors import DeviceNotEligibleError, InstallError
from ...install.command_runner import InstallAborted
from ...install import BlockDevice, OSInstaller
from ...install import image_source, leftovers
from ...install.emulation import probe as probe_emulation
from ...install.installer import IMAGE_FAMILY
from ..elevate import invoking_user_home
from .. import theme
from ..widgets import (
    KNOWN_ERRORS,
    Column,
    UiMailbox,
    WizardFrame,
    add_field,
    data_table,
    run_in_background,
    widget_alive,
)
from .device_step import (
    DANGER_COLOR,
    NOT_ROOT_MESSAGE,
    DeviceStepMixin,
    TerminationGuardMixin,
    is_root as _is_root,
)


class InstallWizardFrame(DeviceStepMixin, TerminationGuardMixin, WizardFrame):
    def __init__(self, parent: tk.Widget, app, config_name: str | None = None):
        self.preselected_config_name = config_name
        self.selected_config_name: str | None = None
        self.selected_config = None
        self.device_path = ""
        self.device_size_gb = "16"
        self.image_path = ""
        self.image_info = None
        self.media_name = ""
        self.media_owner = ""
        self._valid_names: list[str] = []
        self._config_fitness: dict[Path, tuple[bool, str]] = {}
        self._devices: list[BlockDevice] = []
        self._running = False

        super().__init__(
            parent,
            app,
            step_titles=["Образ ОС", "Конфигурация", "Носитель", "Пароль и владелец", "Подтверждение и установка"],
            step_hints=[
                "",
                "",
                "",
                "",
                "Проверьте, что указан именно тот диск.",
            ],
            finish_label="К панели",
        )

    # -- step 1: pick a validated config ----------------------------------
    def _build_step_config(self, body: ttk.Frame) -> None:
        if self.image_info is not None:
            ttk.Label(
                body,
                text=f"Выбранный образ: {self.image_info.label} ({self.image_info.family} "
                f"{self.image_info.release})",
                wraplength=theme.TEXT_WRAP,
                justify="left",
                foreground=theme.TEXT_MUTED,
            ).pack(anchor="w", pady=(0, 6))

        ttk.Label(body, text="Проверенные конфигурации:").pack(anchor="w")
        self.config_table, _empty = data_table(
            body,
            (
                Column("file", "файл", 220, 140),
                Column("target_os", "рассчитана на", 150, 125),
                Column("fits", "подходит образу", 260, 150, stretch=True),
            ),
            height=8,
            pady=(4, 8),
        )
        # Unsuitable rows stay visible rather than being hidden: seeing that a
        # configuration exists but does not fit the chosen image is information;
        # silently omitting it looks like the configuration was lost.
        self.config_table.tag_configure("unfit", foreground=theme.TEXT_DISABLED)

        all_names = self.app.config_manager.list_configs(self.app.container)
        self._valid_names = []
        self._config_fitness = {}
        for name in all_names:
            config = self.app.config_manager.open(self.app.container, name)
            if config.validation_status != "valid":
                continue
            fits, reason = self._fits_image(config)
            self._valid_names.append(name)
            self._config_fitness[name] = (fits, reason)
            self.config_table.insert(
                "", "end", values=(name, config.target_os, reason), tags=() if fits else ("unfit",)
            )

        if not self._valid_names:
            ttk.Label(
                body,
                text="Нет проверенных конфигураций. Сначала создайте конфигурацию и нажмите «Проверить».",
                foreground=theme.DANGER,
                wraplength=600,
                justify="left",
            ).pack(anchor="w", pady=(4, 4))
            ttk.Button(body, text="Перейти к конфигурациям", command=self._go_to_configs).pack(anchor="w")
            return

        preselect = self.selected_config_name or self.preselected_config_name
        if preselect in self._valid_names:
            children = self.config_table.get_children()
            row_id = children[self._valid_names.index(preselect)]
            self.config_table.selection_set(row_id)
            self.config_table.see(row_id)

    def _fits_image(self, config) -> tuple[bool, str]:
        """Whether this configuration can be installed from the chosen image.

        The image decides the family and the architecture, so a configuration
        written for another family simply cannot be used with it. Reported per
        configuration rather than as one error after the fact, so the answer is
        visible while choosing instead of after.
        """
        if self.image_info is None:
            return True, "образ не прочитан — проверка при установке"
        expected = IMAGE_FAMILY.get(config.target_os, config.target_os)
        if expected != self.image_info.family:
            return False, f"нет: образ {self.image_info.family}, конфигурация для {config.target_os}"
        if config.arch and self.image_info.architectures:
            from ...install.backends import get_backend

            wanted = get_backend(config.target_os).normalize_arch(config.arch)
            if wanted not in self.image_info.architectures:
                return False, (
                    f"нет: образ {', '.join(self.image_info.architectures)}, конфигурация {wanted}"
                )
        return True, "да"

    def _go_to_configs(self) -> None:
        from .configs_list import ConfigsListFrame

        self.app.show_frame(ConfigsListFrame)

    def _validate_step_config(self) -> bool:
        if not self._valid_names:
            self.message_bar.error("Сначала создайте и проверьте хотя бы одну конфигурацию.")
            return False
        selection = self.config_table.selection()
        if not selection:
            self.message_bar.error("Выберите конфигурацию в таблице.")
            return False
        index = self.config_table.index(selection[0])
        name = self._valid_names[index]
        fits, reason = self._config_fitness.get(name, (True, ""))
        if not fits:
            self.message_bar.error(
                f"Эта конфигурация не подходит к выбранному образу — {reason}. "
                "Выберите другую конфигурацию или вернитесь и укажите другой образ."
            )
            return False
        self.selected_config_name = name
        self.selected_config = self.app.config_manager.open(self.app.container, name)
        return True

    # -- step 1: the installation image -------------------------------------
    def _build_step_image(self, body: ttk.Frame) -> None:
        ttk.Label(
            body,
            text="Полный установочный образ (DVD); netinst не годится.",
            wraplength=theme.TEXT_WRAP,
            justify="left",
            foreground=theme.TEXT_MUTED,
        ).pack(anchor="w")

        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x", pady=(10, 0))
        self.image_path_var = tk.StringVar(value=self.image_path)
        add_field(
            form,
            0,
            "Файл образа:",
            self.image_path_var,
            hint="оставьте пустым, чтобы ставить из сетевого репозитория конфигурации (медленнее)",
            width=52,
        )
        ttk.Button(form, text="Выбрать образ…", command=self._choose_image).grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        self.image_status_var = tk.StringVar(
            value=self._describe_image() if self.image_info else "Образ не выбран."
        )
        self.image_status = ttk.Label(
            body, textvariable=self.image_status_var, wraplength=theme.TEXT_WRAP, justify="left"
        )
        self.image_status.pack(anchor="w", pady=(10, 0))
        self.image_progress = ttk.Progressbar(body, mode="indeterminate", length=260)

    def _choose_image(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Выберите установочный образ",
            filetypes=[("Образы дисков", "*.iso *.ISO"), ("Все файлы", "*")],
            initialdir=self._image_initial_dir(),
        )
        if not chosen:
            return
        self.image_path_var.set(chosen)
        self._examine_image(chosen)

    def _image_initial_dir(self) -> str:
        current = self.image_path_var.get().strip()
        if current and Path(current).parent.is_dir():
            return str(Path(current).parent)
        home = invoking_user_home()
        documents = home / "Документы"
        if not documents.is_dir():
            documents = home / "Documents"
        return str(documents if documents.is_dir() else home)

    def _examine_image(self, path: str) -> None:
        """Reads the image so the person is told what it is *before* an hour of
        installing, instead of finding out from a failure deep inside
        debootstrap."""
        self.image_info = None
        self.image_status_var.set("Читаю образ…")
        self.image_status.configure(foreground="")
        self.image_progress.pack(anchor="w", pady=(6, 0))
        self.image_progress.start(12)
        run_in_background(
            self,
            lambda: image_source.probe(path),
            on_success=self._on_image_examined,
            on_error=self._on_image_error,
        )

    def _on_image_examined(self, info) -> None:
        self.image_info = info
        if not widget_alive(self.image_progress):
            return
        self.image_progress.stop()
        self.image_progress.pack_forget()
        self.image_status_var.set(self._describe_image())
        self.image_status.configure(foreground="" if info.complete else DANGER_COLOR)

    def _on_image_error(self, exc: BaseException) -> None:
        self.image_info = None
        if not widget_alive(self.image_progress):
            return
        self.image_progress.stop()
        self.image_progress.pack_forget()
        self.image_status_var.set(f"Образ не распознан: {exc}")
        self.image_status.configure(foreground=DANGER_COLOR)

    def _describe_image(self) -> str:
        info = self.image_info
        if info is None:
            return "Образ не выбран."
        if not info.complete:
            return f"{info.label}\n{info.incomplete_reason}"
        return (
            f"{info.label}\n"
            f"Будет установлено: {info.family} {info.release}"
            + (f", {', '.join(info.architectures)}" if info.architectures else "")
        )

    def _save_step_image(self) -> None:
        self.image_path = self.image_path_var.get().strip()

    def _validate_step_image(self) -> bool:
        if not self.image_path:
            # Leaving this empty is the explicit "install from the network"
            # choice, not an omission -- but it only works if a configuration
            # with a repository address is picked on the next step, so it is
            # allowed through with the trade-off stated rather than refused.
            self.message_bar.info(
                "Образ не выбран: система будет установлена из сетевого репозитория, "
                "заданного в конфигурации. Это медленнее и требует доступа к сети; "
                "установка с локального образа предпочтительнее."
            )
            self.image_info = None
            return True
        if not Path(self.image_path).is_file():
            self.message_bar.error(f"Файл не найден: {self.image_path}")
            return False
        if self.image_info is None:
            # Not yet read (typed by hand, or still being examined). Not a
            # refusal: the installer checks the image itself before touching the
            # medium, so a hand-typed path is allowed through with a warning.
            self.message_bar.info("Образ ещё не прочитан — он будет проверен перед началом установки.")
            return True
        if not self.image_info.complete:
            self.message_bar.error(self.image_info.incomplete_reason)
            return False
        return True

    # -- step 2: device -----------------------------------------------------
    # -- step 2: passphrase --------------------------------------------------
    def _build_step_passphrase(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w")
        self.passphrase_var = tk.StringVar(value=getattr(self, "passphrase", ""))
        add_field(form, 0, "Пароль пользователя (LUKS):", self.passphrase_var, show="*", width=30)
        self.passphrase_confirm_var = tk.StringVar(value=getattr(self, "passphrase", ""))
        add_field(form, 2, "Повторите пароль:", self.passphrase_confirm_var, show="*", width=30)
        # Те же два поля, что и в мастере развёртывания, и по той же причине.
        # Этот мастер тоже заводит запись о носителе, но до 12.09.2026 не
        # спрашивал ни имени, ни владельца: носитель, собранный первым
        # сценарием, попадал в список безымянной строкой, а на экране выдачи
        # кода восстановления опознавать звонящего было нечем.
        self.media_name_var = tk.StringVar(value=self.media_name)
        add_field(
            form,
            4,
            "Имя носителя:",
            self.media_name_var,
            width=36,
            hint="например: «Иванов, бухгалтерия»",
        )
        self.media_owner_var = tk.StringVar(value=self.media_owner)
        add_field(form, 6, "Владелец (ФИО):", self.media_owner_var, width=36)

    def _save_step_passphrase(self) -> None:
        self.passphrase = self.passphrase_var.get()
        self.media_name = self.media_name_var.get().strip()
        self.media_owner = self.media_owner_var.get().strip()

    def _validate_step_passphrase(self) -> bool:
        if not self.passphrase_var.get():
            self.message_bar.error("Введите пользовательский пароль (LUKS).")
            return False
        # Длина проверяется и здесь, а не только в мастере развёртывания. Через
        # этот мастер идёт первый сценарий администратора -- «Первый носитель»,
        # -- и до 11.09.2026 он принимал фразу из одного знака: правку 28.08
        # внесли в соседний экран и здесь не повторили. Лимит попыток LUKS от
        # перебора не спасает, он действует только при загрузке носителя, а
        # тот, у кого носитель в руках, снимает образ заголовка и перебирает
        # офлайн, где счётчика нет.
        minimum = self._passphrase_minimum()
        if len(self.passphrase_var.get()) < minimum:
            self.message_bar.error(
                f"Парольная фраза короче {minimum} знаков. Носитель защищён только ею: "
                "тот, у кого он окажется в руках, будет подбирать фразу на своей машине, "
                "где счётчик попыток не действует."
            )
            return False
        if self.passphrase_var.get() != self.passphrase_confirm_var.get():
            self.message_bar.error("Пароли не совпадают.")
            return False
        # Владелец обязателен здесь ровно так же, как в мастере развёртывания:
        # спросить постфактум невозможно, а без него код восстановления
        # выдаётся неизвестно кому.
        if not self.media_owner_var.get().strip():
            self.message_bar.error(
                "Укажите владельца — ФИО сотрудника, которому выдаётся носитель. "
                "По этому имени вы будете опознавать звонящего, когда с носителя "
                "запросят код восстановления."
            )
            return False
        return True

    def _passphrase_minimum(self) -> int:
        """Минимум из выбранной конфигурации, а не из кода этого экрана.

        Шаг выбора конфигурации идёт раньше шага пароля, поэтому она здесь уже
        есть. Если прочитать не удалось, берётся умолчание схемы -- не ноль:
        «не смогли прочитать» не повод не проверять вовсе.
        """
        from ...config.schema import LuksConfig

        luks = getattr(self.selected_config, "luks", None)
        return int(getattr(luks, "min_passphrase_length", 0) or LuksConfig().min_passphrase_length)

    # -- step 3: what will be destroyed, then install --------------------------
    def _build_step_confirm(self, body: ttk.Frame) -> None:
        """The last thing the administrator sees before the medium is erased.

        There is deliberately no "preview what would happen" mode here. This
        tool exists to install; a second mode that emits a list of shell
        commands is developer scaffolding, and offering both on one screen only
        made it unclear which one the button was about to do. What belongs here
        instead is the thing a person actually needs before an irreversible
        action: exactly what is on the device right now, and what will be left
        of it.
        """
        cfg = self.selected_config
        ttk.Label(
            body,
            text=f"Конфигурация: {cfg.name} ({cfg.target_os}, {cfg.release})",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w")

        self._emulation = probe_emulation(cfg.arch) if cfg.arch else None
        if self._emulation is not None and not self._emulation.native:
            box = ttk.LabelFrame(body, text=" Архитектура носителя отличается от этой машины ")
            box.pack(fill="x", pady=(10, 0))
            if self._emulation.available:
                notice = (
                    "Все шаги установки будут выполнены под эмуляцией, но ни одна "
                    "программа целевой системы не запустится на настоящем процессоре "
                    "этой архитектуры. Установка может занять заметно больше времени, "
                    "и возможны сбои, которых не бывает при обычной установке.\n"
                    f"Обязательно проверьте загрузку готового носителя на машине "
                    f"с архитектурой {self._emulation.target_arch}, прежде чем выдавать его."
                )
            else:
                notice = self._emulation.reason + "\nУстановка невозможна."
            ttk.Label(
                box, text=notice, foreground=DANGER_COLOR, wraplength=theme.TEXT_WRAP, justify="left"
            ).pack(anchor="w", padx=10, pady=8)

        device = self._resolve_device(self._parsed_size_gb())
        self._confirmed_device = device

        warning = ttk.LabelFrame(body, text=" Носитель будет полностью очищен ")
        warning.pack(fill="x", pady=(10, 6))
        ttk.Label(
            warning,
            text=(
                f"{device.path}"
                + (f"  ·  {device.model}" if device.model else "")
                + f"  ·  {device.size_bytes / 1024**3:.1f} ГБ"
            ),
            font=theme.FONT_SECTION,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(8, 2))

        if device.partitions:
            ttk.Label(
                warning,
                text="Сейчас на носителе, всё это будет уничтожено безвозвратно:",
                foreground=DANGER_COLOR,
                justify="left",
            ).pack(anchor="w", padx=10)
            for partition in device.partitions:
                ttk.Label(warning, text="    • " + partition.describe(), justify="left").pack(anchor="w", padx=10)
        else:
            ttk.Label(
                warning,
                text="Разделов на носителе не обнаружено — он будет размечен заново.",
                justify="left",
            ).pack(anchor="w", padx=10)

        if device.is_mounted:
            ttk.Label(
                warning,
                text=(
                    "Носитель сейчас смонтирован ("
                    + ", ".join(device.active_mountpoints)
                    + ") — он будет отмонтирован автоматически."
                ),
                foreground=DANGER_COLOR,
                wraplength=theme.TEXT_WRAP,
                justify="left",
            ).pack(anchor="w", padx=10, pady=(4, 0))

        ttk.Label(
            warning,
            text=(
                "Восстановить данные после установки невозможно. Убедитесь, что указан "
                "именно тот носитель, и что всё нужное с него скопировано."
            ),
            foreground=DANGER_COLOR,
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(6, 10))

        confirm_frame = ttk.Frame(body)
        confirm_frame.pack(anchor="w", pady=(4, 0))
        self.confirm_device_var = tk.StringVar()
        confirm_entry, _ = add_field(
            confirm_frame,
            0,
            "Повторите путь к устройству:",
            self.confirm_device_var,
            hint="это последняя защита от установки не на тот диск",
            width=30,
        )
        # Фокус -- в поле, а не на кнопке, стирающей носитель: см. тот же
        # шаг в мастере развёртывания.
        confirm_entry.focus_set()

        self.start_btn = ttk.Button(
            body,
            text="Стереть носитель и установить систему",
            command=self._start_install,
            state="disabled",
        )
        self.start_btn.pack(anchor="w", pady=(8, 4))
        # Недоступна, пока путь не повторён верно -- как в мастере
        # развёртывания, и по той же причине.
        self.confirm_device_var.trace_add("write", self._sync_start_button)
        self._sync_start_button()

        # Determinate, with the stage named next to it: an install takes the
        # better part of an hour and most of it sits inside one command, so an
        # indeterminate spinner tells the person nothing about whether anything
        # is happening or how much is left.
        self.stage_var = tk.StringVar(value="")
        self.stage_label = ttk.Label(body, textvariable=self.stage_var, wraplength=theme.TEXT_WRAP, justify="left")
        self.progress = ttk.Progressbar(body, mode="determinate", length=420, maximum=100)

        self.log_text = tk.Text(body, height=12, width=95, state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

        if not _is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _sync_start_button(self, *_args) -> None:
        """Кнопка запуска доступна ровно тогда, когда повтор пути совпал."""
        button = getattr(self, "start_btn", None)
        if button is None or not button.winfo_exists():
            return
        matches = self.confirm_device_var.get().strip() == (self.device_path or "")
        button.configure(state=("normal" if matches else "disabled"))

    def _start_install(self) -> None:
        if self._running:
            return
        confirm_device = self.confirm_device_var.get().strip()
        if confirm_device != self.device_path:
            self.message_bar.error(
                "Подтверждение устройства не совпадает с путём выше — установка отменена."
            )
            return
        # Checked here rather than left to fail inside parted: without this the
        # user completes the whole wizard, starts an irreversible operation and
        # gets a permission error from a tool they never invoked. Nothing has
        # been written at this point, so refusing is free.
        if not _is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)
            return

        # Building for another architecture is supported, but only where the
        # kernel can actually execute the target's binaries. Refusing here
        # costs nothing; refusing after wipefs costs the medium's contents.
        emulation = getattr(self, "_emulation", None)
        if emulation is not None and not emulation.native and not emulation.available:
            self.message_bar.error(emulation.reason)
            return

        # An install already in flight -- or the wreckage of one that was
        # interrupted -- means the medium is mounted and its volume open.
        # Starting a second run on top of that corrupts both.
        existing = leftovers.find()
        if existing:
            self.message_bar.error(
                "На машине осталась незавершённая установка. Уберите её на панели "
                "(«Проверить состояние») и повторите:\n" + existing.describe()
            )
            return

        device = getattr(self, "_confirmed_device", None) or self._resolve_device(self._parsed_size_gb())

        self._running = True
        self._install_signal_guard()
        self.start_btn.configure(state="disabled")
        # A real install runs in a background thread and may take minutes;
        # if the wizard's own Back/Next/Cancel destroyed this frame mid-run,
        # the completion callback below would fire against dead widgets and
        # OSInstaller's already-recorded media/recovery records would never
        # reach save_container(). Lock navigation until it's done.
        self.back_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        # «Отмена» превращается в «Прервать»: навигацию блокировать надо (уход
        # с экрана уничтожит виджеты и потеряет код восстановления), а вот
        # остановить многочасовую запись должно быть чем.
        self.cancel_btn.configure(text="Прервать установку", command=self._abort)
        self._stages = UiMailbox(self, self._show_stage)
        self._stages.start()
        self.stage_var.set("Подготовка…")
        self.stage_label.pack(anchor="w", pady=(4, 2), before=self.log_text)
        self.progress.configure(value=0)
        self.progress.pack(fill="x", pady=(0, 8), before=self.log_text)
        self.message_bar.info("Идёт установка.")
        self._log(f"--- УСТАНОВКА НА {device.path} ---")

        passphrase_bytes = self.passphrase.encode("utf-8")
        installer = OSInstaller(self.app.session)
        # Kept so the failure path can ask it whether the medium was written to.
        self._installer = installer

        def do_install():
            return installer.run(
                device,
                self.selected_config,
                passphrase_bytes,
                image_path=self.image_path,
                dry_run=False,
                confirm_device=confirm_device,
                on_step=self._on_stage,
                media_name=self.media_name,
                media_owner=self.media_owner,
            )

        run_in_background(
            self,
            do_install,
            on_success=self._on_install_done,
            on_error=self._on_install_error,
        )

    def _on_stage(self, index: int, total: int, text: str) -> None:
        """Called from the install thread; hands the update to the UI thread."""
        self._stages.post(index, total, text)

    def _show_stage(self, index: int, total: int, text: str) -> None:
        self.stage_var.set(f"Шаг {index} из {total}: {text}")
        self.progress.configure(value=int(index * 100 / max(total, 1)))
        self._log(f"[{index}/{total}] {text}")

    def _abort(self) -> None:
        """Останавливает установку по кнопке.

        Носитель после этого непригоден, но уборка отработает: тома
        закроются, разделы размонтируются, и следующая попытка не упрётся в
        занятые ресурсы.
        """
        installer = getattr(self, "_installer", None)
        if installer is None or not self._running:
            return
        self.cancel_btn.configure(state="disabled")
        self.message_bar.info("Прерываю: жду, пока остановится текущая команда…")
        installer.abort()

    def _stop_progress(self) -> None:
        self._running = False
        self._restore_signal_guard()
        if widget_alive(self.cancel_btn):
            self.cancel_btn.configure(text="Отмена", command=self.on_cancel, state="normal")
        if getattr(self, "_stages", None) is not None:
            self._stages.stop()
        self.progress.pack_forget()
        self.stage_label.pack_forget()
        self.start_btn.configure(state="normal")
        self.back_btn.configure(state=("normal" if self.step_index > 0 else "disabled"))
        self.next_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")

    def _on_install_done(self, result) -> None:
        self._stop_progress()
        for entry in result.plan.results:
            line = " ".join(entry.argv)
            if entry.executed:
                line += f"  [rc={entry.returncode}]"
                if entry.stderr.strip():
                    line += f" stderr: {entry.stderr.strip()}"
            self._log(line)
        self.app.save_container()
        # Warnings are put in front of the success text, not appended to it: a
        # medium built under emulation has to be tried on the target
        # architecture, and that is the part a person must not skim past.
        for warning in getattr(result, "warnings", ()):
            self._log("ВНИМАНИЕ: " + warning)
        # Код восстановления здесь не показывается, и это осознанно. Он
        # выводится из секрета администратора в момент, когда пользователь
        # позвонит, а до тех пор не существует нигде: ни на экране, ни в
        # контейнере. Прежняя приписка «запишите его» выносила содержимое
        # контейнера наружу на каждом собранном носителе.
        self.message_bar.success(
            f"Установка завершена. media_id={result.media_record.media_id}. "
            "Код восстановления выдаётся по запросу пользователя, на экране "
            "«Восстановление»."
            + ("  " + " ".join(result.warnings) if getattr(result, "warnings", ()) else "")
        )
        # Носитель заводится на ресурсе отзыва сразу при выпуске: иначе первый
        # же отзыв упёрся бы в «носитель не ваш» -- владельцем записи сервер
        # считает того, кто её завёл.
        from ..widgets import publish_media_state

        publish_media_state(self, result.media_record.media_id)

    def _on_install_error(self, exc: BaseException) -> None:
        self._stop_progress()
        # Whether the medium was touched is a fact the installer knows, not
        # something to assume. Several failures land before the first write --
        # an image of the wrong family or architecture, a device too small, no
        # emulator registered for the target -- and this used to announce that
        # the stick had been erased in all of them. Telling an administrator
        # their medium is gone when it is intact costs them the data they then
        # do not bother to recover.
        installer = getattr(self, "_installer", None)
        if installer is not None and not getattr(installer, "medium_modified", True):
            suffix = " Носитель не изменён — установка остановилась до записи на него."
        else:
            suffix = " Носитель уже изменён — прежние данные на нём не сохранились."
        # Если уборка не смогла закрыть том, об этом надо сказать здесь же:
        # иначе следующий запуск откажется работать, а причина будет неясна.
        stuck = tuple(getattr(installer, "unreleased_resources", ()) or ())
        if stuck:
            suffix += (
                " Не удалось освободить: " + ", ".join(stuck)
                + ". Уберите на панели («Состояние»)."
            )
        if isinstance(exc, InstallAborted):
            self.message_bar.info(f"Установка прервана.{suffix}")
        elif isinstance(exc, (DeviceNotEligibleError, InstallError)) or isinstance(exc, KNOWN_ERRORS):
            self.message_bar.error(f"{exc}{suffix}")
        else:
            self.message_bar.error(f"Непредвиденная ошибка: {exc}{suffix}")

    # -- WizardFrame hooks ------------------------------------------------
    _STEP_BUILDERS = [
        _build_step_image, _build_step_config, DeviceStepMixin._build_step_device, _build_step_passphrase, _build_step_confirm
    ]
    _STEP_SAVERS = [_save_step_image, None, DeviceStepMixin._save_step_device, _save_step_passphrase, None]
    _STEP_VALIDATORS = [
        _validate_step_image, _validate_step_config, DeviceStepMixin._validate_step_device, _validate_step_passphrase, None
    ]

    def build_step(self, index: int, body: ttk.Frame) -> None:
        self._STEP_BUILDERS[index](self, body)

    def save_step(self, index: int) -> None:
        saver = self._STEP_SAVERS[index]
        if saver is not None:
            saver(self)

    def validate_step(self, index: int) -> bool:
        validator = self._STEP_VALIDATORS[index]
        if validator is None:
            return True
        return validator(self)

    def on_finish(self) -> None:
        self.app.show_dashboard()

    def on_cancel(self) -> None:
        self.app.show_dashboard()
