"""Headless coverage for the two TUI additions built on top of the original
flow tested in test_tui.py: the real-execution switch on InstallScreen, and
the repository package browser reachable from ConfigsScreen."""
import asyncio

from portable_os_admin.config.repo_browser import RepoPackage
from portable_os_admin.tui.app import PortableOSAdminApp
from portable_os_admin.tui.screens import package_browser as pb_module
from portable_os_admin.tui.screens.install import InstallScreen
from portable_os_admin.tui.screens.package_browser import PackageBrowserScreen


async def _open_container_and_create_debian_config(pilot, app, container_path):
    app.screen.query_one("#path").value = str(container_path)
    app.screen.query_one("#password").value = "CorrectHorse123"
    await pilot.click("#create")
    await pilot.pause()

    await pilot.click("#configs")
    await pilot.pause()
    app.screen.query_one("#new_name").value = "stick1"
    await pilot.click("#new_debian")
    await pilot.pause()
    await pilot.click("#configs_table")
    await pilot.pause()


def test_install_real_run_switch_blocks_on_mismatched_confirmation(tmp_path):
    async def scenario():
        container_path = tmp_path / "admin.container"
        app = PortableOSAdminApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await _open_container_and_create_debian_config(pilot, app, container_path)
            await pilot.click("#validate")
            await pilot.pause()
            await pilot.click("#back")
            await pilot.pause()

            await pilot.click("#install")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, InstallScreen)
            screen.query_one("#device_path").value = "/dev/sdb"
            screen.query_one("#device_size_gb").value = "32"
            screen.query_one("#passphrase").value = "user-passphrase-123"
            await pilot.click("#configs_table")
            await pilot.pause()

            await pilot.click("#real_run_switch")
            await pilot.pause()
            assert "РЕАЛЬНУЮ" in str(screen.query_one("#run").label)

            screen.query_one("#confirm_device").value = "/dev/sdX-wrong"
            await pilot.click("#run")
            await pilot.pause()
            message = str(screen.query_one("#message").content)
            assert "не совпадает" in message
            # nothing should have been recorded -- the mismatch must refuse
            # before OSInstaller.run() is ever called
            assert len(app.session.container.data.media_records) == 0

            app.close_session()
            await pilot.pause()

    asyncio.run(scenario())


def test_package_browser_add_and_remove_package(tmp_path, monkeypatch):
    class FakeSource:
        def list_packages(self, mirror, release):
            assert mirror == "http://example.test/debian"
            assert release == "bookworm"
            return [RepoPackage(name="htop", version="3.2.0", summary="interactive process viewer")]

    monkeypatch.setattr(pb_module, "get_package_source", lambda target_os: FakeSource())

    async def scenario():
        container_path = tmp_path / "admin.container"
        app = PortableOSAdminApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await _open_container_and_create_debian_config(pilot, app, container_path)

            await pilot.click("#packages")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PackageBrowserScreen)
            screen.query_one("#mirror").value = "http://example.test/debian"
            screen.query_one("#release").value = "bookworm"
            await pilot.click("#connect")
            await pilot.pause()
            assert "Загружено пакетов" in str(screen.query_one("#message").content)

            await pilot.click("#catalog_table")
            await pilot.pause()
            await pilot.click("#add")
            await pilot.pause()
            assert "htop" in screen.config.packages

            await pilot.click("#selected_table")
            await pilot.pause()
            await pilot.click("#remove")
            await pilot.pause()
            assert "htop" not in screen.config.packages

            app.pop_screen()
            app.close_session()
            await pilot.pause()

    asyncio.run(scenario())
