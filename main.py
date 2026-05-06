from __future__ import annotations

import ctypes
import csv
import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


# -----------------------------
# 常量定义
# -----------------------------

APP_NAME = "ScriptUpdater"
APP_VERSION = "1.1.1"
DEFAULT_CONFIG_FILENAME = "updater_config.json"
LOCK_FILENAME = "updater.lock"
DEFAULT_STARTUP_VALUE_NAME = "ScriptUpdater"
PERCENT_RE = re.compile(r"%[0-9A-Fa-f]{2}")

DEFAULT_CONFIG = {
    "network": {
        "timeout_sec": 25,
        "retries": 5,
        "backoff_sec": 1.6,
        "user_agent": f"{APP_NAME}/{APP_VERSION}",
        "max_file_bytes": 2_147_483_648,
    },
    "jobs": [
        {
            "name": "MY",
            "kill_processes": ["Myth.exe"],
            "start_executable": "Myth.exe",
            "source_url": "http://129.28.62.139:6666/yongheng2/MY更新/",
            "target_path": "%DESKTOP%/yongheng2/MY",
            "listing": {"mode": "autoindex", "manifest_url": "", "max_depth": 10},
            "exclude": ["*.log", "*.tmp", "*.part"],
            "start_on_failure": True,
        },
        {
            "name": "SZ韩服",
            "kill_processes": ["SzHF.exe"],
            "start_executable": "Sz韩/SzHF.exe",
            "source_url": "http://129.28.62.139:6666/yongheng2/SZ更新/",
            "target_path": "%DESKTOP%/yongheng2/SZ",
            "listing": {"mode": "autoindex", "manifest_url": "", "max_depth": 10},
            "exclude": ["*.log", "*.tmp", "*.part"],
            "start_on_failure": True,
        },
    ],
}

_WINDOWS_INVALID_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


# -----------------------------
# 数据模型
# -----------------------------

@dataclass
class NetworkConfig:
    timeout_sec: int = 25
    retries: int = 5
    backoff_sec: float = 1.6
    user_agent: str = f"{APP_NAME}/{APP_VERSION}"
    max_file_bytes: int = 2_147_483_648


@dataclass
class ListingConfig:
    mode: str = "autoindex"
    manifest_url: str = ""
    max_depth: int = 10


@dataclass
class JobConfig:
    name: str
    kill_processes: List[str]
    start_executable: str
    source_url: str
    target_path: str
    listing: ListingConfig = field(default_factory=ListingConfig)
    exclude: List[str] = field(default_factory=list)
    start_on_failure: bool = True


@dataclass
class LegacyStartupConfig:
    enabled: bool = False
    method: str = "registry_run_key"
    value_name: str = DEFAULT_STARTUP_VALUE_NAME
    arguments: str = "--silent"


@dataclass
class AppConfig:
    network: NetworkConfig
    jobs: List[JobConfig]
    legacy_startup: LegacyStartupConfig = field(default_factory=LegacyStartupConfig)


@dataclass(frozen=True)
class FileEntry:
    remote_rel: str
    local_rel: str


# -----------------------------
# 通用工具
# -----------------------------

