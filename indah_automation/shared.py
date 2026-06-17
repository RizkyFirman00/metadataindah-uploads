from __future__ import annotations

import csv
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_API_URL = "https://indah-api.bps.go.id"
WEB_URL = "https://indah.bps.go.id/metadata-dashboard"
DEFAULT_STATE_PATH = PROJECT_ROOT / ".indah_session" / "storage_state.json"
DEFAULT_RUN_STATE_DIR = PROJECT_ROOT / "run_state"


class IndahError(RuntimeError):
    pass


@dataclass
class SessionAuth:
    token: str
    user_raw: Dict[str, Any]
    user_payload: Dict[str, Any]


def clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def pick(row: Dict[str, Any], *keys: str, default: Optional[str] = None) -> Optional[str]:
    lower_map = {k.lower().strip(): v for k, v in row.items()}
    for key in keys:
        value = clean(lower_map.get(key.lower().strip()))
        if value is not None:
            return value
    return default


def as_int(value: Any) -> Optional[int]:
    text = clean(value)
    if text is None:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def as_bool(value: Any) -> Optional[bool]:
    text = clean(value)
    if text is None:
        return None
    normalized = text.lower()
    if normalized in {"true", "1", "ya", "y", "yes", "iya"}:
        return True
    if normalized in {"false", "0", "tidak", "no", "n"}:
        return False
    return None


ENUM_ALIASES = {
    "PENCACAHAN LENGKAP": "PENCACAHAN_LENGKAP",
    "SURVEI": "SURVEI",
    "KOMPILASI PRODUK ADMINISTRASI": "KOMPILASI_PRODUK_ADMINISTRASI",
    "CARA LAIN SESUAI PERKEMBANGAN TEKNOLOGI DAN INFORMASI": "CARA_LAIN_SESUAI_PERKEMBANGAN_TEKNOLOGI_DAN_INFORMASI",
    "STATISTIK DASAR": "STATISTIK_DASAR",
    "STATISTIK SEKTORAL": "STATISTIK_SEKTORAL",
    "STATISTIK KHUSUS": "STATISTIK_KHUSUS",
    "BARU": "BARU",
    "BERULANG": "BERULANG",
    "SEKALI": "SEKALI",
    "HARIAN": "HARIAN",
    "MINGGUAN": "MINGGUAN",
    "BULANAN": "BULANAN",
    "TRIWULANAN": "TRIWULANAN",
    "SEMESTERAN": "SEMESTERAN",
    "TAHUNAN": "TAHUNAN",
    "LAINNYA": "LAINNYA",
    "LONGITUDINAL/PANEL": "LONGITUDINAL_PANEL",
    "LONGITUDINAL PANEL": "LONGITUDINAL_PANEL",
    "CROSS SECTIONAL": "CROSS_SECTIONAL",
    "CROSS-SECTIONAL": "CROSS_SECTIONAL",
    "SELURUH WILAYAH INDONESIA": "SELURUH_WILAYAH_INDONESIA",
    "SEBAGIAN WILAYAH INDONESIA": "SEBAGIAN_WILAYAH_INDONESIA",
    "SIMPLE RANDOM SAMPLING": "SIMPLE_RANDOM_SAMPLING",
    "SYSTEMATIC RANDOM SAMPLING": "SYSTEMATIC_RANDOM_SAMPLING",
    "STRATIFIED RANDOM SAMPLING": "STRATIFIED_RANDOM_SAMPLING",
    "CLUSTER SAMPLING": "CLUSTER_SAMPLING",
    "MULTI STAGE SAMPLING": "MULTI_STAGE_SAMPLING",
    "SINGLE STAGE/PHASE": "SINGLE_STAGE_PHASE",
    "MULTI STAGE/PHASE": "MULTI_STAGE_PHASE",
    "SAMPEL PROBABILITAS": "SAMPEL_PROBABILITAS",
    "SAMPEL NONPROBABILITAS": "SAMPEL_NONPROBABILITAS",
    "STAF INSTANSI PENYELENGGARA": "STAF_INSTANSI_PENYELENGGARA",
    "MITRA/TENAGA KONTRAK": "MITRA_ATAU_TENAGA_KONTRAK",
    "MITRA ATAU TENAGA KONTRAK": "MITRA_ATAU_TENAGA_KONTRAK",
    "STAF INSTANSI PENYELENGGARA DAN MITRA/TENAGA KONTRAK": "STAF_INSTANSI_PENYELENGGARA_DAN_MITRA_ATAU_TENAGA_KONTRAK",
    "STAF INSTANSI PENYELENGGARA DAN MITRA ATAU TENAGA KONTRAK": "STAF_INSTANSI_PENYELENGGARA_DAN_MITRA_ATAU_TENAGA_KONTRAK",
    "SMA/SMK": "SMA_ATAU_SMK",
    "SMA ATAU SMK": "SMA_ATAU_SMK",
    "DIPLOMA I/II/III": "DIPLOMA_I_II_III",
    "DIPLOMA I II III": "DIPLOMA_I_II_III",
    "DIPLOMA IV/S1/S2/S3": "DIPLOMA_IV_S1_S2_S3",
    "DIPLOMA IV S1 S2 S3": "DIPLOMA_IV_S1_S2_S3",
    "DESKRIPTIF": "DESKRIPTIF",
    "INFERENSIA": "INFERENSIA",
}


