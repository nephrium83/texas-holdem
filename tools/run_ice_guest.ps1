param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Prepare', 'Start', 'Complete', 'Cleanup')]
    [string] $Phase
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-ProofHex([string] $Value, [int] $Length, [string] $Name) {
    if ($Value -cnotmatch ('^[0-9a-f]{' + $Length + '}$')) {
        throw "Invalid proof input: $Name"
    }
}

function Read-ProofJson([string] $Path, [int] $MaximumBytes = 65536) {
    $item = Get-Item -LiteralPath $Path
    if ($item.PSIsContainer -or $item.Length -gt $MaximumBytes) {
        throw 'Proof metadata has an invalid size'
    }
    return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -AsHashtable
}

function Write-ProofJson($Value, [string] $Path) {
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding utf8NoBOM
}

function Assert-ProofChild([string] $Path, [string] $Root) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Proof artifact is outside its expected build directory'
    }
    return $resolved
}

function Get-ProofPaths {
    if (-not $env:RUNNER_TEMP -or -not [IO.Path]::IsPathFullyQualified($env:RUNNER_TEMP)) {
        throw 'An absolute runner temporary directory is required'
    }
    $root = Join-Path $env:RUNNER_TEMP 'direct-ice-proof'
    return @{
        root = $root
        public = Join-Path $root 'public'
        private = Join-Path $root 'private'
        state = Join-Path $root 'state.json'
        context = Join-Path $root 'context.json'
        cancel = Join-Path $root 'cancel'
        build = Join-Path $env:RUNNER_TEMP 'ice-package'
        artifacts = Join-Path $env:RUNNER_TEMP 'ice-artifact'
    }
}

function Invoke-PrivateProbe([string] $Executable, [string[]] $Arguments, [string] $Log) {
    $quoted = @($Arguments | ForEach-Object { '"' + $_ + '"' })
    $process = Start-Process -FilePath $Executable -ArgumentList $quoted -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $Log -RedirectStandardError ($Log + '.stderr')
    if (-not $process.WaitForExit(30000)) {
        # This is the process object just created above, never a name-based
        # search or a persisted PID. Local validation cannot hang the job.
        $process.Kill()
        $process.WaitForExit(5000) | Out-Null
        throw 'Packaged proof operation exceeded its local deadline'
    }
    if ($process.ExitCode -ne 0) { throw 'Packaged proof operation failed; private logs withheld' }
}

function Publish-ProofArtifact($Paths, $State, [string] $Kind) {
    # The frozen probe owns the schema and performs strict public-field,
    # signature/context checks. Only its validated export is publishable.
    Invoke-PrivateProbe $State.executable @(
        'validate-artifact', '--kind', $Kind, '--context-file', $Paths.context,
        '--input', (Join-Path $Paths.private ($Kind + '.json')),
        '--output', (Join-Path $Paths.public ($Kind + '.json'))
    ) (Join-Path $Paths.private ($Kind + '-validation.log'))
}

function Get-OwnedProofProcess($State) {
    if (-not $State.ContainsKey('process_id')) { return $null }
    $process = Get-Process -Id $State.process_id -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    if ($process.HasExited) { return $null }
    # Never terminate a reused PID or another service. Both executable and
    # kernel process creation time must match the exact process we launched.
    if (-not [string]::Equals($process.Path, $State.executable, [StringComparison]::OrdinalIgnoreCase) -or
        $process.StartTime.ToUniversalTime().Ticks.ToString() -cne $State.process_start_ticks) {
        throw 'Owned proof process identity no longer matches'
    }
    return $process
}

function Get-OwnedSupervisorProcess($State) {
    if (-not $State.ContainsKey('supervisor_id')) { return $null }
    return Get-OwnedProofProcess @{
        process_id = $State.supervisor_id
        executable = $State.supervisor_executable
        process_start_ticks = $State.supervisor_start_ticks
    }
}

function Start-OwnedSupervisor([string] $PowerShell, [string] $Script, [string] $RequestFile) {
    $arguments = @('-NoProfile', '-NonInteractive', '-File', $Script, '-RequestFile', $RequestFile)
    $quoted = @($arguments | ForEach-Object { '"' + $_ + '"' })
    # Redirection here makes Windows inherit the workflow's pipe handles.
    # The runner then cannot finish this step until the guest exits, which
    # prevents uploading its answer while it can still connect. The detached
    # supervisor records its own private transcript instead.
    return Start-Process -FilePath $PowerShell -ArgumentList $quoted -WindowStyle Hidden -PassThru
}

