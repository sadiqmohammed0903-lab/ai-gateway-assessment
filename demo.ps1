# Exercises all three gateways. Run .\start.ps1 first.
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$env:JWT_SECRET = "demo-secret-demo-secret-demo-secret-123"
$json = "application/json"

"=== Task 2: MCP gateway ==="
$viewer = & $py -m task2_mcp_gateway.mint_token --role viewer
$admin = & $py -m task2_mcp_gateway.mint_token --role admin
$call = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"admin_reset_key"}}'
"viewer calls admin_reset_key:"
Invoke-RestMethod -Method Post http://127.0.0.1:9000/mcp -Headers @{Authorization = "Bearer $viewer"} -ContentType $json -Body $call | ConvertTo-Json -Depth 5 -Compress
"admin calls admin_reset_key:"
Invoke-RestMethod -Method Post http://127.0.0.1:9000/mcp -Headers @{Authorization = "Bearer $admin"} -ContentType $json -Body $call | ConvertTo-Json -Depth 6 -Compress

"`n=== Task 3: streaming PII redaction (chunks arrive about 0.3s apart) ==="
'{"model":"m","messages":[{"role":"user","content":"hi"}],"stream":true}' | Set-Content .run\chat.json -Encoding ascii
curl.exe -N -s -X POST http://127.0.0.1:9100/v1/chat/completions -H "Content-Type: application/json" --data-binary "@.run\chat.json" |
  Where-Object { $_ -match '"content":"[^"]' } | ForEach-Object { ($_ -replace '.*"content":', 'content=' -replace '\},"finish_reason".*', '') }

"`n=== Task 4: rate limit and failover ==="
function Ask($label, $prompt, $maxTokens) {
  @{ prompt = $prompt; max_tokens = $maxTokens } | ConvertTo-Json -Compress | Set-Content .run\req.json -Encoding ascii
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $out = curl.exe -s -i -X POST http://127.0.0.1:9300/v1/completions -H "Content-Type: application/json" -H "X-API-Key: demo-key-tenant-a" --data-binary "@.run\req.json"
  $sw.Stop()
  $status = ($out | Select-String '^HTTP/' | Select-Object -First 1).Line.Trim()
  $hdr = ($out | Select-String -Pattern '^(x-gateway-provider|retry-after):' | ForEach-Object { $_.Line.Trim() }) -join '  '
  "{0,-16} {1}  [{2} ms]  {3}" -f $label, $status, [int]$sw.Elapsed.TotalMilliseconds, $hdr
}
function SetMode($mode) { curl.exe -s -X POST http://127.0.0.1:9301/_mode -H "Content-Type: application/json" -d ('{\"mode\":\"' + $mode + '\"}') | Out-Null }

Ask "normal" "hello" 100
SetMode "slow"
Ask "primary slow" "hello" 100
SetMode "rate_limited"
Ask "primary 429s" "hello" 100
SetMode "full_usage"
foreach ($i in 1..3) { Ask "big request $i" "story" 30000 }
SetMode "normal"