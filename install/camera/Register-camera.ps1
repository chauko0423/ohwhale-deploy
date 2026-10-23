param()
. (Join-Path $PSScriptRoot 'camera-common.ps1')
try {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = [Security.Principal.WindowsPrincipal]::new($Identity)
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator rights are required to register the camera source.' }
    $Plan = Get-CameraInstallPlan
    New-Item -ItemType Directory -Path $Plan.target -Force | Out-Null
    foreach ($Name in $Plan.files) {
        $Output = Join-Path $Plan.target $Name
        if (-not (Test-Path -LiteralPath $Output)) {
            [IO.File]::Copy((Join-Path $PSScriptRoot $Name), $Output, $false)
        }
    }
    if ((Get-CameraRegistration ([Microsoft.Win32.RegistryHive]::LocalMachine)) -ne $Plan.dll) {
        $Registration = Start-Process -FilePath (Join-Path $env:WINDIR 'System32\regsvr32.exe') -ArgumentList @('/s', ('"' + $Plan.dll + '"')) -WindowStyle Hidden -Wait -PassThru
        if ($Registration.ExitCode -ne 0) { throw "COM registration failed: $($Registration.ExitCode)" }
    }
    if ((Get-CameraRegistration ([Microsoft.Win32.RegistryHive]::LocalMachine)) -ne $Plan.dll) { throw 'Registered camera source does not match the package.' }
    exit 0
} catch {
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot 'camera-registration-error.txt'), $_.Exception.Message, [Text.UTF8Encoding]::new($false))
    exit 1
}
