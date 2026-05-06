import logging
import os
import tempfile
import unittest
from pathlib import Path

import main


class ConfigServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("test-config")
        self.service = main.ConfigService(self.logger)

    def test_invalid_listing_mode_is_rejected(self) -> None:
        data = {
            "jobs": [
                {
                    "name": "bad",
                    "start_executable": "bad.exe",
                    "source_url": "http://example.com/files/",
                    "target_path": "%DESKTOP%/bad",
                    "listing": {"mode": "unknown"},
                }
            ]
        }

        with self.assertRaises(ValueError):
            self.service._parse_config(data)

    def test_invalid_json_does_not_fallback_to_default_job(self) -> None:
        config_path = Path(tempfile.gettempdir()) / main.APP_NAME / f"invalid_{os.getpid()}.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("{invalid json", encoding="utf-8")
        try:
            with self.assertRaises(RuntimeError):
                self.service.load_or_create(config_path)
        finally:
            config_path.unlink(missing_ok=True)


class ProcessServiceTests(unittest.TestCase):
    def test_tasklist_csv_is_parsed_with_csv_reader(self) -> None:
        logger = logging.getLogger("test-process")
        service = main.ProcessService(logger)
        original_run = main.CommandRunner.run

        def fake_run(cmd, logger, timeout_sec=20):
            return 0, '"App, Test.exe","1234","Console","1","1,024 K"\n', ""

        main.CommandRunner.run = staticmethod(fake_run)
        try:
            self.assertTrue(service.is_process_running_by_image("App, Test.exe"))
        finally:
            main.CommandRunner.run = original_run


class TempWorkspaceServiceTests(unittest.TestCase):
    def test_cleanup_only_removes_stage_directory_contents(self) -> None:
        logger = logging.getLogger("test-cleanup")
        root = Path(tempfile.gettempdir()) / main.APP_NAME / f"stage_unit_{os.getpid()}"
        nested = root / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        target = nested / "file.txt"
        target.write_text("ok", encoding="utf-8")

        main.TempWorkspaceService.cleanup_staging_dir(root, logger)

        self.assertFalse(root.exists())

    def test_cleanup_rejects_non_stage_directory(self) -> None:
        logger = logging.getLogger("test-cleanup")
        root = Path(tempfile.gettempdir()) / main.APP_NAME / f"not_stage_unit_{os.getpid()}"
        root.mkdir(parents=True, exist_ok=True)
        try:
            with self.assertRaises(RuntimeError):
                main.TempWorkspaceService.cleanup_staging_dir(root, logger)
        finally:
            root.rmdir()


if __name__ == "__main__":
    unittest.main()
