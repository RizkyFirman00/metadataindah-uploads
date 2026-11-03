"""Resolve indicators from live parent membership and verify writes before success."""

import json
from typing import Any, Dict, List, Optional, Tuple

from .shared import (
    IndahClient, IndahError, IndahHTTPError, as_int, build_indikator_payload, direct_submit_status,
    extract_created_id, first_result, title_key,
)


def _check_response(response: Any) -> Dict[str, Any]:
    if not isinstance(response, dict):
        raise IndahError("Hasil penyimpanan belum terverifikasi: respons INDAH tidak dapat dibaca.")
    status = as_int(response.get("status"))
    if status is not None and status >= 400:
        raise IndahHTTPError(status, f"HTTP {status}: INDAH menolak permintaan indikator.")
    return response


def live_indicators(client: IndahClient, parent_id: Any) -> List[Dict[str, Any]]:
    """Read the same complete child collection as the official kegiatan view."""
    response = _check_response(client.get(f"/sdi/v2/metadata-statistik/kegiatan/{parent_id}"))
    parent = first_result(response)
    _check_response(parent)
    if str(parent.get("id")) != str(parent_id) or not isinstance(parent.get("ms_ind"), list):
        raise IndahError("Hasil penyimpanan belum terverifikasi: daftar indikator pada kegiatan belum dapat dibaca. Data tidak dikirim agar tidak ganda.")
    items: List[Dict[str, Any]] = []
    seen = set()
    for item in parent["ms_ind"]:
        if not isinstance(item, dict) or item.get("id") is None or not _name(item):
            raise IndahError("Hasil penyimpanan belum terverifikasi: identitas indikator pada daftar INDAH tidak lengkap.")
        item_id = str(item["id"])
        if item_id in seen:
            raise IndahError("Hasil penyimpanan belum terverifikasi: daftar INDAH berisi ID berulang.")
        seen.add(item_id)
        items.append(item)
    return items


def _data(item: Dict[str, Any]) -> Dict[str, Any]:
    data = item.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _name(item: Dict[str, Any]) -> str:
    return str(_data(item).get("nama") or item.get("name") or item.get("nama") or "")


def _detail(client: IndahClient, item_id: Any, parent_id: Any, period: Any, name: str) -> Dict[str, Any]:
    response = _check_response(client.get(f"/sdi/v2/metadata-statistik/indikator/{item_id}"))
    # INDAH returns HTTP 200 / result:[null] for unavailable old indicator IDs.
    if as_int(response.get("status")) == 200 and response.get("result") == [None]:
        raise IndahHTTPError(404, f"Hasil penyimpanan belum terverifikasi: indikator ID {item_id} tidak ditemukan (INDAH mengembalikan hasil kosong).")
    item = first_result(response)
    _check_response(item)
    if str(item.get("id")) != str(item_id):
        raise IndahError("Hasil penyimpanan belum terverifikasi: ID indikator tidak ditemukan pada respons INDAH.")
    if str(item.get("ms_keg_id", item.get("msKegId"))) != str(parent_id):
        raise IndahError("Hasil penyimpanan belum terverifikasi: indikator tidak terhubung ke kegiatan yang dipilih.")
    if as_int(item.get("submission_period", item.get("submissionPeriod"))) != as_int(period):
        raise IndahError("Hasil penyimpanan belum terverifikasi: tahun indikator berbeda dari periode kegiatan.")
    if title_key(_name(item)) != title_key(name):
        raise IndahError("Hasil penyimpanan belum terverifikasi: nama indikator berbeda dari data yang dikirim.")
    if item.get("status") not in {"DRAFT", "SUBMITTED", "REVISED", "CORRECTION_REQUIRED", "UNDER_REVIEW", "APPROVED", "REJECTED"}:
        raise IndahError("Hasil penyimpanan belum terverifikasi: status indikator belum dapat dipastikan.")
    return item


