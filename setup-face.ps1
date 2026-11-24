# Preparation only: download fixed official models, then run completely offline.
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
uv python install 3.12.13 --install-dir .runtime/python --no-bin --no-registry --cache-dir .runtime/uv-cache
if ($LASTEXITCODE -ne 0) { throw 'Python 準備失敗' }
if (-not (Test-Path -LiteralPath '.runtime/face/Scripts/python.exe')) {
    uv venv .runtime/face --python .runtime/python/cpython-3.12.13-windows-x86_64-none/python.exe --no-python-downloads --cache-dir .runtime/uv-cache
    if ($LASTEXITCODE -ne 0) { throw '人臉模型環境建立失敗' }
}
uv pip install --python .runtime/face/Scripts/python.exe -r requirements-face.txt --cache-dir .runtime/uv-cache
if ($LASTEXITCODE -ne 0) { throw '人臉推論套件安裝失敗' }
New-Item -ItemType Directory -Force models/face | Out-Null
$faceCommit='48b10fa505aeb51fbd127fc60a5c660040e823b7'
$faceModels=@(
    @{folder='face_detection_yunet';name='face_detection_yunet_2023mar.onnx';sha='8F2383E4DD3CFBB4553EA8718107FC0423210DC964F9F4280604804ED2552FA4'},
    @{folder='face_recognition_sface';name='face_recognition_sface_2021dec.onnx';sha='0BA9FBFA01B5270C96627C4EF784DA859931E02F04419C829E83484087C34E79'}
)
foreach($faceModel in $faceModels) {
    $faceDestination=Join-Path $PSScriptRoot ('models/face/'+$faceModel.name)
    if (-not (Test-Path -LiteralPath $faceDestination) -or (Get-FileHash -LiteralPath $faceDestination).Hash -ne $faceModel.sha) {
        $faceUrl='https://media.githubusercontent.com/media/opencv/opencv_zoo/'+$faceCommit+'/models/'+$faceModel.folder+'/'+$faceModel.name
        curl.exe -L --fail --retry 2 --max-time 600 $faceUrl -o ($faceDestination+'.partial')
        if ($LASTEXITCODE -ne 0) { throw '模型下載失敗' }
        if ((Get-FileHash -LiteralPath ($faceDestination+'.partial')).Hash -ne $faceModel.sha) { throw '模型 SHA-256 不符，停止安裝' }
        Move-Item -LiteralPath ($faceDestination+'.partial') -Destination $faceDestination -Force
    }
    $faceLicense='https://raw.githubusercontent.com/opencv/opencv_zoo/'+$faceCommit+'/models/'+$faceModel.folder+'/LICENSE'
    curl.exe -L --fail --silent --show-error $faceLicense -o ('models/face/'+$faceModel.folder+'.LICENSE')
    if ($LASTEXITCODE -ne 0) { throw '授權檔案下載失敗' }
}
Write-Output 'YuNet 與 SFace 離線環境已準備完成；人物專用權限仍需另授。'
