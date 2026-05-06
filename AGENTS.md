# AGENTS.md

## 项目定位

这是一个 Windows 自动更新工具，主程序是 `main.py`，应用名为 `YonghengUpdater`。项目用于从配置中的 HTTP 目录或 manifest 下载文件，替换到目标目录，并启动目标程序。

## 技术栈

- Python 3
- Windows API / ctypes
- PowerShell / Windows 命令行
- Windows 注册表
- PyInstaller

## 重要文件

- `main.py`：主程序，包含配置解析、进程控制、HTTP 下载、路径校验、文件替换、旧开机自启清理和入口逻辑。
- `updater_config.json`：默认任务配置，运行前应确认远端地址和本地目标目录。
- `app.ico`：程序图标，打包时使用。
- `requirements.txt`：打包依赖，当前包含 PyInstaller。
- `README.md`：给用户看的运行、配置和打包说明。
- `build_exe.ps1`：打包脚本，用于生成单文件 Windows exe。
- `.gitignore`：忽略虚拟环境、构建产物、日志和临时文件。

## 高风险操作

本项目会修改本地运行环境，维护时必须谨慎：

- 结束进程，尤其是 `taskkill /F /T /IM`。
- 下载远端文件并覆盖本地目标文件。
- 处理自更新场景时生成延迟替换脚本。
- 删除旧版本遗留的开机自启注册表项。
- 清理临时暂存目录。

新增或修改这些能力时必须满足：

- 外部命令必须检查返回码或有后续状态验证。
- 文件写入必须先通过目标目录越界校验。
- 配置读取失败或校验失败时不要静默切换到其他更新任务。
- 清理临时文件前必须确认路径位于本应用创建的临时目录内。
- 不要静默覆盖用户已有配置。

## 运行

```powershell
.\.venv\Scripts\python.exe main.py
```

## 依赖安装

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

运行逻辑仅依赖 Python 标准库；`requirements.txt` 中的 PyInstaller 用于打包。

## 打包

```powershell
.\build_exe.ps1
```

脚本中的完整命令为：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --onefile --name YonghengUpdater --icon "app.ico" "main.py"
```

输出文件：

```text
dist\YonghengUpdater.exe
```

`build_exe.ps1` 可以提交；`build/`、`dist/` 和 `*.spec` 当前被 `.gitignore` 忽略，不应提交到仓库。

## Git 信息

- 目标主分支：`main`
- 远程仓库：`git@github.com:whitePaperCrane/script_update.git`
- 如果本机 SSH 连接 GitHub 22 端口超时，可以改用 HTTPS 远程或配置 SSH over 443 后再推送。

## 验证建议

修改代码后至少执行：

```powershell
.\.venv\Scripts\python.exe -Wall -c "from pathlib import Path; compile(Path('main.py').read_text(encoding='utf-8'), 'main.py', 'exec'); print('syntax ok')"
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

不要在没有用户明确同意的情况下运行会实际结束业务进程、替换目标目录文件或修改系统状态的任务。

## 删除约束

禁止批量删除文件或目录。

不要使用：

- `del /s`
- `rd /s`
- `rmdir /s`
- `Remove-Item -Recurse`
- `rm -rf`

需要删除文件时，只能一次删除一个明确路径的文件，例如：

```powershell
Remove-Item "C:\path\to\file.txt"
```

如果需要批量删除文件，应停止操作并询问用户，让用户手动删除。
