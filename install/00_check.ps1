param([switch]$Json)
. (Join-Path $PSScriptRoot 'common.ps1')
& $ProjectPython -m ohwhale.deploy verify --stage 0
exit $LASTEXITCODE
