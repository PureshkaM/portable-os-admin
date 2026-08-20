"""Headless end-to-end test of the Textual UI, driven via Textual's Pilot.

Exercises the full window flow from the spec: open/create container ->
dashboard -> create+validate a config -> install -> issue a recovery code for
the medium that install just produced.

The installer is stubbed: this test is about the screens reaching each other
and passing the right things along, and there is no disk here to install onto.
"""
import asyncio

import pytest

from portable_os_admin.recovery import RecoveryRequest
from portable_os_admin.tui.app import PortableOSAdminApp
from portable_os_admin.tui.screens import install as install_screen_module


class _StubInstaller:
    """Delegates to the real orchestration in planning mode, so the screen's
    own logic still runs end to end without touching hardware."""

    def __init__(self, session):
        from portable_os_admin.install import OSInstaller

        self._real = OSInstaller(session)

    def run(self, device, config, passphrase, *, dry_run, confirm_device):
        return self._real.run(device, config, passphrase, dry_run=True, confirm_device=confirm_device)


@pytest.fixture
def stub_installer(monkeypatch):
    monkeypatch.setattr(install_screen_module, "OSInstaller", _StubInstaller)
    monkeypatch.setattr(install_screen_module.os, "geteuid", lambda: 0)


def test_tui_full_flow(tmp_path, stub_installer):
    async def scenario():
        container_path = tmp_path / "admin.container"
        app = PortableOSAdminApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            app.screen.query_one("#path").value = str(container_path)
            app.screen.query_one("#password").value = "CorrectHorse123"
            await pilot.click("#create")
            await pilot.pause()
            assert app.session is not None
            assert container_path.exists()

            await pilot.click("#configs")
            await pilot.pause()
            app.screen.query_one("#new_name").value = "stick1"
            await pilot.click("#new_debian")
            await pilot.pause()
            await pilot.click("#configs_table")
            await pilot.pause()
            await pilot.click("#validate")
            await pilot.pause()
            assert "корректна" in str(app.screen.query_one("#message").content)
            await pilot.click("#back")
            await pilot.pause()

            await pilot.click("#install")
            await pilot.pause()
            app.screen.query_one("#device_path").value = "/dev/sdb"
            app.screen.query_one("#device_size_gb").value = "32"
            app.screen.query_one("#passphrase").value = "user-passphrase-123"
            app.screen.query_one("#confirm_device").value = "/dev/sdb"
            await pilot.click("#configs_table")
            await pilot.pause()
            await pilot.click("#run")
            await pilot.pause()

            media_records = app.session.container.data.media_records
            recovery_records = app.session.container.data.recovery_records
            assert len(media_records) == 1
            assert len(recovery_records) == 1
            await pilot.press("escape")
            await pilot.pause()

            await pilot.click("#recovery")
            await pilot.pause()
            request = RecoveryRequest(media_id=media_records[0].media_id, slot=recovery_records[0].slot).encode()
            app.screen.query_one("#request").value = request
            await pilot.click("#issue")
            await pilot.pause()
            message = str(app.screen.query_one("#message").content)
            assert recovery_records[0].code in message

            app.close_session()
            await pilot.pause()
            assert app.session is None

    asyncio.run(scenario())
