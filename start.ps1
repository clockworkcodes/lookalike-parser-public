# Локальный запуск Lookalike Parser на Windows (двойной клик по start.bat).
# Instagram видит ваш домашний IP — тот же, на котором создана сессия.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$Host.UI.RawUI.WindowTitle = "Lookalike Parser"

function Stop-WithMessage([string]$Text) {
    Write-Host ""
    Write-Host $Text -ForegroundColor Yellow
    Read-Host "Нажмите Enter, чтобы закрыть"
    exit 1
}

# 1. Python 3.10+
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Stop-WithMessage "Не найден Python. Установите Python 3.12 с https://www.python.org/downloads/ и запустите снова."
}
& py -3.12 --version *> $null
if ($LASTEXITCODE -ne 0) {
    Stop-WithMessage "Нужен Python 3.12 (instagrapi не работает на 3.8). Установите его с https://www.python.org/downloads/"
}

# 2. Зависимости
Write-Host "Проверяю зависимости..." -ForegroundColor Cyan
& py -3.12 -m pip install --disable-pip-version-check -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Stop-WithMessage "Не удалось установить зависимости (см. ошибку выше)." }

# 3. Настройки
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Создан файл .env — впишите BOT_TOKEN и ALLOWED_USER_ID, сохраните (Ctrl+S) и закройте Блокнот." -ForegroundColor Cyan
    Start-Process notepad.exe ".env" -Wait
}
$envText = Get-Content ".env" -Raw -Encoding UTF8
if ($envText -notmatch "(?m)^[ \t]*BOT_TOKEN[ \t]*=[ \t]*\S+" -or $envText -notmatch "(?m)^[ \t]*ALLOWED_USER_ID[ \t]*=[ \t]*\d+") {
    Stop-WithMessage "В .env не заполнены BOT_TOKEN и/или ALLOWED_USER_ID. Откройте .env в Блокноте, заполните и запустите снова."
}

# 4. Сессия Instagram
if (-not (Test-Path "session.json")) {
    Write-Host "Нет session.json — запускаю вход в Instagram (прокси дома не нужен, просто Enter)." -ForegroundColor Cyan
    & py -3.12 generate_session.py
    if (-not (Test-Path "session.json")) { Stop-WithMessage "Сессия не создана — см. ошибку выше." }
}

# 5. Mini App через бесплатный туннель Cloudflare (необязательно)
$tunnel = $null
$cloudflared = $null
if (Test-Path ".\cloudflared.exe") {
    $cloudflared = (Resolve-Path ".\cloudflared.exe").Path
} elseif (Get-Command cloudflared -ErrorAction SilentlyContinue) {
    $cloudflared = (Get-Command cloudflared).Source
}

if ($cloudflared) {
    Write-Host "Запускаю туннель для Mini App..." -ForegroundColor Cyan
    $tunnelLog = Join-Path $env:TEMP "lookalike-cloudflared.log"
    Remove-Item $tunnelLog -ErrorAction SilentlyContinue
    $tunnel = Start-Process -FilePath $cloudflared `
        -ArgumentList @("tunnel", "--url", "http://localhost:10000", "--no-autoupdate") `
        -RedirectStandardError $tunnelLog -WindowStyle Hidden -PassThru
    $tunnelUrl = $null
    for ($i = 0; $i -lt 40 -and -not $tunnelUrl; $i++) {
        Start-Sleep -Milliseconds 750
        if (Test-Path $tunnelLog) {
            $match = Select-String -Path $tunnelLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -First 1
            if ($match) { $tunnelUrl = $match.Matches[0].Value }
        }
    }
    if ($tunnelUrl) {
        $env:RENDER_EXTERNAL_URL = $tunnelUrl
        Write-Host "Mini App: $tunnelUrl" -ForegroundColor Green
    } else {
        Write-Host "Туннель не поднялся — Mini App недоступен, команды /parse работают." -ForegroundColor Yellow
    }
} else {
    Write-Host "cloudflared не установлен — Mini App недоступен, команды /parse и /diag работают." -ForegroundColor Yellow
    Write-Host "Чтобы включить Mini App: winget install --id Cloudflare.cloudflared  (и перезапустите start.bat)" -ForegroundColor Yellow
}

# 6. Бот (перезапуск при падении)
Write-Host ""
Write-Host "Бот запускается. Не закрывайте это окно и не отправляйте ПК в сон. Остановить — закрыть окно." -ForegroundColor Green
try {
    while ($true) {
        $started = Get-Date
        & py -3.12 bot.py
        $code = $LASTEXITCODE
        if (((Get-Date) - $started).TotalSeconds -lt 60) {
            Write-Host "Бот остановился сразу после запуска (код $code) — см. ошибку выше." -ForegroundColor Yellow
            Read-Host "Исправьте и нажмите Enter для повторного запуска"
        } else {
            Write-Host "Бот остановился (код $code). Перезапуск через 15 секунд..." -ForegroundColor Yellow
            Start-Sleep -Seconds 15
        }
    }
} finally {
    if ($tunnel -and -not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
}
