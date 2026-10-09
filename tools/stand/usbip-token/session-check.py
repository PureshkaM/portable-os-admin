#!/usr/bin/python3 -I
"""Проверка изнутри сеанса (только для стенда): что этому пользователю доступно через pcscd.

Пишет в ~/poa-token-check.txt: сеанс logind, код открытия контекста, считыватели и код
SCardConnect к каждому (0 — пустили, 0x8010006a — отказ polkit). С --probe запускает ещё
зонд ГОСТ (rutoken-probe.py check --only gost) по первому доступному считывателю.
"""
import ctypes
import os
import subprocess
import sys

out = []
sid = os.environ.get("XDG_SESSION_ID", "")
info = subprocess.run(["loginctl", "show-session", sid, "-p", "Remote", "-p", "Active"],
                      capture_output=True, text=True).stdout.split()
out.append(f"user={os.environ.get('USER')} session={sid} {' '.join(info)}")
lib = ctypes.CDLL("libpcsclite.so.1")
ctx = ctypes.c_long()
rv = lib.SCardEstablishContext(2, None, None, ctypes.byref(ctx)) & 0xFFFFFFFF
out.append(f"контекст: {rv:#x}")
ok = False
if rv == 0:
    size = ctypes.c_ulong(0)
    rv = lib.SCardListReaders(ctx, None, None, ctypes.byref(size)) & 0xFFFFFFFF
    names = []
    if rv == 0:
        buf = ctypes.create_string_buffer(size.value)
        lib.SCardListReaders(ctx, None, buf, ctypes.byref(size))
        names = [r for r in buf.raw[:size.value].split(b"\0") if r]
    out.append(f"считыватели ({rv:#x}): {[n.decode() for n in names]}")
    for name in names:
        card, proto = ctypes.c_long(), ctypes.c_ulong()
        rv = lib.SCardConnect(ctx, name, 2, 3, ctypes.byref(card), ctypes.byref(proto)) & 0xFFFFFFFF
        out.append(f"  SCardConnect «{name.decode()}»: {rv:#x}")
        if rv == 0:
            ok = True
            lib.SCardDisconnect(card, 0)
    lib.SCardReleaseContext(ctx)
if "--probe" in sys.argv and ok:
    d = os.path.dirname(os.path.abspath(__file__))
    res = subprocess.run([sys.executable, "-I", f"{d}/rutoken-probe.py", "check", "--only", "gost",
                          "--module", f"{d}/librtpkcs11ecp.so", "--runtime", f"{d}/runtime"],
                         capture_output=True, text=True, timeout=300)
    out.append("зонд ГОСТ:\n" + "\n".join((res.stdout + res.stderr).strip().splitlines()[-8:]))
out.append("== конец")
with open(os.path.expanduser("~/poa-token-check.txt"), "w") as fh:
    fh.write("\n".join(out) + "\n")