class WindowsApi:
    """封装 Windows 相关能力，避免平台细节散落在业务代码里。"""

    @staticmethod
    def is_windows() -> bool:
        return os.name == "nt"

    @staticmethod
    def get_known_folder_path(folder_id: str) -> Path:
        if not WindowsApi.is_windows():
            raise RuntimeError("Known folder lookup is only supported on Windows.")

        folder_guids = {
            "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
            "LocalAppData": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
        }
        guid_str = folder_guids.get(folder_id)
        if not guid_str:
            raise ValueError(f"Unsupported folder_id: {folder_id}")

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def _guid_from_string(s: str) -> GUID:
            ole32 = ctypes.windll.ole32
            guid = GUID()
            hr = ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(guid))
            if hr != 0:
                raise OSError(f"CLSIDFromString failed: {hr}")
            return guid

        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        path_ptr = ctypes.c_wchar_p()
        guid = _guid_from_string(guid_str)

        sh_get_known_folder_path = shell32.SHGetKnownFolderPath
        sh_get_known_folder_path.argtypes = [ctypes.POINTER(GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        sh_get_known_folder_path.restype = ctypes.c_long

        hr = sh_get_known_folder_path(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        if hr != 0:
            raise OSError(f"SHGetKnownFolderPath failed: {hr}")

        try:
            return Path(path_ptr.value)
        finally:
            ole32.CoTaskMemFree(path_ptr)

    @staticmethod
    def get_app_base_dir() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    @staticmethod
    def ensure_dir_writable(dir_path: Path) -> bool:
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            probe = dir_path / f".writable_probe_{os.getpid()}.tmp"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    @staticmethod
    def expand_macros(raw_path: str) -> Path:
        value = (raw_path or "").strip()
        if "%DESKTOP%" in value:
            desktop = WindowsApi.get_known_folder_path("Desktop")
            value = value.replace("%DESKTOP%", str(desktop))
        value = os.path.expandvars(value)
        value = value.replace("/", os.sep)
        return Path(value).expanduser().resolve()

    @staticmethod
    def resolve_config_path(cli_config: str) -> Path:
        if cli_config.strip():
            return Path(cli_config).expanduser().resolve()

        base_dir = WindowsApi.get_app_base_dir()
        exe_side = (base_dir / DEFAULT_CONFIG_FILENAME).resolve()
        if exe_side.exists():
            return exe_side

        lad = (WindowsApi.get_known_folder_path("LocalAppData") / APP_NAME).resolve()
        lad_cfg = (lad / DEFAULT_CONFIG_FILENAME).resolve()
        if lad_cfg.exists():
            return lad_cfg

        if WindowsApi.ensure_dir_writable(base_dir):
            return exe_side

        lad.mkdir(parents=True, exist_ok=True)
        return lad_cfg

    @staticmethod
    def get_log_dir() -> Path:
        if WindowsApi.is_windows():
            base = WindowsApi.get_known_folder_path("LocalAppData")
        else:
            base = Path.home()
        return (base / APP_NAME / "logs").resolve()

    @staticmethod
    def get_runtime_dir() -> Path:
        return (WindowsApi.get_known_folder_path("LocalAppData") / APP_NAME).resolve()

    @staticmethod
    def current_binary_path() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve()
        return Path(__file__).resolve()


class LoggerFactory:
    @staticmethod
    def create(silent: bool) -> logging.Logger:
        log_dir = WindowsApi.get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "updater.debug.log"

        logger = logging.getLogger(APP_NAME)
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        logger.propagate = False

        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(file_handler)

        if not silent:
            console = logging.StreamHandler(stream=sys.stdout)
            console.setLevel(logging.INFO)
            console.setFormatter(logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%H:%M:%S",
            ))
            logger.addHandler(console)

        logger.debug("日志初始化完成。log_file=%s", log_file)
        return logger


class RetryPolicy:
    @staticmethod
    def get_sleep_seconds(backoff_sec: float, attempt: int, upper_bound: float = 8.0) -> float:
        return min(backoff_sec ** attempt, upper_bound)


class CommandRunner:
    """统一的命令执行器，便于日志、超时与异常策略集中管理。"""

    @staticmethod
    def run(cmd: Sequence[str], logger: logging.Logger, timeout_sec: int = 20) -> Tuple[int, str, str]:
        logger.debug("执行命令: %s", list(cmd))
        process: Optional[subprocess.Popen[str]] = None
        try:
            process = subprocess.Popen(
                list(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            stdout, stderr = process.communicate(timeout=timeout_sec)
            return process.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            if process is not None:
                try:
                    process.kill()
                    process.communicate(timeout=2)
                except Exception:
                    pass
            return 124, "", "timeout"
        except Exception as exc:
            return 127, "", str(exc)


class ProcessService:
    """进程控制服务。"""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def is_pid_running(self, pid: int) -> bool:
        if pid <= 0 or not WindowsApi.is_windows():
            return False
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                return False
            return exit_code.value == self.STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def is_process_running_by_image(self, image_name: str) -> bool:
        code, out, err = CommandRunner.run(["tasklist", "/FO", "CSV", "/NH"], self.logger, timeout_sec=20)
        if code != 0:
            self.logger.warning("tasklist 调用失败: code=%s err=%s", code, err)
            return False

        needle = image_name.strip().lower().strip('"')
        for parts in csv.reader(line for line in out.splitlines() if line.strip()):
            if not parts:
                continue
            if parts[0].strip().lower() == needle:
                return True
        return False

    def kill_processes(self, process_names: Sequence[str], grace_sec: float = 0.8, total_timeout_sec: int = 20) -> None:
        unique_names: List[str] = []
        seen = set()
        for name in process_names:
            normalized = (name or "").strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            unique_names.append(normalized)

        if not unique_names:
            self.logger.info("未配置需要结束的进程，跳过进程终止步骤。")
            return

        self.logger.info("准备停止进程: %s", ", ".join(unique_names))
        deadline = time.time() + total_timeout_sec

        for name in unique_names:
            CommandRunner.run(["taskkill", "/IM", name], self.logger, timeout_sec=10)
        time.sleep(grace_sec)

        for name in unique_names:
            CommandRunner.run(["taskkill", "/F", "/T", "/IM", name], self.logger, timeout_sec=15)

        while time.time() < deadline:
            still_running = [name for name in unique_names if self.is_process_running_by_image(name)]
            if not still_running:
                self.logger.info("目标进程已全部停止。")
                return
            self.logger.debug("等待进程退出: %s", still_running)
            time.sleep(0.5)

        still_running = [name for name in unique_names if self.is_process_running_by_image(name)]
        if still_running:
            raise RuntimeError(f"以下进程在超时后仍未退出: {still_running}")

    def start_executable(self, exe_path: Path) -> None:
        if not exe_path.exists():
            raise FileNotFoundError(str(exe_path))
        self.logger.info("启动目标程序: %s", exe_path)
        subprocess.Popen(
            [str(exe_path)],
            cwd=str(exe_path.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )


class FileLock:
    """支持僵尸锁恢复的单实例锁。"""

    def __init__(self, lock_path: Path, logger: logging.Logger, process_service: ProcessService):
        self.lock_path = lock_path
        self.logger = logger
        self.process_service = process_service
        self._fh = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, 3):
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                self._fh = os.fdopen(fd, "w", encoding="utf-8")
                self._fh.write(f"pid={os.getpid()}\n")
                self._fh.flush()
                self.logger.debug("获取单实例锁成功: %s", self.lock_path)
                return True
            except FileExistsError:
                if not self._try_break_stale_lock():
                    self.logger.warning("检测到已有实例运行或锁文件不可回收: %s", self.lock_path)
                    return False
                self.logger.warning("检测到僵尸锁，已清理。准备重试获取锁。attempt=%d", attempt)
            except Exception as exc:
                self.logger.error("获取单实例锁失败: %s; err=%s", self.lock_path, exc, exc_info=True)
                return False
        return False

    def _try_break_stale_lock(self) -> bool:
        try:
            if not self.lock_path.exists():
                return True
            content = self.lock_path.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"pid\s*=\s*(\d+)", content)
            if not match:
                self.logger.warning("锁文件内容异常，按僵尸锁处理: %s", self.lock_path)
                self.lock_path.unlink(missing_ok=True)
                return True

            pid = int(match.group(1))
            if pid == os.getpid():
                self.lock_path.unlink(missing_ok=True)
                return True

            if self.process_service.is_pid_running(pid):
                return False

            self.logger.warning("锁文件对应进程已不存在，清理僵尸锁。pid=%s path=%s", pid, self.lock_path)
            self.lock_path.unlink(missing_ok=True)
            return True
        except Exception as exc:
            self.logger.error("清理僵尸锁失败: %s", exc, exc_info=True)
            return False

    def release(self) -> None:
        try:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:
                    pass
            if self.lock_path.exists():
                self.lock_path.unlink(missing_ok=True)
            self.logger.debug("释放单实例锁完成: %s", self.lock_path)
        except Exception as exc:
            self.logger.error("释放单实例锁失败: %s", exc, exc_info=True)


# -----------------------------
# 配置服务
# -----------------------------

class ConfigService:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    @staticmethod
    def _coerce_bool(value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    def load_or_create(self, config_path: Path) -> AppConfig:
        raw_data: Dict = {}
        if not config_path.exists():
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
                self.logger.info("配置文件不存在，已创建默认配置: %s", config_path)
                raw_data = DEFAULT_CONFIG
            except Exception as exc:
                self.logger.error("创建默认配置失败，使用内置默认配置继续运行: %s", exc, exc_info=True)
                raw_data = DEFAULT_CONFIG
        else:
            try:
                raw_data = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"读取或解析配置失败，请修复配置文件后重试: {config_path}") from exc

        return self._parse_config(raw_data)

    def _parse_config(self, data: Dict) -> AppConfig:
        if not isinstance(data, dict):
            raise ValueError("配置文件顶层必须是对象。")

        network_raw = data.get("network", {}) or {}
        jobs_raw = data.get("jobs", [])
        startup_raw = data.get("startup", {}) or {}
        if not isinstance(network_raw, dict):
            raise ValueError("配置项 network 必须是对象。")
        if not isinstance(startup_raw, dict):
            raise ValueError("配置项 startup 必须是对象。")

        network = NetworkConfig(
            timeout_sec=max(1, int(network_raw.get("timeout_sec", 25))),
            retries=max(1, int(network_raw.get("retries", 5))),
            backoff_sec=max(1.01, float(network_raw.get("backoff_sec", 1.6))),
            user_agent=str(network_raw.get("user_agent", f"{APP_NAME}/{APP_VERSION}")),
            max_file_bytes=max(1, int(network_raw.get("max_file_bytes", 2_147_483_648))),
        )

        if not isinstance(jobs_raw, list) or not jobs_raw:
            raise ValueError("配置项 jobs 必须为非空数组。")

        jobs: List[JobConfig] = []
        for job_raw in jobs_raw:
            if not isinstance(job_raw, dict):
                raise ValueError("jobs 中的每一项都必须是对象。")

            listing_raw = job_raw.get("listing", {}) or {}
            if not isinstance(listing_raw, dict):
                raise ValueError(f"任务 {job_raw.get('name', 'UnnamedJob')} 的 listing 必须是对象。")
            listing = ListingConfig(
                mode=str(listing_raw.get("mode", "autoindex")).strip() or "autoindex",
                manifest_url=str(listing_raw.get("manifest_url", "")).strip(),
                max_depth=max(0, int(listing_raw.get("max_depth", 10))),
            )
            if listing.mode not in {"autoindex", "manifest"}:
                raise ValueError(f"不支持的 listing.mode: {listing.mode}")

            start_executable = str(job_raw.get("start_executable", "")).strip()
            kill_processes = job_raw.get("kill_processes", [])
            if not isinstance(kill_processes, list):
                kill_processes = []
            normalized_kill_processes = [str(item).strip() for item in kill_processes if str(item).strip()]
            if not normalized_kill_processes and start_executable:
                normalized_kill_processes = [Path(start_executable).name]

            exclude_raw = job_raw.get("exclude", [])
            exclude_patterns = [str(item).strip() for item in exclude_raw if str(item).strip()] if isinstance(exclude_raw, list) else []

            job = JobConfig(
                name=str(job_raw.get("name", "UnnamedJob")).strip() or "UnnamedJob",
                kill_processes=normalized_kill_processes,
                start_executable=start_executable,
                source_url=str(job_raw.get("source_url", "")).strip(),
                target_path=str(job_raw.get("target_path", "")).strip(),
                listing=listing,
                exclude=exclude_patterns,
                start_on_failure=self._coerce_bool(job_raw.get("start_on_failure", True), True),
            )

            if not job.start_executable:
                raise ValueError(f"任务 {job.name} 缺少 start_executable。")
            if not job.source_url:
                raise ValueError(f"任务 {job.name} 缺少 source_url。")
            if not job.target_path:
                raise ValueError(f"任务 {job.name} 缺少 target_path。")

            jobs.append(job)

        legacy_startup = LegacyStartupConfig(
            enabled=self._coerce_bool(startup_raw.get("enabled", False), False),
            method=str(startup_raw.get("method", "registry_run_key")).strip() or "registry_run_key",
            value_name=str(startup_raw.get("value_name", DEFAULT_STARTUP_VALUE_NAME)).strip() or DEFAULT_STARTUP_VALUE_NAME,
            arguments=str(startup_raw.get("arguments", "--silent")).strip(),
        )

        return AppConfig(network=network, jobs=jobs, legacy_startup=legacy_startup)


# -----------------------------
# 网络与目录索引服务
# -----------------------------

class AutoIndexParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


class HttpClient:
    def __init__(self, network_config: NetworkConfig, logger: logging.Logger):
        self.network_config = network_config
        self.logger = logger

    def get_bytes(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": self.network_config.user_agent}, method="GET")
        last_error = None
        for attempt in range(1, self.network_config.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.network_config.timeout_sec) as resp:
                    content_length = resp.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            content_length_int = int(content_length)
                            if content_length_int > self.network_config.max_file_bytes:
                                raise RuntimeError(f"响应大小超出上限: {content_length_int}")
                        except ValueError:
                            pass
                    return self._read_limited(resp, self.network_config.max_file_bytes)
            except Exception as exc:
                last_error = exc
                sleep_seconds = RetryPolicy.get_sleep_seconds(self.network_config.backoff_sec, attempt)
                self.logger.warning(
                    "HTTP GET 失败，准备重试: attempt=%d/%d url=%s err=%s sleep=%.2fs",
                    attempt,
                    self.network_config.retries,
                    url,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)
        raise RuntimeError(f"HTTP GET 连续失败: url={url} err={last_error}")

    @staticmethod
    def _read_limited(response, max_bytes: int) -> bytes:
        chunks: List[bytes] = []
        read_bytes = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            chunks.append(chunk)
            read_bytes += len(chunk)
            if read_bytes > max_bytes:
                raise RuntimeError(f"响应读取过程中超出大小上限: {read_bytes}")
        return b"".join(chunks)

    def download_to_file(self, url: str, dest_file: Path) -> None:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = dest_file.with_suffix(dest_file.suffix + ".part")
        req = urllib.request.Request(url, headers={"User-Agent": self.network_config.user_agent}, method="GET")
        last_error = None

        for attempt in range(1, self.network_config.retries + 1):
            try:
                if temp_file.exists():
                    temp_file.unlink(missing_ok=True)

                with urllib.request.urlopen(req, timeout=self.network_config.timeout_sec) as resp:
                    content_length = resp.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            content_length_int = int(content_length)
                            if content_length_int > self.network_config.max_file_bytes:
                                raise RuntimeError(f"文件大小超出上限: {content_length_int}")
                        except ValueError:
                            pass

                    read_bytes = 0
                    with open(temp_file, "wb") as handle:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            handle.write(chunk)
                            read_bytes += len(chunk)
                            if read_bytes > self.network_config.max_file_bytes:
                                raise RuntimeError(f"文件下载过程中超出大小上限: {read_bytes}")

                os.replace(str(temp_file), str(dest_file))
                self.logger.debug("文件下载完成: %s -> %s", url, dest_file)
                return
            except Exception as exc:
                last_error = exc
                try:
                    if temp_file.exists():
                        temp_file.unlink(missing_ok=True)
                except Exception:
                    pass
                sleep_seconds = RetryPolicy.get_sleep_seconds(self.network_config.backoff_sec, attempt)
                self.logger.warning(
                    "下载失败，准备重试: attempt=%d/%d url=%s err=%s sleep=%.2fs",
                    attempt,
                    self.network_config.retries,
                    url,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)

        raise RuntimeError(f"下载连续失败: url={url} err={last_error}")


class PathService:
    @staticmethod
    def normalize_source_url(url: str) -> str:
        value = (url or "").strip()
        if not value:
            raise ValueError("source_url 不能为空")

        parsed = urllib.parse.urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"非法 URL: {url}")

        path = parsed.path
        if not path.endswith("/"):
            path += "/"

        safe_path = urllib.parse.quote(path, safe="/%")
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, safe_path, parsed.query, parsed.fragment))

    @staticmethod
    def sanitize_local_rel_path(local_rel: str) -> str:
        value = (local_rel or "").replace("\\", "/").lstrip("/")
        parts = [part for part in value.split("/") if part != ""]
        if not parts:
            raise ValueError("相对路径为空")

        sanitized: List[str] = []
        for segment in parts:
            if segment in {".", ".."}:
                raise ValueError(f"非法路径段: {segment}")
            if ":" in segment:
                raise ValueError(f"路径段中不允许包含冒号: {segment}")

            if WindowsApi.is_windows():
                if any(ch in _WINDOWS_INVALID_CHARS for ch in segment):
                    raise ValueError(f"Windows 文件名非法字符: {segment}")
                if segment.endswith(" ") or segment.endswith("."):
                    raise ValueError(f"Windows 文件名不允许以空格或点结尾: {segment}")
                base = segment.split(".", 1)[0].upper()
                if base in _WINDOWS_RESERVED_NAMES:
                    raise ValueError(f"Windows 保留设备名不可用: {segment}")

            sanitized.append(segment)

        return "/".join(sanitized)

    @staticmethod
    def safe_path_within(base_dir: Path, rel_posix: str) -> Path:
        base_resolved = base_dir.resolve()
        rel_clean = PathService.sanitize_local_rel_path(rel_posix)
        full = (base_resolved / Path(rel_clean.replace("/", os.sep))).resolve()
        try:
            full.relative_to(base_resolved)
        except Exception:
            raise ValueError(f"目标路径越界: base={base_resolved} rel={rel_clean} full={full}")
        return full

    @staticmethod
    def decode_remote_rel_to_local(remote_rel: str, logger: logging.Logger) -> str:
        value = (remote_rel or "").strip()
        value = value.split("#", 1)[0].split("?", 1)[0]
        value = value.replace("\\", "/").lstrip("/")

        for _ in range(2):
            try:
                raw_bytes = urllib.parse.unquote_to_bytes(value)
                decoded, encoding = PathService._decode_bytes_best_effort(raw_bytes)
                if decoded != value and PERCENT_RE.search(decoded):
                    logger.debug("文件名仍存在百分号编码，继续解码: enc=%s before=%s after=%s", encoding, value, decoded)
                    value = decoded
                    continue
                value = decoded
                break
            except Exception as exc:
                logger.warning("远程文件名解码失败，保留当前值继续处理: input=%s err=%s", value, exc)
                break

        return PathService.sanitize_local_rel_path(value)

    @staticmethod
    def _decode_bytes_best_effort(raw: bytes) -> Tuple[str, str]:
        for encoding in ("utf-8", "gbk", "big5"):
            try:
                return raw.decode(encoding, errors="strict"), encoding
            except Exception:
                continue
        return raw.decode("utf-8", errors="replace"), "utf-8(replace)"

    @staticmethod
    def is_excluded(rel_path: str, patterns: Sequence[str]) -> bool:
        normalized = rel_path.replace("\\", "/")
        for pattern in patterns or []:
            if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(Path(normalized).name, pattern):
                return True
        return False

    @staticmethod
    def strip_query_fragment(value: str) -> str:
        return (value or "").split("#", 1)[0].split("?", 1)[0]

    @staticmethod
    def posix_join(prefix: str, href: str) -> str:
        left = (prefix or "").replace("\\", "/").lstrip("/")
        right = (href or "").replace("\\", "/").lstrip("/")
        if not left:
            return right
        return left.rstrip("/") + "/" + right


class RemoteListingService:
    def __init__(self, http_client: HttpClient, logger: logging.Logger):
        self.http_client = http_client
        self.logger = logger

    def build_remote_file_list(self, job: JobConfig) -> List[FileEntry]:
        mode = (job.listing.mode or "autoindex").strip().lower()
        if mode == "manifest":
            manifest_url = job.listing.manifest_url.strip()
            if not manifest_url:
                manifest_url = urllib.parse.urljoin(PathService.normalize_source_url(job.source_url), "manifest.json")
            self.logger.info("[%s] 使用 manifest 列表模式: %s", job.name, manifest_url)
            return self._list_files_via_manifest(manifest_url)
        if mode == "autoindex":
            base_url = PathService.normalize_source_url(job.source_url)
            self.logger.info("[%s] 使用 autoindex 列表模式: %s", job.name, base_url)
            return self._list_files_via_autoindex(base_url, job.listing.max_depth)
        raise ValueError(f"不支持的 listing.mode: {job.listing.mode}")

    def _list_files_via_manifest(self, manifest_url: str) -> List[FileEntry]:
        payload = self.http_client.get_bytes(manifest_url)
        manifest = json.loads(payload.decode("utf-8"))
        files = manifest.get("files", [])
        if not isinstance(files, list) or not files:
            raise ValueError("manifest 中 files 必须为非空数组")

        entries: List[FileEntry] = []
        for item in files:
            if isinstance(item, str):
                remote_rel = item
            elif isinstance(item, dict):
                remote_rel = str(item.get("path", "")).strip()
            else:
                continue

            if not remote_rel or remote_rel.endswith("/"):
                continue
            remote_rel = PathService.strip_query_fragment(remote_rel).replace("\\", "/").lstrip("/")
            if not remote_rel:
                continue
            local_rel = PathService.decode_remote_rel_to_local(remote_rel, self.logger)
            entries.append(FileEntry(remote_rel=remote_rel, local_rel=local_rel))

        if not entries:
            raise ValueError("manifest 未产生任何可下载文件")
        return entries

    def _list_files_via_autoindex(self, base_url: str, max_depth: int) -> List[FileEntry]:
        seen_dirs: set[str] = set()
        collected: List[FileEntry] = []
        normalized_base = PathService.normalize_source_url(base_url)

        def walk(dir_url: str, rel_prefix: str, depth: int) -> None:
            if depth > max_depth:
                self.logger.warning("达到最大递归深度，跳过目录: depth=%s url=%s", depth, dir_url)
                return
            if dir_url in seen_dirs:
                return
            seen_dirs.add(dir_url)

            self.logger.debug("扫描目录: depth=%d url=%s rel_prefix=%s", depth, dir_url, rel_prefix)
            page_bytes = self.http_client.get_bytes(dir_url)
            parser = AutoIndexParser()
            parser.feed(page_bytes.decode("utf-8", errors="replace"))
            if not parser.hrefs:
                raise RuntimeError(f"目录索引页面未解析到任何链接: {dir_url}")

            for href in parser.hrefs:
                href_clean = PathService.strip_query_fragment(href).strip()
                if not href_clean:
                    continue
                if href_clean in {"../", "./", "/", ".."} or href_clean.startswith("../"):
                    continue

                full_url = urllib.parse.urljoin(dir_url, href_clean)
                remote_rel = self._convert_href_to_remote_rel(normalized_base, full_url, href_clean, rel_prefix)
                if not remote_rel:
                    continue

                if href_clean.endswith("/") or full_url.endswith("/"):
                    walk(full_url if full_url.endswith("/") else full_url + "/", remote_rel.rstrip("/") + "/", depth + 1)
                    continue

                local_rel = PathService.decode_remote_rel_to_local(remote_rel, self.logger)
                collected.append(FileEntry(remote_rel=remote_rel, local_rel=local_rel))

        walk(normalized_base, "", 0)

        unique: Dict[str, FileEntry] = {}
        for entry in collected:
            if entry.local_rel not in unique:
                unique[entry.local_rel] = entry
        result = sorted(unique.values(), key=lambda item: item.local_rel)
        if not result:
            raise RuntimeError(f"autoindex 最终没有解析出任何文件: {normalized_base}")
        return result

    @staticmethod
    def _convert_href_to_remote_rel(base_url: str, full_url: str, href: str, rel_prefix: str) -> Optional[str]:
        parsed_href = urllib.parse.urlsplit(href)
        if parsed_href.scheme and parsed_href.netloc:
            base_parts = urllib.parse.urlsplit(base_url)
            full_parts = urllib.parse.urlsplit(full_url)
            if (full_parts.scheme, full_parts.netloc) != (base_parts.scheme, base_parts.netloc):
                return None
            return RemoteListingService._strip_base_path(base_parts.path, full_parts.path)

        if href.startswith("/"):
            base_parts = urllib.parse.urlsplit(base_url)
            return RemoteListingService._strip_base_path(base_parts.path, href)

        return PathService.posix_join(rel_prefix, href).lstrip("/")

    @staticmethod
    def _strip_base_path(base_path: str, abs_path: str) -> Optional[str]:
        if abs_path.startswith(base_path):
            return abs_path[len(base_path):].lstrip("/")
        base_decoded = urllib.parse.unquote(base_path)
        abs_decoded = urllib.parse.unquote(abs_path)
        if abs_decoded.startswith(base_decoded):
            return abs_decoded[len(base_decoded):].lstrip("/")
        return None


# -----------------------------
# 文件替换服务
# -----------------------------

class FileReplaceService:
    def __init__(self, logger: logging.Logger):
        self.logger = logger

    @staticmethod
    def same_file(left: Path, right: Path) -> bool:
        try:
            return left.resolve().as_posix().lower() == right.resolve().as_posix().lower()
        except Exception:
            return str(left).lower() == str(right).lower()

    def atomic_replace_file(self, staged_file: Path, target_file: Path, retries: int = 8) -> None:
        target_file.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target_file.with_suffix(target_file.suffix + f".tmp_{os.getpid()}")
        last_error = None

        for attempt in range(1, retries + 1):
            try:
                if temp_target.exists():
                    temp_target.unlink(missing_ok=True)
                shutil.copy2(str(staged_file), str(temp_target))
                os.replace(str(temp_target), str(target_file))
                return
            except Exception as exc:
                last_error = exc
                try:
                    if temp_target.exists():
                        temp_target.unlink(missing_ok=True)
                except Exception:
                    pass
                sleep_seconds = min(0.4 * attempt, 3.0)
                self.logger.warning(
                    "原子替换失败，准备重试: attempt=%d/%d target=%s err=%s sleep=%.2fs",
                    attempt,
                    retries,
                    target_file,
                    exc,
                    sleep_seconds,
                )
                time.sleep(sleep_seconds)

        raise RuntimeError(f"文件替换失败: target={target_file} err={last_error}")

    def schedule_self_replace(self, current_exe: Path, new_file: Path) -> None:
        temp_dir = Path(tempfile.gettempdir()) / APP_NAME
        temp_dir.mkdir(parents=True, exist_ok=True)
        script = temp_dir / f"self_replace_{os.getpid()}.cmd"
        script.write_text(
            f"""@echo off
setlocal enabledelayedexpansion
set CUR="{current_exe}"
set NEW="{new_file}"
set TRY=0

:loop
set /a TRY+=1
if !TRY! GTR 30 goto fail

ping 127.0.0.1 -n 2 >nul

del /f /q %CUR% >nul 2>&1
move /y %NEW% %CUR% >nul 2>&1
if exist %CUR% (
  del /f /q "%~f0" >nul 2>&1
  exit /b 0
)
goto loop

:fail
exit /b 1
""",
            encoding="utf-8",
        )
        self.logger.warning("检测到自更新场景，已安排延迟替换脚本: %s", script)
        subprocess.Popen(
            ["cmd.exe", "/c", "start", '""', "/min", "cmd.exe", "/c", str(script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )


class TempWorkspaceService:
    """清理本程序创建的临时工作目录，避免误删非暂存路径。"""

    @staticmethod
    def cleanup_staging_dir(staging_dir: Path, logger: logging.Logger) -> None:
        root = staging_dir.resolve()
        allowed_parent = (Path(tempfile.gettempdir()) / APP_NAME).resolve()
        try:
            root.relative_to(allowed_parent)
        except ValueError as exc:
            raise RuntimeError(f"拒绝清理非应用临时目录: {root}") from exc

        if not root.name.startswith("stage_"):
            raise RuntimeError(f"拒绝清理非暂存目录: {root}")
        if not root.exists():
            return

        for current_root, dir_names, file_names in os.walk(root, topdown=False):
            current_path = Path(current_root)
            TempWorkspaceService._ensure_parent_within_root(current_path, root)

            for file_name in file_names:
                file_path = current_path / file_name
                TempWorkspaceService._ensure_parent_within_root(file_path.parent, root)
                try:
                    file_path.unlink(missing_ok=True)
                except Exception as exc:
                    logger.warning("清理临时文件失败: path=%s err=%s", file_path, exc)

            for dir_name in dir_names:
                child_dir = current_path / dir_name
                TempWorkspaceService._ensure_parent_within_root(child_dir, root)
                try:
                    child_dir.rmdir()
                except Exception as exc:
                    logger.warning("清理临时子目录失败: path=%s err=%s", child_dir, exc)

        try:
            root.rmdir()
        except Exception as exc:
            logger.warning("清理临时目录失败: path=%s err=%s", root, exc)

    @staticmethod
    def _ensure_parent_within_root(path: Path, root: Path) -> None:
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"临时目录清理路径越界: root={root} path={path}") from exc


# -----------------------------
# 开机自启清理服务
# -----------------------------

class StartupCleaner:
    """新需求：程序不再注册开机自启，并主动清理旧版本遗留项。"""

    def __init__(self, logger: logging.Logger):
        self.logger = logger

    def cleanup_legacy_registry_entries(self, legacy_value_name: str) -> None:
        try:
            import winreg  # type: ignore
        except Exception as exc:
            self.logger.error("加载 winreg 失败，无法清理历史开机自启项: %s", exc, exc_info=True)
            return

        run_key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        value_names = {DEFAULT_STARTUP_VALUE_NAME}
        if legacy_value_name.strip():
            value_names.add(legacy_value_name.strip())

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                for value_name in sorted(value_names):
                    try:
                        winreg.DeleteValue(key, value_name)
                        self.logger.info("已清理历史开机自启注册表项: %s", value_name)
                    except FileNotFoundError:
                        self.logger.debug("未发现历史开机自启注册表项: %s", value_name)
                    except Exception as exc:
                        self.logger.error("删除历史开机自启注册表项失败: value_name=%s err=%s", value_name, exc, exc_info=True)
        except FileNotFoundError:
            self.logger.debug("Run 注册表键不存在，无需清理。")
        except Exception as exc:
            self.logger.error("打开 Run 注册表键失败: %s", exc, exc_info=True)


# -----------------------------
# 任务执行器
# -----------------------------

class JobRunner:
    def __init__(
        self,
        logger: logging.Logger,
        process_service: ProcessService,
        listing_service: RemoteListingService,
        http_client: HttpClient,
        file_replace_service: FileReplaceService,
    ):
        self.logger = logger
        self.process_service = process_service
        self.listing_service = listing_service
        self.http_client = http_client
        self.file_replace_service = file_replace_service

    def run_job(self, job: JobConfig) -> None:
        target_dir = WindowsApi.expand_macros(job.target_path)
        target_dir.mkdir(parents=True, exist_ok=True)

        self.logger.info("=== 任务开始: %s ===", job.name)
        self.logger.debug("[%s] target_dir=%s", job.name, target_dir)
        self.logger.debug(
            "[%s] source_url=%s start_executable=%s kill_processes=%s",
            job.name,
            job.source_url,
            job.start_executable,
            job.kill_processes,
        )

        self.process_service.kill_processes(job.kill_processes)
        entries = self.listing_service.build_remote_file_list(job)
        staging_dir, downloaded_files = self._stage_downloads(job, entries)
        try:
            self._apply_updates(job, target_dir, staging_dir, downloaded_files)
        finally:
            try:
                TempWorkspaceService.cleanup_staging_dir(staging_dir, self.logger)
            except Exception as exc:
                self.logger.warning("[%s] 清理暂存目录失败: path=%s err=%s", job.name, staging_dir, exc)

        exe_path = self.resolve_start_executable_path(job, target_dir)
        self.process_service.start_executable(exe_path)
        self.logger.info("=== 任务完成: %s ===", job.name)

    def _stage_downloads(self, job: JobConfig, entries: Sequence[FileEntry]) -> Tuple[Path, List[Tuple[FileEntry, Path]]]:
        base_url = PathService.normalize_source_url(job.source_url)
        staging_dir = Path(tempfile.gettempdir()) / APP_NAME / f"stage_{job.name}_{int(time.time())}_{os.getpid()}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staging_dir_resolved = staging_dir.resolve()

        downloaded: List[Tuple[FileEntry, Path]] = []
        for entry in entries:
            if PathService.is_excluded(entry.local_rel, job.exclude):
                self.logger.debug("[%s] 命中排除规则，跳过文件: %s", job.name, entry.local_rel)
                continue

            remote_rel = PathService.strip_query_fragment(entry.remote_rel).replace("\\", "/").lstrip("/")
            if not remote_rel:
                continue

            quoted_rel = urllib.parse.quote(remote_rel, safe="/%")
            file_url = urllib.parse.urljoin(base_url, quoted_rel)
            staged_file = PathService.safe_path_within(staging_dir_resolved, entry.local_rel)

            self.logger.info("[%s] 下载文件: %s", job.name, entry.local_rel)
            self.http_client.download_to_file(file_url, staged_file)
            downloaded.append((entry, staged_file))

        if not downloaded:
            raise RuntimeError(f"任务 {job.name} 未下载到任何文件，可能全部被排除。")
        return staging_dir, downloaded

    def _apply_updates(
        self,
        job: JobConfig,
        target_dir: Path,
        staging_dir: Path,
        downloaded_files: Sequence[Tuple[FileEntry, Path]],
    ) -> None:
        current_binary = WindowsApi.current_binary_path()
        pending_self_replace: Optional[Tuple[Path, Path]] = None
        target_dir_resolved = target_dir.resolve()

        for entry, staged_file in downloaded_files:
            target_file = PathService.safe_path_within(target_dir_resolved, entry.local_rel)

            if self.file_replace_service.same_file(target_file, current_binary):
                safe_dir = Path(tempfile.gettempdir()) / APP_NAME / "self_update"
                safe_dir.mkdir(parents=True, exist_ok=True)
                safe_new = safe_dir / f"{current_binary.name}.new_{os.getpid()}"
                if safe_new.exists():
                    safe_new.unlink(missing_ok=True)
                shutil.copy2(str(staged_file), str(safe_new))
                pending_self_replace = (current_binary, safe_new)
                self.logger.warning("[%s] 命中自更新文件，改为退出后替换: %s", job.name, current_binary)
                continue

            self.logger.info("[%s] 替换文件: %s", job.name, target_file)
            self.file_replace_service.atomic_replace_file(staged_file, target_file)

        if pending_self_replace is not None:
            current_exe, new_file = pending_self_replace
            self.file_replace_service.schedule_self_replace(current_exe, new_file)

        self.logger.debug("[%s] 本次暂存目录处理完成: %s", job.name, staging_dir)

    def resolve_start_executable_path(self, job: JobConfig, target_dir: Path) -> Path:
        raw_value = (job.start_executable or "").strip()
        if not raw_value:
            raise ValueError(f"任务 {job.name} 的 start_executable 为空")

        raw_path = Path(raw_value)
        if raw_path.is_absolute():
            resolved = raw_path.resolve()
            self.logger.info("[%s] 启动路径解析结果（绝对路径）: %s", job.name, resolved)
            return resolved

        normalized = raw_value.replace("\\", "/").lstrip("/")
        if "/" in normalized:
            resolved = PathService.safe_path_within(target_dir, normalized)
            self.logger.info("[%s] 启动路径解析结果（相对路径）: %s", job.name, resolved)
            return resolved

        resolved = (target_dir / Path(normalized).name).resolve()
        self.logger.info("[%s] 启动路径解析结果（目标目录根）: %s", job.name, resolved)
        return resolved


# -----------------------------
# 诊断服务
# -----------------------------

class DiagnosticService:
    def __init__(self, logger: logging.Logger, process_service: ProcessService):
        self.logger = logger
        self.process_service = process_service

    def run(self, config_path: Path, config: AppConfig) -> None:
        self.logger.info("========== 开始诊断 ==========")
        self.logger.info("当前程序路径: %s", WindowsApi.current_binary_path())
        self.logger.info("配置文件路径: %s", config_path)
        self.logger.info("日志目录: %s", WindowsApi.get_log_dir())
        self.logger.info("运行目录: %s", WindowsApi.get_runtime_dir())
        self.logger.info("任务数量: %s", len(config.jobs))

        for job in config.jobs:
            try:
                target_dir = WindowsApi.expand_macros(job.target_path)
                exe_path = JobRunner(
                    logger=self.logger,
                    process_service=self.process_service,
                    listing_service=RemoteListingService(HttpClient(config.network, self.logger), self.logger),
                    http_client=HttpClient(config.network, self.logger),
                    file_replace_service=FileReplaceService(self.logger),
                ).resolve_start_executable_path(job, target_dir)
                self.logger.info("[诊断][%s] target_dir=%s", job.name, target_dir)
                self.logger.info("[诊断][%s] start_executable=%s exists=%s", job.name, exe_path, exe_path.exists())
                self.logger.info("[诊断][%s] source_url=%s", job.name, PathService.normalize_source_url(job.source_url))
            except Exception as exc:
                self.logger.error("[诊断][%s] 失败: %s", job.name, exc, exc_info=True)

        self.logger.info("========== 诊断结束 ==========")


# -----------------------------
# CLI 与应用入口
# -----------------------------

class CliArguments:
    @staticmethod
    def parse(argv: Sequence[str]) -> Dict[str, object]:
        args: Dict[str, object] = {
            "silent": False,
            "config": "",
            "diagnose": False,
        }
        for arg in argv[1:]:
            value = arg.strip()
            if value == "--silent":
                args["silent"] = True
            elif value.startswith("--config="):
                args["config"] = value.split("=", 1)[1].strip()
            elif value == "--diagnose":
                args["diagnose"] = True
        return args


class Application:
    def __init__(self):
        self.args = CliArguments.parse(sys.argv)
        self.logger = LoggerFactory.create(silent=bool(self.args.get("silent", False)))
        self.process_service = ProcessService(self.logger)
        self.config_service = ConfigService(self.logger)

    def run(self) -> int:
        if not WindowsApi.is_windows():
            print("This updater is designed for Windows only.")
            return 2

        config_path = WindowsApi.resolve_config_path(str(self.args.get("config") or ""))
        self.logger.info("实际生效的配置文件路径: %s", config_path)

        lock = FileLock(WindowsApi.get_runtime_dir() / LOCK_FILENAME, self.logger, self.process_service)
        if not lock.acquire():
            return 3

        try:
            config = self.config_service.load_or_create(config_path)

            startup_cleaner = StartupCleaner(self.logger)
            startup_cleaner.cleanup_legacy_registry_entries(config.legacy_startup.value_name)

            if bool(self.args.get("diagnose", False)):
                DiagnosticService(self.logger, self.process_service).run(config_path, config)
                return 0

            http_client = HttpClient(config.network, self.logger)
            listing_service = RemoteListingService(http_client, self.logger)
            file_replace_service = FileReplaceService(self.logger)
            job_runner = JobRunner(
                logger=self.logger,
                process_service=self.process_service,
                listing_service=listing_service,
                http_client=http_client,
                file_replace_service=file_replace_service,
            )

            overall_ok = True
            self.logger.info("已加载任务: %s", ", ".join(job.name for job in config.jobs))
            for job in config.jobs:
                try:
                    job_runner.run_job(job)
                except Exception as exc:
                    overall_ok = False
                    self.logger.error("任务执行失败: job=%s err=%s", job.name, exc, exc_info=True)
                    if job.start_on_failure:
                        try:
                            target_dir = WindowsApi.expand_macros(job.target_path)
                            exe_path = job_runner.resolve_start_executable_path(job, target_dir)
                            self.process_service.start_executable(exe_path)
                            self.logger.warning("任务失败后已尝试拉起原程序: job=%s exe=%s", job.name, exe_path)
                        except Exception:
                            self.logger.error("任务失败后的兜底启动也失败: job=%s", job.name, exc_info=True)
            return 0 if overall_ok else 10
        except Exception as exc:
            self.logger.critical("程序发生致命错误: %s\n%s", exc, traceback.format_exc())
            return 1
        finally:
            lock.release()


def main() -> int:
    return Application().run()


if __name__ == "__main__":
    raise SystemExit(main())
