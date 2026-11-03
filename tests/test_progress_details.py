"""Test untuk progres langsung: rincian per data, waktu, perkiraan sisa, retry, dan batal."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import indah_ui as ui
import indah_cleanup as cleanup
from indah_automation.shared import IndahError


def event(stage, current, total=3, **fields):
    return "INDAH_PROGRESS " + json.dumps(dict(stage=stage, current=current, total=total, **fields))


def write_timeline(run_dir, entries):
    with (run_dir / "timeline.jsonl").open("w", encoding="utf-8") as handle:
        for stamp, line in entries:
            handle.write(json.dumps({"t": stamp, "line": line}) + "\n")


class RunDetailsTest(unittest.TestCase):
    def make_run(self, name="indikator-20261103-000000-abcdef"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        run_dir = Path(directory.name) / name
        run_dir.mkdir()
        return run_dir

    def test_items_timing_and_eta(self):
        run_dir = self.make_run()
        write_timeline(run_dir, [
            (100.0, event("validation", 0)),
            (100.0, event("upload", 1, title="Indikator A")),
            (102.0, event("item", 1, title="Indikator A", outcome="success")),
            (102.0, event("upload", 2, title="Indikator B")),
            (106.0, event("item", 2, title="Indikator B", outcome="failed", message="HTTP 401 token expired")),
            (106.0, event("upload", 3, title="Indikator C")),
        ])
        details = ui.run_details(run_dir, "", {"started_at": 98.0}, False, 1, now=107.0)
        self.assertEqual(details["stage"], "upload")
        self.assertEqual(details["stage_index"], 2)
        self.assertEqual(details["now_text"], "Mengirim data 3 dari 3")
        self.assertEqual(details["now_title"], "Indikator C")
        self.assertEqual([item["label"] for item in details["items"]], ["Tersimpan", "Gagal"])
        self.assertIn("Sesi login", details["items"][1]["message"])
        self.assertNotIn("token expired", details["items"][1]["message"])
        self.assertEqual(details["elapsed_text"], "9 dtk")
        self.assertEqual(details["avg_text"], "3 dtk / data")
        self.assertEqual(details["eta_text"], "3 dtk")

    def test_retry_and_abort_and_warnings_are_reported(self):
        run_dir = self.make_run()
        write_timeline(run_dir, [
            (100.0, event("upload", 1, title="A")),
            (101.0, "[retry 1/2] HTTP 503 POST /sdi — coba lagi dalam 2s"),
            (101.5, "[warn] A / B: kolom rumus berisi '?'."),
            (102.0, "[abort] Submit dibatalkan: ada 2 row skip."),
        ])
        details = ui.run_details(run_dir, "", {"started_at": 99.0}, False, 2, now=110.0)
        self.assertIn("mencoba mengirim ulang", details["retry"])
        self.assertIn("Tidak ada data yang dikirim", details["aborted"])
        self.assertEqual(details["warnings"], ["A / B: kolom rumus berisi '?'."])
        old = ui.run_details(run_dir, "", {"started_at": 99.0}, False, 2, now=200.0)
        self.assertEqual(old["retry"], "")

    def test_finished_run_uses_finish_time(self):
        run_dir = self.make_run()
        write_timeline(run_dir, [(100.0, event("item", 1, total=1, title="A", outcome="success"))])
        details = ui.run_details(run_dir, "", {"started_at": 40.0, "finished_at": 160.0}, True, 0, now=999.0)
        self.assertEqual(details["elapsed_text"], "2 mnt 0 dtk")
        self.assertEqual(details["now_text"], "Proses selesai")
        self.assertEqual(details["stage_index"], 4)

    def test_old_run_without_timeline_still_lists_items(self):
        run_dir = self.make_run()
        stdout = event("item", 1, total=1, title="A", outcome="skipped", message="tahun kosong") + "\n"
        details = ui.run_details(run_dir, stdout, {}, True, 0)
        self.assertEqual(details["items"][0]["label"], "Dilewati")
        self.assertEqual(details["elapsed_text"], "-")

    def test_cleanup_labels(self):
        run_dir = self.make_run("cleanup-all-20261103-000000-abcdef")
        write_timeline(run_dir, [(1.0, event("upload", 1, total=1, title="Variabel · K / V"))])
        details = ui.run_details(run_dir, "", {"started_at": 1.0}, False, 1, now=2.0)
        self.assertEqual(details["now_text"], "Menghapus data 1 dari 1")

    def test_progress_and_result_pages_render(self):
        html = ui.render_run_progress("indikator-x", "indikator")
        for marker in ('id="stages"', 'id="items"', 'id="t-eta"', 'id="s-left"', "boleh ditutup"):
            self.assertIn(marker, html)
        details = {"items": [{"n": 1, "title": "<b>A</b>", "outcome": "failed", "label": "Gagal",
                              "message": "x", "time": "10.00.00"}],
                   "started_text": "-", "finished_text": "-", "elapsed_text": "1 dtk", "avg_text": "-", "aborted": ""}
        result = ui.render_run_result("indikator-x", "indikator", {}, [], 1, "", "", details)
        self.assertIn("Rincian per data", result)
        self.assertIn("&lt;b&gt;A&lt;/b&gt;", result)
        self.assertIn("Lama proses", result)


class FakeClient:
    def __init__(self, fail_ids):
        self.fail_ids = fail_ids

    def delete_variabel(self, item_id):
        if item_id in self.fail_ids:
            raise IndahError("HTTP 403 forbidden")


class CleanupProgressTest(unittest.TestCase):
    def test_cleanup_emits_progress_per_item(self):
        target = {"id": "10", "title": "Kegiatan A", "status": "DRAFT"}
        children = [{"id": "1", "data": {"nama": "V1"}}, {"id": "2", "data": {"nama": "V2"}}]
        with tempfile.TemporaryDirectory() as directory, io.StringIO() as buffer, redirect_stdout(buffer):
            deleted, failed = cleanup.cleanup_phase_by_kegiatan(
                "variabel", [(target, children)], [target], Path(directory), FakeClient({"2"}), execute=True)
            output = buffer.getvalue()
        self.assertEqual((deleted, failed), (1, 1))
        events = [json.loads(line[len("INDAH_PROGRESS "):]) for line in output.splitlines() if line.startswith("INDAH_PROGRESS ")]
        outcomes = [(e["current"], e["total"], e.get("outcome")) for e in events if e["stage"] == "item"]
        self.assertEqual(outcomes, [(1, 2, "success"), (2, 2, "failed")])
        self.assertTrue(events[0]["title"].startswith("Variabel · Kegiatan A / V1"))


if __name__ == "__main__":
    unittest.main()
