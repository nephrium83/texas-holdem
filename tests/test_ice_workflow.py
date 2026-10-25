"""Safety checks for the manually dispatched external Windows proof.

These run the actual PowerShell input/path/process guards without opening
connections, building a package, or terminating any process.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "direct-ice-proof.yml"
HELPER = ROOT / "tools" / "run_ice_guest.ps1"
PWSH = shutil.which("pwsh")


def _powershell(code: str, extra_env=None):
    if PWSH is None:
        pytest.skip("PowerShell is required for Windows proof orchestration guards")
    env = dict(os.environ, ICE_TEST_HELPER=str(HELPER))
    env.update(extra_env or {})
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", code],
        env=env, capture_output=True, text=True, timeout=20,
    )


def _dispatch_validation():
    """Extract the actual pre-checkout script, preserving its indentation."""
    source = WORKFLOW.read_text(encoding="utf-8")
    block = source.split("      - name: Validate dispatch input before checkout\n", 1)[1]
    block = block.split("        run: |\n", 1)[1].split("      - name:", 1)[0]
    return textwrap.dedent(block)


def test_dispatch_accepts_valid_fixed_width_inputs():
    result = _powershell(_dispatch_validation(), {
        "ICE_SOURCE_REF": "a" * 40,
        "ICE_NONCE": "b" * 64,
        "ICE_MAILBOX_GIST_ID": "c" * 32,
        "ICE_HOST_SIGNING_PUBLIC": "d" * 64,
        "ICE_HOST_ENCRYPTION_PUBLIC": "e" * 64,
        "ICE_GAME_HANDS": "10",
    })
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("field", [
    "ICE_SOURCE_REF", "ICE_NONCE", "ICE_MAILBOX_GIST_ID",
    "ICE_HOST_SIGNING_PUBLIC", "ICE_HOST_ENCRYPTION_PUBLIC",
])
def test_dispatch_rejects_shell_payload_as_data(field, tmp_path):
    marker = tmp_path / "injection-executed"
    values = {
        "ICE_SOURCE_REF": "a" * 40,
        "ICE_NONCE": "b" * 64,
        "ICE_MAILBOX_GIST_ID": "c" * 32,
        "ICE_HOST_SIGNING_PUBLIC": "d" * 64,
        "ICE_HOST_ENCRYPTION_PUBLIC": "e" * 64,
        "ICE_GAME_HANDS": "10",
    }
    values[field] = f"'; New-Item -Path '{marker}'; #"
    result = _powershell(_dispatch_validation(), values)
    assert result.returncode != 0
    assert field in result.stderr
    assert "New-Item" not in result.stderr
    assert not marker.exists()


def test_built_artifact_paths_cannot_escape_the_build_directory(tmp_path):
    result = _powershell(r"""
        . $env:ICE_TEST_HELPER -Phase Prepare
        $root = Join-Path $env:ICE_TEST_ROOT 'build'
        $valid = Join-Path $root 'unique/DirectConnectProbe.zip'
        if ((Assert-ProofChild $valid $root) -cne [IO.Path]::GetFullPath($valid)) { exit 3 }
        $bad = @(
            $root,
            (Join-Path $env:ICE_TEST_ROOT 'build-other/DirectConnectProbe.zip'),
            (Join-Path $root '../private/key.json')
        )
        foreach ($path in $bad) {
            $refused = $false
            try { Assert-ProofChild $path $root | Out-Null } catch { $refused = $true }
            if (-not $refused) { exit 4 }
        }
    """, {"ICE_TEST_ROOT": str(tmp_path)})
    assert result.returncode == 0, result.stderr


def test_cleanup_refuses_reused_process_id_before_any_termination():
    result = _powershell(r"""
        . $env:ICE_TEST_HELPER -Phase Prepare
        $script:created = [DateTime]::UtcNow
        function Get-Process {
            param($Id, $ErrorAction)
            return [pscustomobject]@{ Path = 'C:\owned\probe.exe'; StartTime = $script:created; HasExited = $false }
        }
        $state = @{
            process_id = 12345
            executable = 'C:\owned\probe.exe'
            process_start_ticks = $script:created.ToUniversalTime().Ticks.ToString()
        }
        if ($null -eq (Get-OwnedProofProcess $state)) { exit 2 }
        $state.process_start_ticks = ($script:created.Ticks - 1).ToString()
        $refused = $false
        try { Get-OwnedProofProcess $state | Out-Null } catch { $refused = $true }
        if (-not $refused) { exit 3 }
        $state.process_start_ticks = $script:created.ToUniversalTime().Ticks.ToString()
        $state.executable = 'C:\unrelated\service.exe'
        $refused = $false
        try { Get-OwnedProofProcess $state | Out-Null } catch { $refused = $true }
        if (-not $refused) { exit 4 }
    """)
    assert result.returncode == 0, result.stderr


def test_proof_deadline_remains_bounded_across_workflow_steps():
    result = _powershell(r"""
        . $env:ICE_TEST_HELPER -Phase Prepare
        $expired = @{ deadline_utc = [DateTimeOffset]::UtcNow.AddSeconds(-1).ToString('o') }
        if ((Get-ProofRemainingMilliseconds $expired) -ne 0) { exit 2 }
        $future = @{ deadline_utc = [DateTimeOffset]::UtcNow.AddSeconds(900).ToString('o') }
        if ((Get-ProofRemainingMilliseconds $future) -ne 300000) { exit 3 }
        $running = @{ deadline_utc = [DateTimeOffset]::UtcNow.AddSeconds(5).ToString('o') }
        $remaining = Get-ProofRemainingMilliseconds $running
        if ($remaining -le 0 -or $remaining -gt 5000) { exit 4 }
    """)
    assert result.returncode == 0, result.stderr


def test_result_requires_direct_payload_roundtrips_and_complete_peer_cleanup():
    result = _powershell(r"""
        . $env:ICE_TEST_HELPER -Phase Prepare
        $valid = @{
            role = 'guest'; status = 'connected'; direct = $true; protocol = 'udp'
            sent_messages = 3; received_messages = 3
            cleanup = @{ peer_closed = $true; owned_tasks_pending = 0 }
        }
        Assert-ProofPassed $valid
        foreach ($case in @('host', 'failed', 'relay', 'tcp', 'sent', 'received', 'open', 'tasks')) {
            $report = @{
                role = 'guest'; status = 'connected'; direct = $true; protocol = 'udp'
                sent_messages = 3; received_messages = 3
                cleanup = @{ peer_closed = $true; owned_tasks_pending = 0 }
            }
            switch ($case) {
                host { $report.role = 'host' }
                failed { $report.status = 'failed' }
                relay { $report.direct = $false }
                tcp { $report.protocol = 'tcp' }
                sent { $report.sent_messages = 2 }
                received { $report.received_messages = 2 }
                open { $report.cleanup.peer_closed = $false }
                tasks { $report.cleanup.owned_tasks_pending = 1 }
            }
            $refused = $false
            try { Assert-ProofPassed $report } catch { $refused = $true }
            if (-not $refused) { exit 5 }
        }
    """)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="real supervisor launches a Windows native fixture")
@pytest.mark.parametrize("native_exit", [0, 1])
def test_supervisor_records_actual_native_exit_before_complete_can_pass(tmp_path, native_exit):
    import json

    root = tmp_path / "direct-ice-proof"
    private = root / "private"
    public = root / "public"
    package = root / "package" / "DirectConnectProbe"
    for directory in (private, public, package):
        directory.mkdir(parents=True, exist_ok=True)
    executable = package / "DirectConnectProbe.exe"
    # A small native Windows executable supplies real success/failure exits;
    # neither fixture opens a connection, service, or command interpreter.
    shutil.copy2(Path(os.environ["SystemRoot"]) / "System32" / "where.exe", executable)
    request = {
        "schema": "direct-ice-supervisor-request-v1", "nonce": "a" * 64,
        "executable": str(executable), "arguments": [
            "/q", "cmd.exe" if native_exit == 0 else "no-such-ice-proof-executable-12345.exe"],
        "deadline_utc": None,
    }
    (private / "supervisor-request.json").write_text(json.dumps(request), encoding="utf-8")
    supervisor = ROOT / "tools" / "run_ice_guest_supervisor.ps1"
    result = _powershell(r"""
        . $env:ICE_TEST_HELPER -Phase Prepare
        $paths = Get-ProofPaths
        $requestFile = Join-Path $paths.private 'supervisor-request.json'
        $request = Read-ProofJson $requestFile
        $request.deadline_utc = [DateTimeOffset]::UtcNow.AddSeconds(30).ToString('o')
        Write-ProofJson $request $requestFile
        & $env:ICE_TEST_SUPERVISOR -RequestFile $requestFile
        $supervisorCode = $LASTEXITCODE
        if (($env:ICE_TEST_EXIT -eq '0') -ne ($supervisorCode -eq 0)) { exit 4 }
        $exitReceipt = Read-ProofJson (Join-Path $paths.private 'exit.json')
        if ($exitReceipt.exit_code -ne [int]$env:ICE_TEST_EXIT -or $exitReceipt.forced_stop) { exit 5 }
        $state = @{
            executable = $request.executable; phase = 'running'; proof_nonce = $request.nonce
            game_hands = 0
            deadline_utc = $request.deadline_utc
        }
        Read-NativeOwnership $paths $state | Out-Null
        Write-ProofJson $state $paths.state
        # The complete phase must refuse a nonzero OS exit even if the probe
        # previously wrote a report claiming successful payloads and cleanup.
        function Publish-ProofArtifact($Paths, $State, $Kind) {
            Set-Content -LiteralPath (Join-Path $Paths.root 'publish-called') -Value 'called'
            Write-ProofJson @{
                role = 'guest'; status = 'connected'; direct = $true; protocol = 'udp'
                sent_messages = 3; received_messages = 3
                cleanup = @{ peer_closed = $true; owned_tasks_pending = 0 }
            } (Join-Path $Paths.public 'report.json')
        }
        $refused = $false
        try { Invoke-ProofGuest Complete } catch { $refused = $true }
        if (($env:ICE_TEST_EXIT -eq '0') -eq $refused) { exit 6 }
        if ($refused -and (Test-Path -LiteralPath (Join-Path $paths.root 'publish-called'))) { exit 7 }
        Invoke-ProofGuest Cleanup
        $status = Read-ProofJson (Join-Path $paths.public 'status.json')
        if ($status.native_exit_code -ne [int]$env:ICE_TEST_EXIT -or
            -not $status.supervisor_closed -or -not $status.owned_process_closed -or $status.forced_stop) { exit 8 }
        exit 0
    """, {"RUNNER_TEMP": str(tmp_path), "ICE_TEST_SUPERVISOR": str(supervisor),
          "ICE_TEST_EXIT": str(native_exit), "ICE_SOURCE_REF": "c" * 40, "GITHUB_RUN_ID": "123"})
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads((private / "exit.json").read_text(encoding="utf-8"))
    assert receipt["exit_code"] == native_exit
    assert receipt["forced_stop"] is False
    status = json.loads((public / "status.json").read_text(encoding="utf-8"))
    assert set(status) == {"schema", "source_commit", "run_id", "phase", "owned_process_closed",
                           "supervisor_closed", "native_exit_code", "forced_stop"}
    assert status["native_exit_code"] == native_exit
    assert status["supervisor_closed"] is True


def test_native_exit_receipt_is_bound_to_nonce_process_and_creation_time(tmp_path):
    result = _powershell(r"""
        . $env:ICE_TEST_HELPER -Phase Prepare
        $paths = @{ private = $env:ICE_TEST_ROOT }
        $state = @{ proof_nonce = ('a' * 64); process_id = 123; process_start_ticks = '456' }
        foreach ($case in @('valid', 'nonce', 'pid', 'ticks', 'forced', 'nonzero', 'wrongtype')) {
            $receipt = @{
                schema = 'direct-ice-native-exit-v1'; nonce = $state.proof_nonce
                process_id = 123; process_start_ticks = '456'; exit_code = 0; forced_stop = $false
            }
            switch ($case) {
                nonce { $receipt.nonce = 'b' * 64 }
                pid { $receipt.process_id = 124 }
                ticks { $receipt.process_start_ticks = '457' }
                forced { $receipt.forced_stop = $true }
                nonzero { $receipt.exit_code = 1 }
                wrongtype { $receipt.exit_code = '0' }
            }
            Write-ProofJson $receipt (Join-Path $paths.private 'exit.json')
            $refused = $false
            try { Assert-NativeExit $paths $state | Out-Null } catch { $refused = $true }
            if (($case -eq 'valid') -eq $refused) { exit 3 }
        }
    """, {"ICE_TEST_ROOT": str(tmp_path)})
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows detached process lifetime")
def test_supervisor_and_native_survive_the_starting_shell_exit(tmp_path):
    import json
    import time
    from datetime import datetime, timedelta, timezone

    root = tmp_path / "direct-ice-proof"
    private = root / "private"
    package = root / "package" / "DirectConnectProbe"
    private.mkdir(parents=True)
    package.mkdir(parents=True)
    executable = package / "DirectConnectProbe.exe"
    base = Path(getattr(sys, "_base_executable", sys.executable))
    shutil.copy2(base, executable)
    for pattern in ("python*.dll", "vcruntime*.dll"):
        for library in base.parent.glob(pattern):
            shutil.copy2(library, package / library.name)
    request_file = private / "supervisor-request.json"
    request_file.write_text(json.dumps({
        "schema": "direct-ice-supervisor-request-v1", "nonce": "a" * 64,
        "executable": str(executable),
        "arguments": ["-c", "import time;time.sleep(3);raise SystemExit(0)"],
        "deadline_utc": (datetime.now(timezone.utc) + timedelta(seconds=20)).isoformat(),
    }))
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), PYTHONHOME=str(base.parent),
               ICE_SUPERVISOR=str(ROOT / "tools/run_ice_guest_supervisor.ps1"),
               ICE_REQUEST=str(request_file), ICE_PRIVATE=str(private), ICE_PWSH=PWSH,
               ICE_HELPER=str(HELPER))
    code = r'''
        $ErrorActionPreference = 'Stop'
        . $env:ICE_HELPER -Phase Prepare
        $child = Start-OwnedSupervisor $env:ICE_PWSH $env:ICE_SUPERVISOR $env:ICE_REQUEST
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(8)
        while (-not (Test-Path -LiteralPath (Join-Path $env:ICE_PRIVATE 'process.json'))) {
            if ([DateTimeOffset]::UtcNow -gt $deadline -or $child.HasExited) { exit 3 }
            Start-Sleep -Milliseconds 50
        }
        exit 0
    '''
    # A workflow captures pipes. Its start step must finish and drain those
    # pipes while the actual native peer is still alive for answer exchange.
    parent = subprocess.Popen([PWSH, "-NoProfile", "-NonInteractive", "-Command", code],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    parent.wait(timeout=15)
    try:
        output, error = parent.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        parent.communicate(timeout=10)
        pytest.fail("Detached supervisor/native retained the workflow stdout/stderr pipes")
    assert parent.returncode == 0, (output + error).decode(errors="replace")
    deadline = time.monotonic() + 10
    while not (private / "exit.json").exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    diagnostics = "\n".join(path.read_text(errors="replace") for path in private.glob("*.log"))
    assert (private / "exit.json").exists(), diagnostics
    assert json.loads((private / "exit.json").read_text())["exit_code"] == 0


def test_workflow_uploads_only_explicit_public_files_and_pinned_actions():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "contents: read" in source and "actions: read" in source
    assert "timeout-minutes: 25" in source
    assert "persist-credentials: false" in source
    assert "${{ secrets." not in source
    import re
    actions = re.findall(r"uses: ([^\s]+)", source)
    assert actions
    assert all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action) for action in actions)
    upload_blocks = [block for block in source.split("      - name:")
                     if "uses: actions/upload-artifact@" in block]
    assert len(upload_blocks) == 6
    for block in upload_blocks:
        assert "/private" not in block
        assert "*.json" not in block and "**" not in block
    desktop_uploads = [block for block in upload_blocks if "desktop-proof" in block]
    assert len(desktop_uploads) == 2
    assert {match for block in desktop_uploads for match in
            re.findall(r"desktop-proof/public/([a-z.]+)", block)} == {"ready.json", "result.json"}
    assert source.index("Validate dispatch input") < source.index("uses: actions/checkout@")
    assert source.index("Build the minimal native runtime") < source.index("Install pinned probe dependencies")
    assert source.index("Publish encrypted answer while guest is running") < source.index("-Phase Complete")
