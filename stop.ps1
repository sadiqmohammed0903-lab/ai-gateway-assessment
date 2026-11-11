# Stops everything started by start.ps1
Set-Location $PSScriptRoot
if (Test-Path .run\pids.txt) {
  Get-Content .run\pids.txt | ForEach-Object { Stop-Process -Id ([int]$_) -Force -ErrorAction SilentlyContinue }
  Remove-Item .run\pids.txt
  "Stopped."
} else { "Nothing to stop." }
