"""Мелкие настройки самого интерфейса, переживающие запуск программы.

Не конфигурация системы и не содержимое контейнера -- то, как администратору
удобнее работать: в упрощённом режиме мастера или в экспертном. Такие вещи
нельзя держать в контейнере (он про носители, а не про привычки) и нельзя
терять между запусками: режим, который сбрасывается на умолчание, приходится
переключать каждый раз, и он перестаёт быть выбором.

Файл маленький и необязательный: если его нет, не читается или испорчен, берутся
умолчания. Настройка интерфейса -- не то, из-за чего программа должна
отказываться открыться.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: Куда складывать. XDG-переменная, если задана, иначе ~/.config -- программа
#: запускается через polkit от root, так что это /root/.config.
def _path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "portable-os-admin" / "interface.json"


def load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(values: dict) -> None:
    """Записывает настройки, молча смиряясь с невозможностью.

    Единственный случай, когда молчание уместно: администратору незачем видеть
    ошибку записи файла с одной строкой про режим мастера, если сама работа при
    этом идёт.
    """
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(values, handle, ensure_ascii=False, indent=2)
    except OSError:
        return


def get(key: str, default=None):
    return load().get(key, default)


def set_value(key: str, value) -> None:
    values = load()
    values[key] = value
    save(values)


__all__ = ["load", "save", "get", "set_value"]
