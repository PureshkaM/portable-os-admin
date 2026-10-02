"""Э18: корни организации на стороне программы администратора."""
from __future__ import annotations

import json

import pytest

from portable_os_admin.admin import org_root
from portable_os_admin.container import ContainerManager
from portable_os_admin.install.access.runtime import poa_gost, poa_trust


@pytest.fixture
def fast_kdf(monkeypatch):
    """Стоимость scrypt рабочего файла -- 1 ГиБ; для тестов логики -- меньше.
    Настоящая стоимость проверяется отдельным тестом."""
    monkeypatch.setattr(org_root, "RESERVE_KDF_N", 2 ** 14)


def _container(tmp_path, name="c"):
    return ContainerManager().create(tmp_path / f"{name}.container", "ContainerPass12345")


def test_reserve_root_file_opens_only_with_its_generated_phrase(tmp_path, fast_kdf):
    path = tmp_path / "reserve.poaroot"
    phrase, entry = org_root.create_reserve_root(path)
    assert len(phrase) == org_root.RESERVE_PHRASE_LENGTH and entry["role"] == "reserve"
    assert org_root.read_reserve_entry(path) == entry
    # Фраза переписана группами -- дефисы и пробелы не мешают.
    signer = org_root.open_reserve_root(path, org_root.format_phrase(phrase))
    assert signer.entry == entry
    with pytest.raises(org_root.OrgRootError, match="не подошла"):
        org_root.open_reserve_root(path, phrase[:-1] + ("a" if phrase[-1] != "a" else "b"))
    with pytest.raises(org_root.OrgRootError):
        org_root.create_reserve_root(path)
    # Закрытого ключа в файле в открытом виде нет.
    assert str(signer._d) not in path.read_text() and f"{signer._d:x}" not in path.read_text()


def test_reserve_file_uses_one_gibibyte_scrypt(tmp_path):
    path = tmp_path / "reserve.poaroot"
    phrase, _entry = org_root.create_reserve_root(path)
    assert json.loads(path.read_text())["kdf"]["n"] == 2 ** 20
    assert org_root.open_reserve_root(path, phrase)


def test_a_swapped_anchor_in_the_file_is_detected(tmp_path, fast_kdf):
    path = tmp_path / "reserve.poaroot"
    phrase, _entry = org_root.create_reserve_root(path)
    doc = json.loads(path.read_text())
    doc["entry"]["key"] = poa_trust.GostSigner(poa_gost.new_private_key(poa_gost.TC26_256_A)).entry["key"]
    path.write_text(json.dumps(doc))
    with pytest.raises(org_root.OrgRootError):
        org_root.open_reserve_root(path, phrase)


def test_admin_request_certificate_and_descriptor(tmp_path, fast_kdf):
    path = tmp_path / "reserve.poaroot"
    phrase, entry = org_root.create_reserve_root(path)
    root = org_root.open_reserve_root(path, phrase)
    container = _container(tmp_path)
    org_root.set_anchors(container, [entry])
    request = org_root.admin_request(container, "Петров")
    assert set(request) == {"kind", "key", "name"}  # секретов в запросе нет
    org_root.install_certificate(container, org_root.certify_request(root, request))
    first = org_root.descriptor(container, ["https://a.example"], ["ab" * 32], ["KEY="])
    second = org_root.descriptor(container, ["https://b.example"], ["cd" * 32], ["KEY="])
    anchors = org_root.anchors(container)
    assert poa_trust.newer_descriptor(anchors, second, first["descriptor"])["endpoints"] == ["https://b.example"]
    assert poa_trust.newer_descriptor(anchors, first, second["descriptor"]) is None


def test_a_certificate_for_another_containers_key_is_refused(tmp_path, fast_kdf):
    path = tmp_path / "reserve.poaroot"
    phrase, entry = org_root.create_reserve_root(path)
    root = org_root.open_reserve_root(path, phrase)
    mine, other = _container(tmp_path, "a"), _container(tmp_path, "b")
    org_root.set_anchors(mine, [entry])
    foreign = org_root.certify_request(root, org_root.admin_request(other, "Сидоров"))
    with pytest.raises(org_root.OrgRootError, match="другой рабочий ключ"):
        org_root.install_certificate(mine, foreign)


def test_uncertified_container_cannot_issue_a_descriptor(tmp_path):
    with pytest.raises(org_root.OrgRootError):
        org_root.descriptor(_container(tmp_path), ["https://a.example"], [], [])


def test_anchors_survive_saving_the_container(tmp_path, fast_kdf):
    path = tmp_path / "reserve.poaroot"
    _phrase, entry = org_root.create_reserve_root(path)
    container = _container(tmp_path)
    org_root.set_anchors(container, [entry])
    ContainerManager().save(container)
    reopened = ContainerManager().open(tmp_path / "c.container", "ContainerPass12345")
    assert org_root.anchors(reopened) == [entry]


def test_fingerprint_is_for_people():
    entry = poa_trust.GostSigner(poa_gost.new_private_key(poa_gost.TC26_256_A)).entry
    fp = org_root.fingerprint(entry)
    assert len(fp.split()) == 4 and fp.replace(" ", "") == poa_trust.key_id(entry)


def test_revocation_and_root_replacement_ride_with_descriptors(tmp_path, fast_kdf):
    """Отзыв рабочего ключа и замена основного корня -- с каждым описателем."""
    import base64

    from portable_os_admin.install.access.runtime import poa_trust as trust

    reserve_path = tmp_path / "reserve.poaroot"
    phrase, reserve_entry = org_root.create_reserve_root(reserve_path)
    reserve = org_root.open_reserve_root(reserve_path, phrase)
    main = poa_trust.GostSigner(poa_gost.new_private_key(poa_gost.TC26_256_A), "main")
    container = _container(tmp_path)
    org_root.set_anchors(container, [main.entry, reserve_entry])
    org_root.certify_container(main, container, "Иванов")

    stolen = base64.b64encode(b"\x01" * 32).decode()
    revoked = org_root.revoke_working_keys(reserve, container, [stolen])
    assert revoked["list"]["serial"] == 1
    again = org_root.revoke_working_keys(main, container, [])
    assert again["list"]["serial"] == 2 and stolen in again["list"]["keys"]

    new_main = poa_trust.GostSigner(poa_gost.new_private_key(poa_gost.TC26_256_A), "main")
    org_root.replace_main_root(reserve, container, new_main.entry)
    ids = {trust.key_id(a) for a in org_root.anchors(container)}
    assert trust.key_id(new_main.entry) in ids and trust.key_id(main.entry) not in ids

    org_root.install_certificate(container, org_root.certify_request(
        new_main, org_root.admin_request(container, "Иванов")))
    document = org_root.descriptor(container, ["https://a.example"], [], ["KEY="])
    assert document["revoked"]["list"]["serial"] == 2 and len(document["root_updates"]) == 1
