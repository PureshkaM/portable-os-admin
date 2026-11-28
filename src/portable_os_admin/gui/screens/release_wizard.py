"""Выпуск носителя одним мастером: путь выводится из источника.

Мастеров было три -- установка с образа, сборка дистрибутива, развёртывание
готового, -- и администратор выбирал между ними тремя кнопками на панели,
раньше чем понимал разницу. Разница же вся в одном: что у него под рукой.
Есть установочный образ -- систему надо ставить; есть готовый дистрибутив --
его надо записать. Программа видит это сама, стоит посмотреть на выбранный
путь, и спрашивать об этом незачем (решение 16.09.2026, «источник решает»).

Поэтому первый шаг спрашивает **источник**, а не путь выпуска: файл образа или
каталог с готовым дистрибутивом. Путь складывается сам, и дальше показываются
только те шаги, которые для него имеют смысл.

Шагов в каждой ветви ровно столько же, сколько было в отдельном мастере, --
пять. Это условие, а не совпадение: слияние, удлиняющее работу, ухудшает ровно
то, ради чего затевалось. Отсюда два решения:

* «куда» (носитель или каталог для тиража) спрашивается **на шаге источника** и
  только когда выбран образ. У готового дистрибутива выбора нет -- его можно
  только записать на носитель, -- и лишний шаг с единственным вариантом это
  вопрос, на который ответ уже известен;
* имя и владелец спрашиваются на шаге пароля там, где раздела обмена в работе
  нет, и отдельным шагом «Кому и что» там, где вместе с ними кладутся файлы.

Ветви и их шаги:

| Источник | Куда | Шаги |
|---|---|---|
| образ | носитель | источник, конфигурация, носитель, пароль и владелец, подтверждение |
| образ | каталог | источник, конфигурация, свои пакеты, куда сохранить, сборка |
| дистрибутив | носитель | источник, носитель, пароль, кому и что, подтверждение |

Шаги адресуются ключом, а не номером: у трёх ветвей номера столкнулись бы, и
шаг «Носитель» одной оказался бы шагом «Свои пакеты» другой -- молча, потому
что рисуются они одинаково. На этом проект уже обжигался.
"""
from __future__ import annotations

import os
from ..ui import tk
from pathlib import Path
from ..ui import filedialog, ttk

from ...admin import MediaReleaseService
from ...config.schema import InstallConfig
from ...errors import DeviceNotEligibleError, InstallError
from ...install import BlockDevice, OSInstaller, distribution, image_source, leftovers
from ...install.command_runner import InstallAborted
from ...install.emulation import probe as probe_emulation
from ...install.installer import IMAGE_FAMILY
from .. import preferences, theme
from ..elevate import invoking_user_home
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
    is_root,
)


#: Где запоминается источник между запусками.
#:
#: Тираж -- это один и тот же дистрибутив на два десятка флешек, а мастер
#: начинался с чистого поля каждый раз. Пароль и устройство не запоминаются
#: намеренно и по разным причинам: первый секрет и в файле настроек ему не
#: место, второе меняется от флешки к флешке, и подставленное устройство здесь
#: означает стёртый не тот диск.
SOURCE_PREFERENCE_KEY = "deploy.distribution_directory"

#: Что делать с образом: записать носитель сразу или собрать дистрибутив.
TARGET_MEDIUM = "medium"
TARGET_DISTRIBUTION = "distribution"

#: Что выбрано источником.
SOURCE_IMAGE = "image"
SOURCE_DISTRIBUTION = "distribution"


def _split_digest(digest: str) -> str:
    if not digest:
        return "нет"
    return f"{digest[:32]}\n{digest[32:]}"