function Read-NativeOwnership($Paths, $State) {
    $receipt = Read-ProofJson (Join-Path $Paths.private 'process.json')
    $fields = @('schema', 'nonce', 'executable', 'process_id', 'process_start_ticks')
    if ($receipt.Count -ne $fields.Count -or @($receipt.Keys | Where-Object { $_ -notin $fields }).Count -ne 0 -or
        $receipt.schema -cne 'direct-ice-owned-process-v1' -or $receipt.nonce -cne $State.proof_nonce -or
        -not [string]::Equals($receipt.executable, $State.executable, [StringComparison]::OrdinalIgnoreCase) -or
        ($receipt.process_id -isnot [int] -and $receipt.process_id -isnot [long]) -or $receipt.process_id -le 0 -or
        $receipt.process_start_ticks -isnot [string] -or $receipt.process_start_ticks -cnotmatch '^[1-9][0-9]{0,19}$') {
        throw 'Invalid owned native process receipt'
    }
    $State.process_id = $receipt.process_id
    $State.process_start_ticks = $receipt.process_start_ticks
    return $receipt
}

function Read-NativeExit($Paths, $State) {
    $receipt = Read-ProofJson (Join-Path $Paths.private 'exit.json')
    $fields = @('schema', 'nonce', 'process_id', 'process_start_ticks', 'exit_code', 'forced_stop')
    if ($receipt.Count -ne $fields.Count -or @($receipt.Keys | Where-Object { $_ -notin $fields }).Count -ne 0 -or
        $receipt.schema -cne 'direct-ice-native-exit-v1' -or $receipt.nonce -cne $State.proof_nonce -or
        ($receipt.process_id -isnot [int] -and $receipt.process_id -isnot [long]) -or
        $receipt.process_id -ne $State.process_id -or $receipt.process_start_ticks -isnot [string] -or
        $receipt.process_start_ticks -cne $State.process_start_ticks -or
        ($receipt.exit_code -isnot [int] -and $receipt.exit_code -isnot [long]) -or
        $receipt.exit_code -lt [int]::MinValue -or $receipt.exit_code -gt [int]::MaxValue -or
        $receipt.forced_stop -isnot [bool]) { throw 'Invalid native exit receipt' }
    return $receipt
}

function Assert-NativeExit($Paths, $State) {
    $receipt = Read-NativeExit $Paths $State
    if ($receipt.forced_stop -or $receipt.exit_code -ne 0) {
        throw 'Native probe did not exit successfully'
    }
    return $receipt
}

