param(
    [Parameter(Mandatory=$true)][string]$Profile,
    [string]$TaskName='AgencyPhotoSystem',
    [ValidateSet('Logon','Startup')][string]$Trigger='Logon',
    [switch]$Install
)
$ErrorActionPreference='Stop'
$projectRoot=$PSScriptRoot
$resolvedProfile=(Resolve-Path -LiteralPath $Profile).Path
$python=(Get-Command python.exe).Source
$config=Get-Content -LiteralPath $resolvedProfile -Raw -Encoding UTF8 | ConvertFrom-Json
$runDirectory=$config.run
New-Item -ItemType Directory -Path $runDirectory -Force | Out-Null
$arguments='"'+(Join-Path $projectRoot 'ops.py')+'" run --profile "'+$resolvedProfile+'"'
$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$escapedIdentity=[System.Security.SecurityElement]::Escape($identity)
$escapedPython=[System.Security.SecurityElement]::Escape($python)
$escapedArguments=[System.Security.SecurityElement]::Escape($arguments)
$escapedRoot=[System.Security.SecurityElement]::Escape($projectRoot)
if($Trigger -eq 'Startup') {
    $triggerXml='<BootTrigger><Enabled>true</Enabled></BootTrigger>'
    $logonType='Password'
} else {
    $triggerXml='<LogonTrigger><Enabled>true</Enabled><UserId>'+$escapedIdentity+'</UserId></LogonTrigger>'
    $logonType='InteractiveToken'
}
$xml=@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Internal photo system supervisor</Description></RegistrationInfo>
  <Triggers>$triggerXml</Triggers>
  <Principals><Principal id="Author"><UserId>$escapedIdentity</UserId><LogonType>$logonType</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
  <Actions Context="Author"><Exec><Command>$escapedPython</Command><Arguments>$escapedArguments</Arguments><WorkingDirectory>$escapedRoot</WorkingDirectory></Exec></Actions>
</Task>
"@
[xml]$validatedXml=$xml
$xmlPath=Join-Path $runDirectory 'service-task.xml'
[System.IO.File]::WriteAllText($xmlPath,$xml,[System.Text.Encoding]::Unicode)
Write-Output "Task definition: $xmlPath"
if($Install) {
    if(Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue){throw 'Task already exists. Review and remove or choose another name.'}
    if($Trigger -eq 'Startup') {
        $credential=Get-Credential -UserName $identity -Message 'Use the dedicated deployment account with SMB permissions.'
        Register-ScheduledTask -TaskName $TaskName -Xml $xml -User $credential.UserName -Password $credential.GetNetworkCredential().Password | Out-Null
    } else {Register-ScheduledTask -TaskName $TaskName -Xml $xml | Out-Null}
    Write-Output "Installed: $TaskName. Start-ScheduledTask -TaskName '$TaskName'"
} else {Write-Output 'Preview only. Add -Install to register. Startup requires the deployment account and task registration rights.'}
