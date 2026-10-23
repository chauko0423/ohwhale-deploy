param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$Catalog = Get-Content -LiteralPath (Join-Path $ProjectRoot 'config/python-runtime.json') -Raw | ConvertFrom-Json
$Target = Join-Path $ProjectRoot 'runtime/Python312'
$Python = Join-Path $Target 'python/python.exe'
$Marker = Join-Path $Target '.ohwhale-python-sha256'
if (Test-Path -LiteralPath $Target) {
    if (-not (Test-Path -LiteralPath $Marker) -or (Get-Content -LiteralPath $Marker -Raw).Trim() -ne $Catalog.sha256) {
        throw 'Existing Python runtime has no matching completion record. No files were changed.'
    }
} else {
    $Archive = Join-Path $ProjectRoot $Catalog.destination
    New-Item -ItemType Directory -Path (Split-Path -Parent $Archive) -Force | Out-Null
    if (-not (Test-Path -LiteralPath $Archive)) {
        $Partial = $Archive + '.part'
        & curl.exe --fail --location --retry 3 --proto '=https' --proto-redir '=https' --output $Partial $Catalog.urls[0]
        if ($LASTEXITCODE -ne 0) { throw 'Python download failed.' }
        if ((Get-Item -LiteralPath $Partial).Length -ne $Catalog.size -or (Get-FileHash -LiteralPath $Partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Catalog.sha256) {
            throw 'Python archive checksum failed.'
        }
        Move-Item -LiteralPath $Partial -Destination $Archive
    }
    if ((Get-Item -LiteralPath $Archive).Length -ne $Catalog.size -or (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Catalog.sha256) {
        throw 'Python archive checksum failed.'
    }
    $Names = & tar.exe -tf $Archive
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Python archive.' }
    foreach ($Name in $Names) {
        if ($Name -notmatch '^python/' -or $Name -match '(^|/)\.\.(/|$)|\\|:') { throw 'Unsafe Python archive entry.' }
    }
    New-Item -ItemType Directory -Path $Target | Out-Null
    & tar.exe -xf $Archive -C $Target
    if ($LASTEXITCODE -ne 0) { throw 'Python archive extraction failed. Incomplete directory retained for inspection.' }
    & $Python -c 'import sys; assert sys.version_info[:3] == (3, 12, 15) and sys.maxsize > 2**32; import venv,ensurepip,ssl'
    if ($LASTEXITCODE -ne 0) { throw 'Python runtime validation failed.' }
    [IO.File]::WriteAllText($Marker, $Catalog.sha256, [Text.UTF8Encoding]::new($false))
}
& $Python -c 'import sys; assert sys.version_info[:3] == (3, 12, 15) and sys.maxsize > 2**32; print(sys.version)'
if ($LASTEXITCODE -ne 0) { throw 'Python runtime validation failed.' }
Write-Host 'Project-local Python is ready.'
