# 用途：将当前 Windows 自动更新程序打包为单文件 exe。
# 输出：dist\YonghengUpdater.exe
# 说明：build/、dist/ 和 YonghengUpdater.spec 是本地打包产物，不应提交到 Git。
.\.venv\Scripts\python.exe -m PyInstaller --onefile --name YonghengUpdater --icon "app.ico" "main.py"
