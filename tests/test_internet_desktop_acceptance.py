"""Acceptance courier must not disclose a usable invite through public files."""
import base64
import copy
from pathlib import Path
import sys

import pytest

pytest.importorskip("cryptography")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import internet_desktop_acceptance as acceptance


def delivery(invite):
    return {"invite": invite, "package_url": "https://release-assets.githubusercontent.com/github-production-release-asset/test?ephemeral=token"}


@pytest.fixture
def courier(tmp_path):
    host = acceptance.new_key(tmp_path / "host.key")
    guest = acceptance.new_key(tmp_path / "guest.key")
    context = acceptance.courier_context(run_id="123", run_attempt="1", nonce="a" * 64,
        source_commit="b" * 40, package_sha256="c" * 64,
        host_public=acceptance.public(host), guest_public=acceptance.public(guest))
    return host, guest, context


def test_courier_round_trip_hides_the_private_capability(courier):
    host, guest, context = courier
    invite = delivery("poker3:private-test-capability")
    envelope = acceptance.seal_delivery(invite, host, context)
    assert invite["invite"].encode() not in acceptance.canonical(envelope)
    assert acceptance.open_delivery(envelope, guest, context) == invite


@pytest.mark.parametrize("field,value", [("run_id", "456"), ("nonce", "d" * 64),
                                        ("source_commit", "e" * 40), ("package_sha256", "f" * 64)])
def test_courier_rejects_replay_from_another_run_source_or_zip(courier, field, value):
    host, guest, context = courier
    envelope = acceptance.seal_delivery(delivery("poker3:private"), host, context)
    changed = dict(context, **{field: value})
    with pytest.raises(Exception):
        acceptance.open_delivery(envelope, guest, changed)
    # Replacing the public context as well cannot bypass AEAD authentication.
    envelope["context"] = changed
    with pytest.raises(Exception):
        acceptance.open_delivery(envelope, guest, changed)


def test_courier_rejects_ciphertext_tampering_and_unpinned_guest(courier, tmp_path):
    host, guest, context = courier
    envelope = acceptance.seal_delivery(delivery("poker3:private"), host, context)
    tampered = copy.deepcopy(envelope)
    raw = bytearray(base64.b64decode(tampered["ciphertext"]))
    raw[0] ^= 1
    tampered["ciphertext"] = base64.b64encode(raw).decode("ascii")
    with pytest.raises(Exception):
        acceptance.open_delivery(tampered, guest, context)
    other = acceptance.new_key(tmp_path / "other.key")
    with pytest.raises(ValueError):
        acceptance.open_delivery(envelope, other, context)


def test_courier_refuses_oversized_envelope_before_decrypt(courier):
    host, guest, context = courier
    envelope = acceptance.seal_delivery(delivery("poker3:private"), host, context)
    envelope["ciphertext"] = "A" * 24001
    with pytest.raises(ValueError):
        acceptance.open_delivery(envelope, guest, context)


@pytest.mark.parametrize("url", ["http://release-assets.githubusercontent.com/github-production-release-asset/x",
    "https://example.org/package.zip", "https://release-assets.githubusercontent.com/other/x",
    "https://user:secret@release-assets.githubusercontent.com/github-production-release-asset/x"])
def test_delivery_rejects_untrusted_or_credential_bearing_download_origins(url):
    with pytest.raises(ValueError):
        acceptance.validate_delivery({"invite": "poker3:private", "package_url": url})
