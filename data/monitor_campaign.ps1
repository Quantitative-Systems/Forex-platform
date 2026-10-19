$p = Get-Process -Id 16584 -ErrorAction SilentlyContinue
while ($p -and -not $p.HasExited) {
    Start-Sleep -Seconds 60
    $p = Get-Process -Id 16584 -ErrorAction SilentlyContinue
}
$timestamp = (Get-Date).ToString('o')
Add-Content -Path "data/campaign_monitor.log" -Value "$timestamp CAMPAIGN FINISHED exit=$($p.ExitCode)"
if (Test-Path "data/campaign_stdout.log") {
    Get-Content "data/campaign_stdout.log" -Tail 10 | Add-Content -Path "data/campaign_monitor.log"
}
Add-Content -Path "data/campaign_monitor.log" -Value "---"
