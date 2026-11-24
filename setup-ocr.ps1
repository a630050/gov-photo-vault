# Preparation only: install fixed OCR packages into isolated .runtime/ocr, then run completely offline.
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
uv python install 3.12.13 --install-dir .runtime/python --no-bin --no-registry --cache-dir .runtime/uv-cache
if ($LASTEXITCODE -ne 0) { throw 'Python 準備失敗' }
if (-not (Test-Path -LiteralPath '.runtime/ocr/Scripts/python.exe')) {
    uv venv .runtime/ocr --python .runtime/python/cpython-3.12.13-windows-x86_64-none/python.exe --no-python-downloads --cache-dir .runtime/uv-cache
    if ($LASTEXITCODE -ne 0) { throw 'OCR 模型環境建立失敗' }
}
uv pip install --python .runtime/ocr/Scripts/python.exe -r requirements-ocr.txt --cache-dir .runtime/uv-cache
if ($LASTEXITCODE -ne 0) { throw 'OCR 推論套件安裝失敗' }
Write-Output 'RapidOCR 離線環境已準備完成。'
