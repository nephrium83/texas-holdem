"""Device-secret STORAGE: persistence, corruption refusal, strict storage
(finding 210) and concurrent first launch.

Split out of test_device_secret.py because none of this needs libsodium,
so it also runs on the Windows CI job (which has no libsodium and would
skip a crypto-gated module) -- the Windows publish path is exactly what
these tests need to exercise there. Session wiring, which does need the
crypto backend, stays in test_device_secret.py.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holdem.p2p import device_secret, identity_storage


# ------------------------------------------------------------ the file

def test_created_on_first_use(tmp_path):
    target = tmp_path / "device_secret"
    assert not target.exists()
    secret = device_secret.load_or_create(target)
    assert target.exists()
    assert len(secret) == device_secret.SECRET_BYTES


def test_stable_across_calls(tmp_path):
    """The whole point: a second process must read the first one's secret."""
    target = tmp_path / "device_secret"
    assert device_secret.load_or_create(target) == \
        device_secret.load_or_create(target)


def test_distinct_devices_get_distinct_secrets(tmp_path):
    a = device_secret.load_or_create(tmp_path / "a" / "device_secret")
    b = device_secret.load_or_create(tmp_path / "b" / "device_secret")
    assert a != b


def test_parent_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "deeper" / "device_secret"
    assert len(device_secret.load_or_create(target)) == \
        device_secret.SECRET_BYTES


def test_honours_the_config_dir_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEM_CONFIG_DIR", str(tmp_path / "cfg"))
    assert device_secret.secret_path() == \
        tmp_path / "cfg" / device_secret.FILENAME


# ------------------------------------------------------- corrupt file

def _plant(target, content):
    """Store ``content`` through the SAME strict storage the module uses, so
    a test about a wrong-length secret is not instead tripped up by (and
    silently passing on) the storage-permission checks."""
    with identity_storage.creating(target) as publisher:
        publisher.publish(content)


@pytest.mark.parametrize("content", [b"", b"short", b"x" * 31, b"x" * 33])
def test_wrong_length_raises_rather_than_regenerating(tmp_path, content):
    """Silent regeneration is the exact bug this module fixes -- an identity
    changing without anyone noticing -- so it must not be the recovery."""
    target = tmp_path / "device_secret"
    _plant(target, content)
    with pytest.raises(ValueError, match="expected 32"):
        device_secret.load_or_create(target)
    assert target.read_bytes() == content, "corrupt file must be left alone"


def test_error_message_tells_the_operator_what_to_do(tmp_path):
    target = tmp_path / "device_secret"
    _plant(target, b"nope")
    with pytest.raises(ValueError, match="[Dd]elete it"):
        device_secret.load_or_create(target)


# -------------------------------------------- strict storage (finding 210)

def test_device_secret_no_longer_uses_private_storage():
    """private_storage verified nothing about an existing secret; the module
    must go through identity_storage instead."""
    import inspect
    source = inspect.getsource(device_secret)
    assert "private_storage import" not in source
    assert "identity_storage" in source


def test_unsafe_existing_secret_is_refused_and_left_alone(tmp_path):
    """A secret written with ordinary permissions (0644 under the usual
    umask on Linux; an inherited, non-private DACL on Windows) is exactly
    what the old code produced and accepted. It is refused -- not repaired,
    not replaced -- because another account could have written it."""
    target = tmp_path / "device_secret"
    planted = b"k" * device_secret.SECRET_BYTES
    target.write_bytes(planted)
    if os.name != "nt":
        os.chmod(target, 0o644)
    with pytest.raises(identity_storage.InsecureKeyStorage):
        device_secret.load_or_create(target)
    assert target.read_bytes() == planted, "unsafe secret must not be modified"


def test_insecure_storage_is_still_a_value_error(tmp_path):
    """Callers that already handle ValueError keep working."""
    assert issubclass(identity_storage.InsecureKeyStorage, ValueError)


