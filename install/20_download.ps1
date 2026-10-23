param()
. (Join-Path $PSScriptRoot 'common.ps1')
& $ProjectPython -m ohwhale.deploy download
exit $LASTEXITCODE
