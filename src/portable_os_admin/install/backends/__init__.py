from .base import InstallBackend
from .debian import DebianInstallBackend
from .redos import RedOSInstallBackend

BACKENDS: dict[str, type[InstallBackend]] = {
    "debian": DebianInstallBackend,
    "redos": RedOSInstallBackend,
}


def get_backend(target_os: str) -> InstallBackend:
    try:
        return BACKENDS[target_os]()
    except KeyError as exc:
        raise ValueError(f"неизвестный backend установки: {target_os!r}") from exc


__all__ = ["InstallBackend", "DebianInstallBackend", "RedOSInstallBackend", "BACKENDS", "get_backend"]
