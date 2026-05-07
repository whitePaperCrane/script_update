# ScriptUpdater 自动更新工具

一个面向 Windows 的轻量级自动更新脚本，用于按配置下载远端目录或 manifest 中的文件，安全替换到本地目标目录，并在更新结束后启动目标程序。

## 功能

- 支持多个更新任务，每个任务可配置远端地址、目标目录、启动程序和需结束的进程。
- 支持 `autoindex` 目录索引模式和 `manifest.json` 文件清单模式。
- 下载文件先进入临时暂存区，替换时使用临时文件 + `os.replace`，降低半更新风险。
- 对远端相对路径做越界校验，避免写出目标目录。
- 支持配置下载超时、重试、退避时间、User-Agent 和单文件大小上限。
- 支持 `--diagnose` 诊断模式，输出配置、目标路径和启动文件检查结果。
- 主动清理旧版本遗留的开机自启注册表项。

## 环境

运行逻辑仅使用 Python 标准库。打包为 exe 时需要 PyInstaller。

首次克隆或本地没有 `.venv` 时，先在项目根目录创建虚拟环境并安装依赖：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果 `.venv` 已存在，只需要重新安装或更新依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 运行

```powershell
.\.venv\Scripts\python.exe main.py
```

静默模式：

```powershell
.\.venv\Scripts\python.exe main.py --silent
```

诊断模式：

```powershell
.\.venv\Scripts\python.exe main.py --diagnose
```

指定配置文件：

```powershell
.\.venv\Scripts\python.exe main.py --config="E:\path\to\updater_config.json"
```

## 配置

默认读取程序同目录的 `updater_config.json`。如果不存在，会自动创建内置默认配置。已有配置读取失败或校验失败时，程序会停止运行，避免回退到错误的默认任务后误更新其他目录。

关键配置项：

- `network.timeout_sec`：HTTP 请求超时秒数。
- `network.retries`：下载和读取列表的重试次数。
- `network.max_file_bytes`：单个响应或文件的最大字节数。
- `jobs[].source_url`：远端目录地址。
- `jobs[].target_path`：本地目标目录，支持 `%DESKTOP%`。
- `jobs[].listing.mode`：`autoindex` 或 `manifest`。
- `jobs[].exclude`：跳过下载的通配符规则。

## 打包为 exe

后续需要 exe 时，在项目根目录执行：

```powershell
.\build_exe.ps1
```

脚本用途：将当前更新程序打包为单文件 Windows exe。

`build_exe.ps1` 会使用项目根目录下的 `.venv\Scripts\python.exe`。如果 `.venv` 不存在，脚本会提示先执行环境初始化命令。实际打包命令等价于：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --onefile --name ScriptUpdater --icon "app.ico" "main.py"
```

打包产物：

```text
dist\ScriptUpdater.exe
```

`build_exe.ps1` 可以提交到 Git；`build/`、`dist/` 和 `*.spec` 是本地打包产物，不需要提交。

## 注意

本工具会结束目标进程、下载远端文件并替换本地文件。维护配置时请确认 `source_url`、`target_path` 和 `start_executable` 指向正确目标。不要把不可信远端目录配置为更新源。
