param()
. (Join-Path $PSScriptRoot 'common.ps1')
& $ProjectPython -m ohwhale.astrbot_setup
exit $LASTEXITCODE
