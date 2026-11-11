"""Penyimpanan hasil pengeluaran.

Kalau GOOGLE_SHEET_ID diatur (di .env), baris ditulis ke Google Sheets lewat service account.
Kalau tidak diatur, atau penulisan ke Sheets gagal, baris ditulis ke pengeluaran.tsv
(dipisah tab, bisa ditempel ke Google Sheets), supaya data tidak hilang.
"""
import csv
import logging
import os
from pathlib import Path

log = logging.getLogger("expense-bot")

FOLDER = Path(__file__).parent
DATA_PATH = FOLDER / "pengeluaran.tsv"
KOLOM = ["Date", "Bank", "Category", "Description", "Amount"]

_klien = None  # klien gspread, dibuat sekali saat pertama dipakai


def id_sheet():
    return os.environ.get("GOOGLE_SHEET_ID", "").strip()


def nama_tab():
    return os.environ.get("GOOGLE_SHEET_TAB", "").strip() or "Pengeluaran"


def berkas_kunci():
    """Lokasi key JSON service account. Jalur relatif dibaca dari folder proyek."""
    nilai = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or "service_account.json"
    jalur = Path(nilai)
    return jalur if jalur.is_absolute() else FOLDER / jalur


def tujuan_aktif():
    """Nama tujuan penyimpanan yang sedang dipakai, untuk log dan pesan."""
    return f"Google Sheets (tab {nama_tab()})" if id_sheet() else DATA_PATH.name


def baris_nilai(baris):
    """Ubah baris jadi daftar nilai. Tanggal ditulis ISO (yyyy-mm-dd), Amount tetap angka."""
    return [[b["Date"].isoformat(), b["Bank"], b["Category"] or "", b["Description"], b["Amount"]]
            for b in baris]


def simpan_tsv(baris):
    """Tambahkan baris ke pengeluaran.tsv. Pemisah kolom adalah tab."""
    if not DATA_PATH.exists():
        with open(DATA_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter="\t").writerow(KOLOM)
    with open(DATA_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerows(baris_nilai(baris))


def simpan_sheets(baris):
    """Tulis tepat di bawah baris terakhir kolom A, hanya kolom A sampai E.
    Kolom lain (misalnya Check dan Note) tidak disentuh."""
    global _klien
    import gspread
    from gspread.utils import ValueInputOption

    if _klien is None:
        _klien = gspread.service_account(filename=berkas_kunci())
    ws = _klien.open_by_key(id_sheet()).worksheet(nama_tab())
    nilai = baris_nilai(baris)
    awal = len(ws.col_values(1)) + 1  # col_values berhenti di sel terakhir yang berisi
    akhir = awal + len(nilai) - 1
    # USER_ENTERED: Sheets memperlakukan isinya seperti diketik, jadi "2026-11-11" menjadi tanggal
    ws.update(values=nilai, range_name=f"A{awal}:E{akhir}",
              value_input_option=ValueInputOption.user_entered)


def simpan_baris(baris):
    """Simpan baris ke tujuan yang aktif. Kembalikan (nama tujuan, peringatan atau None).
    Kalau Sheets gagal, baris dialihkan ke TSV dan peringatan berisi alasannya.
    OSError dilempar hanya kalau berkas TSV pun gagal ditulis."""
    global _klien
    if id_sheet():
        try:
            simpan_sheets(baris)
            return tujuan_aktif(), None
        except Exception as e:
            _klien = None  # buat ulang klien pada percobaan berikutnya
            log.exception("gagal menulis ke Google Sheets, dialihkan ke %s", DATA_PATH.name)
            simpan_tsv(baris)
            return DATA_PATH.name, f"Google Sheets gagal ({type(e).__name__}), baris disimpan di {DATA_PATH.name}."
    simpan_tsv(baris)
    return DATA_PATH.name, None