def test_created_secret_is_private_and_strictly_readable(tmp_path):
    target = tmp_path / "device_secret"
    secret = device_secret.load_or_create(target)
    # readable back through the strict verifier = conforming permissions
    assert identity_storage.load_identity_bytes(target) == secret
    if os.name != "nt":
        assert (target.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink policy")
def test_symlinked_secret_is_refused(tmp_path):
    real = tmp_path / "elsewhere" / "device_secret"
    secret = device_secret.load_or_create(real)
    link = tmp_path / "device_secret"
    link.symlink_to(real)
    with pytest.raises(ValueError):
        device_secret.load_or_create(link)
    assert real.read_bytes() == secret


# ------------------------------------------------- concurrent first launch
#
# This is the two-process CI stall, root-caused. PR #25's classifier caught
# it on 3.13: the host dispatched start_hand and emitted no ack, because
# start_p2p_hand RAISED:
#
#   start_hand: device secret at /home/runner/.config/holdem/device_secret
#   is 0 bytes, expected 32. It will not be replaced automatically ...
#
# load_or_create's docstring claims "Concurrency-safe: the file is created
# with O_EXCL, so two processes racing on first launch cannot clobber one
# another -- the loser reads what the winner wrote."
#
# O_EXCL prevents CLOBBERING. It does not prevent a reader from observing
# the file between the winner's os.open (which creates it at zero length)
# and the winner's os.write. In that window the loser's very first _read
# sees a 0-byte file and raises -- it never even reaches the O_EXCL path.
#
# It only bites on a FRESH config directory, which is exactly what CI has
# every run and a developer machine essentially never has. That, not the
# scheduler, is why every occurrence was on Linux CI.

def test_racing_first_launch_does_not_see_a_partial_file(tmp_path, monkeypatch):
    """A reader in the creation window must not observe a 0-byte secret.

    The window is forced rather than raced: the creator's publish is
    delayed, and a second thread calls load_or_create inside that delay.
    It must wait for the creator's lock, then read the ONE secret the
    creator published -- never a partial file, never a second secret.
    """
    import threading

    target = tmp_path / "device_secret"
    in_window = threading.Event()
    release = threading.Event()

    # Hold the window open at the storage layer's publish step, which every
    # platform goes through (Windows publishes via the Win32 API, so an
    # os.write hook would never fire there and the test would prove nothing).
    real_publish = identity_storage.Publisher.publish

    def slow_publish(self, data):
        if len(data) == device_secret.SECRET_BYTES:
            in_window.set()
            release.wait(timeout=5.0)
        return real_publish(self, data)

    monkeypatch.setattr(identity_storage.Publisher, "publish", slow_publish)

    results = {}

    def creator():
        try:
            results["creator"] = device_secret.load_or_create(target)
        except BaseException as exc:                    # noqa: BLE001
            results["creator"] = exc

    def racer():
        assert in_window.wait(timeout=5.0), "creation window never opened"
        # The racer will block on the creator's lock; let the creator finish
        # shortly after the racer is in flight.
        threading.Timer(0.3, release.set).start()
        try:
            results["racer"] = device_secret.load_or_create(target)
        except BaseException as exc:                    # noqa: BLE001
            results["racer"] = exc
        finally:
            release.set()

    tc = threading.Thread(target=creator, name="creator")
    tr = threading.Thread(target=racer, name="racer")
    tc.start()
    tr.start()
    tr.join(timeout=15)
    tc.join(timeout=15)

    for who in ("creator", "racer"):
        got = results.get(who)
        assert not isinstance(got, BaseException), (
            f"{who} failed during a concurrent first launch: {got!r}")
        assert isinstance(got, bytes) and len(got) == device_secret.SECRET_BYTES, (
            f"{who} returned {got!r}")

    # Both must agree: a device has ONE secret, or key shares diverge.
    assert results["creator"] == results["racer"], (
        "racing launches produced different device secrets -- key shares "
        "from the two would not agree")
    assert target.read_bytes() == results["creator"]


@pytest.mark.skipif(os.name == "nt", reason="Windows publishes via the Win32 "
                    "API, not os.write; the race test above covers it there")
def test_no_reader_can_observe_a_zero_byte_secret(tmp_path, monkeypatch):
    """Directly pins the invariant: the published path is never partial.

    Records every size any reader could have seen during creation.
    """
    target = tmp_path / "device_secret"
    seen = []
    real_write = os.write

    def watching_write(fd, data):
        if len(data) == device_secret.SECRET_BYTES:
            # What would a concurrent reader see right now?
            seen.append(target.stat().st_size if target.exists() else None)
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", watching_write)
    device_secret.load_or_create(target)

    assert all(s in (None, device_secret.SECRET_BYTES) for s in seen), (
        f"the secret path was observable at a partial size: {seen}")


# ------------------------------------- existing secret bytes are preserved
#
# The device secret must never be silently regenerated. An unsafe existing
# secret is refused untouched, the refusal says NOT to delete it, and
# restricting its permissions in place -- the documented remedy -- brings
# back the ORIGINAL bytes.

ORIGINAL = bytes(range(32))


def test_refusal_preserves_bytes_and_says_not_to_delete(tmp_path):
    target = tmp_path / "device_secret"
    target.write_bytes(ORIGINAL)
    if os.name != "nt":
        os.chmod(target, 0o644)
    with pytest.raises(identity_storage.InsecureKeyStorage) as info:
        device_secret.load_or_create(target)
    message = str(info.value)
    assert "was not modified" in message
    assert "Do not delete it" in message
    assert target.read_bytes() == ORIGINAL
    assert list(tmp_path.glob("*.tmp")) == []          # no stray replacement


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_secret_written_by_the_old_code_keeps_its_bytes(tmp_path):
    """The previous implementation published the secret and chmod-ed it
    0600. Such a secret must load with exactly its stored bytes."""
    folder = tmp_path / "cfg"
    folder.mkdir(mode=0o700)
    target = folder / "device_secret"
    target.write_bytes(ORIGINAL)
    os.chmod(target, 0o600)
    assert device_secret.load_or_create(target) == ORIGINAL
    assert device_secret.load_or_create(target) == ORIGINAL
    assert target.read_bytes() == ORIGINAL


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_chmod_remedy_restores_the_original_secret(tmp_path):
    target = tmp_path / "device_secret"
    target.write_bytes(ORIGINAL)
    os.chmod(target, 0o644)
    with pytest.raises(identity_storage.InsecureKeyStorage):
        device_secret.load_or_create(target)
    os.chmod(target, 0o600)                            # the documented remedy
    assert device_secret.load_or_create(target) == ORIGINAL
    assert target.read_bytes() == ORIGINAL


@pytest.mark.skipif(os.name != "nt", reason="Windows DACLs")
def test_windows_remedy_restores_the_original_secret(tmp_path):
    """A secret left by the old code inherits its folder's DACL (and, if an
    elevated process wrote it, explicit Administrators/SYSTEM entries) and is
    refused. Running EXACTLY the PowerShell command the refusal message
    prints gives it identity_storage's own descriptor, after which the
    ORIGINAL bytes load."""
    import subprocess
    target = tmp_path / "device_secret"
    target.write_bytes(ORIGINAL)                       # like the old code
    with pytest.raises(identity_storage.InsecureKeyStorage) as info:
        device_secret.load_or_create(target)
    command = device_secret.windows_remedy(target)
    assert command in str(info.value)                  # the message carries it
    # Windows PowerShell 5.1 launched from a PowerShell 7 environment (as on
    # the CI runner) inherits pwsh's PSModulePath and cannot load its own
    # Microsoft.PowerShell.Security module (no Get-Acl). Clearing it lets 5.1
    # rebuild its default -- the state a user's own PowerShell window has.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    run = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          command], capture_output=True, text=True, env=env)
    assert run.returncode == 0, f"remedy failed: {run.stderr.strip()}"
    assert device_secret.load_or_create(target) == ORIGINAL
    assert target.read_bytes() == ORIGINAL


def test_windows_remedy_quotes_the_path_safely():
    cmd = device_secret.windows_remedy(Path(r"C:\Users\o'brien\cfg\device_secret"))
    assert "-LiteralPath 'C:\\Users\\o''brien\\cfg\\device_secret'" in cmd
    assert 'D:P(A;;FA;;;' in cmd and 'O:$($s)' in cmd
