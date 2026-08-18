Get-ChildItem 'C:\Users\Administrador\AppData\Roaming\MetaQuotes\Terminal' -Directory | ForEach-Object { Write-Output $_.Name }
