# Run only in a preparation environment with Internet access. Runtime is offline.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
uv python install 3.12.13 --install-dir .runtime/python --no-bin --no-registry --cache-dir .runtime/uv-cache
if ($LASTEXITCODE -ne 0) { throw 'Python 準備失敗' }
if (-not (Test-Path -LiteralPath '.runtime/semantic/Scripts/python.exe')) {
    uv venv .runtime/semantic --python .runtime/python/cpython-3.12.13-windows-x86_64-none/python.exe --no-python-downloads --cache-dir .runtime/uv-cache
    if ($LASTEXITCODE -ne 0) { throw '模型環境建立失敗' }
}
uv pip install --python .runtime/semantic/Scripts/python.exe torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cpu --cache-dir .runtime/uv-cache
if ($LASTEXITCODE -ne 0) { throw 'CPU 推論套件安裝失敗' }
uv pip install --python .runtime/semantic/Scripts/python.exe --no-deps -r requirements-semantic.txt --cache-dir .runtime/uv-cache
if ($LASTEXITCODE -ne 0) { throw '中文模型套件安裝失敗' }
New-Item -ItemType Directory -Force models/chinese-clip-rn50 | Out-Null
$semanticWeights = Join-Path $PSScriptRoot 'models/chinese-clip-rn50/clip_cn_rn50.pt'
$semanticExpected = 'B196EE3EE528B70BE1158AB1AAFB1D2F1C801AD2D9FFB3BAE31B0D305F82FC88'
if (-not (Test-Path -LiteralPath $semanticWeights) -or (Get-FileHash -LiteralPath $semanticWeights).Hash -ne $semanticExpected) {
    curl.exe -L --fail --retry 2 --max-time 600 'https://huggingface.co/OFA-Sys/chinese-clip-rn50/resolve/717ba215769231e53b9b7c6b9d329b9cc5944418/clip_cn_rn50.pt' -o ($semanticWeights + '.partial')
    if ($LASTEXITCODE -ne 0) { throw '權重下載失敗' }
    if ((Get-FileHash -LiteralPath ($semanticWeights + '.partial')).Hash -ne $semanticExpected) { throw '權重 SHA-256 不符，停止安裝' }
    Move-Item -LiteralPath ($semanticWeights + '.partial') -Destination $semanticWeights -Force
}
curl.exe -L --fail --silent --show-error 'https://huggingface.co/OFA-Sys/chinese-clip-rn50/resolve/717ba215769231e53b9b7c6b9d329b9cc5944418/README.md' -o models/chinese-clip-rn50/README.upstream.md
if ($LASTEXITCODE -ne 0) { throw '模型說明下載失敗' }
Write-Output '模型環境已準備完成；可斷網後啟動本機系統與內容索引。'
