"""Astra Linux backend.

Astra Linux Common Edition is a Debian 9 (stretch) derivative and Special
Edition builds on top of it (suites `orel` < `voronezh` < `smolensk`, in
increasing FSTEC certification level), so this reuses DebianInstallBackend's
debootstrap flow rather than duplicating it. Two real deviations, confirmed
against wiki.astralinux.ru during backend research (not the generic RHEL/
Debian assumptions used elsewhere in this project):

- Astra ships its own patched `debootstrap` package (suffixed e.g.
  `1.0.114astra7`) carrying suite scripts for `orel`/`voronezh`/`smolensk`.
  Stock upstream debootstrap is not guaranteed to recognize these suite
  names -- the *admin host* (not the target root) needs Astra's debootstrap
  package installed, which is an environment precondition this project does
  not (and cannot) install for the user.
- The sudo-equivalent group is `astra-admin`, not `sudo` -- Astra also gates
  privileged execution on a PARSEC integrity level (Special Edition only),
  which this backend does not attempt to configure; see
  README, "Архитектура" "Known limitations" for what that means for
  automated setup on Special Edition targets.
"""
from __future__ import annotations

from .debian import DebianInstallBackend


class AstraInstallBackend(DebianInstallBackend):
    name = "astra"
    sudo_group = "astra-admin"

    # The kiosk profile is deliberately *not* offered for Astra, and this is
    # the line that withholds it: an empty display-manager service is what the
    # validator refuses a kiosk configuration on.
    #
    # Inheriting it from the Debian backend would have been the silent failure
    # mode. The parent's package list was checked against a Debian 13 (trixie)
    # image, while Astra Common Edition derives from Debian 9 (stretch): names
    # like `xserver-xorg-video-all` and `lightdm-gtk-greeter` resolve
    # differently or not at all there, and the install would die partway
    # through with a medium already half-written. Better to say the mode is
    # unavailable than to offer one that has never been near an Astra image.
    display_manager_service = ""

    def desktop_packages(self, config):
        return []

    def display_manager_files(self, config):
        return {}
