import copy
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import indah_indikator
from indah_automation.indicator_upload import live_indicators, save_verified_indicator
from indah_automation.shared import IndahClient, IndahError, IndahHTTPError, SessionAuth, build_indikator_payload


PARENT = {"id": 105383, "submission_period": 2026, "status": "DRAFT"}
AUTH = SessionAuth("fake", {}, {})


class FakeClient:
    def __init__(self):
        self.records = {}
        self.created = []
        self.updated = []
        self.next_id = 200000
        self.silent_update = False
        self.hide_created = False

    def seed(self, item_id, name, **fields):
        payload = build_indikator_payload({"nama": name, **fields}, [], [], PARENT, AUTH)
        self.records[item_id] = {**payload, "id": item_id}

    def get(self, path, params=None):
        if path == "/sdi/v2/metadata-statistik/kegiatan/105383":
            records = [r for r in self.records.values() if not (self.hide_created and r["id"] in self.created)]
            return {"status": 200, "result": [{**PARENT, "ms_ind": copy.deepcopy(records)}]}
        if not path.startswith("/sdi/v2/metadata-statistik/indikator/"):
            raise AssertionError(f"Unsupported endpoint: {path}")
        item_id = int(path.rsplit("/", 1)[-1])
        if item_id not in self.records:
            raise IndahHTTPError(404, "HTTP 404: indikator tidak ditemukan")
        return {"status": 200, "result": [copy.deepcopy(self.records[item_id])]}

    def create_indikator(self, payload, *, retry_server_errors=True):
        if retry_server_errors:
            raise AssertionError("Creating an indicator must disable POST retries")
        self.next_id += 1
        self.created.append(self.next_id)
        self.records[self.next_id] = {**copy.deepcopy(payload), "id": self.next_id}
        return {"id": self.next_id, "status": "DRAFT"}

    def update_indikator(self, item_id, payload):
        self.updated.append(item_id)
        if not self.silent_update:
            self.records[item_id] = {**copy.deepcopy(payload), "id": item_id}
        return {"status": 200, "result": []}


class VerifiedIndicatorTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()

    def save(self, name="Uji", **kwargs):
        return save_verified_indicator(self.client, {"nama": name}, [], [], PARENT, AUTH, **kwargs)

    def test_stale_map_creates_and_verifies_replacement(self):
        saved, action = self.save(mapped_id="162401")
        self.assertEqual(action, "dibuat")
        self.assertNotEqual(saved["id"], 162401)
        self.assertEqual(len(self.client.created), 1)
        self.assertEqual(self.client.updated, [])

    def test_reupload_uses_live_record_even_without_map(self):
        first, _ = self.save(mapped_id="162401")
        second, action = self.save(mapped_id="162401")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(action, "diperbarui")
        self.assertEqual(len(self.client.created), 1)

    def test_numeric_http_status_is_not_saved_as_resource_status(self):
        self.client.seed(99, "Uji")
        saved, _ = self.save(mapped_id="99")
        self.assertEqual(saved["status"], "DRAFT")

    def test_final_submit_uses_live_status_not_stale_map(self):
        self.client.seed(99, "Uji")
        saved, _ = self.save(final_submit=True, mapped_id="99")
        self.assertEqual(saved["status"], "SUBMITTED")
        saved, action = self.save(final_submit=True, mapped_id="99")
        self.assertEqual(saved["status"], "SUBMITTED")
        self.assertEqual(action, "sudah tersimpan dan tidak diubah")
        self.assertEqual(self.client.updated, [99])

    def test_observed_200_null_response_replaces_only_absent_old_id(self):
        original = self.client.get
        for old_id in (162401, 162403):
            def get(path, params=None):
                if path == f"/sdi/v2/metadata-statistik/indikator/{old_id}":
                    return {"status": 200, "result": [None]}
                return original(path, params)
            with patch.object(self.client, "get", side_effect=get):
                saved, action = self.save(name=f"Indikator {old_id}", mapped_id=str(old_id))
            self.assertEqual(action, "dibuat")
            self.assertNotEqual(saved["id"], old_id)
        self.assertEqual(len(self.client.created), 2)

    def test_null_detail_for_listed_indicator_does_not_authorize_create(self):
        self.client.seed(99, "Uji")
        original = self.client.get
        def get(path, params=None):
            if path.endswith("/indikator/99"):
                return {"status":200, "result":[None]}
            return original(path, params)
        with patch.object(self.client, "get", side_effect=get), self.assertRaises(IndahHTTPError):
            self.save(mapped_id="99")
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.client.updated, [])

    def test_only_explicit_200_single_null_authorizes_missing_mapped_id(self):
        original = self.client.get
        for response in ({}, {"status":200,"result":[]}, {"status":200,"result":None},
                         {"status":200,"result":[None,None]}, {"status":500,"result":[None]},
                         {"result":[None]}):
            def get(path, params=None):
                if path.endswith("/indikator/162401"):
                    return response
                return original(path, params)
            with self.subTest(response=response), patch.object(self.client,"get",side_effect=get), self.assertRaises(IndahError):
                self.save(mapped_id="162401")
        self.assertEqual(self.client.created, [])

    def test_post_500_after_commit_recovers_without_second_post(self):
        create = self.client.create_indikator
        def commit_then_fail(payload, **kwargs):
            create(payload, **kwargs)
            raise IndahHTTPError(500, "HTTP 500 POST")
        with patch.object(self.client,"create_indikator",side_effect=commit_then_fail) as post:
            saved, action = self.save()
        self.assertEqual((saved["id"],action), (200001,"dibuat"))
        self.assertEqual(post.call_count, 1)

    def test_post_500_without_visible_record_is_not_success_or_retried(self):
        with patch.object(self.client,"create_indikator",side_effect=IndahHTTPError(500,"HTTP 500 POST")) as post:
            with self.assertRaisesRegex(IndahError,"tidak dikirim ulang otomatis"):
                self.save()
        self.assertEqual(post.call_count, 1)

    def test_post_500_with_incorrect_saved_content_stays_failed(self):
        create = self.client.create_indikator
        def commit_wrong_then_fail(payload, **kwargs):
            response = create(payload, **kwargs)
            self.client.records[response["id"]]["data"]["definisi"] = "Beda"
            raise IndahHTTPError(500,"HTTP 500 POST")
        with patch.object(self.client,"create_indikator",side_effect=commit_wrong_then_fail) as post:
            with self.assertRaisesRegex(IndahError,"dibaca kembali belum sesuai"):
                self.save()
        self.assertEqual(post.call_count, 1)

    def test_nested_update_error_is_not_hidden_by_unchanged_readback(self):
        self.client.seed(99, "Uji")
        with patch.object(self.client,"update_indikator",return_value={"status":200,"result":[{"status":403}]}), self.assertRaises(IndahHTTPError):
            self.save()

    def test_resume_four_submitted_two_null_and_one_temporary_post_failure(self):
        names = ["Retribusi", "Jenis Pajak", "Pajak Bulanan", "BBN-KB", "Kendaraan", "Capaian", "Realisasi"]
        for index in (1, 3, 5, 6):
            self.client.seed(100 + index, names[index])
            self.client.records[100 + index]["status"] = "SUBMITTED"
        original_get = self.client.get
        original_create = self.client.create_indikator
        failed_once = False
        def get(path, params=None):
            if path.endswith("/indikator/162401") or path.endswith("/indikator/162403"):
                return {"status":200,"result":[None]}
            return original_get(path, params)
        def create(payload, **kwargs):
            nonlocal failed_once
            if payload["data"]["nama"] == "Retribusi" and not failed_once:
                failed_once = True
                raise IndahHTTPError(500,"HTTP 500 POST")
            return original_create(payload, **kwargs)
        with patch.object(self.client,"get",side_effect=get), patch.object(self.client,"create_indikator",side_effect=create):
            for index, name in enumerate(names):
                kwargs = {"name":name,"final_submit":True,"mapped_id":{2:"162401",4:"162403"}.get(index)}
                if index == 0:
                    with self.assertRaises(IndahError):
                        self.save(**kwargs)
                else:
                    self.save(**kwargs)
            self.assertEqual(len(self.client.records),6)
            self.assertEqual(self.client.updated,[])
            for index, name in enumerate(names):
                self.save(name=name, final_submit=True, mapped_id={2:"162401",4:"162403"}.get(index))
        self.assertEqual(len(self.client.records),7)
        self.assertEqual(len(self.client.created),3)
        self.assertEqual(self.client.updated,[])
        self.assertTrue(all(item["status"] == "SUBMITTED" for item in self.client.records.values()))

    def test_unreadable_lists_never_authorize_creation(self):
        cases = [{}, {"status": 500}, {"result": [PARENT]},
                 {"result": [{**PARENT, "ms_ind": None}]},
                 {"result": [{**PARENT, "id": 999, "ms_ind": []}]},
                 {"result": [{**PARENT, "ms_ind": [{"id": 99}]}]}]
        for response in cases:
            with self.subTest(response=response), patch.object(self.client, "get", return_value=response):
                with self.assertRaises(IndahError):
                    self.save(mapped_id="162401")
        self.assertEqual(self.client.created, [])
        self.assertEqual(self.client.updated, [])

    def test_401_and_500_do_not_trigger_fallback_create(self):
        for code in (401, 403, 500):
            with self.subTest(code=code), patch.object(self.client, "get", side_effect=IndahError(f"HTTP {code}")):
                with self.assertRaises(IndahError):
                    self.save(mapped_id="162401")
        self.assertEqual(self.client.created, [])

    def test_duplicate_names_require_manual_resolution(self):
        self.client.seed(1, "Uji")
        self.client.seed(2, "Uji")
        with self.assertRaisesRegex(IndahError, "lebih dari satu"):
            self.save()
        self.assertEqual(self.client.updated, [])

    def test_wrong_parent_period_or_identity_never_updated(self):
        for field, wrong in (("ms_keg_id", 999), ("submission_period", 2025), ("id", 88), ("status", 200)):
            with self.subTest(field=field):
                self.client = FakeClient()
                self.client.seed(99, "Uji")
                self.client.records[99][field] = wrong
                # Listing identity must stay as requested to test detail validation.
                original_get = self.client.get
                def get(path, params=None):
                    if path == "/sdi/v2/metadata-statistik/kegiatan/105383":
                        return {"result": [{**PARENT,"ms_ind": [{"id":99,"data":{"nama":"Uji"}}]}]}
                    return original_get(path, params)
                with patch.object(self.client, "get", side_effect=get), self.assertRaises(IndahError):
                    self.save()
                self.assertEqual(self.client.updated, [])

    def test_successful_http_but_unchanged_content_is_failure(self):
        self.client.seed(99, "Uji", definisi="Belum diperbarui")
        self.client.silent_update = True
        with self.assertRaisesRegex(IndahError, "dibaca kembali belum sesuai"):
            self.save()
        self.assertEqual(self.client.created, [])

    def test_missing_membership_after_create_is_not_success(self):
        self.client.hide_created = True
        with self.assertRaisesRegex(IndahError, "belum muncul"):
            self.save()
        self.assertEqual(len(self.client.created), 1)

    def test_post_error_is_not_retried_by_workflow(self):
        with patch.object(self.client, "create_indikator", side_effect=IndahError("HTTP 500")) as create:
            with self.assertRaises(IndahError):
                self.save()
            self.assertEqual(create.call_count, 1)

    def test_old_id_still_exists_but_is_hidden_never_recreated(self):
        self.client.seed(99,"Uji")
        original=self.client.get
        def get(path, params=None):
            if path == "/sdi/v2/metadata-statistik/kegiatan/105383":
                return {"result":[{**PARENT,"ms_ind":[]}]}
            return original(path, params)
        with patch.object(self.client,"get",side_effect=get), self.assertRaisesRegex(IndahError,"masih dapat dibuka"):
            self.save(mapped_id="99")
        self.assertEqual(self.client.created,[])

    def test_missing_membership_with_detail_500_is_not_missing_record(self):
        original=self.client.get
        def get(path, params=None):
            if path == "/sdi/v2/metadata-statistik/kegiatan/105383":
                return original(path,params)
            raise IndahHTTPError(500,"HTTP 500")
        with patch.object(self.client,"get",side_effect=get), self.assertRaises(IndahHTTPError):
            self.save(mapped_id="162401")
        self.assertEqual(self.client.created,[])

    def test_http_transport_does_not_retry_creation(self):
        client = IndahClient("fake")
        error = HTTPError("https://example.invalid", 500, "server error", {}, io.BytesIO(b"{}"))
        with patch("indah_automation.shared.urllib.request.urlopen", side_effect=error) as request:
            with self.assertRaises(IndahError):
                client.create_indikator({}, retry_server_errors=False)
            self.assertEqual(request.call_count, 1)

    def test_parent_collection_must_have_unique_ids(self):
        response = {"result": [{**PARENT, "ms_ind": [{"id":1,"data":{"nama":"A"}}] * 2}]}
        with patch.object(self.client, "get", return_value=response), self.assertRaisesRegex(IndahError, "ID berulang"):
            self.save()
        self.assertEqual(self.client.created, [])

    def test_official_parent_collection_is_not_limited_to_ten_rows(self):
        for item_id in range(1, 26):
            self.client.seed(item_id, f"Lain {item_id}")
        self.client.seed(99, "Uji")
        with patch.object(self.client, "get", wraps=self.client.get) as get:
            saved, action = self.save(mapped_id="162401")
            self.assertTrue(all(call.args[0].startswith("/sdi/v2/metadata-statistik/") for call in get.call_args_list))
            self.assertTrue(all(call.kwargs == {} for call in get.call_args_list))
        self.assertEqual((saved["id"], action), (99, "diperbarui"))
        self.assertEqual(self.client.created, [])

    def test_seven_rows_with_two_stale_ids_end_with_seven_verified_records(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            title = "Kompilasi Data Badan Pendapatan Daerah Provinsi DKI Jakarta"
            names = ["Retribusi", "Jenis Pajak", "Pajak Bulanan", "BBN-KB", "Kendaraan Pembayar Pajak", "Capaian", "Realisasi"]
            rows = [{"judul_kegiatan":title,"nama":name} for name in names]
            with (folder / "ms_indikator.csv").open("w",newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["judul_kegiatan","nama"])
                writer.writeheader()
                writer.writerows(rows)
            state = folder / "run_state"
            state.mkdir()
            with (state / "indikator_map.csv").open("w",newline="") as f:
                keys = ["judul_kegiatan","ms_keg_id","nama_indikator","ms_ind_id","status"]
                writer=csv.DictWriter(f,fieldnames=keys)
                writer.writeheader()
                for index,name in enumerate(names):
                    item_id = {2:162401,4:162403}.get(index, 100 + index)
                    writer.writerow(dict(zip(keys,[title,105383,name,item_id,200])))
                    if index not in (2,4):
                        self.client.seed(item_id,name)
            args = ["indah_indikator.py","--folder",str(folder),"--run-state",str(state),"--submission-period","2026","--submit"]
            with patch.object(sys,"argv",args), patch.object(indah_indikator,"load_auth",return_value=AUTH), \
                 patch.object(indah_indikator,"IndahClient",return_value=self.client), \
                 patch.object(indah_indikator,"resolve_ms_keg_reference",return_value=("105383",PARENT,"direct")), \
                 patch("sys.stdout",new_callable=io.StringIO) as output:
                indah_indikator.main()
            self.assertEqual(len(self.client.records),7)
            self.assertEqual(len(self.client.created),2)
            self.assertEqual(len(self.client.updated),5)
            self.assertEqual(output.getvalue().count('"outcome": "success"'),7)
            with (state / "indikator_map.csv").open() as f:
                mapped=list(csv.DictReader(f))
            self.assertEqual(len(mapped),7)
            self.assertTrue(all(r["status"] == "DRAFT" for r in mapped))
            self.assertFalse({"162401","162403"} & {r["ms_ind_id"] for r in mapped})

    def test_unverified_update_is_reported_failed_and_map_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "ms_indikator.csv").write_text("judul_kegiatan,nama\nKegiatan,Uji\n")
            state = folder / "state"
            state.mkdir()
            path = state / "indikator_map.csv"
            original = "judul_kegiatan,ms_keg_id,nama_indikator,ms_ind_id,status\nKegiatan,105383,Uji,99,200\n"
            path.write_text(original)
            self.client.seed(99,"Uji",definisi="Isian lama")
            self.client.silent_update = True
            args = ["indah_indikator.py","--folder",str(folder),"--run-state",str(state),"--submit"]
            with patch.object(sys,"argv",args), patch.object(indah_indikator,"load_auth",return_value=AUTH), \
                 patch.object(indah_indikator,"IndahClient",return_value=self.client), \
                 patch.object(indah_indikator,"resolve_ms_keg_reference",return_value=("105383",PARENT,"direct")), \
                 patch("sys.stdout",new_callable=io.StringIO) as output:
                with self.assertRaises(SystemExit) as failure:
                    indah_indikator.main()
            self.assertEqual(failure.exception.code,1)
            self.assertIn('"outcome": "failed"',output.getvalue())
            self.assertNotIn('"outcome": "success"',output.getvalue())
            self.assertNotIn("[ok]",output.getvalue())
            self.assertEqual(path.read_text(),original)


if __name__ == "__main__":
    unittest.main()
