"""Test untuk perbaikan hasil audit (T1-T4, S2-S5, R1-R5)."""
import io
import json
import os
import ssl
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import indah_ui as ui
from indah_automation import spreadsheet_io as sio
from indah_automation.shared import IndahClient, IndahError, append_or_replace_map, read_csv_rows, user_run_state_dir


class Form:
    def __init__(self, **values):
        self.values = values

    def getfirst(self, name, default=""):
        return self.values.get(name, default)


class SpreadsheetLinkTest(unittest.TestCase):
    def test_google_sheets_link_is_normalized(self):
        url, ext = sio.normalize_download_url("https://docs.google.com/spreadsheets/d/abc_D-1/edit#gid=12")
        self.assertEqual(url, "https://docs.google.com/spreadsheets/d/abc_D-1/export?format=csv&gid=12")
        self.assertEqual(ext, ".csv")

    def test_other_links_are_rejected(self):
        for url in [
            "file:///app/run_state/kegiatan_map.csv",
            "http://docs.google.com/spreadsheets/d/x/edit",
            "https://docs.google.com.evil.example/spreadsheets/d/x",
            "https://evil.example/spreadsheets/d/x.xlsx",
            "http://127.0.0.1:8765/a.csv",
            "",
        ]:
            with self.subTest(url=url), self.assertRaises(IndahError):
                sio.normalize_download_url(url)

    def test_redirect_outside_google_is_blocked(self):
        handler = sio.GoogleOnlyRedirectHandler()
        with self.assertRaises(IndahError):
            handler.redirect_request(None, None, 302, "Found", {}, "http://169.254.169.254/latest")
        with self.assertRaises(IndahError):
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/x.xlsx")

    def test_download_size_is_capped(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        class Opener:
            def open(self, request, timeout):
                return Response(b"x" * 11)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(sio, "MAX_DOWNLOAD_BYTES", 10), \
             patch.object(sio.urllib.request, "build_opener", return_value=Opener()):
            with self.assertRaises(IndahError):
                sio.download_spreadsheet_url("https://docs.google.com/spreadsheets/d/abc/edit", Path(directory))

    def test_oversized_xlsx_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "big.xlsx"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("xl/worksheets/sheet1.xml", "0" * 5000)
            with patch.object(sio, "MAX_XLSX_UNCOMPRESSED_BYTES", 1000), self.assertRaises(IndahError):
                sio.read_xlsx_tables(path)


class SslTest(unittest.TestCase):
    def test_indah_client_verifies_certificates(self):
        client = IndahClient("token")
        self.assertEqual(client.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client.context.check_hostname)


class FormParserTest(unittest.TestCase):
    def test_multipart_keeps_binary_file_intact(self):
        payload = bytes(range(256)) * 4 + b"\r\n--not-a-boundary\r\n\n\r"
        boundary = "----TestBoundary123"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"source_mode\"\r\n\r\nworkbook\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"only_title\"\r\n\r\nJudul Ä é\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"workbook_file\"; filename=\"data.xlsx\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + payload + (
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"file_ms_kegiatan\"; filename=\"\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        form = ui.parse_form_body(f"multipart/form-data; boundary={boundary}", body)
        self.assertEqual(ui.form_text(form, "source_mode"), "workbook")
        self.assertEqual(ui.form_text(form, "only_title"), "Judul Ä é")
        item = ui.first_file_item(form, "workbook_file")
        self.assertEqual(item.filename, "data.xlsx")
        self.assertEqual(item.file.read(), payload)
        self.assertIsNone(ui.first_file_item(form, "file_ms_kegiatan"))
        self.assertEqual(ui.form_text(form, "missing"), "")

    def test_urlencoded_form(self):
        form = ui.parse_form_body("application/x-www-form-urlencoded", b"username=a%40b&password=p+w&captcha=")
        self.assertEqual(form.getfirst("username"), "a@b")
        self.assertEqual(form.getfirst("password"), "p w")
        self.assertEqual(form.getfirst("captcha"), "")

    def test_oversized_request_is_refused_before_reading(self):
        self.assertTrue(ui.request_too_large({"Content-Length": str(ui.MAX_REQUEST_BYTES + 1)}))
        self.assertTrue(ui.request_too_large({"Content-Length": "abc"}))
        self.assertFalse(ui.request_too_large({"Content-Length": "100"}))


class RunOwnershipTest(unittest.TestCase):
    def test_other_session_cannot_read_run(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            run_dir = runs / "kegiatan-20260101-000000-abcdef"
            run_dir.mkdir()
            owner_state = Path("/x/ui_sessions") / ("a" * 64 + ".json")
            ui.write_run_owner(run_dir, owner_state)
            (run_dir / "summary.json").write_text(json.dumps({"returncode": 0}), encoding="utf-8")
            (run_dir / "stdout.txt").write_text("[ok] satu\n", encoding="utf-8")
            with patch.object(ui, "UI_RUNS_ROOT", runs):
                mine = ui.run_status(run_dir.name, ui.session_owner("a" * 64))
                other = ui.run_status(run_dir.name, ui.session_owner("b" * 64))
                self.assertNotIn("Run tidak ditemukan", mine["output"])
                self.assertIn("Run tidak ditemukan", other["output"])
                self.assertIn("tidak ditemukan", ui.render_saved_run(run_dir.name, ui.session_owner("b" * 64)))

    def test_legacy_run_without_owner_is_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            (runs / "kegiatan-old").mkdir()
            with patch.object(ui, "UI_RUNS_ROOT", runs):
                self.assertIn("Run tidak ditemukan", ui.run_status("kegiatan-old", ui.session_owner("a" * 64))["output"])


class CleanupTest(unittest.TestCase):
    def run_cleanup(self, form):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(ui, "UI_RUNS_ROOT", root / "runs"), \
                 patch.object(ui, "parse_multipart", return_value=form), \
                 patch.object(ui, "run_state_for", return_value=root / "run_state" / "users" / "u"), \
                 patch.object(ui.threading, "Thread") as thread:
                html = ui.handle_cleanup_run("all", object(), root / "state.json")
                return html, thread

    def test_cleanup_requires_title_or_id(self):
        html, thread = self.run_cleanup(Form())
        self.assertIn("Isi judul atau nomor kegiatan", html)
        thread.assert_not_called()

    def test_cleanup_rejects_non_numeric_id(self):
        html, thread = self.run_cleanup(Form(cleanup_ms_keg_id="12; rm"))
        self.assertIn("Nomor kegiatan harus berupa angka", html)
        thread.assert_not_called()

    def test_cleanup_runs_in_background_for_one_kegiatan(self):
        html, thread = self.run_cleanup(Form(cleanup_title="Judul A"))
        self.assertIn("Penghapusan data berjalan", html)
        command = thread.call_args.kwargs["args"][3]
        self.assertIn("--title", command)
        self.assertNotIn("--from-kegiatan-map", command)
        self.assertIn(str(Path("users") / "u"), command[command.index("--run-state") + 1])
        thread.return_value.start.assert_called_once()

    def test_non_numeric_limit_is_rejected(self):
        with patch.object(ui, "run_state_for", return_value=Path("/tmp/rs")), self.assertRaises(IndahError):
            ui.build_phase_command("kegiatan", ui.PHASES["kegiatan"], Path("/tmp/p"),
                                   Form(submission_period="2026", limit="5; x"), Path("/tmp/s.json"))


class RunStateTest(unittest.TestCase):
    def test_user_dir_is_seeded_without_kegiatan_map(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("kegiatan_map.csv", "variabel_map.csv", "indikator_map.csv"):
                (root / name).write_text("a\n1\n", encoding="utf-8")
            target = user_run_state_dir("budi.santoso", root)
            self.assertEqual(target, root / "users" / "budi.santoso")
            self.assertTrue((target / "variabel_map.csv").exists())
            self.assertTrue((target / "indikator_map.csv").exists())
            self.assertFalse((target / "kegiatan_map.csv").exists())

    def test_username_cannot_escape_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = user_run_state_dir("../../etc", root)
            self.assertEqual(target.parent, root / "users")
            with self.assertRaises(IndahError):
                user_run_state_dir("", root)

    def test_concurrent_map_writes_lose_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kegiatan_map.csv"
            def writer(index):
                append_or_replace_map(path, ["judul_kegiatan", "ms_keg_id"], ["judul_kegiatan"],
                                      [{"judul_kegiatan": f"K{index}", "ms_keg_id": str(index)}])
            threads = [threading.Thread(target=writer, args=(i,)) for i in range(25)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(read_csv_rows(path)), 25)


class MaintenanceTest(unittest.TestCase):
    def test_interrupted_runs_are_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory)
            for name, code in (("a", None), ("b", 0)):
                (runs / name).mkdir()
                (runs / name / "summary.json").write_text(json.dumps({"returncode": code}), encoding="utf-8")
            with patch.object(ui, "UI_RUNS_ROOT", runs):
                self.assertEqual(ui.mark_interrupted_runs(), 1)
                self.assertEqual(ui.mark_interrupted_runs(), 0)
            self.assertIn("Proses terputus", (runs / "a" / "error.txt").read_text(encoding="utf-8"))
            self.assertFalse((runs / "b" / "error.txt").exists())
            self.assertTrue(ui.friendly_error(ui.INTERRUPTED_MESSAGE).startswith("Proses terputus"))

    def test_old_sessions_and_runs_are_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions, runs, uploads = root / "s", root / "r", root / "u"
            for folder in (sessions, runs, uploads):
                folder.mkdir()
            now = time.time()
            old = now - 400 * 24 * 3600
            (sessions / "old.json").write_text("{}")
            (sessions / "new.json").write_text("{}")
            os.utime(sessions / "old.json", (old, old))
            (runs / "old-run").mkdir()
            (runs / "new-run").mkdir()
            os.utime(runs / "old-run", (old, old))
            (uploads / "old-up").mkdir()
            os.utime(uploads / "old-up", (old, old))
            with patch.object(ui, "UI_SESSION_ROOT", sessions), patch.object(ui, "UI_RUNS_ROOT", runs), \
                 patch.object(ui, "UPLOAD_ROOT", uploads):
                removed = ui.prune_old_data(now)
            self.assertEqual(removed, {"sessions": 1, "runs": 1, "uploads": 1})
            self.assertTrue((sessions / "new.json").exists())
            self.assertTrue((runs / "new-run").exists())


if __name__ == "__main__":
    unittest.main()
