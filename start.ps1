# Starts all services in the background (logs in .run\). Usage:  .\start.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No .venv found. Run: python -m venv .venv ; .venv\Scripts\python -m pip install -e '.[dev]'" }
New-Item -ItemType Directory -Force .run | Out-Null
if (Test-Path .run\pids.txt) { & "$PSScriptRoot\stop.ps1" }

$env:JWT_SECRET = "demo-secret-demo-secret-demo-secret-123"
$env:DOWNSTREAM_URL = "http://127.0.0.1:9001/mcp"
$env:MOCK_LLM_DELAY_MS = "300"
$env:DATABASE_PATH = Join-Path $PSScriptRoot ".run\gateway.db"
Remove-Item "$env:DATABASE_PATH*" -ErrorAction SilentlyContinue

$services = @(
  @{ name = "mcp-downstream";   port = 9001; args = "-m uvicorn task2_mcp_gateway.mock_downstream:app --port 9001 --log-level warning" },
  @{ name = "mcp-gateway";      port = 9000; args = "-m task2_mcp_gateway.app" },
  @{ name = "mock-llm";         port = 9101; args = "-m uvicorn task3_llm_guardrail.mock_llm:app --port 9101 --log-level warning" },
  @{ name = "llm-gateway";      port = 9100; args = "-m task3_llm_guardrail.app" },
  @{ name = "provider-primary"; port = 9301; args = "-m uvicorn task4_model_router.mock_providers:primary_app --port 9301 --log-level warning" },
  @{ name = "provider-secondary"; port = 9302; args = "-m uvicorn task4_model_router.mock_providers:secondary_app --port 9302 --log-level warning" },
  @{ name = "model-router";     port = 9300; args = "-m task4_model_router.app" }
)

$pids = @()
foreach ($s in $services) {
  $p = Start-Process $py -ArgumentList $s.args -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput ".run\$($s.name).out.log" -RedirectStandardError ".run\$($s.name).err.log"
  $pids += $p.Id
  $s.pid = $p.Id
}
$pids | Set-Content .run\pids.txt

foreach ($s in $services) {
  $up = $false
  for ($i = 0; $i -lt 40 -and -not $up; $i++) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $s.port); $c.Close(); $up = $true }
    catch { Start-Sleep -Milliseconds 250 }
  }
  if ($up) { "{0,-20} http://127.0.0.1:{1}  OK" -f $s.name, $s.port }
  else { "{0,-20} port {1}  FAILED - see .run\{2}.err.log" -f $s.name, $s.port, $s.name }
}
"`nRun .\demo.ps1 to try it, .\stop.ps1 to stop everything."
