import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import indah_ui as ui


class Form:
    def __init__(self, **values):
        self.values = values

    def getfirst(self, name, default=""):
        return self.values.get(name, default)


class WorkflowTest(unittest.TestCase):
    def test_selected_link_does_not_upload_inactive_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(ui, "UPLOAD_ROOT", root / "uploads"), patch.object(ui, "UI_RUNS_ROOT", root / "runs"), \
                 patch.object(ui, "parse_multipart", return_value=Form(source_mode="url", spreadsheet_url="https://example.com/sheet")), \
                 patch.object(ui, "save_specific_files") as individual, patch.object(ui, "save_workbook_files") as workbook, \
                 patch.object(ui, "download_spreadsheet_url", return_value=root / "sheet.xlsx") as download, \
                 patch.object(ui, "prepare_phase_sources", return_value={}) as prepare, \
                 patch.object(ui, "build_phase_command", return_value=[]), patch.object(ui.threading, "Thread") as thread:
                ui.handle_phase_run("kegiatan", object(), root / "state.json")
                individual.assert_not_called()
                workbook.assert_not_called()
                download.assert_called_once()
                self.assertEqual(prepare.call_args.kwargs["specific_files"], {})
                thread.return_value.start.assert_called_once()

    def test_missing_workbook_stops_before_preparation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(ui, "UPLOAD_ROOT", root / "uploads"), patch.object(ui, "UI_RUNS_ROOT", root / "runs"), \
                 patch.object(ui, "parse_multipart", return_value=Form(source_mode="workbook")), \
                 patch.object(ui, "save_workbook_files", return_value=[]), \
                 patch.object(ui, "prepare_phase_sources") as prepare, patch.object(ui.threading, "Thread") as thread:
                result = ui.handle_phase_run("kegiatan", object(), root / "state.json")
                self.assertIn("File Excel belum dipilih", result)
                prepare.assert_not_called()
                thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
