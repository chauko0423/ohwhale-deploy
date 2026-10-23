param([string]$ReportPath)
# Read-only host inspection. No Python, administrator rights or credentials required.
$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $ReportPath) { $ReportPath = Join-Path $ProjectRoot 'data\machine-preflight.json' }
$OS = Get-CimInstance Win32_OperatingSystem
$System = Get-CimInstance Win32_ComputerSystem
$GPUs = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [ordered]@{ name = $_.Name; driver = $_.DriverVersion }
})
$Nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $Nvidia) {
    $NvidiaPath = Join-Path $env:WINDIR 'System32\nvidia-smi.exe'
    if (Test-Path -LiteralPath $NvidiaPath) { $Nvidia = Get-Item -LiteralPath $NvidiaPath }
}
$CUDA = @()
if ($Nvidia) {
    $Exe = if ($Nvidia.Source) { $Nvidia.Source } else { $Nvidia.FullName }
    $Lines = & $Exe --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0) {
        $CUDA = @($Lines | ForEach-Object {
            $Values = $_ -split ',\s*'
            [ordered]@{ name = $Values[0]; vram_mib = [int]$Values[1]; driver = $Values[2] }
        })
    }
}
$Pythons = @()
$Launcher = Get-Command py -ErrorAction SilentlyContinue
if ($Launcher) {
    $Found = & $Launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
    if ($LASTEXITCODE -eq 0) { $Pythons += $Found }
}
$Pythons += @(Get-Command python, python3 -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
$Pythons += @(Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
$Versions = @()
foreach ($Python in ($Pythons | Select-Object -Unique)) {
    if (-not $Python -or $Python -match '\\WindowsApps\\') { continue }
    $Version = & $Python -c 'import json,sys; print(json.dumps(dict(version=sys.version.split()[0],bits=64 if sys.maxsize>2**32 else 32)))' 2>$null
    if ($LASTEXITCODE -eq 0) { $Versions += ($Version | ConvertFrom-Json) }
}
$Drive = Get-CimInstance Win32_LogicalDisk | Where-Object { $ProjectRoot.StartsWith($_.DeviceID, [StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1
$Audio = @(Get-CimInstance Win32_SoundDevice | ForEach-Object { $_.Name })
$Ports = @(6185, 6199, 9880, 8899 | ForEach-Object {
    $Port = $_
    $Busy = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue).Count -gt 0
    [ordered]@{ port = $Port; occupied = $Busy }
})
$Report = [ordered]@{
    schema = 1; recorded_at = (Get-Date).ToUniversalTime().ToString('o')
    scope = 'hardware-and-prerequisites-only'; fresh_install_passed = $false
    os = [ordered]@{ caption = $OS.Caption; build = [int]$OS.BuildNumber; architecture = $OS.OSArchitecture }
    ram_gib = [math]::Round($System.TotalPhysicalMemory / 1GB, 1)
    install_disk_free_gib = [math]::Round($Drive.FreeSpace / 1GB, 1)
    display_adapters = $GPUs; nvidia = $CUDA; python = $Versions
    audio_devices = $Audio; ports = $Ports
    notes = @('No account names, host names, IP addresses, API keys or login data are collected.', 'GPU memory and installed drivers do not establish TTS latency or successful QQ audio routing.')
}
$OutputPath = [System.IO.Path]::GetFullPath($ReportPath)
New-Item -ItemType Directory -Path ([System.IO.Path]::GetDirectoryName($OutputPath)) -Force | Out-Null
$JSON = $Report | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($OutputPath, $JSON, [System.Text.UTF8Encoding]::new($false))
Write-Output $JSON
Write-Host "Saved: $OutputPath"
