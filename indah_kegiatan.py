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
    retry_api_result,
    load_auth,
    pick,
    print_dry_run_payload,
    read_csv_rows,
    resolve_produsen_data,
    response_status,
    rows_for_title,
    append_or_replace_map,
    upload_progress,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1: input MS-Kegiatan dari CSV.")
    parser.add_argument("--folder", default=str(PROJECT_ROOT / "dispora"), help="Folder sumber CSV.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Storage state hasil indah_login.py.")
    parser.add_argument("--run-state", default=str(DEFAULT_RUN_STATE_DIR), help="Folder output mapping.")
    parser.add_argument("--submission-period", type=int, help="Periode tahun submission API, misalnya 2025.")
    parser.add_argument("--submit", action="store_true", help="Benar-benar POST simpan sementara ke INDAH.")
    parser.add_argument("--final-submit", action="store_true", help="POST ke INDAH dengan status SUBMITTED.")
    parser.add_argument("--limit", type=int, default=0, help="Batasi jumlah row untuk test.")
    parser.add_argument("--only-title", help="Proses hanya judul kegiatan tertentu.")
    parser.add_argument("--verbose", action="store_true", help="Print payload saat dry-run.")
    args = parser.parse_args()
    if args.submission_period is not None and not 2000 <= args.submission_period <= 2100:
        parser.error("--submission-period harus berada antara 2000 dan 2100")
    if args.final_submit:
        args.submit = True

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
    upload_progress("validation", 0, len(kegiatan_rows))

    auth = load_auth(Path(args.state)) if args.submit else None
    client = IndahClient(auth.token) if auth else None
    map_rows = []

    for index, row in enumerate(kegiatan_rows, 1):
        if args.submission_period is not None:
            row = {**row, "submission_period": str(args.submission_period)}
        title = pick(row, "judul_kegiatan") or f"row-{index}"
        upload_progress("validation", index, len(kegiatan_rows), title)
        errors = draft_kegiatan_errors(row)
        if errors:
            upload_progress("item", index, len(kegiatan_rows), title, "skipped", ", ".join(errors))
            print(f"[skip] {title}: {', '.join(errors)}")
            continue
        assert auth is not None or not args.submit
        produsen = resolve_produsen_data(row, auth, client) if auth else {"name": pick(row, "produsen_data_name"), "id": pick(row, "produsen_data_id")}
        target_status = "SUBMITTED" if args.final_submit else "DRAFT"
        upload_progress("payload", index, len(kegiatan_rows), title)
        payload = build_kegiatan_payload(
            row,
            rows_for_title(child_variabel_rows, title),
            rows_for_title(child_wilayah_rows, title),
            auth,
            produsen,
            status=target_status,
        ) if auth else None
        if not args.submit:
            dummy_auth = SessionAuth(token="", user_raw={}, user_payload={"username": None, "name": None, "email": None, "organization": {}})
            payload = build_kegiatan_payload(
                row,
                rows_for_title(child_variabel_rows, title),
                rows_for_title(child_wilayah_rows, title),
                dummy_auth,
                produsen,
            )
            print_dry_run_payload("kegiatan", payload, args.verbose)
            continue

        upload_progress("upload", index, len(kegiatan_rows), title)
        result = retry_api_result(lambda: client.create_kegiatan(payload), f"kegiatan {title}")
        ms_keg_id = extract_created_id(result)
        if not ms_keg_id:
            upload_progress("item", index, len(kegiatan_rows), title, "failed", "API tidak mengembalikan id kegiatan")
            print(f"[error] {title}: API tidak mengembalikan id kegiatan. Respons: {result}")
            continue
        action = "tersubmit" if args.final_submit else "tersimpan"
        print(f"[ok] kegiatan {action}: {title} -> {ms_keg_id}")
        upload_progress("item", index, len(kegiatan_rows), title, "success")
        map_rows.append(
            {
                "judul_kegiatan": title,
                "submission_period": payload["submission_period"],
                "ms_keg_id": ms_keg_id,
                "detail_url": f"https://indah.bps.go.id/metadata/view-kegiatan/{ms_keg_id}",
                "status": response_status(result) or target_status,
            }
        )

    if args.submit and map_rows:
        upload_progress("mapping", len(kegiatan_rows), len(kegiatan_rows))
        run_state = Path(args.run_state)
        append_or_replace_map(
            run_state / "kegiatan_map.csv",
            ["judul_kegiatan", "submission_period", "ms_keg_id", "detail_url", "status"],
            ["judul_kegiatan", "submission_period"],
            map_rows,
        )
        print(f"Map ditulis: {run_state / 'kegiatan_map.csv'}")


if __name__ == "__main__":
    try:
        main()
    except IndahError as exc:
        raise SystemExit(f"Error: {exc}") from None