def enum_value(value: Any) -> Optional[str]:
    text = clean(value)
    if text is None:
        return None
    upper = text.upper().strip()
    if upper in ENUM_ALIASES:
        return ENUM_ALIASES[upper]
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9]+", "_", upper)).strip("_")


def bool_or_false(value: Any) -> bool:
    parsed = as_bool(value)
    return False if parsed is None else parsed


def split_multi(value: Any) -> List[str]:
    text = clean(value)
    if text is None:
        return []
    delimiter = "|" if "|" in text else ";"
    return [part.strip() for part in text.split(delimiter) if part.strip()]


def nullable_list(value: Any) -> List[Optional[str]]:
    parts = split_multi(value)
    return parts if parts else [None]


def list_or_default(value: Any, default: str) -> List[str]:
    parts = split_multi(value)
    return parts if parts else [default]


def list_dicts_from_names(value: Any, *, source: Optional[str] = None, default_name: Optional[str] = None) -> List[Dict[str, Any]]:
    names = split_multi(value)
    if not names:
        if default_name is None:
            return [{"nama": None, "sumber": source or "input-manual"}]
        names = [default_name]
    return [{"nama": name, "sumber": source or "input-manual"} for name in names]


def maybe_id(value: Any) -> Any:
    text = clean(value)
    if text is None:
        return None
    number = as_int(text)
    return number if str(number) == text or text.replace(".0", "") == str(number) else text


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            {clean(k) or "": clean(v) or "" for k, v in row.items()}
            for row in reader
            if any(clean(v) for v in row.values())
        ]


def find_csv(folder: Path, slug: str, *, required: bool = True) -> Optional[Path]:
    folder = folder.expanduser().resolve()
    if folder.is_file():
        return folder
    if not folder.exists():
        if required:
            raise IndahError(f"Folder tidak ditemukan: {folder}")
        return None
    candidates = sorted(folder.rglob("*.csv"))
    slug = slug.lower().replace(".csv", "")
    exact = [
        path for path in candidates
        if path.stem.lower().strip() == slug or path.stem.lower().strip().endswith(slug)
    ]
    if exact:
        return exact[0]
    loose = [path for path in candidates if slug in path.stem.lower()]
    if loose:
        return loose[0]
    if required:
        raise IndahError(f"Tidak menemukan CSV untuk '{slug}' di {folder}")
    return None


def rows_for_title(rows: Sequence[Dict[str, str]], title: str) -> List[Dict[str, str]]:
    wanted = (title or "").strip().lower()
    return [row for row in rows if (pick(row, "judul_kegiatan") or "").strip().lower() == wanted]


