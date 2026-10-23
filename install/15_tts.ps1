param()
. (Join-Path $PSScriptRoot 'common.ps1')
& $ProjectPython -m ohwhale.runtime
if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: pinned TTS source setup failed' }
$TTSPython = Join-Path $ProjectRoot '.venv-tts\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $TTSPython)) {
    & $ProjectPython -m venv (Join-Path $ProjectRoot '.venv-tts')
    if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: TTS venv creation failed' }
}
& $TTSPython -m pip install 'torch==2.9.1+cu128' 'torchaudio==2.9.1+cu128' --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: PyTorch CUDA installation failed' }
& $TTSPython -m pip install -r (Join-Path $ProjectRoot 'requirements-tts.txt')
if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: TTS dependency installation failed' }
& $TTSPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: TTS dependency conflicts detected' }
& $TTSPython -c 'import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else None); raise SystemExit(0 if torch.cuda.is_available() else 1)'
if ($LASTEXITCODE -ne 0) { throw 'BLOCKED: CUDA unavailable; inspect the NVIDIA driver and GPU before starting TTS' }
Write-Host 'TTS sources and dependencies prepared. Download models, then run an actual synthesis test.'
