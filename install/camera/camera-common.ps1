$ErrorActionPreference = 'Stop'
$CameraClass = '{9C5E4B7A-3D21-4F8E-A6B0-1E7C52D9F3A4}'
$CameraRegistryPath = "Software\Classes\CLSID\$CameraClass\InprocServer32"

function Get-CameraRegistration([Microsoft.Win32.RegistryHive]$Hive) {
    $Base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($Hive, [Microsoft.Win32.RegistryView]::Registry64)
    try {
        $Key = $Base.OpenSubKey($CameraRegistryPath)
        if ($null -eq $Key) { return $null }
        try { return [string]$Key.GetValue('') } finally { $Key.Dispose() }
    } finally { $Base.Dispose() }
}

function Get-CameraInstallPlan {
    if (-not [Environment]::Is64BitProcess) { throw 'Use 64-bit Windows PowerShell.' }
    if ([Environment]::OSVersion.Version.Build -lt 22000) { throw 'Windows 11 is required.' }
    $Info = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'BUILD_INFO.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $Names = @('CameraEmulator.exe', 'CameraEmulatorTray.exe', 'CameraEmulatorSource.dll', 'SourceTest.exe', 'ReaderTest.exe', 'CameraEmulator-Help.html')
    if (@($Info.files).Count -ne $Names.Count) { throw 'Unexpected camera build manifest.' }
    $ProgramFilesRoot = [Environment]::GetFolderPath('ProgramFiles')
    $Target = Join-Path $ProgramFilesRoot 'OhWhale\CameraEmulator-0.1.0a1'
    $TargetDll = Join-Path $Target 'CameraEmulatorSource.dll'
    foreach ($Hive in @([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryHive]::CurrentUser)) {
        $Existing = Get-CameraRegistration $Hive
        if ($Existing -and -not [string]::Equals($Existing, $TargetDll, [StringComparison]::OrdinalIgnoreCase)) {
            throw "CameraEmulator is already registered from another location. No changes made: $Existing"
        }
    }
    foreach ($Name in $Names) {
        $Entry = @($Info.files | Where-Object { $_.path -ceq $Name })
        if ($Entry.Count -ne 1) { throw "Missing or duplicate camera manifest entry: $Name" }
        $Source = Join-Path $PSScriptRoot $Name
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "Camera package is incomplete: $Name" }
        $File = Get-Item -LiteralPath $Source
        if ($File.Length -ne $Entry[0].size -or (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash -ne $Entry[0].sha256) {
            throw "Camera package checksum failed: $Name"
        }
    }
    $CopyNames = $Names + @('LICENSE', 'BUILD.md', 'BUILD_INFO.json')
    foreach ($Name in $CopyNames) {
        $Source = Join-Path $PSScriptRoot $Name
        if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) { throw "Camera package is incomplete: $Name" }
        $Output = Join-Path $Target $Name
        if ((Test-Path -LiteralPath $Output) -and
            (Get-FileHash -LiteralPath $Output -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash) {
            throw "Existing camera files differ. No files changed: $Output"
        }
    }
    [pscustomobject]@{ target=$Target; dll=$TargetDll; files=$CopyNames; signed=$false; registry_scope='machine'; camera_scope='current-user'; modifies_privacy_settings=$false }
}
