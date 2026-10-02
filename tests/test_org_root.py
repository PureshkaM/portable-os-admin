"""Э18: корневой ключ организации на стороне программы администратора."""
from __future__ import annotations

import json

import pytest

from portable_os_admin.admin import org_root
from portable_os_admin.container import ContainerManager
from portable_os_admin.install.access.runtime import poa_trust

PASSWORD = "Root-Key-Passphrase-2026"


def test_root_file_is_encrypted_and_opens_with_its_password(tmp_path):
    path = tmp_path / "org.poaroot"
    public = org_root.create_root_file(path, PASSWORD)
    assert public not in path.read_text() or "key" in json.loads(path.read_text())
    key = org_root.open_root_file(path, PASSWORD)
    assert poa_trust.b64(poa_trust.raw_public(key)) == public
    with pytest.raises(org_root.OrgRootError):
        org_root.open_root_file(path, "wrong-password-123456")
    with pytest.raises(org_root.OrgRootError):
        org_root.create_root_file(path, PASSWORD)


def test_certified_container_issues_descriptors_the_medium_accepts(tmp_path):
    root = org_root.open_root_file(_root(tmp_path), PASSWORD)
    container = ContainerManager().create(tmp_path / "c.container", "ContainerPass12345")
    org_root.certify_container(root, container)
    first = org_root.descriptor(container, ["https://a.example"], ["ab" * 32], ["KEY="])
    second = org_root.descriptor(container, ["https://b.example"], ["cd" * 32], ["KEY="])
    root_pub = container.data.org_root_public_b64
    assert poa_trust.verify_document(root_pub, first, "descriptor")["endpoints"] == ["https://a.example"]
    assert poa_trust.newer_descriptor(root_pub, second, first["descriptor"])["endpoints"] == ["https://b.example"]
    assert poa_trust.newer_descriptor(root_pub, first, second["descriptor"]) is None


def test_uncertified_container_cannot_issue_a_descriptor(tmp_path):
    container = ContainerManager().create(tmp_path / "c.container", "ContainerPass12345")
    with pytest.raises(org_root.OrgRootError):
        org_root.descriptor(container, ["https://a.example"], [], [])


def _root(tmp_path):
    path = tmp_path / "org.poaroot"
    org_root.create_root_file(path, PASSWORD)
    return path