function Get-ProofRemainingMilliseconds($State) {
    $deadline = [DateTimeOffset]::Parse($State.deadline_utc)
    return [int][Math]::Max(0, [Math]::Min(300000, ($deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds))
}

function Assert-ProofPassed($Report) {
    if ($Report.role -cne 'guest' -or $Report.status -cne 'connected' -or -not $Report.direct -or $Report.protocol -cne 'udp' -or
        $Report.sent_messages -lt 3 -or $Report.received_messages -lt 3 -or
        -not $Report.cleanup.peer_closed -or $Report.cleanup.owned_tasks_pending -ne 0) {
        throw 'Packaged guest did not pass the direct connection and cleanup gate'
    }
}

function Invoke-ProofGuest([string] $SelectedPhase) {
    $paths = Get-ProofPaths
    if ($SelectedPhase -eq 'Prepare') {
        Assert-ProofHex $env:ICE_SOURCE_REF 40 'source_ref'
        Assert-ProofHex $env:ICE_NONCE 64 'nonce'
        Assert-ProofHex $env:ICE_MAILBOX_GIST_ID 32 'mailbox_gist_id'
        Assert-ProofHex $env:ICE_HOST_SIGNING_PUBLIC 64 'host_signing_public'
        Assert-ProofHex $env:ICE_HOST_ENCRYPTION_PUBLIC 64 'host_encryption_public'
        if ($env:GITHUB_REPOSITORY -cnotmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
            $env:GITHUB_RUN_ID -cnotmatch '^[0-9]+$' -or $env:GITHUB_RUN_ATTEMPT -cnotmatch '^[0-9]+$') {
            throw 'Invalid authenticated workflow context'
        }
        $actualSource = (& git rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $actualSource -cne $env:ICE_SOURCE_REF) {
            throw 'Checkout does not match the requested immutable source'
        }
        foreach ($directory in @($paths.root, $paths.public, $paths.private, $paths.artifacts)) {
            if (Test-Path -LiteralPath $directory) { throw 'Proof work directory must be fresh' }
            New-Item -ItemType Directory -Path $directory | Out-Null
        }
        $build = Read-ProofJson (Join-Path $env:RUNNER_TEMP 'ice-build-result.json')
        $zip = Assert-ProofChild $build.zip $paths.build
        $manifest = Assert-ProofChild $build.manifest $paths.build
        Assert-ProofHex $build.zip_sha256 64 'package hash'
        Assert-ProofHex $build.manifest_sha256 64 'manifest hash'
        if ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -cne $build.zip_sha256 -or
            (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant() -cne $build.manifest_sha256) {
            throw 'Built package or manifest hash mismatch'
        }
        # Exact bytes are staged for GitHub upload; BWING downloads this ZIP.
        Copy-Item -LiteralPath $zip -Destination (Join-Path $paths.artifacts 'DirectConnectProbe.zip')
        Copy-Item -LiteralPath $manifest -Destination (Join-Path $paths.artifacts 'manifest.json')
        Expand-Archive -LiteralPath $zip -DestinationPath (Join-Path $paths.root 'package')
        $executable = Join-Path $paths.root 'package/DirectConnectProbe/DirectConnectProbe.exe'
        if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Packaged executable is missing' }
        $context = @{
            repository = $env:GITHUB_REPOSITORY
            run_id = $env:GITHUB_RUN_ID
            run_attempt = $env:GITHUB_RUN_ATTEMPT
            nonce = $env:ICE_NONCE
            source_commit = $env:ICE_SOURCE_REF
            package_sha256 = $build.zip_sha256
            host_signing_public = $env:ICE_HOST_SIGNING_PUBLIC
            host_encryption_public = $env:ICE_HOST_ENCRYPTION_PUBLIC
        }
        Write-ProofJson $context $paths.context
        $state = @{ executable = [IO.Path]::GetFullPath($executable); phase = 'prepared'; proof_nonce = $env:ICE_NONCE }
        Invoke-PrivateProbe $state.executable @(
            'prepare', '--context-file', $paths.context, '--key-dir', $paths.private,
            '--ready-file', (Join-Path $paths.private 'ready.json')
        ) (Join-Path $paths.private 'prepare.log')
        Publish-ProofArtifact $paths $state 'ready'
        Write-ProofJson $state $paths.state
        Write-Host 'Packaged guest prepared; only public keys and package artifacts are publishable.'
        return
    }

    if (-not (Test-Path -LiteralPath $paths.state)) {
        if ($SelectedPhase -eq 'Cleanup') { return }
        throw 'The packaged guest has not been prepared'
    }
    $state = Read-ProofJson $paths.state
    if ($SelectedPhase -eq 'Start') {
        if ($state.phase -cne 'prepared') { throw 'Proof guest may only start once' }
        $arguments = @(
            'answer', '--context-file', $paths.context, '--key-dir', $paths.private,
            '--mailbox-gist-id', $env:ICE_MAILBOX_GIST_ID,
            '--answer-file', (Join-Path $paths.private 'answer.json'),
            '--report-file', (Join-Path $paths.private 'report.json'),
            '--cancel-file', $paths.cancel, '--timeout', '300'
        )
        $gameHands = if ($env:ICE_GAME_HANDS) { $env:ICE_GAME_HANDS } else { '0' }
        if ($gameHands -cnotin @('0', '10')) { throw 'Invalid proof input: ICE_GAME_HANDS' }
        $arguments += @('--game-hands', $gameHands)
        $state.game_hands = [int]$gameHands
        $state.deadline_utc = [DateTimeOffset]::UtcNow.AddSeconds(300).ToString('o')
        $requestFile = Join-Path $paths.private 'supervisor-request.json'
        Write-ProofJson @{
            schema = 'direct-ice-supervisor-request-v1'; nonce = $state.proof_nonce
            executable = $state.executable; arguments = $arguments; deadline_utc = $state.deadline_utc
        } $requestFile
        $state.supervisor_executable = (Get-Process -Id $PID).Path
        $supervisorScript = Join-Path $PSScriptRoot 'run_ice_guest_supervisor.ps1'
        $supervisor = Start-OwnedSupervisor $state.supervisor_executable $supervisorScript $requestFile
        $state.supervisor_id = $supervisor.Id
        $state.supervisor_start_ticks = $supervisor.StartTime.ToUniversalTime().Ticks.ToString()
        $state.phase = 'starting'
        Write-ProofJson $state $paths.state
        $ownershipDeadline = [DateTimeOffset]::UtcNow.AddSeconds(10)
        while (-not (Test-Path -LiteralPath (Join-Path $paths.private 'process.json'))) {
            $supervisor.Refresh()
            if ($supervisor.HasExited) { throw 'Owned proof supervisor ended before native startup' }
            if ([DateTimeOffset]::UtcNow -ge $ownershipDeadline) { throw 'Owned native startup deadline expired' }
            Start-Sleep -Milliseconds 100
        }
        Read-NativeOwnership $paths $state | Out-Null
        $state.phase = 'running'
        Write-ProofJson $state $paths.state
        while (-not (Test-Path -LiteralPath (Join-Path $paths.private 'answer.json'))) {
            $supervisor.Refresh()
            if ($supervisor.HasExited) { throw 'Packaged guest ended before publishing an encrypted answer' }
            if ((Get-ProofRemainingMilliseconds $state) -le 0) { throw 'Encrypted answer deadline expired' }
            Start-Sleep -Milliseconds 500
        }
        Publish-ProofArtifact $paths $state 'answer'
        Write-Host 'Encrypted answer validated; the bounded guest continues while it is uploaded.'
        return
    }

    if ($SelectedPhase -eq 'Complete') {
        if ($state.phase -cne 'running') { throw 'Proof guest is not running' }
        Read-NativeOwnership $paths $state | Out-Null
        $supervisor = Get-OwnedSupervisorProcess $state
        if ($null -ne $supervisor -and -not $supervisor.WaitForExit((Get-ProofRemainingMilliseconds $state))) {
            throw 'Packaged guest deadline expired'
        }
        Assert-NativeExit $paths $state | Out-Null
        if ($null -ne (Get-OwnedProofProcess $state)) { throw 'Native probe still running after exit receipt' }
        Publish-ProofArtifact $paths $state 'report'
        $report = Read-ProofJson (Join-Path $paths.public 'report.json')
        Assert-ProofPassed $report
        if ($state.game_hands -gt 0 -and (-not $report.ContainsKey('poker') -or $report.poker.hands -ne $state.game_hands)) {
            throw 'Requested poker hands did not complete'
        }
        $state.phase = 'completed'
        Write-ProofJson $state $paths.state
        Write-Host 'Packaged guest report validated.'
        return
    }

    # Cancellation is cooperative first. A fallback termination is limited
    # to the exact recorded executable + creation-time pair from this run.
    $forced = $false
    if (Test-Path -LiteralPath (Join-Path $paths.private 'process.json')) {
        Read-NativeOwnership $paths $state | Out-Null
    }
    $supervisor = Get-OwnedSupervisorProcess $state
    $process = Get-OwnedProofProcess $state
    if ($null -ne $process -or $null -ne $supervisor) {
        Set-Content -LiteralPath $paths.cancel -Value 'cancel' -Encoding ascii
        if ($null -ne $supervisor) {
            $supervisor.WaitForExit(10000) | Out-Null
        } elseif ($null -ne $process) {
            $process.WaitForExit(10000) | Out-Null
        }
        # Startup may have completed while cancellation was being requested.
        if (Test-Path -LiteralPath (Join-Path $paths.private 'process.json')) {
            Read-NativeOwnership $paths $state | Out-Null
        }
        $process = Get-OwnedProofProcess $state
        if ($null -ne $process) {
            $process = Get-OwnedProofProcess $state
            if ($null -ne $process) {
                $process.Kill()
                $forced = $true
                if (-not $process.WaitForExit(5000)) { throw 'Owned proof process did not close' }
            }
        }
        $supervisor = Get-OwnedSupervisorProcess $state
        if ($null -ne $supervisor -and -not $supervisor.WaitForExit(5000)) {
            $supervisor = Get-OwnedSupervisorProcess $state
            if ($null -ne $supervisor) {
                $supervisor.Kill()
                $forced = $true
                if (-not $supervisor.WaitForExit(5000)) { throw 'Owned supervisor did not close' }
            }
        }
        if ($null -ne (Get-OwnedProofProcess $state) -or $null -ne (Get-OwnedSupervisorProcess $state)) {
            throw 'Owned proof cleanup could not confirm process closure'
        }
    }
    $publicStatus = @{
        schema = 'direct-ice-workflow-status-v1'
        source_commit = $env:ICE_SOURCE_REF
        run_id = $env:GITHUB_RUN_ID
        phase = $state.phase
        owned_process_closed = $true
        supervisor_closed = $true
        native_exit_code = $null
        forced_stop = $forced
    }
    if (Test-Path -LiteralPath (Join-Path $paths.private 'exit.json')) {
        $nativeExit = Read-NativeExit $paths $state
        $publicStatus.native_exit_code = $nativeExit.exit_code
        if ($nativeExit.forced_stop) { $publicStatus.forced_stop = $true; $forced = $true }
    }
    Write-ProofJson $publicStatus (Join-Path $paths.public 'status.json')
    if ((Test-Path -LiteralPath (Join-Path $paths.private 'report.json')) -and
        -not (Test-Path -LiteralPath (Join-Path $paths.public 'report.json'))) {
        # Start can fail before an answer exists. Keep its fixed, validated
        # failure code without ever publishing the private diagnostics.
        Publish-ProofArtifact $paths $state 'report'
    }
    Write-Host 'Owned proof cleanup finished; private runner files are not uploaded.'
    if ($forced) { throw 'The packaged guest required forced termination' }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-ProofGuest $Phase
}
