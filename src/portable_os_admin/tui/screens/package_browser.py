"""Extension of UC-3 "Управление файлом конфигурации установки": connect to
the target OS's real repository and pick packages to preinstall, instead of
typing exact package names from memory straight into `config.packages`."""
from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from ...config.repo_browser import RepoPackage, get_package_source
from ...errors import RepoBrowseError

CATALOG_DISPLAY_LIMIT = 200


class PackageBrowserScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Назад")]

    def __init__(self, config_path: Path):
        super().__init__()
        self.config_path = config_path
        self._catalog: list[RepoPackage] = []
        self._catalog_view: list[RepoPackage] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("", id="title"),
            Horizontal(
                Input(placeholder="Адрес репозитория (mirror)", id="mirror"),
                Input(placeholder="Релиз/suite", id="release"),
            ),
            Button("Сохранить и подключиться к репозиторию", id="connect", variant="primary"),
            Input(placeholder="Фильтр по имени/описанию", id="filter"),
            Static("Найдено в репозитории:", id="catalog_label"),
            DataTable(id="catalog_table"),
            Button("Добавить выбранный пакет", id="add"),
            Static("Уже выбрано для установки:", id="selected_label"),
            DataTable(id="selected_table"),
            Button("Убрать выбранный пакет", id="remove"),
            Static("", id="message"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self.config = self.app.config_manager.open(self.config_path)
        self.query_one("#title", Static).update(
            f"Пакеты для «{self.config.name}» ({self.config.target_os})"
        )
        self.query_one("#mirror", Input).value = self.config.mirror
        self.query_one("#release", Input).value = self.config.release
        self.query_one("#catalog_table", DataTable).add_columns("пакет", "версия", "описание")
        self.query_one("#selected_table", DataTable).add_columns("пакет")
        self._refresh_selected()

    def _refresh_selected(self) -> None:
        table = self.query_one("#selected_table", DataTable)
        table.clear()
        for name in self.config.packages:
            table.add_row(name)

    def _refresh_catalog(self, items: list[RepoPackage]) -> None:
        table = self.query_one("#catalog_table", DataTable)
        table.clear()
        self._catalog_view = items[:CATALOG_DISPLAY_LIMIT]
        for pkg in self._catalog_view:
            table.add_row(pkg.name, pkg.version, pkg.summary)
        label = self.query_one("#catalog_label", Static)
        if len(items) > CATALOG_DISPLAY_LIMIT:
            label.update(
                f"Найдено в репозитории: {len(items)} — показаны первые {CATALOG_DISPLAY_LIMIT}, "
                "уточните фильтр, чтобы найти нужный пакет:"
            )
        else:
            label.update(f"Найдено в репозитории: {len(items)}:")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        message = self.query_one("#message", Static)

        if event.button.id == "connect":
            self.config.mirror = self.query_one("#mirror", Input).value.strip()
            self.config.release = self.query_one("#release", Input).value.strip()
            self.app.config_manager.save(self.config, self.config_path)
            self.query_one("#connect", Button).disabled = True
            message.update(
                "[yellow]Подключение к репозиторию… для больших репозиториев (десятки тысяч "
                "пакетов) это может занять до минуты, интерфейс останется отзывчивым[/yellow]"
            )
            self._load_catalog(self.config.target_os, self.config.mirror, self.config.release)
            return

        elif event.button.id == "add":
            table = self.query_one("#catalog_table", DataTable)
            if not self._catalog_view or table.cursor_row is None:
                message.update("[red]Сначала подключитесь к репозиторию и выберите пакет[/red]")
                return
            pkg = self._catalog_view[table.cursor_row]
            if pkg.name not in self.config.packages:
                self.config.packages.append(pkg.name)
                self.app.config_manager.save(self.config, self.config_path)
                self._refresh_selected()
            message.update(f"[green]Добавлен пакет «{pkg.name}»[/green]")

        elif event.button.id == "remove":
            table = self.query_one("#selected_table", DataTable)
            if not self.config.packages or table.cursor_row is None:
                message.update("[red]Выберите пакет для удаления[/red]")
                return
            name = self.config.packages.pop(table.cursor_row)
            self.app.config_manager.save(self.config, self.config_path)
            self._refresh_selected()
            message.update(f"[green]Убран пакет «{name}»[/green]")

    @work(thread=True, exclusive=True, group="repo-fetch")
    def _load_catalog(self, target_os: str, mirror: str, release: str) -> None:
        # Runs off the UI thread: fetching + parsing a real repo's package
        # index (tens of thousands of entries, tens of MB) synchronously on
        # Textual's event loop thread was the actual cause of the TUI
        # appearing to freeze/render garbage during live testing -- it
        # starved the loop that redraws the screen for as long as the
        # network+parse took.
        try:
            source = get_package_source(target_os)
            catalog = source.list_packages(mirror, release)
        except RepoBrowseError as exc:
            self.app.call_from_thread(self._on_catalog_error, exc)
            return
        except Exception as exc:  # network/parse failure not modeled as RepoBrowseError
            self.app.call_from_thread(self._on_catalog_error, exc)
            return
        self.app.call_from_thread(self._on_catalog_loaded, catalog)

    def _on_catalog_loaded(self, catalog: list[RepoPackage]) -> None:
        self._catalog = catalog
        self._refresh_catalog(catalog)
        self.query_one("#connect", Button).disabled = False
        self.query_one("#message", Static).update(f"[green]Загружено пакетов из репозитория: {len(catalog)}[/green]")

    def _on_catalog_error(self, exc: Exception) -> None:
        self.query_one("#connect", Button).disabled = False
        self.query_one("#message", Static).update(f"[red]{exc}[/red]")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "filter" or not self._catalog:
            return
        query = event.value.strip().lower()
        if not query:
            self._refresh_catalog(self._catalog)
            return
        filtered = [p for p in self._catalog if query in p.name.lower() or query in p.summary.lower()]
        self._refresh_catalog(filtered)
