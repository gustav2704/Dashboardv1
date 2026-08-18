param(
  [Parameter(Mandatory=$true)][string]$HostName,
  [Parameter(Mandatory=$true)][string]$User,
  [Parameter(Mandatory=$true)][string]$Key,
  [Parameter(Mandatory=$true)][string]$ScriptFile
)
$content = Get-Content -Raw -LiteralPath $ScriptFile
$bytes = [System.Text.Encoding]::Unicode.GetBytes($content)
$enc = [System.Convert]::ToBase64String($bytes)
$remoteCmd = "powershell.exe -NoProfile -NonInteractive -EncodedCommand $enc"
$sshArgs = @('-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=20','-i',$Key,"$User@$HostName",$remoteCmd)
& ssh @sshArgs 2>&1
exit $LASTEXITCODE
