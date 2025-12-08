Write-Host "Starting Video Downloader..." -ForegroundColor Green
Write-Host ""
python vid.py
Write-Host ""
Write-Host "Press any key to exit..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

