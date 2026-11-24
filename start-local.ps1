param([int]$Port = 8000)
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.vendor/django')) {
    python -m pip install --target .vendor -r requirements-local.txt
    if ($LASTEXITCODE -ne 0) { throw '依賴安裝失敗' }
}
python manage.py migrate
if ($LASTEXITCODE -ne 0) { throw '資料庫初始化失敗' }
python manage.py seed_demo
$contentStop = $null
if ((Test-Path -LiteralPath '.runtime/semantic/Scripts/python.exe') -and (Test-Path -LiteralPath 'models/chinese-clip-rn50/clip_cn_rn50.pt')) {
    $contentStop = Join-Path $PSScriptRoot ('.runtime/semantic-stop-' + [guid]::NewGuid().ToString() + '.signal')
    $contentPython = (Get-Command python).Source
    $contentArguments = 'manage.py semantic_index --watch --stop-file "' + $contentStop + '"'
    Start-Process -FilePath $contentPython -ArgumentList $contentArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput '.runtime/indexer-output.log' -RedirectStandardError '.runtime/indexer-error.log' | Out-Null
}
try { python manage.py runserver "127.0.0.1:$Port" --noreload }
finally { if ($contentStop) { [IO.File]::WriteAllText($contentStop, 'stop') } }
