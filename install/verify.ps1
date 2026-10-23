param([ValidateRange(0,6)][int]$Stage = 3, [switch]$Json, [switch]$Source)
. (Join-Path $PSScriptRoot 'common.ps1')
if ($Source) {
    & $ProjectPython (Join-Path $ProjectRoot 'scripts\release_check.py')
} else {
    & $ProjectPython -m ohwhale.deploy verify --stage $Stage
}
exit $LASTEXITCODE