def _matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _matches(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(_matches(a, b) for a, b in zip(expected, actual))
    if expected in (None, "") and actual in (None, ""):
        return True
    return expected == actual


def save_verified_indicator(
    client: IndahClient, row: Dict[str, str], indicator_children, variable_children,
    parent: Dict[str, Any], auth, *, final_submit: bool = False,
    mapped_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    parent_id = parent["id"]
    period = parent.get("submission_period", parent.get("submissionPeriod"))
    if as_int(period) is None:
        raise IndahError("Hasil penyimpanan belum terverifikasi: periode kegiatan belum diketahui.")
    parent = {**parent, "submission_period": period}
    name = row.get("nama") or row.get("indikator_nama") or ""
    candidates = [item for item in live_indicators(client, parent_id) if title_key(_name(item)) == title_key(name)]
    if len(candidates) > 1:
        raise IndahError("Hasil penyimpanan belum terverifikasi: ada lebih dari satu indikator dengan nama yang sama. Periksa data ganda di INDAH.")
    current = _detail(client, candidates[0]["id"], parent_id, period, name) if candidates else None
    if current is None and mapped_id:
        try:
            _detail(client, mapped_id, parent_id, period, name)
        except IndahHTTPError as exc:
            if exc.status_code not in {404, 410}:
                raise
        else:
            raise IndahError("Hasil penyimpanan belum terverifikasi: ID lama masih dapat dibuka tetapi tidak muncul pada daftar kegiatan. Data baru tidak dibuat agar tidak ganda.")
    if mapped_id and (current is None or str(current["id"]) != str(mapped_id)):
        print(f"[info] Referensi indikator lama {mapped_id} tidak dipakai; menggunakan daftar aktual dari kegiatan {parent_id}.", flush=True)
    status = direct_submit_status(current.get("status") if current else None) if final_submit else "DRAFT"
    payload = build_indikator_payload(row, indicator_children, variable_children, parent, auth, status=status)
    if current and final_submit and current.get("status") in {"SUBMITTED", "REVISED"} and _matches(payload["data"], _data(current)):
        return current, "sudah tersimpan dan tidak diubah"
    if current:
        _check_response(first_result(_check_response(client.update_indikator(current["id"], payload))))
        item_id = current["id"]
        action = "diperbarui"
    else:
        # Retrying a POST after HTTP 500 may create duplicates if the first write committed.
        try:
            response = _check_response(first_result(_check_response(client.create_indikator(payload, retry_server_errors=False))))
        except IndahHTTPError as exc:
            if exc.status_code not in {500, 502, 503, 504}:
                raise
            # Read back only: never repeat an uncertain POST automatically.
            recovered = [item for item in live_indicators(client, parent_id) if title_key(_name(item)) == title_key(name)]
            if len(recovered) != 1:
                raise IndahError("Hasil penyimpanan belum terverifikasi: INDAH mengalami gangguan saat menyimpan. Hasil belum dapat dipastikan; data tidak dikirim ulang otomatis agar tidak ganda.") from exc
            response = recovered[0]
        item_id = extract_created_id(response)
        if not item_id:
            raise IndahError("Hasil penyimpanan belum terverifikasi: INDAH belum memberikan ID. Periksa hasil di INDAH sebelum mencoba lagi.")
        action = "dibuat"
    saved = _detail(client, item_id, parent_id, period, name)
    if not _matches(payload["data"], _data(saved)) or saved.get("status") != status:
        raise IndahError("Hasil penyimpanan belum terverifikasi: isian atau status yang dibaca kembali belum sesuai. Periksa hasil di INDAH sebelum mencoba lagi.")
    if str(item_id) not in {str(item["id"]) for item in live_indicators(client, parent_id)}:
        raise IndahError("Hasil penyimpanan belum terverifikasi: indikator belum muncul pada daftar kegiatan. Jangan mengulang upload sebelum memeriksa hasil di INDAH.")
    return saved, action
