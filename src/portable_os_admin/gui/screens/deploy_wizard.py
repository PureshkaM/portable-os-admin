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

Подпись проверяется ещё раньше -- при выборе дистрибутива. Сумма ловит порчу,
но не подмену: подменивший архив подменит и файл хеша. Подпись ключом этого
контейнера отвечает на вопрос «собирал ли его я».
"""
from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from ...config.schema import InstallConfig
from ...errors import DeviceNotEligibleError, InstallError
from ...install.command_runner import InstallAborted
from ...admin import MediaReleaseService
from ...install import distribution, leftovers
from .. import theme
from ..elevate import invoking_user_home
from .. import preferences
from ..widgets import KNOWN_ERRORS, UiMailbox, WizardFrame, add_field, run_in_background, widget_alive
from .device_step import (
    DANGER_COLOR,
    NOT_ROOT_MESSAGE,
    DeviceStepMixin,
    TerminationGuardMixin,
    is_root,
)


#: Где запоминается каталог дистрибутива между запусками мастера.
DISTRIBUTION_PREFERENCE_KEY = "deploy.distribution_directory"


def _split_digest(digest: str) -> str:
    if not digest:
        return "нет"
    return f"{digest[:32]}\n{digest[32:]}"


class DeployWizardFrame(DeviceStepMixin, TerminationGuardMixin, WizardFrame):
    def __init__(self, parent: tk.Widget, app):
        self.app = app
        # Каталог дистрибутива запоминается между запусками, и только он.
        #
        # Тираж -- это один и тот же дистрибутив на два десятка флешек, а
        # мастер начинался с чистого поля каждый раз: путь набирался заново на
        # каждую. Пароль и устройство не запоминаются намеренно и по разным
        # причинам: первый -- секрет, которому не место в файле настроек, а
        # второе меняется от флешки к флешке, и подставленное устройство здесь
        # означает стёртый не тот диск.
        self.directory = str(preferences.get(DISTRIBUTION_PREFERENCE_KEY, "") or "")
        self.device_path = ""
        self.device_size_gb = ""
        self.passphrase = ""
        #: Как назвать носитель в списке -- спрашивается здесь, потому что
        #: здесь администратор и решает, кому носитель пойдёт.
        self.media_name = ""
        self.media_owner = ""
        #: Файлы и каталоги, которые лягут в раздел обмена сразу при записи.
        self.exchange_files: list[str] = []
        self.metadata = None
        self.dist_config: InstallConfig | None = None
        #: Вердикт подписи, с которым администратор согласился при выборе.
        #: Перед записью проверка повторяется и должна дать тот же вердикт.
        self.signature_verdict = None
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
                "Владелец обязателен.",
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
        preferences.set_value(DISTRIBUTION_PREFERENCE_KEY, self.directory)

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
        verdict = distribution.verify_distribution(self.directory, self.app.container.admin_secret)
        if not self._accept_signature(verdict):
            self.metadata = None
            self.signature_verdict = None
            return False
        self.signature_verdict = verdict
        self.dist_config = InstallConfig.from_dict(self.metadata.config)
        return True

    def _accept_signature(self, verdict) -> bool:
        """Решает по вердикту подписи, можно ли идти дальше.

        Свой -- молча. Испорченный -- отказ без вариантов: подпись или описание
        изменены, и продолжать тут не с чем. Неподписанный и чужой -- на усмотрение
        администратора, но только явным согласием: дистрибутивы 1.0.0 подписи
        не имеют, а чужой ключ может быть ключом коллеги, и отличить его от
        ключа злоумышленника программа не может -- открытых ключей других
        контейнеров у неё нет.
        """
        if verdict.status == distribution.SIGNED_BY_THIS_CONTAINER:
            return True
        if verdict.status == distribution.INVALID:
            self.message_bar.error(
                f"Дистрибутив отклонён: {verdict.reason}. Соберите его заново."
            )
            return False
        if verdict.status == distribution.FOREIGN:
            title = "Дистрибутив подписан другим ключом"
            text = (
                f"Подпись сделана не ключом этого контейнера (отпечаток {verdict.fingerprint}).\n\n"
                "Если его собрал другой администратор — сверьте отпечаток с ним. "
                "Подменённый дистрибутив выглядит точно так же.\n\n"
                "Записать его на носитель?"
            )
        else:
            title = "Дистрибутив не подписан"
            text = (
                "Подтвердить, что этот дистрибутив собран вами, нечем: так выглядят "
                "и собранные версией 1.0.0, и подменённые.\n\n"
                "Записать его на носитель?"
            )
        if messagebox.askyesno(title, text, icon="warning", default="no", parent=self):
            return True
        self.message_bar.error(f"{title}. Выберите другой или соберите заново.")
        return False

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
            hint="например: «Иванов, бухгалтерия»",
        )
        # Владелец спрашивается отдельно от имени и обязателен.
        #
        # Имя носителя администратор придумывает себе для списка, а владелец --
        # это тот, кого придётся опознать по телефону, когда с этого носителя
        # попросят код восстановления. Без владельца опознавать нечем: носитель
        # показывает код запроса сам, стоит трижды ошибиться паролем, и звонящий
        # может оказаться кем угодно, кто нашёл флешку.
        self.media_owner_var = tk.StringVar(value=self.media_owner)
        add_field(
            form,
            2,
            "Владелец (ФИО):",
            self.media_owner_var,
            width=36,
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
        self.media_owner = self.media_owner_var.get().strip()
        self.exchange_files = list(self.exchange_list.get(0, "end"))

    def _validate_step_handover(self) -> bool:
        """Проверяет, что выбранное ещё существует и поместится.

        Файл, исчезнувший между выбором и записью, иначе обернулся бы
        предупреждением в конце часовой работы; а превышение размера раздела --
        отказом копирования там, где носитель уже стёрт.
        """
        import os

        # Владелец обязателен, и это единственное обязательное поле шага.
        #
        # Носитель, выданный без владельца, нечем связать с человеком, а связь
        # нужна ровно в тот момент, когда с этого носителя просят код
        # восстановления: экран выдачи называет владельца, и администратор
        # опознаёт звонящего по нему. Спросить постфактум невозможно -- через
        # полгода никто не помнит, кому ушла двадцатая флешка из тиража.
        if not self.media_owner_var.get().strip():
            self.message_bar.error(
                "Укажите владельца — ФИО сотрудника, которому выдаётся носитель. "
                "По этому имени вы будете опознавать звонящего, когда с носителя "
                "запросят код восстановления."
            )
            return False

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
        confirm_entry, _ = add_field(
            confirm_frame,
            0,
            "Повторите путь к устройству:",
            self.confirm_device_var,
            hint="это последняя защита от записи не на тот диск",
            width=30,
        )
        # Фокус -- в поле, а не на кнопке, которая стирает носитель.
        confirm_entry.focus_set()

        self.start_btn = ttk.Button(
            body,
            text="Стереть носитель и записать дистрибутив",
            style="Accent.TButton",
            command=self._start,
            state="disabled",
        )
        self.start_btn.pack(anchor="w", pady=(8, 4))
        # Кнопка недоступна, пока путь не повторён верно.
        #
        # Раньше повтор сверялся при нажатии, а кнопка всё это время была
        # готова сработать -- в том числе от пробела и Enter, которыми
        # пролистывают мастер. Условие доступности надёжнее проверки
        # постфактум: пока набрано не то, нажимать нечего, и это видно
        # глазом. Сверка внутри `_start` остаётся -- она отвечает за случай,
        # когда устройство подменили между набором и нажатием.
        self.confirm_device_var.trace_add("write", self._sync_start_button)
        self._sync_start_button()

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

    def _sync_start_button(self, *_args) -> None:
        """Кнопка запуска доступна ровно тогда, когда повтор пути совпал."""
        button = getattr(self, "start_btn", None)
        if button is None or not button.winfo_exists():
            return
        matches = self.confirm_device_var.get().strip() == (self.device_path or "")
        button.configure(state=("normal" if matches else "disabled"))

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

        # Подпись -- ещё раз, перед самой записью. Между выбором дистрибутива
        # и этой кнопкой проходят минуты, и подмена за это время не должна
        # пройти под согласием, данным на другой вердикт.
        accepted = self.signature_verdict
        current = distribution.verify_distribution(self.directory, self.app.container.admin_secret)
        if accepted is None or current != accepted:
            self.message_bar.error(
                "Дистрибутив изменился после выбора — запись отменена. "
                "Вернитесь к первому шагу и выберите его заново."
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
        release = MediaReleaseService(self.app.session)
        # Держим службу, а не установщик: у неё же спрашивают на пути отказа,
        # тронут ли носитель, и её же просят остановиться по кнопке.
        self._release = release

        def work():
            return release.deploy_distribution(
                device,
                self.directory,
                passphrase_bytes,
                dry_run=False,
                confirm_device=confirm_device,
                on_step=self._on_stage,
                media_name=self.media_name,
                media_owner=self.media_owner,
                exchange_files=self.exchange_files,
                # Установщик сверит подпись ещё раз там, где читает описание, и
                # откажет, если вердикт уже не тот, на который дано согласие.
                accepted_signature=accepted,
            )

        run_in_background(self, work, on_success=self._done, on_error=self._failed)

    def _abort(self) -> None:
        """Останавливает запись по кнопке.

        Носитель после этого непригоден — незавершённая установка не
        загрузится, — но уборка отработает: тома закроются, разделы
        размонтируются, и следующая попытка не упрётся в занятые ресурсы.
        """
        release = getattr(self, "_release", None)
        if release is None or not self._running:
            return
        self.cancel_btn.configure(state="disabled")
        self.message_bar.info("Прерываю: жду, пока остановится текущая команда…")
        release.abort()

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
        from ..widgets import publish_media_state

        publish_media_state(self, result.media_record.media_id)

    def _failed(self, exc: BaseException) -> None:
        self._stop_progress()
        release = getattr(self, "_release", None)
        installer = release.installer if release is not None else None
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
    #: Шаги адресуются ключом, а не номером. Именно здесь номер уже подводил:
    #: шаг «Кому и что» вставили между паролем и подтверждением, и `step_index
    #: = 3` с тех пор открывал не тот шаг -- молча, потому что рисуется он так
    #: же. Ключ вставку переживает, и он же -- то, чем будет ветвиться путь
    #: выпуска при слиянии трёх мастеров (зона И5).
    _STEP_KEYS = ("distribution", "device", "passphrase", "handover", "confirm")

    def build_step(self, index: int, body: ttk.Frame) -> None:
        getattr(self, f"_build_step_{self._STEP_KEYS[index]}")(body)

    def save_step(self, index: int) -> None:
        saver = getattr(self, f"_save_step_{self._STEP_KEYS[index]}", None)
        if saver is not None:
            saver()

    def validate_step(self, index: int) -> bool:
        validator = getattr(self, f"_validate_step_{self._STEP_KEYS[index]}", None)
        return True if validator is None else validator()

    def on_finish(self) -> None:
        self.app.show_dashboard()

    def on_cancel(self) -> None:
        self.app.show_dashboard()
