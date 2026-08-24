"""Runs the target distribution's own kickstart parser, out of process.

This file is never imported by the application. It is executed as a separate
interpreter with PYTHONPATH pointing at a pykickstart unpacked from the
installation image, and prints one line of JSON on stdout. Isolation is the
point: the parser is third-party code of an unknown version, it needs
compatibility shims to import at all on newer Pythons, and it exits the
process on some inputs. None of that may reach the GUI.

Usage: python3 kickstart_runner.py <file.ks> [<syntax version>]
"""
import json
import sys


def _install_compatibility_shims():
    """Makes a pykickstart built for an older Python importable on this one.

    Both shims were established by running the real thing, and both are
    deliberately narrow -- nothing here changes what the parser accepts or
    rejects, because a validator that has been "adjusted" is no longer an
    independent opinion.
    """
    applied = []

    # Python 3.13 removed `pipes`. pykickstart imports it in commands/realm.py
    # for pipes.quote, which has always been an alias of shlex.quote.
    if "pipes" not in sys.modules:
        try:
            import pipes  # noqa: F401
        except ImportError:
            import shlex
            import types

            shim = types.ModuleType("pipes")
            shim.quote = shlex.quote
            sys.modules["pipes"] = shim
            applied.append("pipes")
    return applied


def _restore_argparse_parse_optional():
    """Puts argparse's own _parse_optional back in place of pykickstart's.

    pykickstart overrides that method for exactly one purpose: warning about
    deprecated options. The parsing itself is done by the base implementation
    it calls. Meanwhile Python 3.13 changed the method to return a *list* of
    option tuples, which makes the override fail with AttributeError on every
    command that takes options -- that is, on nearly every line of a real
    configuration.

    Restoring the base implementation therefore costs a warning and changes no
    verdict. It is done unconditionally rather than after probing which Python
    this is: the probe would have to build a parser, and building one is
    exactly what needs the version machinery that is not set up yet.
    """
    import argparse

    from pykickstart.options import KSOptionParser

    KSOptionParser._parse_optional = argparse.ArgumentParser._parse_optional
    return True


def _pick_version(requested):
    """Chooses a syntax version this pykickstart actually implements.

    `requested` comes from the image (its release), not from a constant here.
    When that version is unknown to this parser -- an older image, a newer
    one, a rebuild that dropped a version -- the newest version it does
    implement is used rather than failing, because parsing with a slightly
    different dialect still catches unknown commands, misspelt options and
    missing required arguments.
    """
    from pykickstart.version import returnClassForVersion, versionMap

    candidates = []
    if requested:
        candidates.append(requested)
    # highest-numbered known versions first
    candidates.extend(name for name, _ in sorted(versionMap.items(), key=lambda kv: -kv[1]))
    for name in candidates:
        try:
            returnClassForVersion(name)
        except Exception:  # noqa: BLE001 - "this parser lacks that version"
            continue
        return name
    return ""


def main(argv):
    if len(argv) < 2:
        print(json.dumps({"ok": False, "available": False, "reason": "не передан файл конфигурации"}))
        return 2

    path = argv[1]
    requested = argv[2] if len(argv) > 2 else ""

    _install_compatibility_shims()
    try:
        from pykickstart.parser import KickstartParser
        from pykickstart.version import makeVersion
    except Exception as exc:  # noqa: BLE001 - unusable parser, not a bad config
        print(json.dumps({"ok": False, "available": False, "reason": f"не удалось загрузить pykickstart: {exc}"}))
        return 3

    _restore_argparse_parse_optional()

    version = _pick_version(requested)
    if not version:
        print(json.dumps({"ok": False, "available": False, "reason": "pykickstart не знает ни одной версии синтаксиса"}))
        return 3

    try:
        parser = KickstartParser(makeVersion(version))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "available": False, "reason": f"не удалось построить разбор: {exc}"}))
        return 3

    try:
        parser.readKickstart(path)
    except Exception as exc:  # noqa: BLE001 - this is the verdict, not a crash
        message = " ".join(str(exc).split())
        print(json.dumps({"ok": False, "available": True, "version": version, "errors": [message]}))
        return 1

    print(json.dumps({"ok": True, "available": True, "version": version, "errors": []}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
