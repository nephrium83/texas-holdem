"""Exercise native wheel bindings without sockets, device capture, or a GUI."""
from __future__ import annotations

import ctypes
import importlib.metadata
import json
from pathlib import Path


def main():
    import aiortc
    import av
    from cryptography.hazmat.primitives.asymmetric import ec
    import google_crc32c
    from OpenSSL import SSL
    import pylibsrtp

    # Exercise crypto, CRC and SRTP bindings as well as importing their modules.
    key = ec.generate_private_key(ec.SECP256R1())
    if not key.public_key():
        raise RuntimeError("EC native key generation failed")
    if google_crc32c.value(b"123456789") != 0xE3069283:
        raise RuntimeError("Native CRC32C vector failed")
    context = SSL.Context(SSL.DTLS_METHOD)
    if context is None:
        raise RuntimeError("DTLS context creation failed")
    srtp = pylibsrtp.Session(pylibsrtp.Policy(
        key=bytes(30), ssrc_type=pylibsrtp.Policy.SSRC_ANY_INBOUND))
    if srtp is None:
        raise RuntimeError("SRTP native session creation failed")
    av_dist = importlib.metadata.distribution("av")
    avutil = next(Path(av_dist.locate_file(item)) for item in av_dist.files or ()
                  if item.name.startswith("avutil-") and item.suffix == ".dll")
    native = ctypes.CDLL(str(avutil))
    native.avutil_license.restype = ctypes.c_char_p
    native.avutil_configuration.restype = ctypes.c_char_p
    print(json.dumps({
        "ok": True, "aiortc_version": aiortc.__version__, "av_version": av.__version__,
        "ffmpeg_library_versions": av.library_versions,
        "ffmpeg_license": native.avutil_license().decode("utf-8"),
        "ffmpeg_configuration": native.avutil_configuration().decode("utf-8"),
        "openssl_version": SSL.SSLeay_version(SSL.SSLEAY_VERSION).decode("ascii"),
        "srtp_session_created": True, "crc32c_vector_passed": True,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
