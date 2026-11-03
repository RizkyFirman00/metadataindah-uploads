import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from indah_automation.shared import (
    IndahClient,
    SessionAuth,
    build_kegiatan_payload,
    map_rows_by_title,
    retry_api_result,
    resolve_ms_keg_reference,
    write_csv,
)
from indah_ui import build_phase_command, run_command_streaming


class FakeForm:
    def __init__(self, values):
        self.values = values

    def getfirst(self, name, default=""):
        return self.values.get(name, default)


class FakeClient:
    def __init__(self):
        self.requested_period = None

    def get_ms_keg(self, _item_id):
        return {"id": 2026, "submission_period": 2026}

    def find_ms_keg_by_title(self, _title, *, submission_period=None, max_scan_pages=2):
        self.requested_period = submission_period
        return {"id": 2025, "submission_period": submission_period, "status": "DRAFT"}


class SearchClient(IndahClient):
    def __init__(self):
        super().__init__("unused-in-test")

    def list_ms_keg(self, params=None):
        return {
            "content": [
                {"id": 26, "submission_period": 2026, "data": {"judul_kegiatan": "Judul"}},
                {"id": 25, "submission_period": 2025, "data": {"judul_kegiatan": "Judul"}},
            ],
            "total_pages": 1,
        }


class SubmissionPeriodTest(unittest.TestCase):
    def test_kegiatan_period_does_not_replace_data_year(self):
        auth = SessionAuth(token="token", user_raw={}, user_payload={})
        payload = build_kegiatan_payload(
            {"judul_kegiatan": "Uji", "tahun": "2026", "submission_period": "2025"},
            [],
            [],
            auth,
            {},
        )
        self.assertEqual(payload["submission_period"], 2025)
        self.assertEqual(payload["data"]["tahun"], "2026")

    def test_map_rows_are_filtered_by_period(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kegiatan_map.csv"
            write_csv(
                path,
                ["judul_kegiatan", "submission_period", "ms_keg_id"],
                [
                    {"judul_kegiatan": "Judul", "submission_period": 2025, "ms_keg_id": 25},
                    {"judul_kegiatan": "Judul", "submission_period": 2026, "ms_keg_id": 26},
                ],
            )
            self.assertEqual(map_rows_by_title(path, 2025)["Judul"]["ms_keg_id"], "25")
            self.assertEqual(map_rows_by_title(path, 2026)["Judul"]["ms_keg_id"], "26")

    def test_parent_with_wrong_period_is_re_resolved(self):
        client = FakeClient()
        item_id, item, source = resolve_ms_keg_reference(
            {"judul_kegiatan": "Judul"},
            {"Judul": {"ms_keg_id": "2026"}},
            client,
            submission_period=2025,
        )
        self.assertEqual((item_id, source), ("2025", "auto"))
        self.assertEqual(item["submission_period"], 2025)
        self.assertEqual(client.requested_period, 2025)

    def test_api_search_filters_candidates_by_period(self):
        item = SearchClient().find_ms_keg_by_title("Judul", submission_period=2025)
        self.assertEqual(item["id"], 25)

    def test_ui_passes_selected_period_to_script(self):
        with patch("indah_ui.run_state_for", return_value=Path("/tmp/run_state/users/u")):
            command = build_phase_command(
                "kegiatan",
                {"script": "indah_kegiatan.py"},
                Path("/tmp/prepared"),
                FakeForm({"submission_period": "2025"}),
                Path("/tmp/state.json"),
            )
        index = command.index("--submission-period")
        self.assertEqual(command[index + 1], "2025")

    def test_streaming_command_writes_stdout_as_process_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            command = ["ignored"]
            process = FakeProcess(["satu\n", "dua\n"])
            with patch("indah_ui.subprocess.Popen", return_value=process):
                result = run_command_streaming(command, Path("/tmp/project"), run_dir)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "satu\ndua\n")
            self.assertEqual((run_dir / "stdout.txt").read_text(encoding="utf-8"), "satu\ndua\n")

    def test_retry_api_result_retries_error_result_until_success(self):
        calls = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                return {"status": 500, "message": "sibuk"}
            return {"id": 99, "status": "DRAFT"}

        with patch("indah_automation.shared.time.sleep"):
            result = retry_api_result(operation, "uji retry")

        self.assertEqual(result["id"], 99)
        self.assertEqual(len(calls), 3)


class FakeProcess:
    def __init__(self, stdout_lines, stderr_lines=None, returncode=0):
        self.stdout = stdout_lines
        self.stderr = stderr_lines or []
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode


if __name__ == "__main__":
    unittest.main()
