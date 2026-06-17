import argparse
from pathlib import Path

from indah_automation.shared import (
    DEFAULT_RUN_STATE_DIR,
    DEFAULT_STATE_PATH,
    IndahClient,
    IndahError,
    PROJECT_ROOT,
    SessionAuth,
    build_kegiatan_payload,
    draft_kegiatan_errors,
    extract_created_id,
    find_csv,
    is_error_result,
    load_auth,
    pick,
    print_dry_run_payload,
    read_csv_rows,
    resolve_produsen_data,
    response_status,
    rows_for_title,
    append_or_replace_map,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1: input MS-Kegiatan dari CSV.")
    parser.add_argument("--folder", default=str(PROJECT_ROOT / "dispora"), help="Folder sumber CSV.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Storage state hasil indah_login.py.")
    parser.add_argument("--run-state", default=str(DEFAULT_RUN_STATE_DIR), help="Folder output mapping.")
    parser.add_argument("--submit", action="store_true", help="Benar-benar POST simpan sementara ke INDAH.")
    parser.add_argument("--limit", type=int, default=0, help="Batasi jumlah row untuk test.")
    parser.add_argument("--only-title", help="Proses hanya judul kegiatan tertentu.")
    parser.add_argument("--verbose", action="store_true", help="Print payload saat dry-run.")
    args = parser.parse_args()

    folder = Path(args.folder)
    kegiatan_path = find_csv(folder, "ms_kegiatan", required=True)
    variabel_path = find_csv(folder, "ms_kegiatan_variabel_dikumpulkan", required=False)
    wilayah_path = find_csv(folder, "ms_kegiatan_wilayah", required=False)

    kegiatan_rows = read_csv_rows(kegiatan_path)
    child_variabel_rows = read_csv_rows(variabel_path) if variabel_path else []
    child_wilayah_rows = read_csv_rows(wilayah_path) if wilayah_path else []
    if args.only_title:
        kegiatan_rows = [row for row in kegiatan_rows if pick(row, "judul_kegiatan") == args.only_title]
    if args.limit:
        kegiatan_rows = kegiatan_rows[: args.limit]

    print(f"CSV kegiatan: {kegiatan_path}")
    print(f"Rows kegiatan: {len(kegiatan_rows)}")

    auth = load_auth(Path(args.state)) if args.submit else None
    client = IndahClient(auth.token) if auth else None
    map_rows = []

    for index, row in enumerate(kegiatan_rows, 1):
        title = pick(row, "judul_kegiatan") or f"row-{index}"
        errors = draft_kegiatan_errors(row)
        if errors:
            print(f"[skip] {title}: {', '.join(errors)}")
            continue
        assert auth is not None or not args.submit
        produsen = resolve_produsen_data(row, auth, client) if auth else {"name": pick(row, "produsen_data_name"), "id": pick(row, "produsen_data_id")}
        payload = build_kegiatan_payload(row, rows_for_title(child_variabel_rows, title), rows_for_title(child_wilayah_rows, title), auth, produsen) if auth else None
        if not args.submit:
            dummy_auth = SessionAuth(token="", user_raw={}, user_payload={"username": None, "name": None, "email": None, "organization": {}})
            payload = build_kegiatan_payload(row, rows_for_title(child_variabel_rows, title), rows_for_title(child_wilayah_rows, title), dummy_auth, produsen)
            print_dry_run_payload("kegiatan", payload, args.verbose)
            continue

        result = client.create_kegiatan(payload)
        if is_error_result(result):
            print(f"[error] {title}: {result}")
            continue
        ms_keg_id = extract_created_id(result)
        if not ms_keg_id:
            print(f"[error] {title}: API tidak mengembalikan id kegiatan. Respons: {result}")
            continue
        print(f"[ok] kegiatan tersimpan: {title} -> {ms_keg_id}")
        map_rows.append(
            {
                "judul_kegiatan": title,
                "ms_keg_id": ms_keg_id,
                "detail_url": f"https://indah.bps.go.id/metadata/view-kegiatan/{ms_keg_id}",
                "status": response_status(result) or "DRAFT",
            }
        )

    if args.submit and map_rows:
        run_state = Path(args.run_state)
        append_or_replace_map(
            run_state / "kegiatan_map.csv",
            ["judul_kegiatan", "ms_keg_id", "detail_url", "status"],
            ["judul_kegiatan"],
            map_rows,
        )
        print(f"Map ditulis: {run_state / 'kegiatan_map.csv'}")


if __name__ == "__main__":
    try:
        main()
    except IndahError as exc:
        raise SystemExit(f"Error: {exc}") from None
