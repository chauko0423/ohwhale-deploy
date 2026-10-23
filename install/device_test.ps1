param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Data = Join-Path $ProjectRoot 'data'
New-Item -ItemType Directory -Path $Data -Force | Out-Null
$StatusPath = Join-Path $Data 'device-setup.json'
$Report = [ordered]@{ schema=1; started_at=(Get-Date).ToUniversalTime().ToString('o'); completed_steps=@(); ok=$false; full_acceptance_passed=$false; current_step='starting' }
function Save-Status {
    [IO.File]::WriteAllText($StatusPath, ($Report | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
}
function Run-Step([string]$Name, [string]$Script) {
    $Report.current_step=$Name
    Save-Status
    Write-Host "=== $Name ==="
    # Child native output can bypass Start-Transcript on Windows PowerShell 5.1.
    # Route both streams through the host so pip failures survive in the log.
    # Continue is scoped to this native pipeline; its exit code remains decisive.
    $PreviousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $Script) 2>&1 |
            ForEach-Object { Write-Host $_ }
        $StepExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorAction
    }
    if ($StepExitCode -ne 0) { throw "Step failed: $Name (exit $StepExitCode)" }
    $Report.completed_steps += $Name
    Save-Status
}
$ExitCode = 1
Start-Transcript -Path (Join-Path $Data 'device-setup.log') -Append | Out-Null
try {
    Set-Location -LiteralPath $ProjectRoot
    $env:PIP_CACHE_DIR=Join-Path $ProjectRoot 'cache/pip'
    $env:PIP_DISABLE_PIP_VERSION_CHECK='1'
    if (-not $env:PIP_DEFAULT_TIMEOUT) { $env:PIP_DEFAULT_TIMEOUT='120' }
    if (-not $env:PIP_RETRIES) { $env:PIP_RETRIES='10' }
    Run-Step 'hardware-report' '00_machine.ps1'
    $Machine = Get-Content -LiteralPath (Join-Path $Data 'machine-preflight.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Machine.install_disk_free_gib -lt 25) { throw 'At least 25 GiB free space is required.' }
    Run-Step 'project-python' '05_python.ps1'
    Run-Step 'main-dependencies' '10_deps.ps1'
    Run-Step 'tts-dependencies' '15_tts.ps1'
    Run-Step 'qq-client-unpack' '16_qq_client.ps1'
    Run-Step 'models' '20_download.ps1'
    $Report.current_step='tts-synthesis'
    Save-Status
    $env:OHWHALE_HOME=$ProjectRoot
    $env:PYTHONPATH=$ProjectRoot
    $env:PYTHONUTF8='1'
    $env:PYTHONIOENCODING='utf-8'
    & (Join-Path $ProjectRoot '.venv/Scripts/python.exe') -m ohwhale.tts_smoke
    if ($LASTEXITCODE -ne 0) { throw 'TTS synthesis test failed; see data/tts-smoke.json and data/tts-smoke.log.' }
    $Report.completed_steps += 'tts-synthesis'
    $Report.ok=$true
    $Report.current_step='dependencies-and-tts-complete'
    $ExitCode=0
    Write-Host 'Dependency and TTS checks completed. QQ login, audio routing, camera and full acceptance remain pending.'
} catch {
    $Report.error=$_.Exception.Message
    Write-Host ('BLOCKED: ' + $_.Exception.Message) -ForegroundColor Red
} finally {
    $Report.finished_at=(Get-Date).ToUniversalTime().ToString('o')
    Save-Status
    Stop-Transcript | Out-Null
}
exit $ExitCode
