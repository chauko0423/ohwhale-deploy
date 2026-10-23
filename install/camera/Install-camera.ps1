param([switch]$CheckOnly)
. (Join-Path $PSScriptRoot 'camera-common.ps1')
try {
    $Plan = Get-CameraInstallPlan
    if ($CheckOnly) {
        $Plan | ConvertTo-Json -Depth 4
        exit 0
    }
    Write-Host 'Installing the unsigned OhWhale camera candidate. Accept the Windows administrator prompt to register its media source.'
    $Arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + (Join-Path $PSScriptRoot 'Register-camera.ps1') + '"'))
    $Registration = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $Arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($Registration.ExitCode -ne 0) {
        $ErrorFile = Join-Path $PSScriptRoot 'camera-registration-error.txt'
        if (Test-Path -LiteralPath $ErrorFile) { throw (Get-Content -LiteralPath $ErrorFile -Raw -Encoding UTF8) }
        throw 'Camera registration did not complete.'
    }
    if ((Get-CameraRegistration ([Microsoft.Win32.RegistryHive]::LocalMachine)) -ne $Plan.dll) { throw 'Camera registration verification failed.' }
    # Run this part under the original user, because the virtual camera belongs
    # to that user. Do not set camera privacy permissions or start screen capture.
    & (Join-Path $Plan.target 'CameraEmulator.exe') --install --layout fit
    if ($LASTEXITCODE -ne 0) { throw 'Camera creation failed. See the message above; QQ video has not been verified.' }
    Write-Host 'CameraEmulator is installed for this user. Select it in QQ and verify the image before enabling the avatar.'
    Write-Host 'No screen capture, auto-start entry, or camera privacy setting was enabled.'
    exit 0
} catch {
    Write-Host ('BLOCKED: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
