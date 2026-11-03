import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from indah_ui import upload_summary, render_run_result, render_phase_form, PHASES, run_command_streaming


def event(stage, current, total=3, **fields):
    return "INDAH_PROGRESS " + json.dumps(dict(stage=stage, current=current, total=total, **fields)) + "\n"


class UploadProgressTest(unittest.TestCase):
    def test_percent_counts_items_not_log_lines(self):
        log = event("item", 1, outcome="success") + "noise\n" * 90 + event("upload", 2, title="Data kedua")
        result = upload_summary(log, "")
        self.assertEqual(result["percent"], 33)
        self.assertIn("Data kedua", result["last_line"])
        self.assertFalse(result["done"])

    def test_partial_failure_has_no_false_success(self):
        log = event("item", 1, outcome="success") + event("upload", 2, title="Data kedua")
        result = upload_summary(log, "HTTP 401: secret-internal-response", 1)
        self.assertEqual((result["success"], result["failed"], result["remaining"]), (1, 1, 1))
        self.assertIn("Login kembali", result["output"])
        self.assertNotIn("secret-internal-response", result["output"])

    def test_skipped_with_zero_exit_is_not_all_success(self):
        result = upload_summary(event("item", 1, total=1, outcome="skipped", message="tahun kosong"), "", 0)
        self.assertEqual(result["status"], "Selesai dengan masalah")
        self.assertIn("tahun", result["output"])

    def test_old_logs_are_summarized(self):
        result = upload_summary("Rows kegiatan: 2\n[ok] kegiatan tersimpan: Satu -> 9\n[skip] Dua: tahun kosong", "", 0)
        self.assertEqual((result["success"], result["skipped"]), (1, 1))

    def test_results_do_not_expose_commands_payload_or_traceback(self):
        rendered = render_run_result("test", "kegiatan", {}, ["SECRET_COMMAND"], 1, "Rows kegiatan: 1", "Traceback PRIVATE_TOKEN HTTP 400")
        for forbidden in ("SECRET_COMMAND", "PRIVATE_TOKEN", "Traceback", "<h3>Output</h3>"):
            self.assertNotIn(forbidden, rendered)
        self.assertIn("Kesimpulan", rendered)
        self.assertNotIn("Verbose", render_phase_form("kegiatan", PHASES["kegiatan"]))

    def test_concurrent_streams_do_not_deadlock(self):
        with tempfile.TemporaryDirectory() as directory:
            script = "import sys; sys.stderr.write('x'*200000); sys.stderr.flush(); print('completed', flush=True)"
            result = run_command_streaming([sys.executable, "-u", "-c", script], Path(directory), Path(directory))
            self.assertEqual(result.returncode, 0)
            self.assertIn("completed", result.stdout)
            self.assertEqual(len(result.stderr), 200000)


if __name__ == "__main__":
    unittest.main()
