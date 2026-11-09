$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
    & '.venv/Scripts/python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
}
& '.venv/Scripts/python.exe' -u bot.py