def write_csv(path: Path, fieldnames: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def append_or_replace_map(path: Path, fieldnames: Sequence[str], key_fields: Sequence[str], new_rows: Sequence[Dict[str, Any]]) -> None:
    existing = read_csv_rows(path)
    by_key: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for row in existing:
        by_key[tuple(str(row.get(k, "")) for k in key_fields)] = row
    for row in new_rows:
        by_key[tuple(str(row.get(k, "")) for k in key_fields)] = row
    write_csv(path, fieldnames, list(by_key.values()))


def _local_storage_value(storage_state: Dict[str, Any], name: str) -> Optional[str]:
    for origin in storage_state.get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == name:
                return item.get("value")
    return None


def load_auth(state_path: Path = DEFAULT_STATE_PATH) -> SessionAuth:
    if not state_path.exists():
        raise IndahError(
            f"Session belum ada: {state_path}. Jalankan `python indah_login.py` lalu login manual dulu."
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    vuex_text = _local_storage_value(state, "vuex")
    if not vuex_text:
        raise IndahError("Local storage `vuex` tidak ditemukan. Ulangi login manual.")
    vuex = json.loads(vuex_text)
    auth = vuex.get("auth") or {}
    token = auth.get("token")
    user = auth.get("user")
    if not token or not user:
        raise IndahError("Token/user tidak ditemukan di session. Ulangi login manual.")
    return SessionAuth(token=token, user_raw=user, user_payload=user_payload(user))


def user_payload(user: Dict[str, Any]) -> Dict[str, Any]:
    organization = user.get("sdiOrganization") or {}
    details = organization.get("details") or {}
    return {
        "username": user.get("username"),
        "name": user.get("name"),
        "email": user.get("email"),
        "organization": {
            "id": organization.get("id"),
            "name": organization.get("name"),
            "address": details.get("address"),
            "province": details.get("province"),
            "city": details.get("city"),
        },
    }


def org_from_user(user_payload_value: Dict[str, Any]) -> Dict[str, Any]:
    org = user_payload_value.get("organization") or {}
    province = org.get("province") or {}
    city = org.get("city") or {}
    return {
        "id": org.get("id"),
        "name": org.get("name"),
        "province_code": province.get("code"),
        "city_code": city.get("code"),
    }


class IndahClient:
    def __init__(self, token: str, base_url: str = BASE_API_URL):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.context = ssl._create_unverified_context()

    def request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self.base_url + path
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = url + ("&" if "?" in url else "?") + query
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=120) as response:
                text = response.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="replace")
            raise IndahError(f"HTTP {exc.code} {method.upper()} {path}: {text}") from exc

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.request("GET", path, params=params)

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", path, payload=payload)

    def put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("PUT", path, payload=payload)

    def delete(self, path: str) -> Dict[str, Any]:
        return self.request("DELETE", path)

    def get_ms_keg(self, ms_keg_id: Any) -> Dict[str, Any]:
        return first_result(self.get(f"/sdi/v2/metadata-statistik/kegiatan/{ms_keg_id}"))

    def list_ms_keg(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        response = self.get("/sdi/v2/metadata-statistik/kegiatan", params=params)
        result = response.get("result")
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        if isinstance(result, dict):
            return result
        return {}

    def find_ms_keg_by_title(
        self,
        title: str,
        *,
        submission_period: Optional[Any] = None,
        max_scan_pages: int = 2,
    ) -> Optional[Dict[str, Any]]:
        wanted = title_key(title)
        if not wanted:
            return None

        candidates: List[Dict[str, Any]] = []
        seen_ids = set()

        def collect(params: Dict[str, Any], pages: int) -> None:
            for page_number in range(max(1, pages)):
                query = {**params, "page": page_number, "size": 50}
                try:
                    page = self.list_ms_keg(query)
                except IndahError:
                    continue
                for item in page.get("content") or []:
                    if not isinstance(item, dict):
                        continue
                    item_id = item.get("id")
                    if item_id in seen_ids:
                        continue
                    if title_key(ms_keg_title(item)) == wanted:
                        seen_ids.add(item_id)
                        candidates.append(item)
                total_pages = as_int(page.get("total_pages"))
                if total_pages is not None and page_number + 1 >= total_pages:
                    break

        base: Dict[str, Any] = {}
        if submission_period:
            base["submissionPeriod"] = submission_period

        for term in ms_keg_search_terms(title):
            collect({**base, "name": term}, 1)
            if candidates:
                break

        if not candidates:
            collect(base, max_scan_pages)

        if not candidates:
            return None
        return choose_ms_keg_candidate(candidates)

    def list_ms_keg_children(self, ms_keg_id: Any, child_type: str, *, size: int = 100) -> List[Dict[str, Any]]:
        path_map = {
            "variabel": f"/sdi/metadata-statistik-kegiatan/{ms_keg_id}/metadata-statistik-variabel",
            "indikator": f"/sdi/metadata-statistik-kegiatan/{ms_keg_id}/metadata-statistik-indikator",
        }
        path = path_map[child_type]
        children: List[Dict[str, Any]] = []
        page_number = 0
        while True:
            response = self.get(path, params={"page": page_number, "size": size})
            result = response.get("result")
            page = result[0] if isinstance(result, list) and result else result
            if not isinstance(page, dict):
                return children
            for item in page.get("content") or []:
                if isinstance(item, dict):
                    children.append(item)
            total_pages = as_int(page.get("total_pages")) or 0
            page_number += 1
            if page_number >= total_pages or not page.get("content"):
                break
        return children

    def create_kegiatan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return first_result(self.post("/sdi/v2/metadata-statistik/kegiatan", payload))

    def create_variabel(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return first_result(self.post("/sdi/v2/metadata-statistik/variabel", payload))

    def update_variabel(self, ms_var_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.put(f"/sdi/v2/metadata-statistik/variabel/{ms_var_id}", payload)

    def delete_variabel(self, ms_var_id: Any) -> Dict[str, Any]:
        return self.delete(f"/sdi/v2/metadata-statistik/variabel/{ms_var_id}")

    def create_indikator(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return first_result(self.post("/sdi/v2/metadata-statistik/indikator", payload))

    def update_indikator(self, ms_ind_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.put(f"/sdi/v2/metadata-statistik/indikator/{ms_ind_id}", payload)

    def delete_indikator(self, ms_ind_id: Any) -> Dict[str, Any]:
        return self.delete(f"/sdi/v2/metadata-statistik/indikator/{ms_ind_id}")

    def find_sdi_organization(self, name: str, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        org = user.get("organization") or {}
        province = org.get("province") or {}
        city = org.get("city") or {}
        data = self.get(
            "/api/sdi-organizations",
            params={
                "size": 10,
                "page": 0,
                "name": name,
                "provinceCode": province.get("code"),
                "cityCode": city.get("code"),
                "roles": "PRODUSEN_DATA,WALIDATA",
            },
        )
        content = data.get("content") or data.get("result") or []
        exact = [item for item in content if clean(item.get("name")).lower() == clean(name).lower()]
        return exact[0] if exact else (content[0] if len(content) == 1 else None)


def first_result(response: Dict[str, Any]) -> Dict[str, Any]:
    if "result" in response:
        result = response["result"]
        if isinstance(result, list) and result:
            return result[0]
        if isinstance(result, dict):
            return result
    if "body" in response and isinstance(response["body"], dict):
        return response["body"]
    return response


def response_status(item: Dict[str, Any]) -> str:
    status = item.get("status")
    return str(status) if status is not None else ""


def is_error_result(item: Dict[str, Any]) -> bool:
    status = item.get("status")
    return isinstance(status, int) and status >= 400


def extract_created_id(item: Dict[str, Any]) -> Any:
    if item.get("id"):
        return item.get("id")
    result = item.get("result")
    if isinstance(result, dict) and result.get("id"):
        return result.get("id")
    if isinstance(result, list):
        for value in result:
            if isinstance(value, dict):
                if value.get("id"):
                    return value.get("id")
                content = value.get("content")
                if isinstance(content, list):
                    for child in content:
                        if isinstance(child, dict) and child.get("id"):
                            return child.get("id")
    return None


def title_key(value: Any) -> str:
    text = clean(value) or ""
    return re.sub(r"\s+", " ", text).casefold()


def ms_keg_title(item: Dict[str, Any]) -> Optional[str]:
    data = item.get("data") or {}
    return clean(data.get("judul_kegiatan")) or clean(item.get("name"))


def ms_keg_search_terms(title: str) -> List[str]:
    words = re.findall(r"[A-Za-z0-9]+", title)
    stop_words = {
        "dan",
        "yang",
        "untuk",
        "dari",
        "dalam",
        "data",
        "tahun",
        "kota",
        "kabupaten",
        "provinsi",
        "dinas",
        "kompilasi",
    }
    terms: List[str] = []
    for word in words:
        normalized = word.casefold()
        if len(normalized) < 5 or normalized in stop_words:
            continue
        if normalized not in {term.casefold() for term in terms}:
            terms.append(word)
    terms.sort(key=len, reverse=True)
    return terms or [title]


def choose_ms_keg_candidate(candidates: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    priority = {
        "DRAFT": 0,
        "CORRECTION_REQUIRED": 1,
        "REVISED": 2,
        "SUBMITTED": 3,
        "UNDER_REVIEW": 4,
        "APPROVED": 5,
        "REJECTED": 6,
    }

    def sort_key(item: Dict[str, Any]) -> Tuple[int, int]:
        status_rank = priority.get(str(item.get("status") or ""), 50)
        item_id = as_int(item.get("id")) or 0
        return (status_rank, -item_id)

    return sorted(candidates, key=sort_key)[0]


def draft_kegiatan_errors(row: Dict[str, str]) -> List[str]:
    errors: List[str] = []
    if not pick(row, "judul_kegiatan"):
        errors.append("judul_kegiatan kosong")
    if not pick(row, "tahun"):
        errors.append("tahun kosong")
    if not pick(row, "produsen_data_id", "produsen_data_name"):
        errors.append("produsen_data_id/produsen_data_name kosong")
    return errors


def draft_name_errors(row: Dict[str, str], field: str = "nama", *aliases: str) -> List[str]:
    return [f"{field} kosong"] if not pick(row, field, *aliases) else []


def resolve_produsen_data(row: Dict[str, str], auth: SessionAuth, client: Optional[IndahClient] = None) -> Dict[str, Any]:
    current_org = org_from_user(auth.user_payload)
    row_id = maybe_id(pick(row, "produsen_data_id"))
    row_name = pick(row, "produsen_data_name")
    if row_id or row_name:
        resolved = {
            "id": row_id,
            "name": row_name or current_org.get("name"),
            "province_code": pick(row, "produsen_data_province_code") or current_org.get("province_code"),
            "city_code": pick(row, "produsen_data_city_code") or current_org.get("city_code"),
        }
        if client and (not resolved.get("id") or not resolved.get("province_code")) and row_name:
            found = client.find_sdi_organization(row_name, auth.user_payload)
            if found:
                resolved = {
                    "id": found.get("id"),
                    "name": found.get("name"),
                    "province_code": found.get("provinceCode") or found.get("province_code") or resolved.get("province_code"),
                    "city_code": found.get("cityCode") or found.get("city_code") or resolved.get("city_code"),
                    "details": found.get("details") or {},
                }
        return resolved
    return current_org


def parent_refs_from_ms_keg(ms_keg: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "submission_period": ms_keg.get("submission_period"),
        "submitter": user,
        "produsen_data": {
            "id": ms_keg.get("produsen_data_id"),
            "name": ms_keg.get("produsen_data_name"),
            "province_code": ms_keg.get("produsen_data_province_code"),
            "city_code": ms_keg.get("produsen_data_city_code"),
        },
        "walidata_pusat": {
            "id": ms_keg.get("walidata_pusat_id"),
            "name": ms_keg.get("walidata_pusat_name"),
        },
    }


def map_rows_by_title(path: Path) -> Dict[str, Dict[str, str]]:
    rows = read_csv_rows(path)
    mapped: Dict[str, Dict[str, str]] = {}
    for row in rows:
        title = pick(row, "judul_kegiatan")
        if title:
            mapped[title] = row
    return mapped


def resolve_ms_keg_id(row: Dict[str, str], kegiatan_map: Dict[str, Dict[str, str]]) -> Optional[str]:
    direct = pick(row, "ms_keg_id")
    if direct:
        return direct
    title = pick(row, "judul_kegiatan")
    if title and title in kegiatan_map:
        return pick(kegiatan_map[title], "ms_keg_id")
    return None


def row_submission_period(row: Dict[str, str]) -> int:
    return as_int(pick(row, "submission_period", "submissionPeriod", "periode_pelaporan", "tahun")) or now_year()


def is_ms_keg_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("id") is not None


def resolve_ms_keg_reference(
    row: Dict[str, str],
    kegiatan_map: Dict[str, Dict[str, str]],
    client: Optional[IndahClient] = None,
    *,
    auto_resolve: bool = True,
    max_scan_pages: int = 2,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    direct = resolve_ms_keg_id(row, kegiatan_map)
    if direct:
        if client is None:
            return direct, None, "direct"
        try:
            item = client.get_ms_keg(direct)
        except IndahError:
            item = None
        if is_ms_keg_item(item):
            return str(item.get("id")), item, "direct"

    title = pick(row, "judul_kegiatan")
    if auto_resolve and client is not None and title:
        item = client.find_ms_keg_by_title(
            title,
            submission_period=row_submission_period(row),
            max_scan_pages=max_scan_pages,
        )
        if is_ms_keg_item(item):
            return str(item.get("id")), item, "auto"

    if direct:
        return direct, None, "stale"
    return None, None, "missing"


def load_phase_rows(folder: Path, slug: str, required: bool = True) -> Tuple[Optional[Path], List[Dict[str, str]]]:
    path = find_csv(folder, slug, required=required)
    if path is None:
        return None, []
    return path, read_csv_rows(path)


def now_year() -> int:
    return int(time.strftime("%Y"))


def repeated_dates(row: Dict[str, str], key: str) -> List[str]:
    return split_multi(pick(row, key))


def date_range(row: Dict[str, str], prefix: str) -> List[Dict[str, Optional[str]]]:
    awal_values = repeated_dates(row, f"{prefix}_awal")
    akhir_values = repeated_dates(row, f"{prefix}_akhir")
    if not awal_values and not akhir_values:
        return [{"awal": None, "akhir": None}]
    size = max(len(awal_values), len(akhir_values))
    return [
        {
            "awal": awal_values[i] if i < len(awal_values) else None,
            "akhir": akhir_values[i] if i < len(akhir_values) else None,
        }
        for i in range(size)
    ]


def build_kegiatan_data(row: Dict[str, str], variabel_rows: Sequence[Dict[str, str]], wilayah_rows: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    cara_pengumpulan_data = enum_value(pick(row, "cara_pengumpulan_data"))
    is_survei = cara_pengumpulan_data == "SURVEI"
    produk_tercetak = bool_or_false(pick(row, "produk_tercetak"))
    produk_digital = bool_or_false(pick(row, "produk_digital"))
    produk_mikrodata = bool_or_false(pick(row, "produk_mikrodata"))
    return {
        "judul_kegiatan": pick(row, "judul_kegiatan"),
        "tahun": pick(row, "tahun"),
        "jenis_statistik": enum_value(pick(row, "jenis_statistik", default="STATISTIK_SEKTORAL")),
        "cara_pengumpulan_data": cara_pengumpulan_data,
        "sektor_kegiatan": enum_value(pick(row, "sektor_kegiatan")),
        "identitas_rekomendasi": pick(row, "identitas_rekomendasi") if bool_or_false(pick(row, "rekomendasi_dari_bps")) else None,
        "blok_i": {
            "instansi_penyelanggara": pick(row, "produsen_data_name"),
            "alamat_instansi_penyelenggara": {
                "alamat": pick(row, "alamat_instansi"),
                "telepon": pick(row, "telepon_instansi"),
                "email": pick(row, "email_instansi"),
                "faksimile": pick(row, "faksimile_instansi"),
            },
        },
        "blok_ii": {
            "unit_penanggung_jawab": {
                "eselon1": pick(row, "unit_eselon1"),
                "eselon2": pick(row, "unit_eselon2"),
            },
            "penanggung_jawab_teknis": {
                "nama": pick(row, "pj_teknis_nama"),
                "jabatan": pick(row, "pj_teknis_jabatan"),
                "alamat": pick(row, "pj_teknis_alamat"),
                "telepon": pick(row, "pj_teknis_telepon"),
                "email": pick(row, "pj_teknis_email"),
                "faksimile": pick(row, "pj_teknis_faksimile"),
            },
        },
        "blok_iii": {
            "latar_belakang_kegiatan": pick(row, "latar_belakang_kegiatan"),
            "tujuan_kegiatan": pick(row, "tujuan_kegiatan"),
            "rencana_jadwal_kegiatan": {
                "perencanaan_kegiatan": date_range(row, "jadwal_perencanaan"),
                "desain": date_range(row, "jadwal_desain"),
                "pengumpulan_data": date_range(row, "jadwal_pengumpulan"),
                "pengolahan_data": date_range(row, "jadwal_pengolahan"),
                "analisis": date_range(row, "jadwal_analisis"),
                "diseminasi_hasil": date_range(row, "jadwal_diseminasi"),
                "evaluasi": date_range(row, "jadwal_evaluasi"),
            },
            "variabel_yang_dikumpulkan": build_variabel_yang_dikumpulkan(variabel_rows),
        },
        "blok_iv": {
            "kegiatan_ini_dilakukan": enum_value(pick(row, "kegiatan_ini_dilakukan")),
            "frekuensi_penyelanggara": enum_value(pick(row, "frekuensi_penyelenggaraan")),
            "tipe_pengumpulan_data": enum_value(pick(row, "tipe_pengumpulan_data")),
            "cakupan_wilayah_pengumpulan_data": enum_value(pick(row, "cakupan_wilayah")),
            "sebagian_cakupan_wilayah_pengumpulan_data": build_wilayah(wilayah_rows),
            "metode_pengumpulan_data": {
                "wawancara": bool_or_false(pick(row, "metode_wawancara")),
                "mengisi_kuesioner_sendiri": bool_or_false(pick(row, "metode_mengisi_sendiri")),
                "pengamatan": bool_or_false(pick(row, "metode_pengamatan")),
                "pengumpulan_data_sekunder": bool_or_false(pick(row, "metode_pengumpulan_sekunder")),
                "lainnya": bool_or_false(pick(row, "metode_lainnya")),
                "metode_pengumpulan_lainnya": pick(row, "metode_lainnya_text"),
            },
            "sarana_pengumpulan_data": {
                "papi": bool_or_false(pick(row, "sarana_papi")),
                "capi": bool_or_false(pick(row, "sarana_capi")),
                "cati": bool_or_false(pick(row, "sarana_cati")),
                "cawi": bool_or_false(pick(row, "sarana_cawi")),
                "mail": bool_or_false(pick(row, "sarana_mail")),
                "lainnya": bool_or_false(pick(row, "sarana_lainnya")),
                "sarana_pengumpulan_lainnya": pick(row, "sarana_lainnya_text"),
            },
            "unit_pengumpulan_data": {
                "individu": bool_or_false(pick(row, "unit_individu")),
                "rumah_tangga": bool_or_false(pick(row, "unit_rumah_tangga")),
                "usaha_atau_perusahaan": bool_or_false(pick(row, "unit_usaha_perusahaan")),
                "lainnya": bool_or_false(pick(row, "unit_lainnya")),
                "unit_pengumpulan_data_lainnya": pick(row, "unit_lainnya_text"),
            },
        },
        "blok_v": {
            "jenis_rancangan_sampel": enum_value(pick(row, "jenis_rancangan_sampel")) if is_survei else None,
            "metode_pemilihan_sampel_tahap_terakhir": enum_value(pick(row, "metode_pemilihan_sampel_tahap_terakhir")) if is_survei else None,
            "metode_yang_digunakan": enum_value(pick(row, "metode_yang_digunakan")) if is_survei else None,
            "kerangka_sampel_tahap_terakhir": pick(row, "kerangka_sampel_tahap_terakhir") if is_survei else None,
            "fraksi_sampel_keseluruhan": pick(row, "fraksi_sampel_keseluruhan") if is_survei else None,
            "nilai_perkiraan_sampling_error_variabel_utama": pick(row, "sampling_error_variabel_utama") if is_survei else None,
            "unit_sampel": pick(row, "unit_sampel") if is_survei else None,
            "unit_observasi": pick(row, "unit_observasi") if is_survei else None,
        },
        "blok_vi": {
            "apakah_melakukan_uji_coba": bool_or_false(pick(row, "melakukan_uji_coba")),
            "metode_pemeriksaan_kualitas_pengumpulan_data": {
                "kunjungan_kembali": bool_or_false(pick(row, "kualitas_kunjungan_kembali")),
                "supervisi": bool_or_false(pick(row, "kualitas_supervisi")),
                "taskforce": bool_or_false(pick(row, "kualitas_taskforce")),
                "lainnya": bool_or_false(pick(row, "kualitas_lainnya")),
                "metode_pemeriksaan_kualitas_pengumpulan_data_lainnya": pick(row, "kualitas_lainnya_text"),
            },
            "apakah_melakukan_penyesuaian_nonrespon": bool_or_false(pick(row, "penyesuaian_nonrespon")),
            "petugas_pengumpulan_data": enum_value(pick(row, "petugas_pengumpulan_data")),
            "persyaratan_pendidikan_terendah_petugas_pengumpulan_data": enum_value(pick(row, "pendidikan_terendah_petugas")),
            "jumlah_petugas_supervisor": as_int(pick(row, "jumlah_petugas_supervisor")) or 0,
            "jumlah_petugas_enumerator": as_int(pick(row, "jumlah_petugas_enumerator")) or 0,
            "apakah_melakukan_pelatihan_petugas": bool_or_false(pick(row, "pelatihan_petugas")),
        },
        "blok_vii": {
            "tahapan_pengolahan_data": {
                "editing": bool_or_false(pick(row, "tahap_editing")),
                "coding": bool_or_false(pick(row, "tahap_coding")),
                "data_entry": bool_or_false(pick(row, "tahap_data_entry")),
                "validasi": bool_or_false(pick(row, "tahap_validasi")),
            },
            "metode_analisis": enum_value(pick(row, "metode_analisis")),
            "unit_analsis": {
                "individu": bool_or_false(pick(row, "unit_analisis_individu")),
                "rumah_tangga": bool_or_false(pick(row, "unit_analisis_rumah_tangga")),
                "usaha_atau_perusahaan": bool_or_false(pick(row, "unit_analisis_usaha_perusahaan")),
                "lainnya": bool_or_false(pick(row, "unit_analisis_lainnya")),
                "unit_analisis_lainnya": pick(row, "unit_analisis_lainnya_text"),
            },
            "tingkat_penyajian_hasil_analisis": {
                "nasional": bool_or_false(pick(row, "tingkat_nasional")),
                "provinsi": bool_or_false(pick(row, "tingkat_provinsi")),
                "kabupaten_atau_kota": bool_or_false(pick(row, "tingkat_kabupaten_kota")),
                "lainnya": bool_or_false(pick(row, "tingkat_lainnya")),
                "tingkat_penyajian_hasil_analisis_lainnya": pick(row, "tingkat_lainnya_text"),
            },
        },
        "blok_viii": {
            "ketersediaan_produk_tercetak": produk_tercetak,
            "ketersediaan_produk_digital": produk_digital,
            "ketersediaan_produk_mikrodata": produk_mikrodata,
            "rencana_jadwal_rilis_produk_tercetak": split_multi(pick(row, "rilis_tercetak_tanggal")) if produk_tercetak else [],
            "rencana_jadwal_rilis_produk_digital": split_multi(pick(row, "rilis_digital_tanggal")) if produk_digital else [],
            "rencana_jadwal_rilis_produk_mikrodata": split_multi(pick(row, "rilis_mikrodata_tanggal")) if produk_mikrodata else [],
        },
    }


def build_variabel_yang_dikumpulkan(rows: Sequence[Dict[str, str]]) -> List[Dict[str, Optional[str]]]:
    if not rows:
        return [{"nama": None, "konsep": None, "definisi": None, "referensi_waktu": None}]
    return [
        {
            "nama": pick(row, "variabel_nama", "nama"),
            "konsep": pick(row, "variabel_konsep", "konsep"),
            "definisi": pick(row, "variabel_definisi", "definisi"),
            "referensi_waktu": pick(row, "variabel_referensi_waktu", "referensi_waktu"),
        }
        for row in rows
    ]


def build_wilayah(rows: Sequence[Dict[str, str]]) -> List[Dict[str, Optional[str]]]:
    return [
        {
            "kode_provinsi": pick(row, "wilayah_provinsi_code", "kode_provinsi"),
            "nama_provinsi": pick(row, "wilayah_provinsi", "nama_provinsi"),
            "kode_kabupaten_kota": pick(row, "wilayah_kabupaten_kota_code", "kode_kabupaten_kota"),
            "nama_kabupaten_kota": pick(row, "wilayah_kabupaten_kota", "nama_kabupaten_kota"),
        }
        for row in rows
    ]


def build_kegiatan_payload(row: Dict[str, str], child_variabel: Sequence[Dict[str, str]], child_wilayah: Sequence[Dict[str, str]], auth: SessionAuth, produsen_data: Dict[str, Any]) -> Dict[str, Any]:
    data = build_kegiatan_data(row, child_variabel, child_wilayah)
    details = produsen_data.get("details") or {}
    address = data["blok_i"]["alamat_instansi_penyelenggara"]
    address["alamat"] = address.get("alamat") or details.get("address")
    address["telepon"] = address.get("telepon") or details.get("phone")
    address["email"] = address.get("email") or details.get("email")
    address["faksimile"] = address.get("faksimile") or details.get("fax")
    payload_produsen_data = {
        "id": produsen_data.get("id"),
        "name": produsen_data.get("name"),
        "province_code": produsen_data.get("province_code"),
        "city_code": produsen_data.get("city_code"),
    }
    return {
        "base_id": None,
        "submission_period": as_int(pick(row, "submission_period")) or now_year(),
        "data": data,
        "reviews": None,
        "status": "DRAFT",
        "rejection_note": None,
        "submitter": auth.user_payload,
        "produsen_data": payload_produsen_data,
        "walidata_pusat": None,
        "version": "v2",
        "changes": None,
    }


def build_value_domain(rows: Sequence[Dict[str, str]]) -> List[Dict[str, Optional[str]]]:
    if not rows:
        return [{"kode": "-", "nilai": "-"}]
    return [{"kode": pick(row, "value_domain_kode"), "nilai": pick(row, "value_domain_nilai")} for row in rows]


def build_variabel_payload(row: Dict[str, str], value_domain_rows: Sequence[Dict[str, str]], ms_keg: Dict[str, Any], auth: SessionAuth) -> Dict[str, Any]:
    parent = parent_refs_from_ms_keg(ms_keg, auth.user_payload)
    data = {
        "nama": pick(row, "nama", "variabel_nama"),
        "alias": pick(row, "alias", default="-"),
        "definisi": pick(row, "definisi", "variabel_definisi"),
        "konsep": nullable_list(pick(row, "konsep", "variabel_konsep")),
        "referensi_pemilihan": list_or_default(pick(row, "referensi_pemilihan"), "-"),
        "referensi_waktu": pick(row, "referensi_waktu", "variabel_referensi_waktu"),
        "ukuran": pick(row, "ukuran", default="-"),
        "satuan": pick(row, "satuan", default="-"),
        "tipe_data": pick(row, "tipe_data", default="CHARACTER"),
        "value_domain": build_value_domain(value_domain_rows),
        "aturan_validasi": list_or_default(pick(row, "aturan_validasi"), "HARUS TERISI"),
        "kalimat_pertanyaan": pick(row, "kalimat_pertanyaan", default="-"),
        "apakah_variabel_bisa_diakses_umum": bool_or_false(pick(row, "bisa_diakses_umum", default="TRUE")),
        "id_sds": maybe_id(pick(row, "id_sds")),
    }
    return {
        "ms_keg_id": ms_keg.get("id"),
        "submission_period": parent["submission_period"],
        "data": data,
        "reviews": [],
        "changes": [],
        "status": "DRAFT",
        "rejection_note": None,
        "submitter": parent["submitter"],
        "produsen_data": parent["produsen_data"],
        "walidata_pusat": parent["walidata_pusat"],
        "version": "v2",
    }


def build_indikator_payload(
    row: Dict[str, str],
    indikator_pembangun_rows: Sequence[Dict[str, str]],
    variabel_pembangun_rows: Sequence[Dict[str, str]],
    ms_keg: Dict[str, Any],
    auth: SessionAuth,
) -> Dict[str, Any]:
    parent = parent_refs_from_ms_keg(ms_keg, auth.user_payload)
    is_komposit = bool_or_false(pick(row, "indikator_komposit", default="FALSE"))
    data = {
        "nama": pick(row, "nama", "indikator_nama"),
        "definisi": pick(row, "definisi", default="-"),
        "konsep": list_or_default(pick(row, "konsep"), "-"),
        "interpretasi": pick(row, "interpretasi", default="-"),
        "metode_perhitungan": pick(row, "metode_perhitungan", default="-"),
        "rumus": pick(row, "rumus", default="-"),
        "ukuran": pick(row, "ukuran", default="-"),
        "satuan": pick(row, "satuan", default="-"),
        "variabel_disaggregasi": list_dicts_from_names(
            pick(row, "klasifikasi_penyajian"),
            default_name="Wilayah",
        ),
        "apakah_indikator_komposit": is_komposit,
        "indikator_pembangun": build_indikator_pembangun(indikator_pembangun_rows) if is_komposit else [],
        "variabel_pembangun": [] if is_komposit else build_variabel_pembangun(variabel_pembangun_rows),
        "level_estimasi": pick(row, "level_estimasi", default="Provinsi"),
        "apakah_indikator_bisa_diakses_umum": bool_or_false(pick(row, "bisa_diakses_umum", default="TRUE")),
        "id_sds": maybe_id(pick(row, "id_sds")),
    }
    return {
        "ms_keg_id": ms_keg.get("id"),
        "submission_period": parent["submission_period"],
        "data": data,
        "reviews": [],
        "changes": [],
        "status": "DRAFT",
        "rejection_note": None,
        "submitter": parent["submitter"],
        "produsen_data": parent["produsen_data"],
        "walidata_pusat": parent["walidata_pusat"],
        "version": "v2",
    }


def build_indikator_pembangun(rows: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    if not rows:
        return [{"nama": "-", "sumber_publikasi": ["-"]}]
    return [
        {
            "nama": pick(row, "indikator_pembangun_nama", "nama_indikator_pembangun", default="-"),
            "sumber_publikasi": list_or_default(pick(row, "sumber_publikasi"), "-"),
        }
        for row in rows
    ]


def build_variabel_pembangun(rows: Sequence[Dict[str, str]]) -> List[Dict[str, Any]]:
    if not rows:
        return [{"nama": "-", "kegiatan_penghasil": ["-"]}]
    return [
        {
            "nama": pick(row, "variabel_pembangun_nama", "nama_variabel_pembangun", "nama_variabel", default="-"),
            "kegiatan_penghasil": list_or_default(pick(row, "kegiatan_penghasil"), "-"),
        }
        for row in rows
    ]


def rows_for_variabel(rows: Sequence[Dict[str, str]], title: str, variabel_name: str) -> List[Dict[str, str]]:
    title_l = (title or "").strip().lower()
    var_l = (variabel_name or "").strip().lower()
    return [
        row for row in rows
        if (pick(row, "judul_kegiatan") or "").strip().lower() == title_l
        and (pick(row, "variabel_nama", "nama_variabel", "nama") or "").strip().lower() == var_l
    ]


def rows_for_indikator(rows: Sequence[Dict[str, str]], title: str, indicator_name: str) -> List[Dict[str, str]]:
    title_l = (title or "").strip().lower()
    indicator_l = (indicator_name or "").strip().lower()
    return [
        row for row in rows
        if (pick(row, "judul_kegiatan") or "").strip().lower() == title_l
        and (pick(row, "indikator_nama", "nama_indikator", "nama") or "").strip().lower() == indicator_l
    ]


def print_dry_run_payload(label: str, payload: Dict[str, Any], verbose: bool = False) -> None:
    title = payload.get("data", {}).get("judul_kegiatan") or payload.get("data", {}).get("nama") or "-"
    print(f"[dry-run] {label}: {title}")
    if verbose:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
