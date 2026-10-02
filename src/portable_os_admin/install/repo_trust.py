"""Доверие TLS к репозиториям дистрибутива при сборке (LIVE-2.0, Э22).

dnf сборки работает на машине администратора и проверяет TLS репозиториев
набором УЦ этой машины. Зеркала РЕД ОС подписаны УЦ Минцифры («Russian Trusted
Root CA»), которого в Debian нет, а сервер к тому же отдаёт неполную цепочку:
сертификат выпущен промежуточным УЦ 2024 года, а приложен промежуточный 2022-го
(живая сборка 02.10.2026). Браузеры добирают звено по ссылке AIA из
сертификата, curl и dnf -- нет.

Здесь делается то же, что браузер, но с якорем в самом дистрибутиве:

* корни -- из пакета `ca-certificates-ru` **на ISO** (подлинность ISO -- это
  уже принятое доверие к дистрибутиву);
* недостающий промежуточный УЦ скачивается по AIA и принимается, **только если
  его подпись проверяется одним из этих корней** -- подменить его по дороге
  нельзя;
* итоговый набор: УЦ машины + корни дистрибутива + принятые промежуточные.

Отключать проверку TLS нельзя даже при подписанных пакетах: без неё зеркало
можно подменить и отдавать старые, подлинно подписанные пакеты с известными
уязвимостями (откат).
"""
from __future__ import annotations

import os
import socket
import ssl
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

#: Пакет корней УЦ дистрибутива на ISO.
ANCHOR_PACKAGES = ("ca-certificates-ru-*.rpm",)
HOST_BUNDLES = ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")
TIMEOUT = 20


def load_certificates(data: bytes) -> list[x509.Certificate]:
    """PEM (сколько угодно) или один DER."""
    if b"-----BEGIN CERTIFICATE-----" in data:
        return x509.load_pem_x509_certificates(data)
    return [x509.load_der_x509_certificate(data)]


def distribution_anchors(image_mountpoint: str) -> list[x509.Certificate]:
    """Корни УЦ из пакета дистрибутива на ISO. Нет пакета -- пусто."""
    from .rpm_extract import PackageNotOnImageError, find_package, unpack

    try:
        package = find_package(image_mountpoint, ANCHOR_PACKAGES)
    except PackageNotOnImageError:
        return []
    with tempfile.TemporaryDirectory(prefix="poa-ca-") as tmp:
        unpack(package, tmp)
        found = []
        for path in sorted(Path(tmp).rglob("*")):
            if path.is_file() and path.suffix in (".crt", ".pem", ".cer"):
                try:
                    found += load_certificates(path.read_bytes())
                except ValueError:
                    continue
        return [c for c in found if _is_ca(c)]


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value.ca
    except x509.ExtensionNotFound:
        return False


def issued_by_anchor(cert: x509.Certificate, anchors: list[x509.Certificate]) -> bool:
    """Подписан ли `cert` одним из корней -- проверкой подписи, не по имени."""
    for anchor in anchors:
        try:
            cert.verify_directly_issued_by(anchor)
            return _is_ca(cert)
        except (ValueError, TypeError, Exception):  # noqa: BLE001 - любая несостыковка = нет
            continue
    return False


def aia_issuer_urls(cert: x509.Certificate) -> list[str]:
    try:
        aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return []
    return [d.access_location.value for d in aia
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
            and isinstance(d.access_location, x509.UniformResourceIdentifier)]


def _final_host(url: str) -> str:
    """Хост, на который в итоге уводит адрес (зеркала РЕД ОС перенаправляют)."""
    context = ssl._create_unverified_context()  # только узнать адрес; доверие -- ниже
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            return urllib.parse.urlsplit(response.geturl()).hostname or ""
    except Exception:  # noqa: BLE001 - не дошли -- значит, проверять нечего
        return urllib.parse.urlsplit(url).hostname or ""


def _peer_leaf(host: str) -> x509.Certificate | None:
    context = ssl._create_unverified_context()
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
    except OSError:
        return None


def missing_intermediates(urls: list[str], anchors: list[x509.Certificate],
                          fetch=None) -> list[x509.Certificate]:
    """Промежуточные УЦ, которых не хватает серверам, -- проверенные корнями."""
    fetch = fetch or (lambda u: urllib.request.urlopen(u, timeout=TIMEOUT).read())
    accepted: list[x509.Certificate] = []
    hosts = sorted({_final_host(u) for u in urls if u.startswith("https://")} - {""})
    for host in hosts:
        leaf = _peer_leaf(host)
        if leaf is None:
            continue
        for issuer_url in aia_issuer_urls(leaf):
            if not issuer_url.startswith(("http://", "https://")):
                continue
            try:
                candidates = load_certificates(fetch(issuer_url))
            except Exception:  # noqa: BLE001 - недоступен -- пробуем следующий
                continue
            for cert in candidates:
                if issued_by_anchor(cert, anchors) and cert not in accepted:
                    accepted.append(cert)
    return accepted


def build_bundle(urls: list[str], image_mountpoint: str) -> str:
    """Файл набора УЦ для dnf сборки или пусто, если дистрибутив корней не несёт."""
    anchors = distribution_anchors(image_mountpoint) if image_mountpoint else []
    if not anchors:
        return ""
    intermediates = missing_intermediates(urls, anchors)
    host = next((p for p in HOST_BUNDLES if os.path.exists(p)), "")
    text = Path(host).read_text(encoding="utf-8", errors="replace") if host else ""
    pems = [c.public_bytes(serialization.Encoding.PEM).decode("ascii") for c in anchors + intermediates]
    fd, path = tempfile.mkstemp(prefix="poa-repo-ca-", suffix=".pem")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text.rstrip("\n") + "\n" + "".join(pems))
    return path
