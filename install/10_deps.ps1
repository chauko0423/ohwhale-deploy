param([switch]$Video)
. (Join-Path $PSScriptRoot 'common.ps1')
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.venv\Scripts\python.exe'))) {
    & $ProjectPython -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: stage1 venv creation failed' }
}
$ProjectPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
& $ProjectPython -m pip install -r (Join-Path $ProjectRoot 'requirements.txt') -e $ProjectRoot
if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: stage1 dependency installation failed' }
& $ProjectPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: stage1 dependency conflicts detected' }
if ($Video) {
    & $ProjectPython -m venv (Join-Path $ProjectRoot '.venv-video')
    if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: stage1 video venv creation failed' }
    $VideoPython = Join-Path $ProjectRoot '.venv-video\Scripts\python.exe'
    & $VideoPython -m pip install -r (Join-Path $ProjectRoot 'requirements-video.txt') -e $ProjectRoot
    if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: stage1 video dependencies failed' }
}
& $ProjectPython -m ohwhale.deploy verify --stage 1
exit $LASTEXITCODE
