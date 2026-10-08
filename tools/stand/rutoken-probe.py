#!/usr/bin/env python3
"""Зонд Рутокена для LIVE-2.0: что умеет экземпляр и сходится ли его подпись с нашей.

Работает через PKCS#11-библиотеку Актива (`librtpkcs11ecp.so`) на ctypes, без
сторонних модулей. Итоги прогона 08.10.2026 — `docs/RUTOKEN.md`.

    rutoken-probe.py info  [--module …]            # без PIN, ничего не меняет
    rutoken-probe.py check [--module …] [--runtime …]

`check` входит PIN пользователя (`POA_TOKEN_PIN`; без него — только если токен
сам сообщает, что PIN заводской), создаёт на чипе временные ключи с меткой
`poa-probe`, подписывает, сверяет подпись с `poa_gost` и `cryptography` и
удаляет ключи. Неверный PIN тратит попытку — поэтому без явного PIN зонд
заводской не пробует, если токен говорит, что PIN уже сменён.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import os
import sys
import time

MODULE = "/root/rutoken/lib/librtpkcs11ecp.so"
RUNTIME = "/root/poa-live2/src/portable_os_admin/install/access/runtime"
LABEL = b"poa-probe"
DEFAULT_USER_PIN = b"12345678"   # заводской PIN пользователя Рутокена

ULONG = C.c_ulong
BBOOL = C.c_ubyte

# -- константы PKCS#11 (pkcs11t.h, pkcs11tc26_12.h, rtpkcs11t.h) --------------
CKF_RW_SESSION, CKF_SERIAL_SESSION = 0x2, 0x4
CKU_SO, CKU_USER = 0, 1
CKO_PUBLIC_KEY, CKO_PRIVATE_KEY, CKO_HW_FEATURE = 2, 3, 4
CKK_EC, CKK_GOSTR3410 = 0x3, 0x30
CKA_CLASS, CKA_TOKEN, CKA_PRIVATE, CKA_LABEL = 0x0, 0x1, 0x2, 0x3
CKA_VALUE, CKA_KEY_TYPE, CKA_ID, CKA_SENSITIVE = 0x11, 0x100, 0x102, 0x103
CKA_SIGN, CKA_VERIFY = 0x108, 0x10A
CKA_EXTRACTABLE, CKA_LOCAL, CKA_NEVER_EXTRACTABLE, CKA_ALWAYS_SENSITIVE = 0x162, 0x163, 0x164, 0x165
CKA_EC_PARAMS, CKA_EC_POINT = 0x180, 0x181
CKA_GOSTR3410_PARAMS, CKA_GOSTR3411_PARAMS = 0x250, 0x251
CKA_HW_FEATURE_TYPE = 0x300
CKH_VENDOR_TOKEN_INFO, CKH_VENDOR_PIN_POLICY = 0x80000001, 0x80000006
CKM_EC_KEY_PAIR_GEN, CKM_ECDSA = 0x1040, 0x1041
CKM_GOSTR3410_KEY_PAIR_GEN, CKM_GOSTR3410 = 0x1200, 0x1201
TC26 = 0xD4321000
CKM_GOSTR3410_512_KEY_PAIR_GEN = TC26 | 0x005
CKM_GOSTR3410_WITH_GOSTR3411_12_256 = TC26 | 0x008
CKM_GOSTR3411_12_256 = TC26 | 0x012
CKM_EC_EDWARDS_KEY_PAIR_GEN, CKM_EDDSA = 0x1055, 0x1057

# OID в DER
OID_TC26_256_A = bytes.fromhex("06092a8503070102010101")      # 1.2.643.7.1.2.1.1.1
OID_TC26_256_B = bytes.fromhex("06092a8503070102010102")      # 1.2.643.7.1.2.1.1.2 (= CryptoPro-A)
OID_CRYPTOPRO_A = bytes.fromhex("06072a850302022301")      # 1.2.643.2.2.35.1
OID_CRYPTOPRO_XCHA = bytes.fromhex("06072a850302022400")   # 1.2.643.2.2.36.0
OID_STREEBOG_256 = bytes.fromhex("06082a85030701010202")      # 1.2.643.7.1.1.2.2
OID_P256 = bytes.fromhex("06082a8648ce3d030107")

CKR = {0x0: "OK", 0x3: "GENERAL_ERROR", 0x5: "GENERAL_ERROR", 0x6: "FUNCTION_FAILED",
       0x7: "ARGUMENTS_BAD", 0x10: "ATTRIBUTE_READ_ONLY", 0x11: "ATTRIBUTE_SENSITIVE",
       0x12: "ATTRIBUTE_TYPE_INVALID", 0x13: "ATTRIBUTE_VALUE_INVALID", 0x54: "FUNCTION_NOT_SUPPORTED",
       0x70: "MECHANISM_INVALID", 0x71: "MECHANISM_PARAM_INVALID", 0xA0: "PIN_INCORRECT",
       0xA4: "PIN_LOCKED", 0xD0: "TEMPLATE_INCOMPLETE", 0xD1: "TEMPLATE_INCONSISTENT",
       0xE0: "TOKEN_NOT_PRESENT", 0x100: "USER_ALREADY_LOGGED_IN", 0x101: "USER_NOT_LOGGED_IN",
       0x150: "BUFFER_TOO_SMALL", 0x60: "KEY_HANDLE_INVALID", 0x63: "KEY_TYPE_INCONSISTENT"}


class P11Error(Exception):
    def __init__(self, func: str, rv: int) -> None:
        super().__init__(f"{func}: CKR_{CKR.get(rv, hex(rv))}")
        self.rv = rv


class Version(C.Structure):
    _fields_ = [("major", C.c_ubyte), ("minor", C.c_ubyte)]


class Info(C.Structure):
    _fields_ = [("cryptokiVersion", Version), ("manufacturerID", C.c_char * 32), ("flags", ULONG),
                ("libraryDescription", C.c_char * 32), ("libraryVersion", Version)]


class TokenInfo(C.Structure):
    _fields_ = [("label", C.c_char * 32), ("manufacturerID", C.c_char * 32), ("model", C.c_char * 16),
                ("serialNumber", C.c_char * 16), ("flags", ULONG), ("ulMaxSessionCount", ULONG),
                ("ulSessionCount", ULONG), ("ulMaxRwSessionCount", ULONG), ("ulRwSessionCount", ULONG),
                ("ulMaxPinLen", ULONG), ("ulMinPinLen", ULONG), ("ulTotalPublicMemory", ULONG),
                ("ulFreePublicMemory", ULONG), ("ulTotalPrivateMemory", ULONG),
                ("ulFreePrivateMemory", ULONG), ("hardwareVersion", Version),
                ("firmwareVersion", Version), ("utcTime", C.c_char * 16)]


class TokenInfoExtended(C.Structure):   # rtpkcs11t.h, CK_TOKEN_INFO_EXTENDED
    _fields_ = [("ulSizeofThisStructure", ULONG), ("ulTokenType", ULONG), ("ulProtocolNumber", ULONG),
                ("ulMicrocodeNumber", ULONG), ("ulOrderNumber", ULONG), ("flags", ULONG),
                ("ulMaxAdminPinLen", ULONG), ("ulMinAdminPinLen", ULONG), ("ulMaxUserPinLen", ULONG),
                ("ulMinUserPinLen", ULONG), ("ulMaxAdminRetryCount", ULONG),
                ("ulAdminRetryCountLeft", ULONG), ("ulMaxUserRetryCount", ULONG),
                ("ulUserRetryCountLeft", ULONG), ("serialNumber", C.c_ubyte * 8),
                ("ulTotalMemory", ULONG), ("ulFreeMemory", ULONG), ("ATR", C.c_ubyte * 64),
                ("ulATRLen", ULONG), ("ulTokenClass", ULONG), ("ulBatteryVoltage", ULONG),
                ("ulBodyColor", ULONG), ("ulFirmwareChecksum", ULONG),
                ("ulBatteryPercentage", ULONG), ("ulBatteryFlags", ULONG)]


class MechanismInfo(C.Structure):
    _fields_ = [("ulMinKeySize", ULONG), ("ulMaxKeySize", ULONG), ("flags", ULONG)]


class Attribute(C.Structure):
    _fields_ = [("type", ULONG), ("pValue", C.c_void_p), ("ulValueLen", ULONG)]


class Mechanism(C.Structure):
    _fields_ = [("mechanism", ULONG), ("pParameter", C.c_void_p), ("ulParameterLen", ULONG)]


TOKEN_FLAGS_EXT = {0x1: "ADMIN_CHANGE_USER_PIN", 0x2: "USER_CHANGE_USER_PIN",
                   0x4: "ADMIN_PIN_NOT_DEFAULT", 0x8: "USER_PIN_NOT_DEFAULT", 0x10: "SUPPORT_FKN",
                   0x40: "SUPPORT_SM", 0x80: "HAS_FLASH_DRIVE", 0x100: "SUPPORT_SECURE_MESSAGING",
                   0x200: "HAS_BUTTON", 0x400: "SUPPORT_JOURNAL", 0x800: "USER_PIN_UTF8",
                   0x1000: "ADMIN_PIN_UTF8", 0x40000000: "FW_CHECKSUM_UNAVAILABLE",
                   0x80000000: "FW_CHECKSUM_INVALID"}
TOKEN_CLASS = {0: "S", 1: "ECP", 2: "LITE", 3: "WEB", 4: "PINPAD", 9: "ECPDUAL"}
VENDOR_TOKEN_ATTRS = {
    0x80003000: "SECURE_MESSAGING_AVAILABLE", 0x80003001: "CURRENT_SECURE_MESSAGING_MODE",
    0x80003002: "SUPPORTED_SECURE_MESSAGING_MODES", 0x80003003: "CURRENT_TOKEN_INTERFACE",
    0x80003004: "SUPPORTED_TOKEN_INTERFACE", 0x80003005: "EXTERNAL_AUTHENTICATION",
    0x80003006: "BIOMETRIC_AUTHENTICATION", 0x80003007: "SUPPORT_CUSTOM_PIN",
    0x80003008: "CUSTOM_ADMIN_PIN", 0x80003009: "CUSTOM_USER_PIN",
    0x8000300A: "SUPPORT_INTERNAL_TRUSTED_CERTS", 0x8000300B: "SUPPORT_FKC2",
    0x8000300C: "SUPPORT_HW_RESULT_FOR_GOST28147_KEY_UNWRAP", 0x8000300D: "SUPPORT_HW_KDF_TREE",
    0x8000300E: "SUPPORT_KIMP15", 0x8000300F: "FLASH_DRIVE_AVAILABLE", 0x80003010: "MODEL_NAME",
    0x80003011: "SUPPORT_HW_AES_WRAP", 0x80003012: "SUPPORT_REPAIR_FORMAT",
    0xC0003013: "PROTECTION_FEATURES", 0xC0003015: "SUPPORTED_EMITENT_KEY_ALGS"}
VENDOR_PIN_ATTRS = {
    0x80003200: "PIN_POLICY_STATE", 0x80003201: "PIN_POLICIES_DELETABLE",
    0xC0003202: "SUPPORTED_PIN_POLICIES", 0x80003203: "MIN_LENGTH", 0x80003204: "HISTORY_DEPTH",
    0x80003205: "ALLOW_DEFAULT_PIN_USAGE", 0x80003206: "DIGIT_REQUIRED",
    0x80003207: "UPPERCASE_REQUIRED", 0x80003208: "LOWERCASE_REQUIRED",
    0x80003209: "SPEC_CHAR_REQUIRED", 0x8000320A: "DIFF_CHARS_REQUIRED", 0x8000320B: "USER_TYPE"}


class Token:
    def __init__(self, module: str) -> None:
        self.lib = C.CDLL(module)
        self._keep: list = []
        self.call("C_Initialize", None)
        slots = (ULONG * 16)()
        count = ULONG(16)
        self.call("C_GetSlotList", BBOOL(1), slots, C.byref(count))
        if not count.value:
            raise SystemExit("токен не найден")
        self.slot = slots[0]
        self.session = ULONG(0)

    def call(self, name: str, *args) -> None:
        rv = getattr(self.lib, name)(*args)
        if rv:
            raise P11Error(name, rv)

    def open(self, rw: bool = False) -> None:
        flags = CKF_SERIAL_SESSION | (CKF_RW_SESSION if rw else 0)
        self.call("C_OpenSession", self.slot, ULONG(flags), None, None, C.byref(self.session))

    def close(self) -> None:
        if self.session.value:
            self.lib.C_CloseSession(self.session)
            self.session = ULONG(0)
        self.lib.C_Finalize(None)

    # -- сведения ---------------------------------------------------------------
    def info(self) -> Info:
        i = Info()
        self.call("C_GetInfo", C.byref(i))
        return i

    def token_info(self) -> TokenInfo:
        t = TokenInfo()
        self.call("C_GetTokenInfo", self.slot, C.byref(t))
        return t

    def token_info_ext(self) -> TokenInfoExtended:
        t = TokenInfoExtended()
        t.ulSizeofThisStructure = C.sizeof(TokenInfoExtended)
        self.call("C_EX_GetTokenInfoExtended", self.slot, C.byref(t))
        return t

    def mechanisms(self) -> "list[int]":
        count = ULONG(0)
        self.call("C_GetMechanismList", self.slot, None, C.byref(count))
        arr = (ULONG * count.value)()
        self.call("C_GetMechanismList", self.slot, arr, C.byref(count))
        return list(arr)

    def mechanism_info(self, mech: int) -> MechanismInfo:
        m = MechanismInfo()
        self.call("C_GetMechanismInfo", self.slot, ULONG(mech), C.byref(m))
        return m

    # -- объекты ----------------------------------------------------------------
    def template(self, pairs) -> "tuple[Attribute * int, int]":
        arr = (Attribute * len(pairs))()
        for i, (kind, value) in enumerate(pairs):
            if isinstance(value, bool):
                buf = (BBOOL * 1)(int(value))
            elif isinstance(value, int):
                buf = (ULONG * 1)(value)
            else:
                buf = C.create_string_buffer(bytes(value), len(value))
            self._keep.append(buf)
            arr[i].type, arr[i].pValue, arr[i].ulValueLen = kind, C.cast(buf, C.c_void_p), C.sizeof(buf)
        return arr, len(pairs)

    def find(self, pairs) -> "list[int]":
        tpl, n = self.template(pairs)
        self.call("C_FindObjectsInit", self.session, tpl, ULONG(n))
        out, handles, got = [], (ULONG * 32)(), ULONG(0)
        try:
            while True:
                self.call("C_FindObjects", self.session, handles, ULONG(32), C.byref(got))
                if not got.value:
                    return out
                out.extend(handles[: got.value])
        finally:
            self.lib.C_FindObjectsFinal(self.session)

    def attr(self, handle: int, kind: int) -> bytes:
        a = Attribute(kind, None, 0)
        self.call("C_GetAttributeValue", self.session, ULONG(handle), C.byref(a), ULONG(1))
        if a.ulValueLen in (0, ULONG(-1).value):
            return b""
        buf = C.create_string_buffer(a.ulValueLen)
        a.pValue = C.cast(buf, C.c_void_p)
        self.call("C_GetAttributeValue", self.session, ULONG(handle), C.byref(a), ULONG(1))
        return buf.raw[: a.ulValueLen]

    def generate_pair(self, mech: int, public, private) -> "tuple[int, int]":
        m = Mechanism(mech, None, 0)
        pub_t, pub_n = self.template(public)
        prv_t, prv_n = self.template(private)
        pub, prv = ULONG(0), ULONG(0)
        self.call("C_GenerateKeyPair", self.session, C.byref(m), pub_t, ULONG(pub_n),
                  prv_t, ULONG(prv_n), C.byref(pub), C.byref(prv))
        return pub.value, prv.value

    def sign(self, key: int, mech: int, data: bytes) -> bytes:
        m = Mechanism(mech, None, 0)
        self.call("C_SignInit", self.session, C.byref(m), ULONG(key))
        size = ULONG(0)
        self.call("C_Sign", self.session, data, ULONG(len(data)), None, C.byref(size))
        out = C.create_string_buffer(size.value)
        self.call("C_Sign", self.session, data, ULONG(len(data)), out, C.byref(size))
        return out.raw[: size.value]

    def digest(self, mech: int, data: bytes) -> bytes:
        m = Mechanism(mech, None, 0)
        self.call("C_DigestInit", self.session, C.byref(m))
        size = ULONG(64)
        out = C.create_string_buffer(64)
        self.call("C_Digest", self.session, data, ULONG(len(data)), out, C.byref(size))
        return out.raw[: size.value]

    def random(self, n: int) -> bytes:
        out = C.create_string_buffer(n)
        self.call("C_GenerateRandom", self.session, out, ULONG(n))
        return out.raw

    def destroy(self, handle: int) -> None:
        self.call("C_DestroyObject", self.session, ULONG(handle))


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace").rstrip(" \0")


def _value(raw: bytes) -> str:
    if len(raw) == 1:
        return str(bool(raw[0]))
    if len(raw) == C.sizeof(ULONG):
        return hex(int.from_bytes(raw, sys.byteorder))
    if raw and all(32 <= b < 127 for b in raw):
        return repr(raw.decode())
    if raw and len(raw) % C.sizeof(ULONG) == 0:
        n = C.sizeof(ULONG)
        return "[" + ", ".join(hex(int.from_bytes(raw[i:i + n], sys.byteorder))
                               for i in range(0, len(raw), n)) + "]"
    return raw.hex() or "(пусто)"


def _hw_feature(tok: Token, feature: int, names: dict) -> None:
    handles = tok.find([(CKA_CLASS, CKO_HW_FEATURE), (CKA_HW_FEATURE_TYPE, feature)])
    if not handles:
        print("    (объекта нет)")
    for h in handles:
        for kind, name in names.items():
            try:
                print(f"    {name:44} {_value(tok.attr(h, kind))}")
            except P11Error as exc:
                print(f"    {name:44} — {exc}")


def cmd_info(tok: Token) -> None:
    i = tok.info()
    print(f"Библиотека: {_text(i.libraryDescription)} {i.libraryVersion.major}.{i.libraryVersion.minor}, "
          f"Cryptoki {i.cryptokiVersion.major}.{i.cryptokiVersion.minor}")
    t = tok.token_info()
    print(f"Токен: {_text(t.model)}, серийный {_text(t.serialNumber)}, метка {_text(t.label)!r}")
    print(f"  аппаратная версия {t.hardwareVersion.major}.{t.hardwareVersion.minor}, "
          f"прошивка {t.firmwareVersion.major}.{t.firmwareVersion.minor}, PIN {t.ulMinPinLen}–{t.ulMaxPinLen}, "
          f"flags {t.flags:#x}")
    e = tok.token_info_ext()
    flags = [n for bit, n in TOKEN_FLAGS_EXT.items() if e.flags & bit]
    print(f"Расширенно: класс {TOKEN_CLASS.get(e.ulTokenClass, e.ulTokenClass)}, тип {e.ulTokenType:#x}, "
          f"протокол {e.ulProtocolNumber}, микрокод {e.ulMicrocodeNumber}, заказ {e.ulOrderNumber}")
    print(f"  флаги {e.flags:#x}: {', '.join(flags) or '—'}")
    print(f"  PIN администратора {e.ulMinAdminPinLen}–{e.ulMaxAdminPinLen}, попыток {e.ulAdminRetryCountLeft}"
          f"/{e.ulMaxAdminRetryCount}; PIN пользователя {e.ulMinUserPinLen}–{e.ulMaxUserPinLen}, "
          f"попыток {e.ulUserRetryCountLeft}/{e.ulMaxUserRetryCount}")
    print(f"  память {e.ulFreeMemory} из {e.ulTotalMemory} байт свободно; серийный {bytes(e.serialNumber).hex()}; "
          f"контрольная сумма прошивки {e.ulFirmwareChecksum:#x}")
    print(f"  ATR {bytes(e.ATR[: e.ulATRLen]).hex(' ')}")
    tok.open()
    print("Аппаратные сведения (CKH_VENDOR_TOKEN_INFO):")
    _hw_feature(tok, CKH_VENDOR_TOKEN_INFO, VENDOR_TOKEN_ATTRS)
    print("Политика PIN (CKH_VENDOR_PIN_POLICY):")
    _hw_feature(tok, CKH_VENDOR_PIN_POLICY, VENDOR_PIN_ATTRS)
    print("Публичные объекты:", len(tok.find([])))
    print("Механизмы, важные для проекта:")
    mechs = set(tok.mechanisms())
    for name, mech in [("ГОСТ 2012-256 генерация", CKM_GOSTR3410_KEY_PAIR_GEN),
                       ("ГОСТ 2012-256 подпись хеша", CKM_GOSTR3410),
                       ("ГОСТ 2012-256 подпись со Стрибогом", CKM_GOSTR3410_WITH_GOSTR3411_12_256),
                       ("ГОСТ 2012-512 генерация", CKM_GOSTR3410_512_KEY_PAIR_GEN),
                       ("Стрибог-256", CKM_GOSTR3411_12_256),
                       ("ECDSA генерация", CKM_EC_KEY_PAIR_GEN), ("ECDSA", CKM_ECDSA),
                       ("EdDSA генерация", CKM_EC_EDWARDS_KEY_PAIR_GEN), ("EdDSA", CKM_EDDSA)]:
        if mech in mechs:
            m = tok.mechanism_info(mech)
            print(f"    {name:38} есть, ключ {m.ulMinKeySize}–{m.ulMaxKeySize}, flags {m.flags:#x}")
        else:
            print(f"    {name:38} НЕТ")


# -- проверка подписи --------------------------------------------------------------
def _gost_pair(tok: Token, curve_oid: bytes, tag: bytes) -> "tuple[int, int]":
    common = [(CKA_KEY_TYPE, CKK_GOSTR3410), (CKA_TOKEN, True), (CKA_LABEL, LABEL), (CKA_ID, tag),
              (CKA_GOSTR3410_PARAMS, curve_oid), (CKA_GOSTR3411_PARAMS, OID_STREEBOG_256)]
    return tok.generate_pair(
        CKM_GOSTR3410_KEY_PAIR_GEN,
        [(CKA_CLASS, CKO_PUBLIC_KEY), (CKA_PRIVATE, False), (CKA_VERIFY, True)] + common,
        [(CKA_CLASS, CKO_PRIVATE_KEY), (CKA_PRIVATE, True), (CKA_SENSITIVE, True),
         (CKA_EXTRACTABLE, False), (CKA_SIGN, True)] + common)


def _gost_variants(sig: bytes) -> dict:
    """Возможные прочтения подписи токена в нашем формате s||r, big-endian."""
    a, b = sig[:32], sig[32:]
    return {"как есть": sig, "половины местами": b + a,
            "каждая половина развёрнута": a[::-1] + b[::-1],
            "целиком развёрнута": sig[::-1]}


def check_gost(tok: Token, gost) -> "list[int]":
    created: list = []
    curve = gost.TC26_256_A
    print("ГОСТ Р 34.10-2012, 256 бит:")
    for name, oid in [("tc26 paramSetA (наш корень)", OID_TC26_256_A), ("tc26 paramSetB", OID_TC26_256_B),
                      ("CryptoPro-A", OID_CRYPTOPRO_A), ("CryptoPro-XchA", OID_CRYPTOPRO_XCHA)]:
        try:
            t0 = time.monotonic()
            pub, prv = _gost_pair(tok, oid, b"probe-" + oid[-3:])
            created += [pub, prv]
            print(f"    генерация на чипе, {name}: да ({time.monotonic() - t0:.2f} с)")
        except P11Error as exc:
            print(f"    генерация на чипе, {name}: нет — {exc}")
    keys = tok.find([(CKA_CLASS, CKO_PRIVATE_KEY), (CKA_LABEL, LABEL), (CKA_ID, b"probe-" + OID_TC26_256_A[-3:])])
    pubs = tok.find([(CKA_CLASS, CKO_PUBLIC_KEY), (CKA_LABEL, LABEL), (CKA_ID, b"probe-" + OID_TC26_256_A[-3:])])
    if not keys or not pubs:
        print("    paramSetA не создан — сверять нечего")
        return created
    prv, pub = keys[0], pubs[0]
    raw = tok.attr(pub, CKA_VALUE)
    print(f"    CKA_VALUE открытого ключа: {len(raw)} байт, начало {raw[:4].hex()}")
    if len(raw) == 66 and raw[:2] == b"\x04\x40":
        raw = raw[2:]
        print("    (обёрнут в DER OCTET STRING — снят)")
    point = None
    for label, cand in [("x||y little-endian", (int.from_bytes(raw[:32], "little"), int.from_bytes(raw[32:], "little"))),
                        ("x||y big-endian", (int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")))]:
        if gost.on_curve(curve, cand):
            point = cand
            print(f"    открытый ключ — точка кривой при прочтении {label}")
            break
    if point is None:
        print("    открытый ключ не лёг на кривую ни при каком прочтении")
        return created
    for kind, name in [(CKA_SENSITIVE, "SENSITIVE"), (CKA_EXTRACTABLE, "EXTRACTABLE"), (CKA_LOCAL, "LOCAL"),
                       (CKA_NEVER_EXTRACTABLE, "NEVER_EXTRACTABLE"), (CKA_ALWAYS_SENSITIVE, "ALWAYS_SENSITIVE")]:
        print(f"    закрытый ключ {name}: {_value(tok.attr(prv, kind))}")
    try:
        tok.attr(prv, CKA_VALUE)
        print("    !!! закрытый ключ ПРОЧИТАН")
    except P11Error as exc:
        print(f"    чтение закрытого ключа: отказ ({exc})")

    data = b"poa-probe " + os.urandom(16).hex().encode()
    ours = gost.streebog(data, 256)
    theirs = tok.digest(CKM_GOSTR3411_12_256, data)
    print(f"    Стрибог-256 на чипе = poa_gost.streebog: {theirs == ours}"
          + ("" if theirs == ours else f" (развёрнутый: {theirs[::-1] == ours})"))

    t0 = time.monotonic()
    sig = tok.sign(prv, CKM_GOSTR3410_WITH_GOSTR3411_12_256, data)
    dt = time.monotonic() - t0
    hits = [n for n, v in _gost_variants(sig).items() if gost.verify(curve, point, data, v)]
    print(f"    подпись со Стрибогом на чипе ({len(sig)} байт, {dt:.2f} с): poa_gost.verify — {hits or 'НЕ СХОДИТСЯ'}")
    for order, digest in [("хеш как есть", ours), ("хеш развёрнут", ours[::-1])]:
        sig = tok.sign(prv, CKM_GOSTR3410, digest)
        hits = [n for n, v in _gost_variants(sig).items() if gost.verify(curve, point, data, v)]
        print(f"    подпись готового хеша ({order}): poa_gost.verify — {hits or 'не сходится'}")
    return created


def check_ecdsa(tok: Token) -> "list[int]":
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    print("ECDSA P-256:")
    # CKA_EC_PARAMS -- только в шаблоне открытого ключа: в закрытом токен
    # отвечает CKR_TEMPLATE_INCONSISTENT (08.10.2026)
    common = [(CKA_KEY_TYPE, CKK_EC), (CKA_TOKEN, True), (CKA_LABEL, LABEL), (CKA_ID, b"probe-p256")]
    try:
        t0 = time.monotonic()
        pub, prv = tok.generate_pair(
            CKM_EC_KEY_PAIR_GEN,
            [(CKA_CLASS, CKO_PUBLIC_KEY), (CKA_PRIVATE, False), (CKA_VERIFY, True),
             (CKA_EC_PARAMS, OID_P256)] + common,
            [(CKA_CLASS, CKO_PRIVATE_KEY), (CKA_PRIVATE, True), (CKA_SENSITIVE, True),
             (CKA_EXTRACTABLE, False), (CKA_SIGN, True)] + common)
        print(f"    генерация на чипе: да ({time.monotonic() - t0:.2f} с)")
    except P11Error as exc:
        print(f"    генерация на чипе: нет — {exc}")
        return []
    point = tok.attr(pub, CKA_EC_POINT)
    if point[:1] == b"\x04" and len(point) == 67:      # DER OCTET STRING вокруг 04||X||Y
        point = point[2:]
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    data = b"poa-probe " + os.urandom(16).hex().encode()
    t0 = time.monotonic()
    sig = tok.sign(prv, CKM_ECDSA, hashlib.sha256(data).digest())
    dt = time.monotonic() - t0
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    try:
        public.verify(der, data, ec.ECDSA(hashes.SHA256()))
        print(f"    подпись r||s ({len(sig)} байт, {dt:.2f} с): cryptography.verify — сходится")
    except InvalidSignature:
        print("    подпись: cryptography.verify — НЕ СХОДИТСЯ")
    return [pub, prv]


def cmd_check(tok: Token, runtime: str, only: "str | None") -> None:
    sys.path.insert(0, runtime)
    import poa_gost
    e = tok.token_info_ext()
    pin = os.environ.get("POA_TOKEN_PIN", "").encode()
    if not pin:
        if e.flags & 0x8:
            raise SystemExit("PIN пользователя не заводской — задайте POA_TOKEN_PIN")
        pin = DEFAULT_USER_PIN
    print(f"Попыток PIN пользователя до входа: {e.ulUserRetryCountLeft}/{e.ulMaxUserRetryCount}; "
          f"свободно {e.ulFreeMemory} байт")
    tok.open(rw=True)
    tok.call("C_Login", tok.session, ULONG(CKU_USER), C.c_char_p(pin), ULONG(len(pin)))
    leftovers = tok.find([(CKA_LABEL, LABEL)])
    for h in leftovers:
        tok.destroy(h)
    if leftovers:
        print(f"Удалены остатки прошлого прогона: {len(leftovers)}")
    print(f"Генератор случайных чисел: {len(tok.random(32))} байт получено")
    created: list = []
    try:
        if only != "ecdsa":
            created += check_gost(tok, poa_gost)
        if only != "gost":
            created += check_ecdsa(tok)
    finally:
        for h in tok.find([(CKA_LABEL, LABEL)]):
            tok.destroy(h)
        rest = tok.find([(CKA_LABEL, LABEL)])
        tok.lib.C_Logout(tok.session)
        print(f"Временные ключи удалены, осталось с меткой poa-probe: {len(rest)}; "
              f"свободно {tok.token_info_ext().ulFreeMemory} байт")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("command", choices=["info", "check"])
    p.add_argument("--module", default=MODULE)
    p.add_argument("--runtime", default=RUNTIME, help="каталог с poa_gost.py")
    p.add_argument("--only", choices=["gost", "ecdsa"], help="check: только одна из проверок")
    args = p.parse_args()
    tok = Token(args.module)
    try:
        cmd_info(tok) if args.command == "info" else cmd_check(tok, args.runtime, args.only)
    finally:
        tok.close()


if __name__ == "__main__":
    main()
