# Jinnee OS installer – Windows (PowerShell)
$ErrorActionPreference = "Stop"
Write-Host "`nJinnee OS installer" -ForegroundColor Cyan
$dir = "$env:USERPROFILE\jinnee-os"
if (-not (Get-Command git -ErrorAction SilentlyContinue))    { winget install -e --id Git.Git }
if (-not (Get-Command node -ErrorAction SilentlyContinue))   { winget install -e --id OpenJS.NodeJS.LTS }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { winget install -e --id Python.Python.3.12 }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { npm install -g @anthropic-ai/claude-code }
if (Test-Path "$dir\.git") { git -C $dir pull -q } else { git clone -q https://github.com/<github-user>/jinnee-os.git $dir }
Set-Location $dir
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$token = Read-Host "Telegram bot token"
$owner = Read-Host "Your Telegram user ID"
$name  = Read-Host "What should your agent be called? [Jinnee]"; if (-not $name) { $name = "Jinnee" }
$pack  = Read-Host "Pack: [1] general only  [2] general + ecom"
$packs = if ($pack -eq "2") { "general,ecom" } else { "general" }
(Get-Content .env) -replace '^TELEGRAM_BOT_TOKEN=.*',"TELEGRAM_BOT_TOKEN=$token" `
                   -replace '^TELEGRAM_OWNER_ID=.*',"TELEGRAM_OWNER_ID=$owner" `
                   -replace '^JINNEE_NAME=.*',"JINNEE_NAME=$name" `
                   -replace '^PACKS=.*',"PACKS=$packs" | Set-Content .env
New-Item -ItemType Directory -Force brain,handoffs | Out-Null
$env:PACKS = $packs; python core\pack_loader.py --init
pip install -q -r requirements.txt
claude login
Start-Process python -ArgumentList "core\jinnee.py" -WindowStyle Hidden
Start-Process python -ArgumentList "dashboard\app.py" -WindowStyle Hidden
Write-Host "`nDone. Message $name on Telegram and open: http://localhost:8080" -ForegroundColor Green
Write-Host "Autostart on login: Task Scheduler → python $dir\core\jinnee.py"
