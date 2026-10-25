# External packaged ICE proof bootstrap

This manually dispatched workflow supplies a bounded Windows guest on a
GitHub-hosted runner. It is an engineering connectivity experiment, not an
Internet poker release. It must first exist on the default branch before a
maintainer can dispatch an immutable feature commit containing the probe,
its builder, and the optional Python dependencies.

No introduction service, TURN server, VPN, UPnP rule, or router change is
created by this workflow. It proves a home-to-cloud route when successful;
it does not establish that every pair of residential routers will connect.

## Inputs and trust

All five dispatch inputs are public, fixed-width lowercase hexadecimal:

| Input | Length | Meaning |
| --- | --- | --- |
| source_ref | 40 | Immutable commit checked out from this repository |
| nonce | 64 | Fresh proof-run nonce |
| mailbox_gist_id | 32 | Identifier of the encrypted offer mailbox |
| host_signing_public | 64 | BWING's ephemeral Ed25519 public key |
| host_encryption_public | 64 | BWING's ephemeral X25519 public key |

Inputs enter shell steps through environment variables and are validated
before checkout. They are never interpolated into shell source. The job has
only contents: read and actions: read, and checkout does not persist its
credential. No mailbox token or repository secret is exported to the probe.
The runner only reads the gist anonymously. A maintainer on BWING creates
the mailbox and writes the signed, encrypted offer using existing local
authentication.

The context binds repository, run ID, run attempt, nonce, source commit,
exact package ZIP SHA-256, and both host public keys. The public ready
artifact carries the guest public keys and matching context. BWING must
download it through the authenticated repository/run/artifact identity and
check every context field before using those keys. Knowing a gist ID does
not authenticate a peer or authorize a poker seat.

## One build on both computers

The runner uses an isolated Python 3.13 environment, builds minimal shared
FFmpeg and PyAV from pinned sources, then installs the constrained optional
Internet dependencies and PyInstaller. The stock PyPI PyAV native closure
cannot pass the package provenance gate. It then runs:

    python tools/build_ice_probe.py --self-test --output-dir <runner-temp>/ice-package --result-json <runner-temp>/ice-build-result.json

The builder returns application, zip, zip_sha256, manifest,
manifest_sha256, build_logs, and self_test. Unique build paths preserve
prior results. The helper verifies the ZIP and manifest hashes and stages
byte-for-byte copies as DirectConnectProbe.zip and manifest.json.
The runner extracts and executes that exact ZIP; BWING downloads and uses
the same artifact and verifies its SHA-256 against the authenticated context.
It must not rebuild a nominally equivalent local copy.

## Probe CLI contract

The frozen program lives at DirectConnectProbe/DirectConnectProbe.exe
inside the ZIP. Paths below are private runner files unless explicitly
exported for publication:

    DirectConnectProbe.exe prepare --context-file context.json --key-dir private --ready-file private/ready.json
    DirectConnectProbe.exe answer --context-file context.json --key-dir private --mailbox-gist-id <id> --answer-file private/answer.json --report-file private/report.json --cancel-file cancel --timeout 300
    DirectConnectProbe.exe validate-artifact --kind ready|answer|report --context-file context.json --input private/<kind>.json --output public/<kind>.json

Preparation creates fresh local signing/encryption keys. Public ready
output contains no private key, SDP, ICE address, or ICE credential. The
answer process waits for a signed and encrypted offer.json in the mailbox,
writes its signed encrypted answer atomically, and stays alive while the
workflow uploads the answer artifact. BWING then decrypts that answer and
finishes the direct handshake.

The validate-artifact command owns the exact schema and public-field
allowlists. It checks the expected context and signatures where applicable
and exports a validated copy. Schema validation of a report is separate
from passing the proof. The helper accepts only:

- status "connected", direct true, and protocol "udp";
- at least three messages sent and received;
- cleanup.peer_closed true and no owned tasks pending.

The probe's status additionally depends on verifying the intended 4 KiB,
32 KiB, and 512 KiB payload round trips. Candidate types are limited to
host, srflx, prflx, or null. TURN/relay candidates cannot pass.

## Bounds, cleanup, and evidence

The job lasts at most twenty-five minutes, including the explicit native
source build. The guest has one 300-second budget,
including the answer upload interval; later workflow steps do not reset it.
Start-Process -WindowStyle Hidden starts the one-use supervisor and its
owned frozen executable.
The outer launcher uses a separate hidden Windows process without redirected
standard streams. Redirecting them there inherits the workflow's pipe handles
and can prevent the answer-upload step from starting while the peer is alive.
The supervisor writes its own private transcript and owns the native child's
redirected streams. A real regression fixture checks that the launching shell's
captured pipes close while its three-second native child remains alive.
A one-use PowerShell supervisor keeps the native process handle across
workflow steps. It publishes private, atomic ownership and exit receipts,
bound to the run nonce, native PID, and kernel process creation time. The
complete phase requires a real native exit code of zero, no forced stop,
and a validated guest report. A report claiming success cannot override a
nonzero native exit or a missing exit receipt.

Cleanup first writes the cancellation file and waits ten seconds. If
necessary, it verifies executable path and process creation time before
terminating the exact owned native process or supervisor, records a forced
stop, and fails cleanup. It never enumerates and kills game or service
processes by name. The CLI reserves cleanup time inside the advertised
300-second budget. The supervisor independently uses that original deadline;
an unexpected deadline failure gets at most ten seconds of cooperative
cancellation and five seconds to confirm forced closure, and cannot pass
the proof. Its request, logs, ownership receipt, and exit receipt stay
outside the public artifact directory.

The helper's public status schema is `direct-ice-workflow-status-v1` with
exact fields `schema`, `source_commit`, `run_id`, `phase`,
`owned_process_closed`, `supervisor_closed`, `native_exit_code`, and
`forced_stop`. The native exit is an integer from the validated private
receipt, or null if unavailable. No PID, process creation time, executable
path, command arguments, or private receipt is published.

Only these artifacts are uploaded, with one-day retention:

1. Exact package ZIP and manifest.
2. Validated public ready.json.
3. Validated encrypted answer.json, while the peer process remains alive.
4. Validated public report.json and helper-generated status.json.

Raw stdout/stderr, key directories, context work files, unencrypted
offer/answer SDP, and environment dumps remain outside artifact paths.
Failures do not print private probe logs. Cancellation or a direct-path
failure is recorded as a failed experiment, not silently retried through
another transport.

Local guard checks run with:

    python -m pytest tests/test_ice_workflow.py -q

They execute the actual PowerShell guards for malicious dispatch values,
escaping artifact paths, reused process IDs, elapsed deadlines, and failed
direct/cleanup reports. Windows tests also launch a small native executable
that exits normally with zero or one, proving the supervisor records the
actual exit and the complete phase rejects a nonzero exit even beside a
report claiming success. They open no network connection or service and
terminate no process. The PowerShell checks skip explicitly when pwsh is
unavailable; the workflow-policy check remains platform independent.