class ReleaseWizardFrame(DeviceStepMixin, TerminationGuardMixin, WizardFrame):
    def __init__(self, parent: tk.Widget, app, config_name: "str | None" = None):
        self.app = app
        self.preselected_config_name = config_name

        # -- источник и путь
        self.source_path = str(preferences.get(SOURCE_PREFERENCE_KEY, "") or "")
        self.source_kind = ""
        self.target = TARGET_MEDIUM

        # -- ветвь образа
        self.image_path = ""
        self.image_info = None
        self.selected_config_name: "str | None" = None
        self.selected_config = None
        self._valid_names: list[str] = []
        self._config_fitness: dict[str, tuple[bool, str]] = {}
        self.extra_packages: list[str] = []
        self.destination = ""

        # -- ветвь готового дистрибутива
        self.directory = ""
        self.metadata = None
        self.dist_config: "InstallConfig | None" = None
        #: Вердикт подписи, с которым администратор согласился при выборе.
        #: Перед записью проверка повторяется и должна дать тот же вердикт.
        self.signature_verdict = None

        # -- общее
        self.device_path = ""
        self.passphrase = ""
        self.generated_passphrase: str | None = None
        self.media_name = ""
        self.media_owner = ""
        self.exchange_files: list[str] = []
        self._devices: list[BlockDevice] = []
        self._running = False

        plan = self._plan()
        super().__init__(
            parent,
            app,
            step_titles=[title for _key, title, _hint in plan],
            step_hints=[hint for _key, _title, hint in plan],
            finish_label="К панели",
        )

    # -- план шагов --------------------------------------------------------
    def _plan(self) -> "list[tuple[str, str, str]]":
        """Какие шаги имеют смысл для выбранного источника.

        Считается каждый раз, а не запоминается: источник меняется возвратом на
        первый шаг, и запомненный план показывал бы шаги прошлой ветви.
        """
        source = ("source", "Источник", "")
        if not self.source_kind:
            # Источник ещё не прочитан -- обещать шаги нечем.
            #
            # Рельс слева существует затем, чтобы ответить «что впереди и
            # сколько осталось». Показать до выбора «Носитель» и «Пароль»
            # значит пообещать ветвь, которую администратор, может быть, и не
            # выберет: он вправе собирать дистрибутив, где носителя нет вовсе.
            # Честный ответ здесь -- назвать причину, а не выдумать путь.
            return [
                source,
                ("unknown", "Дальше — по источнику", ""),
            ]
        if self.source_kind == SOURCE_DISTRIBUTION:
            return [
                source,
                ("device", "Носитель", ""),
                ("passphrase", "Пароль", ""),
                ("handover", "Кому и что", "Владелец обязателен."),
                ("confirm", "Подтверждение и запись", ""),
            ]
        if self.source_kind == SOURCE_IMAGE and self.target == TARGET_DISTRIBUTION:
            return [
                source,
                ("config", "Конфигурация", ""),
                (
                    "packages",
                    "Свои пакеты",
                    # Единственная подсказка ветви, и она несёт неочевидное:
                    # почему это делается здесь, а не на собранном носителе.
                    "Ставятся при сборке, поэтому зависимости разрешаются по образу "
                    "один раз, и каждый носитель из этого дистрибутива получает "
                    "одно и то же.",
                ),
                ("destination", "Куда сохранить", ""),
                ("run", "Сборка", ""),
            ]
        return [
            source,
            ("config", "Конфигурация", ""),
            ("device", "Носитель", ""),
            ("passphrase", "Пароль и владелец", ""),
            ("confirm", "Подтверждение и запись", "Проверьте, что указан именно тот диск."),
        ]

    def step_plan(self) -> "list[tuple[str, str, str]]":
        return self._plan()

    def _key(self, index: int) -> str:
        """Ключ шага по его номеру -- из плана, а не из запомненного списка."""
        return self._plan()[index][0]

    def build_step(self, index: int, body: ttk.Frame) -> None:
        getattr(self, f"_build_step_{self._key(index)}")(body)

    def save_step(self, index: int) -> None:
        saver = getattr(self, f"_save_step_{self._key(index)}", None)
        if saver is not None:
            saver()

    def validate_step(self, index: int) -> bool:
        validator = getattr(self, f"_validate_step_{self._key(index)}", None)
        return True if validator is None else validator()

    # -- шаг: источник -----------------------------------------------------
    def _build_step_source(self, body: ttk.Frame) -> None:
        ttk.Label(
            body,
            text="Установочный образ (полный DVD; netinst не годится) или каталог "
            "с готовым дистрибутивом, собранным ранее.",
            wraplength=theme.TEXT_WRAP,
            justify="left",
            foreground=theme.TEXT_MUTED,
        ).pack(anchor="w")

        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x", pady=(theme.SPACE_SM, 0))
        self.source_var = tk.StringVar(value=self.source_path)
        add_field(
            form,
            0,
            "Что берём за основу:",
            self.source_var,
            hint="оставьте пустым, чтобы ставить из сетевого репозитория конфигурации (медленнее)",
            width=52,
        )
        buttons = ttk.Frame(body)
        buttons.pack(anchor="w", pady=(theme.SPACE_SM, 0))
        ttk.Button(buttons, text="Выбрать образ…", command=self._choose_image).pack(side="left")
        ttk.Button(buttons, text="Выбрать дистрибутив…", command=self._choose_distribution).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

        self.source_status_var = tk.StringVar(value=self._describe_source())
        self.source_status = ttk.Label(
            body, textvariable=self.source_status_var, wraplength=theme.TEXT_WRAP, justify="left"
        )
        self.source_status.pack(anchor="w", pady=(theme.SPACE_SM, 0))
        self.source_progress = ttk.Progressbar(body, mode="indeterminate", length=260)

        # Что делать с образом -- здесь же, а не отдельным шагом.
        #
        # Отдельный шаг с двумя вариантами удлинил бы главный путь на шаг ради
        # вопроса, который умещается в две строки. У готового дистрибутива
        # выбора нет вовсе -- его можно только записать, -- поэтому переключатель
        # виден только когда речь об образе.
        self._target_box = ttk.LabelFrame(body, text=" Что сделать ", padding=theme.SPACE_SM)
        self.target_var = tk.StringVar(value=self.target)
        ttk.Radiobutton(
            self._target_box,
            text="Записать носитель сразу",
            value=TARGET_MEDIUM,
            variable=self.target_var,
        ).pack(anchor="w")
        ttk.Radiobutton(
            self._target_box,
            text="Собрать дистрибутив для тиража",
            value=TARGET_DISTRIBUTION,
            variable=self.target_var,
        ).pack(anchor="w")
        ttk.Label(
            self._target_box,
            text="Дистрибутив собирается один раз, а разворачивается на сколько "
            "угодно носителей за минуты. Шифрование создаётся заново на каждом.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(theme.SPACE_XS, 0))
        self._sync_target_box()
        self.source_var.trace_add("write", lambda *_a: self._sync_target_box())

    def _sync_target_box(self, *_args) -> None:
        """Переключатель «что сделать» -- только для образа."""
        box = getattr(self, "_target_box", None)
        if box is None or not widget_alive(box):
            return
        chosen = self.source_var.get().strip()
        if chosen and os.path.isdir(chosen):
            box.pack_forget()
        else:
            box.pack(fill="x", pady=(theme.SPACE_MD, 0))

    def _choose_image(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Выберите установочный образ",
            filetypes=[("Образы дисков", "*.iso *.ISO *.img"), ("Все файлы", "*")],
            initialdir=self._initial_dir(),
        )
        if not chosen:
            return
        self.source_var.set(chosen)
        self._examine_image(chosen)

    def _choose_distribution(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Каталог с готовым дистрибутивом",
            initialdir=self._initial_dir(),
        )
        if chosen:
            self.source_var.set(chosen)
            self._sync_target_box()

    def _initial_dir(self) -> str:
        current = self.source_var.get().strip()
        if current and Path(current).parent.is_dir():
            return str(Path(current).parent)
        home = invoking_user_home()
        documents = home / "Документы"
        if not documents.is_dir():
            documents = home / "Documents"
        return str(documents if documents.is_dir() else home)

    def _examine_image(self, path: str) -> None:
        """Читает образ, чтобы сказать, что это, **до** часа установки, а не
        из отказа в глубине debootstrap."""
        self.image_info = None
        self.source_status_var.set("Читаю образ…")
        self.source_status.configure(foreground="")
        self.source_progress.pack(anchor="w", pady=(theme.SPACE_XS, 0))
        self.source_progress.start(12)
        run_in_background(
            self,
            lambda: image_source.probe(path),
            on_success=self._on_image_examined,
            on_error=self._on_image_error,
        )

    def _on_image_examined(self, info) -> None:
        self.image_info = info
        if not widget_alive(self.source_progress):
            return
        self.source_progress.stop()
        self.source_progress.pack_forget()
        self.source_status_var.set(self._describe_source())
        self.source_status.configure(foreground="" if info.complete else DANGER_COLOR)

    def _on_image_error(self, exc: BaseException) -> None:
        self.image_info = None
        if not widget_alive(self.source_progress):
            return
        self.source_progress.stop()
        self.source_progress.pack_forget()
        self.source_status_var.set(f"Образ не распознан: {exc}")
        self.source_status.configure(foreground=DANGER_COLOR)

    def _describe_source(self) -> str:
        info = self.image_info
        if info is None:
            return "Источник не выбран."
        if not info.complete:
            return f"{info.label}\n{info.incomplete_reason}"
        return (
            f"{info.label}\n"
            f"Будет установлено: {info.family} {info.release}"
            + (f", {', '.join(info.architectures)}" if info.architectures else "")
        )

    def _save_step_source(self) -> None:
        self.source_path = self.source_var.get().strip()
        self.target = self.target_var.get()
        preferences.set_value(SOURCE_PREFERENCE_KEY, self.source_path)

    def _validate_step_source(self) -> bool:
        """Определяет путь выпуска по тому, что выбрано.

        Здесь же пересобирается план шагов: переход увеличивает номер шага
        **после** проверки, поэтому пересборка внутри неё безопасна -- дальше
        администратор попадает на первый шаг выбранной ветви, а не на шаг с
        тем же номером из прежней.
        """
        chosen = self.source_path
        if not chosen:
            # Пустое поле -- осознанный выбор «ставить из сети», а не упущение.
            # Работает он только если на следующем шаге выбрана конфигурация с
            # адресом репозитория, поэтому пропускается с названным следствием,
            # а не отказом.
            self.message_bar.info(
                "Источник не выбран: система будет установлена из сетевого репозитория, "
                "заданного в конфигурации. Это медленнее и требует доступа к сети; "
                "установка с локального образа предпочтительнее."
            )
            self.source_kind = SOURCE_IMAGE
            self.image_path = ""
            self.image_info = None
            self._apply_step_plan()
            return True

        if os.path.isdir(chosen):
            return self._accept_distribution(chosen)

        if not Path(chosen).is_file():
            self.message_bar.error(f"Не найдено: {chosen}")
            return False
        return self._accept_image(chosen)

    def _accept_image(self, path: str) -> bool:
        self.source_kind = SOURCE_IMAGE
        self.image_path = path
        self.directory = ""
        self.metadata = None
        if self.image_info is None or self.image_info.path != path:
            # Читается здесь, а не только по кнопке «Выбрать образ…».
            #
            # Найдено живым прогоном 17.09.2026: путь, набранный в поле или
            # подставленный из запомненного, оставлял мастер слепым --
            # `image_info` пустой, -- и на шаге конфигураций все строки
            # получали «образ не прочитан», то есть отбор по пригодности молча
            # выключался. Debian-конфигурация предлагалась наравне с РЕД ОС для
            # образа РЕД ОС, а отказ приходил внутри установки, когда носитель
            # уже стёрт.
            #
            # Синхронно, а не в фоне: от прочитанного зависит решение идти
            # дальше, и «проверим потом» -- это и есть та добровольность правды
            # об образе, которую зона И1 убирала из мастера конфигураций.
            try:
                self.image_info = image_source.probe(path)
            except Exception as exc:  # noqa: BLE001 -- причина показывается человеку
                self.image_info = None
                self.message_bar.info(
                    f"Образ не удалось прочитать ({exc}). Он будет проверен перед "
                    "началом работы, а конфигурации показаны без отбора по семейству."
                )
                self._apply_step_plan()
                return True
        if not self.image_info.complete:
            self.message_bar.error(self.image_info.incomplete_reason)
            return False
        self._apply_step_plan()
        return True

    def _accept_distribution(self, directory: str) -> bool:
        try:
            metadata = distribution.read_metadata(directory)
        except KNOWN_ERRORS as exc:
            self.metadata = None
            self.message_bar.error(str(exc))
            return False
        verdict = distribution.verify_distribution(directory, self.app.container.admin_secret, expected=metadata)
        if not self._accept_signature(verdict):
            self.metadata = None
            self.signature_verdict = None
            return False
        self.source_kind = SOURCE_DISTRIBUTION
        self.directory = directory
        self.metadata = metadata
        self.signature_verdict = verdict
        self.dist_config = InstallConfig.from_dict(metadata.config)
        self.image_path = ""
        self.image_info = None
        self._apply_step_plan()
        return True

    def _accept_signature(self, verdict) -> bool:
        """К выдаче допускается дистрибутив с проверенной подписью его описания."""
        if verdict.status == distribution.SIGNED_BY_THIS_CONTAINER:
            return True
        reason = verdict.reason or "требуется проверяемая подпись открытого контейнера"
        self.message_bar.error(f"Дистрибутив отклонён: {reason}. Соберите его заново.")
        return False

    def _build_step_unknown(self, body: ttk.Frame) -> None:
        """Шаг-заглушка: до него не доходят, но нарисовать его должно быть чем.

        План пересобирается проверкой первого шага, а номер увеличивается уже
        после неё, поэтому администратор сюда не попадает. Строитель всё равно
        нужен: раздача ищет метод по имени ключа, и ключ без метода -- это
        падение в середине выпуска вместо честного шага.
        """
        ttk.Label(
            body,
            text="Сначала выберите источник: от него зависит, что будет дальше — "
            "запись носителя или сборка дистрибутива.",
            wraplength=theme.TEXT_WRAP,
            justify="left",
            foreground=theme.TEXT_MUTED,
        ).pack(anchor="w")

    # -- шаг: конфигурация -------------------------------------------------
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
            # Пять строк видно сразу: одна означала, что вторая конфигурация
            # существует только под прокруткой, и администратор её не находит.
            min_rows=5,
            pady=(4, 8),
        )
        # Непригодные строки остаются видимыми: то, что конфигурация есть, но не
        # подходит выбранному образу, -- это сведения; молча её опустить значит
        # показать, что она пропала.
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
            ttk.Button(body, text="Перейти к конфигурациям", command=self._go_to_configs).pack(
                anchor="w"
            )
            return

        preselect = self.selected_config_name or self.preselected_config_name
        if preselect in self._valid_names:
            children = self.config_table.get_children()
            row_id = children[self._valid_names.index(preselect)]
            self.config_table.selection_set(row_id)
            self.config_table.see(row_id)

    def _fits_image(self, config) -> "tuple[bool, str]":
        """Можно ли из этой конфигурации ставить с выбранного образа.

        Образ решает семейство и архитектуру, поэтому конфигурация для другого
        семейства с ним просто не работает. Сказано по каждой строке, а не одной
        ошибкой постфактум: ответ нужен в момент выбора, а не после него.
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

        self.app.show_section("configs")

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

    # -- шаг: свои пакеты --------------------------------------------------
    def _build_step_packages(self, body: ttk.Frame) -> None:
        """Файлы .deb/.rpm, которых нет ни в одном репозитории.

        Ради этого шага инструмент во многом и существует. Клиента VPN и
        клиента удалённого рабочего стола в базовой системе нет намеренно: у
        каждой организации свой шлюз и свой клиент, и выбор за администратора
        не сделать.
        """
        self.packages_table, _empty = data_table(
            body,
            (
                Column("name", "Файл", 420, 200, stretch=True),
                Column("size", "Размер", 120, 100, numeric=True),
            ),
            height=7,
        )

        buttons = ttk.Frame(body)
        buttons.pack(anchor="w", fill="x", pady=(theme.SPACE_SM, 0))
        ttk.Button(buttons, text="Добавить пакеты…", command=self._browse_packages).pack(side="left")
        ttk.Button(buttons, text="Убрать", command=self._remove_package).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )
        self._refresh_packages()

    def _package_suffix(self) -> str:
        """Какое расширение подходит выбранной конфигурации.

        Берётся из установщика, а не пишется здесь второй раз: разойдись эти
        два списка -- и интерфейс принимал бы файл, который сборка отвергнет
        через полчаса работы.
        """
        config = self.selected_config or self.app.config_manager.open(
            self.app.container, self.selected_config_name
        )
        return OSInstaller.PACKAGE_SUFFIX.get(config.target_os, "")

    def _refresh_packages(self) -> None:
        self.packages_table.delete(*self.packages_table.get_children())
        for path in self.extra_packages:
            size = Path(path).stat().st_size if Path(path).is_file() else 0
            self.packages_table.insert(
                "", "end", values=(Path(path).name, f"{size / 1024 / 1024:.1f} МБ")
            )

    def _browse_packages(self) -> None:
        suffix = self._package_suffix()
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="Выберите пакеты",
            filetypes=[(f"Пакеты {suffix}", f"*{suffix}"), ("Все файлы", "*")],
        )
        # Расширение проверяется здесь, а не только перед сборкой: узнать о том,
        # что взят .deb вместо .rpm, следует в тот же миг, а не после получаса
        # распаковки.
        wrong = [name for name in chosen if suffix and not name.lower().endswith(suffix)]
        if wrong:
            names = ", ".join(Path(name).name for name in wrong)
            self.message_bar.error(f"Нужен {suffix}: {names}")
        elif chosen:
            # Снять прошлый отказ: иначе экран противоречит сам себе -- список
            # пополнился, а под ним красным висит «Нужен .rpm» от прошлой попытки.
            self.message_bar.clear()
        for name in chosen:
            if name not in self.extra_packages and name not in wrong:
                self.extra_packages.append(name)
        self._refresh_packages()

    def _remove_package(self) -> None:
        selection = self.packages_table.selection()
        if not selection:
            return
        for item in selection:
            index = self.packages_table.index(item)
            del self.extra_packages[index]
        self._refresh_packages()

    # -- шаг: куда сохранить -----------------------------------------------
    def _build_step_destination(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        # Умолчание -- каталог рядом с контейнером администратора: там же, где он
        # работает, и туда точно есть право писать.
        default = str(self.app.container_path.parent) if self.app.container_path else ""
        self.destination_var = tk.StringVar(value=self.destination or default)
        add_field(
            form,
            0,
            "Каталог для дистрибутива:",
            self.destination_var,
            width=52,
            hint="Появятся файл системы, его контрольная сумма и описание.",
        )
        ttk.Button(body, text="Выбрать каталог…", command=self._browse_destination).pack(
            anchor="w", pady=(theme.SPACE_SM, 0)
        )

    def _browse_destination(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Куда сохранить дистрибутив")
        if chosen:
            self.destination_var.set(chosen)

    def _save_step_destination(self) -> None:
        self.destination = self.destination_var.get().strip()

    def _validate_step_destination(self) -> bool:
        if not self.destination:
            self.message_bar.error("Укажите каталог для дистрибутива.")
            return False
        return True

    # -- шаг: сборка -------------------------------------------------------
    def _chosen_config(self):
        """Выбранная конфигурация -- по имени, если объекта ещё нет.

        Объект заполняет проверка шага «Конфигурация», но попасть на шаги
        сборки можно и минуя её (мастер открывают на нужном шаге). Имя же
        известно всегда, а открытие по имени -- ровно то, что делал отдельный
        мастер сборки в каждом таком месте.
        """
        if self.selected_config is None and self.selected_config_name:
            self.selected_config = self.app.config_manager.open(
                self.app.container, self.selected_config_name
            )
        return self.selected_config

    def _build_step_run(self, body: ttk.Frame) -> None:
        config = self._chosen_config()
        summary = ttk.Frame(body)
        summary.pack(fill="x")
        for label, value in (
            ("Образ:", Path(self.image_path).name if self.image_path else "сетевой репозиторий"),
            ("Конфигурация:", self.selected_config_name),
            ("Режим:", "киоск" if config.kiosk.enabled else "консольная система"),
            ("Свои пакеты:", ", ".join(Path(p).name for p in self.extra_packages) or "нет"),
            ("Куда:", self.destination),
        ):
            row = ttk.Frame(summary)
            row.pack(anchor="w", fill="x")
            ttk.Label(row, text=label, width=16).pack(side="left")
            ttk.Label(row, text=value).pack(side="left")

        self.start_btn = ttk.Button(
            body, text="Собрать дистрибутив", style="Accent.TButton", command=self._start_build
        )
        self.start_btn.pack(anchor="w", pady=(theme.SPACE_MD, theme.SPACE_SM))

        self.stage_var = tk.StringVar(value="")
        self.stage_label = ttk.Label(body, textvariable=self.stage_var, wraplength=theme.TEXT_WRAP)
        self.stage_label.pack(anchor="w")
        self.progress = ttk.Progressbar(body, maximum=10)
        self.progress.pack(fill="x", pady=(theme.SPACE_XS, theme.SPACE_SM))

        self.log_text = tk.Text(body, height=10, width=90, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    # -- шаг: пароль (и владелец, когда шага «Кому и что» нет) -------------
    def _build_step_passphrase(self, body: ttk.Frame) -> None:
        from ...crypto import generate_passphrase

        # По умолчанию фраза сгенерирована (Э11): оценка стойкости верна только
        # для случайной. Показывается открыто -- администратор передаёт её
        # пользователю, а скрытое поле заставило бы её переписывать вслепую.
        if not self.passphrase:
            self.passphrase = generate_passphrase()
            self.generated_passphrase = self.passphrase
        form = ttk.Frame(body)
        form.pack(anchor="w")
        self.passphrase_var = tk.StringVar(value=self.passphrase)
        add_field(form, 0, "Пароль пользователя (LUKS):", self.passphrase_var, width=30)
        self.passphrase_confirm_var = tk.StringVar(value=self.passphrase)
        add_field(form, 2, "Повторите пароль:", self.passphrase_confirm_var, width=30)

        def regenerate() -> None:
            value = generate_passphrase()
            self.generated_passphrase = value
            self.passphrase_var.set(value)
            self.passphrase_confirm_var.set(value)

        ttk.Button(form, text="Сгенерировать", command=regenerate).grid(
            row=0, column=2, padx=(theme.SPACE_SM, 0), sticky="w")

        # Имя и владелец -- здесь только тогда, когда отдельного шага «Кому и
        # что» в этой ветви нет. Заводится запись о носителе в обеих, и носитель
        # без владельца нечем связать с человеком: опознавать звонящего, который
        # просит код восстановления, будет не по чему.
        if not self._asks_handover():
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

    def _asks_handover(self) -> bool:
        return any(key == "handover" for key, _title, _hint in self._plan())

    def _save_step_passphrase(self) -> None:
        self.passphrase = self.passphrase_var.get()
        if not self._asks_handover():
            self.media_name = self.media_name_var.get().strip()
            self.media_owner = self.media_owner_var.get().strip()

    def _validate_step_passphrase(self) -> bool:
        if not self.passphrase_var.get():
            self.message_bar.error("Задайте парольную фразу.")
            return False
        # Длина проверяется здесь, потому что больше нигде. Лимит попыток LUKS
        # от перебора не спасает -- он действует только при загрузке носителя, а
        # тот, у кого носитель в руках, снимает образ заголовка и перебирает
        # офлайн, где счётчика нет. Там работают ровно два множителя: стоимость
        # попытки (argon2id уже выбран) и длина фразы.
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
        if self.passphrase_var.get() != getattr(self, "generated_passphrase", None):
            # Не запрет -- решение Максима 01.10.2026: своя фраза от минимума
            # допустима. Но стойкость придуманной человеком фразы не оценить.
            self.message_bar.info(
                "Фраза задана вручную: её стойкость не оценить. Сгенерированная "
                "из 12 знаков даёт около 70 бит, придуманная той же длины — 20–35."
            )
        if not self._asks_handover() and not self.media_owner_var.get().strip():
            self.message_bar.error(
                "Укажите владельца — ФИО сотрудника, которому выдаётся носитель. "
                "По этому имени вы будете опознавать звонящего, когда с носителя "
                "запросят код восстановления."
            )
            return False
        return True

    def _passphrase_minimum(self) -> int:
        """Минимум из конфигурации носителя, а не из кода этого экрана.

        В ветви образа это выбранная конфигурация, в ветви дистрибутива -- та, с
        которой он собран. Если прочитать не удалось, берётся умолчание схемы --
        не ноль: «не смогли прочитать» не повод не проверять вовсе.
        """
        from ...config.schema import LuksConfig

        config = self.selected_config if self.source_kind == SOURCE_IMAGE else self.dist_config
        luks = getattr(config, "luks", None)
        return int(getattr(luks, "min_passphrase_length", 0) or LuksConfig().min_passphrase_length)

    # -- шаг: кому и что ---------------------------------------------------
    def _build_step_handover(self, body: ttk.Frame) -> None:
        """Два вопроса, которые иначе задаются постфактум.

        **Имя.** После записи носитель попадает в список, где строки различаются
        идентификаторами вроде `46aed677`. Они отвечают на вопрос «тот ли это
        носитель», но не на вопрос «чей он», а второй как раз и задают, когда
        носитель надо отозвать или заменить.

        **Файлы.** Так на носитель едут настройки программ, которые
        администратор доустановил в образ. Раздела обмена больше нет (разметка
        v3): файлы ложатся в слой носителя на зашифрованном томе и в системе
        видны в /mnt/exchange только для чтения.
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
        # Владелец спрашивается отдельно от имени и обязателен: имя носителя
        # администратор придумывает себе для списка, а владелец -- это тот, кого
        # придётся опознать по телефону, когда с носителя попросят код
        # восстановления.
        self.media_owner_var = tk.StringVar(value=self.media_owner)
        add_field(form, 2, "Владелец (ФИО):", self.media_owner_var, width=36)

        files = ttk.LabelFrame(body, text=" Файлы на носитель ", padding=theme.SPACE_SM)
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
        chosen = filedialog.askopenfilenames(parent=self, title="Файлы на носитель")
        for path in chosen or ():
            if path not in self.exchange_list.get(0, "end"):
                self.exchange_list.insert("end", path)

    def _add_exchange_directory(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Каталог на носитель")
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
        config = self.dist_config
        # Файлы ложатся на зашифрованный том; часть его занята слоем носителя
        # и состоянием -- им оставляется четверть.
        capacity_mb = int(getattr(getattr(config, "disk", None), "state_partition_mb", 0) or 0)
        if capacity_mb and total > capacity_mb * 1024 * 1024 * 0.75:
            self.message_bar.error(
                f"Выбрано {total / 1024 / 1024:.0f} МБ, а зашифрованный том этого дистрибутива — "
                f"{capacity_mb} МБ. Уберите лишнее или соберите дистрибутив с большим томом."
            )
            return False
        return True

    # -- шаг: подтверждение и запись ---------------------------------------
    def _build_step_confirm(self, body: ttk.Frame) -> None:
        """Последнее, что администратор видит до стирания носителя.

        Режима «показать, что было бы» здесь намеренно нет: инструмент
        существует, чтобы выпускать носители, а второй режим, печатающий список
        команд, -- строительные леса разработчика. Здесь нужно другое: что на
        устройстве сейчас и что от него останется.
        """
        if self.source_kind == SOURCE_DISTRIBUTION:
            self._describe_distribution(body)
        else:
            self._describe_image_target(body)

        device = getattr(self, "_selected_device", None)
        if device and device.path != self.device_path:
            device = None
        self._confirmed_device = device

        warning = ttk.LabelFrame(body, text=" Носитель будет полностью очищен ")
        warning.pack(fill="x", pady=(10, 6))
        ttk.Label(
            warning,
            text=(
                f"{device.path}  ·  {device.model}  ·  {device.size_bytes / 1024**3:.1f} ГБ"
                if device else f"{self.device_path}  ·  диск не определён"
            ),
            font=theme.FONT_SECTION,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(8, 2))

        if device and (device.wwn or device.serial):
            ttk.Label(warning, text=f"Идентификатор диска: {device.wwn or device.serial}").pack(anchor="w", padx=10)
        if device and device.partitions:
            ttk.Label(
                warning,
                text="Сейчас на носителе, всё это будет уничтожено безвозвратно:",
                foreground=DANGER_COLOR,
                justify="left",
            ).pack(anchor="w", padx=10)
            for partition in device.partitions:
                ttk.Label(warning, text="    • " + partition.describe(), justify="left").pack(
                    anchor="w", padx=10
                )
        elif device:
            ttk.Label(
                warning,
                text="Разделов на носителе не обнаружено — он будет размечен заново.",
                justify="left",
            ).pack(anchor="w", padx=10)

        if device and device.active_mountpoints:
            ttk.Label(
                warning,
                text=(
                    "Носитель сейчас смонтирован ("
                    + ", ".join(device.active_mountpoints)
                    + "). Отмонтируйте его и выберите заново."
                ),
                foreground=DANGER_COLOR,
                wraplength=theme.TEXT_WRAP,
                justify="left",
            ).pack(anchor="w", padx=10, pady=(4, 0))

        ttk.Label(
            warning,
            text=(
                "Восстановить данные после записи невозможно. Убедитесь, что указан "
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
            hint="это последняя защита от записи не на тот диск",
            width=30,
        )
        # Фокус -- в поле, а не на кнопке, стирающей носитель: пробелом и Enter
        # мастер пролистывают.
        confirm_entry.focus_set()

        self.start_btn = ttk.Button(
            body,
            text="Стереть носитель и записать систему",
            style="Accent.TButton",
            command=self._start_write,
            state="disabled",
        )
        self.start_btn.pack(anchor="w", pady=(8, 4))
        # Кнопка недоступна, пока путь не повторён верно. Условие доступности
        # надёжнее проверки постфактум: пока набрано не то, нажимать нечего, и
        # это видно глазом. Сверка внутри `_start_write` остаётся -- она
        # отвечает за подмену устройства между набором и нажатием.
        self.confirm_device_var.trace_add("write", self._sync_start_button)
        self._sync_start_button()

        self.stage_var = tk.StringVar(value="")
        self.stage_label = ttk.Label(
            body, textvariable=self.stage_var, wraplength=theme.TEXT_WRAP, justify="left"
        )
        self.progress = ttk.Progressbar(body, mode="determinate", length=420, maximum=100)

        self.log_text = tk.Text(body, height=10, width=95, state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

        if not is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)

    def _describe_image_target(self, body: ttk.Frame) -> None:
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

    def _describe_distribution(self, body: ttk.Frame) -> None:
        meta = self.metadata
        box = ttk.LabelFrame(body, text=" Что будет записано ")
        box.pack(fill="x")
        mode = "киоск" if meta.profile == "kiosk" else "консольная система"
        if meta.desktop_environment:
            mode += f" / {meta.desktop_environment}"
        rows = [
            ("Дистрибутив:", meta.name or "(без имени)"),
            ("Система:", f"{meta.target_os} {meta.release} · {meta.arch}"),
            ("Режим:", mode),
            ("Размер системы:", f"{meta.rootfs_bytes / 1024**3:.1f} ГБ"),
            ("Собран:", meta.built_at or "неизвестно"),
            # Целиком, а не первыми символами: её сверяют глазами с той, что
            # записана рядом с образом. Пополам -- потому что в шестидесяти
            # четырёх символах без пробелов переносить нечего.
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
                text=f"Собран под эмуляцией: проверьте загрузку на машине {meta.arch}.",
                foreground=DANGER_COLOR,
                wraplength=theme.TEXT_WRAP,
                justify="left",
            ).pack(anchor="w", pady=(theme.SPACE_SM, 0))

    def _sync_start_button(self, *_args) -> None:
        """Кнопка запуска доступна ровно тогда, когда повтор пути совпал."""
        button = getattr(self, "start_btn", None)
        if button is None or not button.winfo_exists():
            return
        matches = self.confirm_device_var.get().strip() == (self.device_path or "")
        device = self._confirmed_device
        button.configure(state=("normal" if matches and device and not device.is_mounted else "disabled"))

    # -- работа ------------------------------------------------------------
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

    def _start_build(self) -> None:
        if self._running:
            return
        self._running = True
        self.start_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self._stages = UiMailbox(self, self._show_stage)
        self._stages.start()
        self.message_bar.info("Идёт сборка.")
        self._log(f"--- СБОРКА ДИСТРИБУТИВА {self.selected_config_name} ---")

        release = MediaReleaseService(self.app.session)
        self._release = release
        config = self.selected_config

        def work():
            return release.build_distribution(
                config,
                self.destination,
                image_path=self.image_path,
                extra_packages=list(self.extra_packages),
                dry_run=False,
                on_step=self._on_stage,
            )

        run_in_background(self, work, on_success=self._build_done, on_error=self._build_failed)

    def _start_write(self) -> None:
        if self._running:
            return
        confirm_device = self.confirm_device_var.get().strip()
        if confirm_device != self.device_path:
            self.message_bar.error(
                "Подтверждение устройства не совпадает с путём выше — запись отменена."
            )
            return
        # Проверяется здесь, а не оставляется на отказ внутри parted: иначе
        # администратор проходит мастер целиком, запускает необратимую работу и
        # получает отказ в правах от программы, которой не вызывал. Ничего ещё
        # не записано, и отказать бесплатно.
        if not is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)
            return

        # Сборка для другой архитектуры поддержана, но только там, где ядро
        # умеет исполнять двоичные файлы цели. Отказать здесь ничего не стоит;
        # отказ после wipefs стоит содержимого носителя.
        emulation = getattr(self, "_emulation", None)
        if emulation is not None and not emulation.native and not emulation.available:
            self.message_bar.error(emulation.reason)
            return

        # Идущая установка -- или обломки прерванной -- означают, что носитель
        # смонтирован, а его том открыт. Вторая работа поверх портит обе.
        existing = leftovers.find()
        if existing:
            self.message_bar.error(
                "На машине осталась незавершённая установка. Уберите её в «Обслуживании» "
                "и повторите:\n" + existing.describe()
            )
            return

        accepted = None
        if self.source_kind == SOURCE_DISTRIBUTION:
            # Подпись -- ещё раз, перед самой записью. Между выбором
            # дистрибутива и этой кнопкой проходят минуты, и подмена за это
            # время не должна пройти под согласием, данным на другой вердикт.
            accepted = self.signature_verdict
            current = distribution.verify_distribution(
                self.directory, self.app.container.admin_secret
            )
            if accepted is None or current != accepted:
                self.message_bar.error(
                    "Дистрибутив изменился после выбора — запись отменена. "
                    "Вернитесь к первому шагу и выберите его заново."
                )
                return

        device = self._confirmed_device
        if device is None or device.path != confirm_device or device.is_mounted:
            self.message_bar.error("Выберите свободный носитель и подтвердите его повторно.")
            return

        self._running = True
        self._install_signal_guard()
        self.start_btn.configure(state="disabled")
        # Навигация блокируется намеренно: уход с экрана уничтожит виджеты, и
        # запись о носителе, сделанная установщиком, не попадёт в контейнер --
        # она сохраняется в обработчике завершения. А вот прервать работу должно
        # быть чем: «Отмена» становится «Прервать».
        self.back_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.cancel_btn.configure(text="Прервать запись", command=self._abort)
        self._stages = UiMailbox(self, self._show_stage)
        self._stages.start()
        self.stage_var.set("Подготовка…")
        self.stage_label.pack(anchor="w", pady=(4, 2), before=self.log_text)
        self.progress.configure(value=0)
        self.progress.pack(fill="x", pady=(0, 8), before=self.log_text)
        self.message_bar.info("Идёт запись на носитель.")
        self._log(f"--- ЗАПИСЬ НА {device.path} ---")

        passphrase_bytes = self.passphrase.encode("utf-8")
        release = MediaReleaseService(self.app.session)
        # Держим службу, а не установщик: у неё же спрашивают на пути отказа,
        # тронут ли носитель, и её же просят остановиться по кнопке.
        self._release = release

        if self.source_kind == SOURCE_DISTRIBUTION:
            directory = self.directory

            def work():
                return release.deploy_distribution(
                    device,
                    directory,
                    passphrase_bytes,
                    dry_run=False,
                    confirm_device=confirm_device,
                    on_step=self._on_stage,
                    media_name=self.media_name,
                    media_owner=self.media_owner,
                    exchange_files=self.exchange_files,
                    accepted_signature=accepted,
                )

        else:
            config = self.selected_config

            def work():
                return release.install_from_image(
                    device,
                    config,
                    passphrase_bytes,
                    image_path=self.image_path,
                    dry_run=False,
                    confirm_device=confirm_device,
                    on_step=self._on_stage,
                    media_name=self.media_name,
                    media_owner=self.media_owner,
                )

        run_in_background(self, work, on_success=self._write_done, on_error=self._write_failed)

    def _abort(self) -> None:
        """Останавливает запись по кнопке.

        Носитель после этого непригоден -- незавершённая установка не
        загрузится, -- но уборка отработает: тома закроются, разделы
        размонтируются, и следующая попытка не упрётся в занятые ресурсы.
        """
        release = getattr(self, "_release", None)
        if release is None or not self._running:
            return
        self.cancel_btn.configure(state="disabled")
        self.message_bar.info("Прерываю: жду, пока остановится текущая команда…")
        release.abort()

    def _stop_progress(self) -> None:
        self._running = False
        self._restore_signal_guard()
        if widget_alive(self.cancel_btn):
            self.cancel_btn.configure(text="Отмена", command=self.on_cancel, state="normal")
        if getattr(self, "_stages", None) is not None:
            self._stages.stop()
        if widget_alive(self.progress):
            self.progress.pack_forget()
        if widget_alive(self.stage_label):
            self.stage_label.pack_forget()
        for button in (self.start_btn, self.back_btn, self.next_btn, self.cancel_btn):
            if widget_alive(button):
                button.configure(state="normal")

    def _write_done(self, result) -> None:
        self._stop_progress()
        for entry in result.plan.results:
            line = " ".join(entry.argv)
            if entry.executed:
                line += f"  [rc={entry.returncode}]"
                if entry.stderr.strip():
                    line += f" stderr: {entry.stderr.strip()}"
            self._log(line)
        self.app.save_container()
        # Предупреждения -- перед словами об успехе, а не после: носитель,
        # собранный под эмуляцией, надо проверить на целевой архитектуре, и это
        # та часть, мимо которой нельзя проскользнуть взглядом.
        for warning in getattr(result, "warnings", ()):
            self._log("ВНИМАНИЕ: " + warning)
        # Кода восстановления здесь нет намеренно: он выводится из секрета
        # администратора в момент, когда пользователь позвонит, и до тех пор не
        # существует нигде -- ни на экране, ни в контейнере.
        self.message_bar.success(
            f"Носитель готов. media_id={result.media_record.media_id}. "
            "Код восстановления выдаётся по запросу пользователя, на экране "
            "«Восстановление»."
            + ("  " + " ".join(result.warnings) if getattr(result, "warnings", ()) else "")
        )
        # Носитель заводится на ресурсе отзыва сразу при выпуске: иначе первый
        # же отзыв упёрся бы в «носитель не ваш» -- владельцем записи сервер
        # считает того, кто её завёл.
        from ..widgets import publish_media_state

        publish_media_state(self, result.media_record.media_id)

    def _write_failed(self, exc: BaseException) -> None:
        self._stop_progress()
        # Тронут ли носитель -- факт, который знает установщик, а не то, о чём
        # можно догадываться. Часть отказов приходит до первой записи: образ не
        # того семейства, слишком маленькое устройство, нет эмулятора. Сказать
        # администратору, что флешка стёрта, когда она цела, стоит ему тех
        # данных, которые он после этого не станет восстанавливать.
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
                " Не удалось освободить: " + ", ".join(stuck) + ". Уберите в «Обслуживании»."
            )
        if isinstance(exc, InstallAborted):
            # Не ошибка, а решение человека: так и сказать.
            self.message_bar.info(f"Запись прервана.{suffix}")
        elif isinstance(exc, (DeviceNotEligibleError, InstallError)) or isinstance(
            exc, KNOWN_ERRORS
        ):
            self.message_bar.error(f"{exc}{suffix}")
        else:
            self.message_bar.error(f"Непредвиденная ошибка: {exc}{suffix}")

    def _build_done(self, result) -> None:
        self._running = False
        if getattr(self, "_stages", None) is not None:
            self._stages.stop()
        meta = result.metadata
        self._log(f"Готово. Архив: {meta.archive}")
        self._log(f"Контрольная сумма: {meta.sha256}")
        # Подпись ключом открытого контейнера -- здесь, а не в установщике,
        # потому что именно здесь известно, чей это дистрибутив. Без неё
        # развёртывание не отличит этот архив от подменённого: файл хеша
        # подменяется вместе с архивом.
        try:
            distribution.sign_distribution(result.directory, self.app.container.admin_secret)
        except (OSError, ValueError) as exc:
            self.message_bar.error(
                f"Дистрибутив собран, но не подписан: {exc}. При развёртывании "
                "его придётся подтверждать вручную."
            )
        else:
            # Секрет создаётся при первом обращении у контейнеров, где его ещё
            # не было; несохранённый, он дал бы подпись, которую этот же
            # контейнер после перезапуска не признает.
            self.app.save_container()
            self._log(f"Подпись: {meta.archive}.sig")
            self.message_bar.success(
                f"Дистрибутив собран и подписан: {meta.archive}, сумма записана "
                f"в {meta.archive}.sha256."
            )
        if widget_alive(self.start_btn):
            self.start_btn.configure(state="disabled")
        for button in (self.back_btn, self.cancel_btn, self.next_btn):
            if widget_alive(button):
                button.configure(state="normal")

    def _build_failed(self, exc: BaseException) -> None:
        self._running = False
        if getattr(self, "_stages", None) is not None:
            self._stages.stop()
        if isinstance(exc, KNOWN_ERRORS):
            self.message_bar.error(f"{exc} Носитель не затрагивался.")
        else:
            self.message_bar.error(f"Непредвиденная ошибка: {exc}")
        for button in (self.start_btn, self.back_btn, self.cancel_btn, self.next_btn):
            if widget_alive(button):
                button.configure(state="normal")

    # -- выходы ------------------------------------------------------------
    def on_finish(self) -> None:
        self.app.show_dashboard()

    def on_cancel(self) -> None:
        self.app.show_dashboard()
