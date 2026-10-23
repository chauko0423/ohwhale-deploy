param()
. (Join-Path $PSScriptRoot 'common.ps1')
& $ProjectPython -m ohwhale.runtime ll-client
exit $LASTEXITCODE
