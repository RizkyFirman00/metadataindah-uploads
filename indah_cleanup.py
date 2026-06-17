import argparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from indah_automation.shared import (
    DEFAULT_RUN_STATE_DIR,
    DEFAULT_STATE_PATH,
    IndahClient,
    IndahError,
    clean,
    load_auth,
    ms_keg_title,
    read_csv_rows,
    write_csv,
)


MAP_CONFIG = {
    "variabel": {
        "path": "variabel_map.csv",
        "id_field": "ms_var_id",
        "label_field": "nama_variabel",
        "fieldnames": ["judul_kegiatan", "ms_keg_id", "nama_variabel", "ms_var_id", "status"],
    },
    "indikator": {
        "path": "indikator_map.csv",
        "id_field": "ms_ind_id",
        "label_field": "nama_indikator",
        "fieldnames": ["judul_kegiatan", "ms_keg_id", "nama_indikator", "ms_ind_id", "status"],
    },
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Hapus MS-Variabel/MS-Indikator dari INDAH.")
    parser.add_argument("--phase", choices=["variabel", "indikator", "all"], default="all")
    parser.add_argument("--title", help="Judul kegiatan yang child variabel/indikatornya akan dihapus.")
    parser.add_argument("--ms-keg-id", help="ID MS-Kegiatan yang child variabel/indikatornya akan dihapus.")
    parser.add_argument("--from-kegiatan-map", action="store_true", help="Hapus child untuk semua judul di kegiatan_map.csv.")
    parser.add_argument("--allow-non-draft", action="store_true", help="Izinkan hapus child dari kegiatan non-DRAFT.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="Storage state hasil indah_login.py.")
    parser.add_argument("--run-state", default=str(DEFAULT_RUN_STATE_DIR), help="Folder mapping upload.")
    parser.add_argument("--execute", action="store_true", help="Benar-benar DELETE data di INDAH.")
    args = parser.parse_args()

    phases = ["indikator", "variabel"] if args.phase == "all" else [args.phase]
    client = None
    title_mode = bool(args.title or args.ms_keg_id or args.from_kegiatan_map)
    if args.execute or title_mode:
        auth = load_auth(Path(args.state))
        client = IndahClient(auth.token)
    total_deleted = 0
    total_failed = 0

    run_state = Path(args.run_state)
    if title_mode:
        targets = resolve_kegiatan_targets(args, run_state, client)
        if not targets:
            print("[info] Tidak ada kegiatan target untuk cleanup.")
        for phase in phases:
            deleted, failed = cleanup_phase_by_kegiatan(
                phase,
                targets,
                run_state,
                client,
                execute=args.execute,
                allow_non_draft=args.allow_non_draft,
            )
            total_deleted += deleted
            total_failed += failed
    else:
        for phase in phases:
            deleted, failed = cleanup_phase_from_map(phase, run_state, client, execute=args.execute)
            total_deleted += deleted
            total_failed += failed

    mode = "execute" if args.execute else "dry-run"
    print(f"Selesai cleanup ({mode}). deleted={total_deleted}, failed={total_failed}")
    if total_failed:
        raise SystemExit(1)


def resolve_kegiatan_targets(args: argparse.Namespace, run_state: Path, client: IndahClient) -> List[Dict[str, object]]:
    rows: List[Dict[str, str]] = []
    if args.from_kegiatan_map:
        rows.extend(read_csv_rows(run_state / "kegiatan_map.csv"))
    if args.title or args.ms_keg_id:
        rows.append({"judul_kegiatan": args.title or "", "ms_keg_id": args.ms_keg_id or ""})

    targets: List[Dict[str, object]] = []
    seen = set()
    for row in rows:
        title = clean(row.get("judul_kegiatan"))
        ms_keg_id = clean(row.get("ms_keg_id"))
        row_status = clean(row.get("status"))
        detail = None
        if ms_keg_id:
            try:
                detail = client.get_ms_keg(ms_keg_id)
            except IndahError as exc:
                print(f"[warn] MS-Kegiatan {ms_keg_id} tidak bisa dibuka dari ID: {exc}")
                detail = None
        if not detail and title and not ms_keg_id:
            found = client.find_ms_keg_by_title(title, max_scan_pages=2)
            if found:
                ms_keg_id = clean(found.get("id"))
                row_status = clean(found.get("status")) or row_status
                try:
                    detail = client.get_ms_keg(ms_keg_id)
                except IndahError:
                    detail = found
        if not ms_keg_id:
            print(f"[skip] kegiatan tidak ditemukan: {title or '-'}")
            continue
        key = str(ms_keg_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            {
                "id": ms_keg_id,
                "title": title or ms_keg_title(detail or {}) or "-",
                "status": clean((detail or {}).get("status")) or row_status or "",
                "detail": detail if isinstance(detail, dict) else None,
            }
        )
    return targets


def cleanup_phase_by_kegiatan(
    phase: str,
    targets: List[Dict[str, object]],
    run_state: Path,
    client: IndahClient,
    *,
    execute: bool,
    allow_non_draft: bool = False,
) -> Tuple[int, int]:
    deleted = 0
    failed = 0
    deleted_ids = set()
    failed_ids = set()
    target_titles = {clean(target.get("title")) or "" for target in targets}

    for target in targets:
        ms_keg_id = target["id"]
        title = clean(target.get("title")) or "-"
        status = clean(target.get("status")) or ""
        if not allow_non_draft and status not in {"DRAFT", "CORRECTION_REQUIRED", ""}:
            print(f"[skip] {phase}: kegiatan {title} ({ms_keg_id}) status {status}; tidak dihapus tanpa --allow-non-draft")
            continue
        children = children_for_target(client, target, phase)
        if not children:
            print(f"[info] {phase}: tidak ada data yang bisa dibaca untuk kegiatan {title} ({ms_keg_id})")
            continue
        for child in children:
            item_id = clean(child.get("id"))
            label = child_label(child)
            if not item_id:
                print(f"[skip] {phase}: id kosong untuk {title} / {label}")
                continue
            if not execute:
                print(f"[dry-run] hapus {phase}: {title} / {label} -> {item_id}")
                continue
            try:
                delete_child(client, phase, item_id)
            except IndahError as exc:
                failed += 1
                failed_ids.add(item_id)
                print(f"[error] gagal hapus {phase}: {title} / {label} -> {item_id}: {exc}")
                continue
            deleted += 1
            deleted_ids.add(item_id)
            print(f"[ok] hapus {phase}: {title} / {label} -> {item_id}")

    if execute and (deleted_ids or target_titles):
        rewrite_phase_map(run_state, phase, target_titles, deleted_ids, failed_ids)

    return deleted, failed


def children_for_target(client: IndahClient, target: Dict[str, object], phase: str) -> List[Dict[str, object]]:
    detail = target.get("detail")
    detail_key = "ms_var" if phase == "variabel" else "ms_ind"
    children = []
    if isinstance(detail, dict):
        for item in detail.get(detail_key) or []:
            if isinstance(item, dict):
                children.append(item)
    if children:
        return dedupe_children(children)
    try:
        return dedupe_children(client.list_ms_keg_children(target["id"], phase))
    except IndahError as exc:
        print(f"[warn] {phase}: tidak bisa membaca child kegiatan {target['id']}: {exc}")
        return []


def child_label(child: Dict[str, object]) -> str:
    data = child.get("data") if isinstance(child.get("data"), dict) else {}
    return clean(data.get("nama")) or clean(child.get("name")) or clean(child.get("nama")) or "-"


def dedupe_children(children: List[Dict[str, object]]) -> List[Dict[str, object]]:
    result = []
    seen = set()
    for child in children:
        item_id = clean(child.get("id"))
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        result.append(child)
    return result


def delete_child(client: IndahClient, phase: str, item_id: str) -> None:
    if phase == "variabel":
        client.delete_variabel(item_id)
    else:
        client.delete_indikator(item_id)


def rewrite_phase_map(
    run_state: Path,
    phase: str,
    target_titles: set,
    deleted_ids: set,
    failed_ids: set,
) -> None:
    config = MAP_CONFIG[phase]
    path = run_state / config["path"]
    id_field = config["id_field"]
    rows = read_csv_rows(path)
    if not rows:
        return
    remaining = []
    for row in rows:
        item_id = clean(row.get(id_field)) or ""
        title = clean(row.get("judul_kegiatan")) or ""
        if item_id in failed_ids:
            remaining.append(row)
        elif item_id in deleted_ids:
            continue
        elif title in target_titles and not item_id:
            continue
        else:
            remaining.append(row)
    write_csv(path, config["fieldnames"], remaining)
    print(f"Map diperbarui: {path} ({len(remaining)} tersisa)")


def cleanup_phase_from_map(phase: str, run_state: Path, client: Optional[IndahClient], *, execute: bool) -> Tuple[int, int]:
    config = MAP_CONFIG[phase]
    path = run_state / config["path"]
    rows = read_csv_rows(path)
    if not rows:
        print(f"[info] {phase}: map kosong/tidak ada ({path})")
        return 0, 0

    id_field = config["id_field"]
    label_field = config["label_field"]
    remaining: List[Dict[str, str]] = []
    deleted = 0
    failed = 0

    for row in rows:
        item_id = clean(row.get(id_field))
        title = clean(row.get("judul_kegiatan")) or "-"
        label = clean(row.get(label_field)) or "-"
        if not item_id:
            remaining.append(row)
            print(f"[skip] {phase}: id kosong untuk {title} / {label}")
            continue

        if not execute:
            print(f"[dry-run] hapus {phase}: {title} / {label} -> {item_id}")
            remaining.append(row)
            continue

        try:
            if phase == "variabel":
                client.delete_variabel(item_id)
            else:
                client.delete_indikator(item_id)
        except IndahError as exc:
            failed += 1
            remaining.append(row)
            print(f"[error] gagal hapus {phase}: {title} / {label} -> {item_id}: {exc}")
            continue

        deleted += 1
        print(f"[ok] hapus {phase}: {title} / {label} -> {item_id}")

    if execute:
        write_csv(path, config["fieldnames"], remaining)
        print(f"Map diperbarui: {path} ({len(remaining)} tersisa)")

    return deleted, failed


if __name__ == "__main__":
    try:
        main()
    except IndahError as exc:
        raise SystemExit(f"Error: {exc}") from None
