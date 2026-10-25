# Direct Internet connection engineering gate

This optional host component is the first gate in the approved direct UDP
hole-punching plan. It does not yet add Internet Host/Join to the Poker desktop.
The transferable poker API and its dependency-free base installation are unchanged.

`holdem.ice_peer.DirectPeer` owns one reliable ordered binary data channel. It
uses pinned aiortc1.15.0 and aioice0.10.2, with STUN and no TURN. Messages up to
512KiB use bounded16KiB fragments, queue/backpressure limits and reassembly expiry.
The peer closes only its own tasks and sockets. Selected-pair diagnostics and
Windows Proactor cleanup have isolated accessors for those pinned implementations.

`holdem.ice_signaling` and `holdem.ice_probe` implement the bounded engineering
exchange. Fresh proof keys are independent of game identity/admission. Every
signed, encrypted description binds the repository, workflow run/attempt, fresh
nonce, immutable source commit, exact package hash and peer public keys. The host
authenticates guest keys by downloading the ready artifact from that exact run.
Each process consumes one offer/answer; a new proof must use a fresh nonce/keys.
Public exports reject unknown fields and contain public keys, ciphertext or fixed
report fields. Raw SDP, candidate addresses, ICE passwords and private keys are
never intended for upload.

Build Windows x64 in an isolated environment with installed MSVC and Git Bash:

```powershell
python -m venv build/direct-ice-venv
& build/direct-ice-venv/Scripts/python.exe -m pip install --only-binary=:all: setuptools==84.2.1 Cython==3.2.9 wheel==0.48.0 delvewheel==1.13.2 pyinstaller==6.20.0
& build/direct-ice-venv/Scripts/python.exe tools/build_ice_native.py --install
& build/direct-ice-venv/Scripts/python.exe -m pip install --only-binary=:all: -c packaging/ice-probe-constraints.txt '.[internet]'
& build/direct-ice-venv/Scripts/python.exe tools/build_ice_probe.py --self-test --result-json build/probe-result.json
```

The explicit source build uses vanilla FFmpeg8.1.3 with media codecs, formats,
devices, filters, protocols, autodetection and network support disabled. Only
seven small shared LGPL2.1 libraries remain to satisfy PyAV's mandatory imports;
the data channel does not encode media. Stock PyPI PyAV wheels cannot pass the
package gate. The custom wheel supplies the original source archives, configure
flags, build script, licenses and shared-library replacement/relink instructions.
The folder build permits replacement of compatible LGPL libraries.

The builder verifies installed native hashes and exact source/build provenance,
copies runtime and native notices, records source/versions/file hashes, runs the
frozen local pair from an unrelated directory with a stripped PATH, and creates a
fresh ZIP. It neither installs Poker Preview nor changes a router/firewall rule.
`DirectConnectProbe.exe --mailbox-self-test` separately checks the packaged
asynchronous DNS/verified TLS path; it makes one read-only GitHub request.

The outside-network workflow builds one ZIP and uses those exact bytes on its
Windows guest and BWING. Engineering signaling uses an encrypted offer in a
temporary secret gist and an encrypted answer artifact. A successful proof must
verify4KiB/32KiB/512KiB round trips, an observed direct UDP candidate pair, native
exit0, closed sockets and no pending peer tasks. Local success alone is not an
Internet acceptance result. A GitHub-hosted guest proves a home-to-cloud route,
not all residential NAT pairs. Failure must be reported without a silent relay
fallback. Authenticated poker integration and automatic invitation setup follow
this gate.
