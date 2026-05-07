# 用途：将当前 Windows 自动更新程序打包为单文件 exe。
# 输出：dist\ScriptUpdater.exe
# 说明：build/、dist/ 和 ScriptUpdater.spec 是本地打包产物，不应提交到 Git。
$ErrorActionPreference = "Stop"

$ProjectRoot = $PSScriptRoot
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "未找到 .venv。请先在项目根目录执行：py -3 -m venv .venv；然后执行：.\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

& $PythonExe -m PyInstaller --onefile --name ScriptUpdater --icon (Join-Path $ProjectRoot "app.ico") (Join-Path $ProjectRoot "main.py")
