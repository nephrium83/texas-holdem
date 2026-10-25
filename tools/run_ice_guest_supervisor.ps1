param([Parameter(Mandatory = $true)][string] $RequestFile)

# A one-use supervisor owns the native Process handle across workflow steps.
# The request and receipts stay private. No command text is evaluated.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-NewReceipt($Value, [string] $Path) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
        # File.Move without overwrite publishes a complete, exclusive receipt.
        [IO.File]::Move($temporary, $Path)
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

function Assert-RequestChild([string] $Path, [string] $Root) {
    if (-not [IO.Path]::IsPathFullyQualified($Path)) { throw 'Absolute private path required' }
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Supervisor path escaped the owned proof directory'
    }
    return $full
}

$native = $null
$transcribing = $false
try {
    if (-not $env:RUNNER_TEMP -or -not [IO.Path]::IsPathFullyQualified($env:RUNNER_TEMP)) {
        throw 'Absolute runner directory required'
    }
    $root = Join-Path $env:RUNNER_TEMP 'direct-ice-proof'
    $private = Join-Path $root 'private'
    Start-Transcript -LiteralPath (Join-Path $private 'supervisor.session.log') | Out-Null
    $transcribing = $true
    $requestPath = Assert-RequestChild $RequestFile $private
    $item = Get-Item -LiteralPath $requestPath
    if ($item.PSIsContainer -or $item.Length -gt 32768) { throw 'Invalid supervisor request size' }
    $request = Get-Content -LiteralPath $requestPath -Raw | ConvertFrom-Json -AsHashtable
    $fields = @('schema', 'nonce', 'executable', 'arguments', 'deadline_utc')
    if ($request.Count -ne $fields.Count -or @($request.Keys | Where-Object { $_ -notin $fields }).Count -ne 0 -or
        $request.schema -cne 'direct-ice-supervisor-request-v1' -or
        $request.nonce -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid supervisor request schema' }
    $executable = Assert-RequestChild $request.executable (Join-Path $root 'package')
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'Native probe missing' }
    if ($request.arguments -isnot [array] -or $request.arguments.Count -gt 30 -or
        @($request.arguments | Where-Object { $_ -isnot [string] -or $_.Length -gt 4096 -or $_.Contains('"') }).Count -ne 0) {
        throw 'Invalid supervisor arguments'
    }
    $deadline = [DateTimeOffset]::Parse($request.deadline_utc)
    if (($deadline - [DateTimeOffset]::UtcNow).TotalSeconds -gt 300) { throw 'Unbounded supervisor deadline' }
    $cancel = Join-Path $root 'cancel'
    if ((Test-Path -LiteralPath $cancel) -or $deadline -le [DateTimeOffset]::UtcNow) {
        throw 'Proof already cancelled or expired'
    }
    $quoted = @($request.arguments | ForEach-Object { '"' + $_ + '"' })
    $native = Start-Process -FilePath $executable -ArgumentList $quoted -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $private 'guest.stdout.log') `
        -RedirectStandardError (Join-Path $private 'guest.stderr.log')
    $ownership = @{
        schema = 'direct-ice-owned-process-v1'; nonce = $request.nonce
        executable = $executable; process_id = $native.Id
        process_start_ticks = $native.StartTime.ToUniversalTime().Ticks.ToString()
    }
    Write-NewReceipt $ownership (Join-Path $private 'process.json')
    # The CLI reserves cleanup time inside its advertised 300-second budget.
    # Do not restart or extend that deadline between workflow steps.
    $remaining = [int][Math]::Max(0, [Math]::Min(300000,
        ($deadline - [DateTimeOffset]::UtcNow).TotalMilliseconds))
    $forced = $false
    if (-not $native.WaitForExit($remaining)) {
        # Unexpected deadline failure gets bounded cooperative cleanup before
        # this exact owned process is forced closed; it cannot pass the gate.
        Set-Content -LiteralPath $cancel -Value 'cancel' -Encoding ascii
        if (-not $native.WaitForExit(10000)) {
            # This is the exact Process object launched here, never a PID lookup.
            $native.Kill()
            $forced = $true
            if (-not $native.WaitForExit(5000)) { throw 'Owned native probe did not close' }
        }
    }
    Write-NewReceipt @{
        schema = 'direct-ice-native-exit-v1'; nonce = $request.nonce
        process_id = $ownership.process_id; process_start_ticks = $ownership.process_start_ticks
        exit_code = $native.ExitCode; forced_stop = $forced
    } (Join-Path $private 'exit.json')
    if ($forced -or $native.ExitCode -ne 0) { exit 1 }
    exit 0
} catch {
    # Never abandon the native process if writing its ownership/exit receipt fails.
    if ($null -ne $native -and -not $native.HasExited) {
        $native.Kill()
        $native.WaitForExit(5000) | Out-Null
    }
    Write-Error 'Owned proof supervisor failed; private details withheld.'
    exit 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
}
