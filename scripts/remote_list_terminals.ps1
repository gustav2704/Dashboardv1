Get-ChildItem 'C:\Users\Administrator\AppData\Roaming\MetaQuotes\Terminal' -Directory | ForEach-Object { Write-Output $_.Name }
