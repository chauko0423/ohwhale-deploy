param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:OHWHALE_HOME = $ProjectRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = $ProjectRoot
$ProjectPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ProjectPython)) {
    $Candidates = @()
    if ($env:OHWHALE_PYTHON) { $Candidates += $env:OHWHALE_PYTHON }
    $Candidates += (Join-Path $ProjectRoot 'runtime/Python312/python/python.exe')
    $Launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($Launcher) {
        $Found = & $Launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0) { $Candidates += $Found }
    }
    $Candidates += @(Get-Command python, python3 -ErrorAction SilentlyContinue | ForEach-Object { $_.Source })
    $Candidates += @(Get-ChildItem -Path "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    $ProjectPython = $null
    foreach ($Candidate in ($Candidates | Select-Object -Unique)) {
        if (-not $Candidate -or $Candidate -match '\\WindowsApps\\' -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { continue }
        & $Candidate -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) and sys.maxsize > 2**32 else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $ProjectPython = $Candidate; break }
    }
    if (-not $ProjectPython) { throw 'BLOCKED: Python 3.12 x64 is required. Install it from python.org, or set OHWHALE_PYTHON to its python.exe path. Windows Store aliases are skipped.' }
}
