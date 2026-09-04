"""Развёртывание собранного дистрибутива на носитель — вторая половина установки.

Первая половина (экран «Собрать дистрибутив») ничего не стирает и делается
один раз. Эта — деструктивная и повторяется для каждой флешки, поэтому здесь
есть всё, чего там нарочно нет: выбор устройства, парольная фраза,
подтверждение стирания.

Что здесь важно понимать про безопасность схемы: дистрибутив — это эталонный
корень без единого секрета. Шифрование создаётся заново на каждом носителе,
и мастер-ключ LUKS у двух флешек из одного дистрибутива разный. Побайтовое
клонирование готовой флешки такого свойства не даёт и потому запрещено; см.
шапку install/distribution.py.

Контрольная сумма дистрибутива проверяется до того, как носитель тронут:
повреждённый архив должен стоить сообщения, а не стёртой флешки и часа
ожидания.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ...config.schema import InstallConfig
from ...errors import DeviceNotEligibleError, InstallError
from ...install.command_runner import InstallAborted
from ...install import OSInstaller, distribution, leftovers
from .. import theme
from ..elevate import invoking_user_home
from ..widgets import KNOWN_ERRORS, UiMailbox, WizardFrame, add_field, run_in_background, widget_alive
from .device_step import (
    DANGER_COLOR,
    NOT_ROOT_MESSAGE,
    DeviceStepMixin,
    TerminationGuardMixin,
    is_root,
)


def _split_digest(digest: str) -> str:
    if not digest:
        return "нет"
    return f"{digest[:32]}\n{digest[32:]}"


class DeployWizardFrame(DeviceStepMixin, TerminationGuardMixin, WizardFrame):
    def __init__(self, parent: tk.Widget, app):
        self.app = app
        self.directory = ""
        self.device_path = ""
        self.device_size_gb = ""
        self.passphrase = ""
        #: Как назвать носитель в списке -- спрашивается здесь, потому что
        #: здесь администратор и решает, кому носитель пойдёт.
        self.media_name = ""
        #: Файлы и каталоги, которые лягут в раздел обмена сразу при записи.
        self.exchange_files: list[str] = []
        self.metadata = None
        self.dist_config: InstallConfig | None = None
        self._running = False

        super().__init__(
            parent,
            app,
            step_titles=[
                "Дистрибутив",
                "Носитель",
                "Пароль",
                "Кому и что",
                "Подтверждение и запись",
            ],
            step_hints=[
                "",
                "",
                "",
                "Необязательно, но здесь это дешевле, чем потом.",
                "",
            ],
            finish_label="К панели",
        )

    # -- шаг 0: дистрибутив -------------------------------------------------
    def _build_step_distribution(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        self.directory_var = tk.StringVar(value=self.directory)
        add_field(form, 0, "Каталог дистрибутива:", self.directory_var, width=52)
        ttk.Button(body, text="Выбрать каталог…", command=self._browse_directory).pack(
            anchor="w", pady=(theme.SPACE_SM, 0)
        )

    def _browse_directory(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Каталог с дистрибутивом",
            initialdir=self.directory_var.get().strip() or invoking_user_home(),
        )
        if chosen:
            self.directory_var.set(chosen)

    def _save_step_distribution(self) -> None:
        self.directory = self.directory_var.get().strip()

    def _validate_step_distribution(self) -> bool:
        if not self.directory:
            self.message_bar.error("Укажите каталог с дистрибутивом.")
            return False
        try:
            self.metadata = distribution.read_metadata(self.directory)
        except KNOWN_ERRORS as exc:
            self.metadata = None
            self.message_bar.error(str(exc))
            return False
        self.dist_config = InstallConfig.from_dict(self.metadata.config)
        return True

    # -- шаг 2: пароль ------------------------------------------------------
    def _build_step_passphrase(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w")
        self.passphrase_var = tk.StringVar(value=self.passphrase)
        add_field(form, 0, "Пароль пользователя (LUKS):", self.passphrase_var, show="*", width=30)
        self.passphrase_confirm_var = tk.StringVar(value=self.passphrase)
        add_field(form, 2, "Повторите пароль:", self.passphrase_confirm_var, show="*", width=30)

    def _save_step_passphrase(self) -> None:
        self.passphrase = self.passphrase_var.get()

    def _validate_step_passphrase(self) -> bool:
        if not self.passphrase_var.get():
            self.message_bar.error("Задайте парольную фразу.")
            return False
        # Длина проверяется здесь, потому что больше нигде: до 28.08.2026
        # принималась фраза из одного символа. Лимит попыток LUKS от перебора
        # не спасает -- он действует только при загрузке носителя, а тот, у
        # кого носитель в руках, снимает образ заголовка и перебирает офлайн.
        # Там работают ровно два множителя: стоимость попытки (argon2id уже
        # выбран) и длина фразы.
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
        return True

    def _passphrase_minimum(self) -> int:
        """Минимум из конфигурации носителя, а не из кода этого экрана.

        Конфигурация здесь -- та, с которой собран выбранный дистрибутив, и
        порог задаётся вместе с остальными свойствами LUKS. Если прочитать её
        не удалось (экран открыт до выбора дистрибутива), берётся значение по
        умолчанию из схемы -- не ноль, потому что «не смогли прочитать» не
        повод не проверять вовсе.
        """
        from ...config.schema import LuksConfig

        config = getattr(self.metadata, "config", None)
        luks = getattr(config, "luks", None)
        return int(getattr(luks, "min_passphrase_length", 0) or LuksConfig().min_passphrase_length)

    # -- шаг 3: имя носителя и файлы в раздел обмена ------------------------
    def _build_step_handover(self, body: ttk.Frame) -> None:
        """Два вопроса, которые иначе задаются постфактум.

        **Имя.** После записи носитель попадает в список, где строки
        различаются идентификаторами вроде `46aed677`. Они отвечают на вопрос
        «тот ли это носитель», но не на вопрос «чей он», а второй как раз и
        задают, когда носитель надо отозвать или заменить.

        **Файлы.** Через раздел обмена едут настройки программ, которые
        администратор доустановил в образ. Раздел в момент записи уже
        смонтирован -- положить в него файл здесь ничего не стоит, тогда как
        после сборки это отдельный заход: открыть контейнер, подключить
        носитель, открыть том, скопировать.
        """
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        self.media_name_var = tk.StringVar(value=self.media_name)
        add_field(
            form,
            0,
            "Имя носителя:",
            self.media_name_var,
            width=36,
            hint="Например: «Иванов, бухгалтерия». Хранится в контейнере "
            "администратора и на носитель не пишется — переименовать можно в любой момент.",
        )

        files = ttk.LabelFrame(body, text=" Файлы в раздел обмена ", padding=theme.SPACE_SM)
        files.pack(fill="both", expand=True, pady=(theme.SPACE_MD, 0))
        ttk.Label(
            files,
            text="Настройки доустановленных программ и всё, что должно быть у "
            "пользователя с первого запуска. Каталоги копируются целиком.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))

        self.exchange_list = tk.Listbox(
            files,
            height=6,
            selectmode="extended",
            exportselection=False,
            background=theme.SURFACE,
            foreground=theme.TEXT,
            highlightthickness=1,
            highlightbackground=theme.BORDER,
            borderwidth=0,
        )
        self.exchange_list.pack(fill="both", expand=True)
        for path in self.exchange_files:
            self.exchange_list.insert("end", path)

        buttons = ttk.Frame(files)
        buttons.pack(fill="x", pady=(theme.SPACE_SM, 0))
        ttk.Button(buttons, text="Добавить файлы…", command=self._add_exchange_files).pack(
            side="left"
        )
        ttk.Button(buttons, text="Добавить каталог…", command=self._add_exchange_directory).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )
        ttk.Button(buttons, text="Убрать выбранное", command=self._remove_exchange_entries).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

    def _add_exchange_files(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilenames(parent=self, title="Файлы для раздела обмена")
        for path in chosen or ():
            if path not in self.exchange_list.get(0, "end"):
                self.exchange_list.insert("end", path)

    def _add_exchange_directory(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(parent=self, title="Каталог для раздела обмена")
        if chosen and chosen not in self.exchange_list.get(0, "end"):
            self.exchange_list.insert("end", chosen)

    def _remove_exchange_entries(self) -> None:
        for index in reversed(self.exchange_list.curselection()):
            self.exchange_list.delete(index)

    def _save_step_handover(self) -> None:
        self.media_name = self.media_name_var.get().strip()
        self.exchange_files = list(self.exchange_list.get(0, "end"))

    def _validate_step_handover(self) -> bool:
        """Проверяет, что выбранное ещё существует и поместится.

        Файл, исчезнувший между выбором и записью, иначе обернулся бы
        предупреждением в конце часовой работы; а превышение размера раздела --
        отказом копирования там, где носитель уже стёрт.
        """
        import os

        missing = [path for path in self.exchange_files if not os.path.exists(path)]
        if missing:
            self.message_bar.error(
                "Не найдено: " + ", ".join(missing) + ". Уберите из списка или выберите заново."
            )
            return False

        total = 0
        for path in self.exchange_files:
            if os.path.isfile(path):
                total += os.path.getsize(path)
                continue
            for directory, _dirs, names in os.walk(path):
                for name in names:
                    try:
                        total += os.path.getsize(os.path.join(directory, name))
                    except OSError:
                        pass
        config = getattr(self.metadata, "config", None)
        capacity_mb = int(getattr(getattr(config, "disk", None), "exchange_partition_mb", 0) or 0)
        if capacity_mb and total > capacity_mb * 1024 * 1024 * 0.9:
            self.message_bar.error(
                f"Выбрано {total / 1024 / 1024:.0f} МБ, а раздел обмена этого дистрибутива — "
                f"{capacity_mb} МБ. Уберите лишнее или соберите дистрибутив с большим разделом."
            )
            return False
        return True

    # -- шаг 3: подтверждение и запись --------------------------------------
    def _build_step_confirm(self, body: ttk.Frame) -> None:
        meta = self.metadata
        box = ttk.LabelFrame(body, text=" Что будет записано ")
        box.pack(fill="x")
        mode = "киоск" if meta.profile == "kiosk" else "консольная система"
        if meta.desktop_environment:
            mode += f" / {meta.desktop_environment}"
            if meta.session_type:
                mode += f" ({meta.session_type})"
        rows = [
            ("Дистрибутив:", meta.name or "(без имени)"),
            ("Система:", f"{meta.target_os} {meta.release} · {meta.arch}"),
            ("Режим:", mode),
            ("Размер системы:", f"{meta.rootfs_bytes / 1024**3:.1f} ГБ"),
            ("Собран:", meta.built_at or "неизвестно"),
            # Целиком, а не первыми символами: её сверяют глазами с той, что
            # записана рядом с образом. Пополам — потому что в шестидесяти
            # четырёх символах без пробелов переносить нечего, и строка просто
            # уезжала за край окна.
            ("Контрольная сумма:", _split_digest(meta.sha256)),
        ]
        for label, value in rows:
            row = ttk.Frame(box)
            row.pack(anchor="w", fill="x", padx=10, pady=1)
            ttk.Label(row, text=label, width=20).pack(side="left")
            ttk.Label(row, text=value, wraplength=520, justify="left").pack(side="left")


        if meta.built_under_emulation:
            ttk.Label(
                body,
                text=(
                    f"Собран под эмуляцией: проверьте загрузку на машине {meta.arch}."
                ),
                foreground=DANGER_COLOR,
                wraplength=theme.TEXT_WRAP,
                justify="left",
            ).pack(anchor="w", pady=(theme.SPACE_SM, 0))

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

        if device.active_mountpoints:
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

        confirm_frame = ttk.Frame(body)
        confirm_frame.pack(anchor="w", pady=(4, 0))
        self.confirm_device_var = tk.StringVar()
        add_field(
            confirm_frame,
            0,
            "Повторите путь к устройству:",
            self.confirm_device_var,
            hint="это последняя защита от записи не на тот диск",
            width=30,
        )

        self.start_btn = ttk.Button(
            body,
            text="Стереть носитель и записать дистрибутив",
            style="Accent.TButton",
            command=self._start,
        )
        self.start_btn.pack(anchor="w", pady=(8, 4))

        self.stage_var = tk.StringVar(value="")
        self.stage_label = ttk.Label(body, textvariable=self.stage_var, wraplength=theme.TEXT_WRAP, justify="left")
        self.progress = ttk.Progressbar(body, mode="determinate", length=420, maximum=100)

        self.log_text = tk.Text(body, height=10, width=95, state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

        if not is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)

    def _log(self, text: str) -> None:
        if not widget_alive(self.log_text):
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _on_stage(self, index: int, total: int, text: str) -> None:
        # Из рабочего потока: здесь нельзя трогать виджеты, только отправить.
        self._stages.post(index, total, text)

    def _show_stage(self, index: int, total: int, text: str) -> None:
        self.stage_var.set(f"Шаг {index} из {total}: {text}")
        self.progress.configure(value=int(index * 100 / max(total, 1)))
        self._log(f"[{index}/{total}] {text}")

    def _start(self) -> None:
        if self._running:
            return
        confirm_device = self.confirm_device_var.get().strip()
        if confirm_device != self.device_path:
            self.message_bar.error(
                "Подтверждение устройства не совпадает с путём выше — запись отменена."
            )
            return
        if not is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)
            return

        existing = leftovers.find()
        if existing:
            self.message_bar.error(
                "На машине осталась незавершённая установка. Уберите её на панели "
                "(«Состояние») и повторите:\n" + existing.describe()
            )
            return

        device = getattr(self, "_confirmed_device", None) or self._resolve_device(self._parsed_size_gb())

        self._running = True
        self._install_signal_guard()
        self.start_btn.configure(state="disabled")
        # Навигация блокируется намеренно: уход с экрана уничтожит виджеты, и
        # код восстановления от уже записанного носителя не попадёт в
        # контейнер — он сохраняется в обработчике завершения. А вот прервать
        # работу должно быть чем: раньше вместе с навигацией отключалась и
        # «Отмена», и остановить многочасовую запись из интерфейса было
        # нельзя вовсе.
        for button in (self.back_btn, self.next_btn):
            button.configure(state="disabled")
        self.cancel_btn.configure(text="Прервать запись", command=self._abort)
        self._stages = UiMailbox(self, self._show_stage)
        self._stages.start()
        self.stage_var.set("Подготовка…")
        self.stage_label.pack(anchor="w", pady=(4, 2), before=self.log_text)
        self.progress.configure(value=0)
        self.progress.pack(fill="x", pady=(0, 8), before=self.log_text)
        self.message_bar.info("Идёт запись на носитель.")
        self._log(f"--- РАЗВЁРТЫВАНИЕ НА {device.path} ---")
        self._log(f"Дистрибутив: {self.directory}")

        passphrase_bytes = self.passphrase.encode("utf-8")
        installer = OSInstaller(self.app.session)
        # Нужен пути отказа, чтобы спросить у него, тронут ли носитель.
        self._installer = installer

        def work():
            return installer.deploy_distribution(
                device,
                self.directory,
                passphrase_bytes,
                dry_run=False,
                confirm_device=confirm_device,
                on_step=self._on_stage,
                media_name=self.media_name,
                exchange_files=self.exchange_files,
            )

        run_in_background(self, work, on_success=self._done, on_error=self._failed)

    def _abort(self) -> None:
        """Останавливает запись по кнопке.

        Носитель после этого непригоден — незавершённая установка не
        загрузится, — но уборка отработает: тома закроются, разделы
        размонтируются, и следующая попытка не упрётся в занятые ресурсы.
        """
        installer = getattr(self, "_installer", None)
        if installer is None or not self._running:
            return
        self.cancel_btn.configure(state="disabled")
        self.message_bar.info("Прерываю: жду, пока остановится текущая команда…")
        installer.abort()

    def _restore_cancel(self) -> None:
        if widget_alive(self.cancel_btn):
            self.cancel_btn.configure(text="Отмена", command=self.on_cancel, state="normal")

    def _stop_progress(self) -> None:
        self._running = False
        self._restore_cancel()
        self._restore_signal_guard()
        if getattr(self, "_stages", None) is not None:
            self._stages.stop()
        if widget_alive(self.progress):
            self.progress.pack_forget()
        if widget_alive(self.stage_label):
            self.stage_label.pack_forget()
        for button in (self.start_btn, self.back_btn, self.next_btn, self.cancel_btn):
            if widget_alive(button):
                button.configure(state="normal")

    def _done(self, result) -> None:
        self._stop_progress()
        self.app.save_container()
        for warning in getattr(result, "warnings", ()):
            self._log("ВНИМАНИЕ: " + warning)
        # Кода на экране нет намеренно -- см. install_wizard: он выводится
        # из секрета администратора тогда, когда понадобится, и до этого
        # момента не существует нигде.
        self.message_bar.success(
            f"Носитель готов. media_id={result.media_record.media_id}. "
            "Код восстановления выдаётся по запросу пользователя, на экране "
            "«Восстановление»."
        )

    def _failed(self, exc: BaseException) -> None:
        self._stop_progress()
        installer = getattr(self, "_installer", None)
        if installer is not None and not getattr(installer, "medium_modified", True):
            suffix = " Носитель не изменён — запись остановилась до того, как его тронули."
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
            # Не ошибка, а решение человека: так и сказать.
            self.message_bar.info(f"Запись прервана.{suffix}")
        elif isinstance(exc, (DeviceNotEligibleError, InstallError)) or isinstance(exc, KNOWN_ERRORS):
            self.message_bar.error(f"{exc}{suffix}")
        else:
            self.message_bar.error(f"Непредвиденная ошибка: {exc}{suffix}")

    # -- связка -------------------------------------------------------------
    _STEP_BUILDERS = [
        _build_step_distribution,
        DeviceStepMixin._build_step_device,
        _build_step_passphrase,
        _build_step_handover,
        _build_step_confirm,
    ]
    _STEP_SAVERS = [
        _save_step_distribution,
        DeviceStepMixin._save_step_device,
        _save_step_passphrase,
        _save_step_handover,
        None,
    ]
    _STEP_VALIDATORS = [
        _validate_step_distribution,
        DeviceStepMixin._validate_step_device,
        _validate_step_passphrase,
        _validate_step_handover,
        None,
    ]

    def build_step(self, index: int, body: ttk.Frame) -> None:
        self._STEP_BUILDERS[index](self, body)

    def save_step(self, index: int) -> None:
        saver = self._STEP_SAVERS[index]
        if saver is not None:
            saver(self)

    def validate_step(self, index: int) -> bool:
        validator = self._STEP_VALIDATORS[index]
        return True if validator is None else validator(self)

    def on_finish(self) -> None:
        self.app.show_dashboard()

    def on_cancel(self) -> None:
        self.app.show_dashboard()
