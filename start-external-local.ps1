Set-Location -LiteralPath $PSScriptRoot
python manage.py prepare_external
if ($LASTEXITCODE -ne 0) { throw '閘道設定準備失敗' }
python manage-gateway.py migrate
if ($LASTEXITCODE -ne 0) { throw '閘道資料庫初始化失敗' }
$externalStop=Join-Path $PSScriptRoot ('.runtime/external-stop-'+[guid]::NewGuid().ToString()+'.signal')
$externalPython=(Get-Command python).Source
$externalArgs='manage.py external_bridge --watch --stop-file "'+$externalStop+'"'
Start-Process -FilePath $externalPython -ArgumentList $externalArgs -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput '.runtime/external-bridge-output.log' -RedirectStandardError '.runtime/external-bridge-error.log' | Out-Null
Write-Output '本機接收閘道：http://127.0.0.1:8002；內網核心請另以 start-local.ps1 啟動。'
try { python manage-gateway.py runserver 127.0.0.1:8002 --noreload }
finally { [IO.File]::WriteAllText($externalStop,'stop') }
