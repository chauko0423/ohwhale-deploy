param([ValidateSet('Prepare','Apply')][string]$Action = 'Prepare', [switch]$NoEditor)
. (Join-Path (Split-Path -Parent $PSScriptRoot) 'install/common.ps1')
Set-Location -LiteralPath $ProjectRoot
if ($Action -eq 'Prepare') {
    & $ProjectPython -m ohwhale.deploy configure
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (-not $NoEditor) {
        & (Join-Path $PSScriptRoot 'account_wizard.ps1')
    }
    exit 0
}
$ReportPath = Join-Path $ProjectRoot 'data/account-setup.json'
$Report = [ordered]@{ schema=1; ok=$false; config_presence_passed=$false; profile_prepared=$false; online_tested=$false; qq_logged_in=$false }
try {
    & $ProjectPython -m ohwhale.deploy verify --stage 3
    if ($LASTEXITCODE -ne 0) { throw 'Local configuration is incomplete. Edit .env and config.json, then retry.' }
    $Report.config_presence_passed = $true
    & $ProjectPython -m ohwhale.astrbot_setup
    if ($LASTEXITCODE -ne 0) { throw 'AstrBot profile preparation failed; keep this window for diagnosis.' }
    $Report.profile_prepared = $true
    $Report.ok = $true
    Write-Host 'Local profile prepared. QQ login, audio routing and online tests are still pending.'
} catch {
    Write-Host ('BLOCKED: ' + $_.Exception.Message)
} finally {
    [IO.Directory]::CreateDirectory((Split-Path -Parent $ReportPath)) | Out-Null
    [IO.File]::WriteAllText($ReportPath, ($Report | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
}
if (-not $Report.ok) { exit 1 }
exit 0
