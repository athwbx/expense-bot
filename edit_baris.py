"""Penyuntingan baris hasil sebelum disimpan: mengurai nilai baru yang diketik pengguna.

Semua fungsi mengembalikan None kalau teksnya tidak bisa dipahami, supaya bot bisa meminta ulang.
"""
import re

from catat_teks import cari_nominal, cari_tanggal

MAKS_DESKRIPSI = 100


def baca_jumlah(teks):
    """Jumlah rupiah dari teks seperti 71k, 71.000, rp71.000, atau 71000. Angka kecil polos (500) juga diterima."""
    nominal, _, _ = cari_nominal(teks)
    if nominal:
        return nominal
    bersih = re.sub(r"rp|[.\s]", "", teks.lower())
    return int(bersih) if bersih.isdigit() and int(bersih) > 0 else None


def baca_tanggal(teks, hari_ini):
    """Tanggal dari kemarin, hari ini, 6 agustus, 6/8, atau 6 agustus 2025. None kalau tidak dikenali."""
    tanggal, _ = cari_tanggal(teks.strip(), hari_ini)
    return tanggal


def baca_deskripsi(teks):
    """Deskripsi baru, spasi dirapikan dan dibatasi panjangnya. None kalau kosong."""
    bersih = re.sub(r"\s+", " ", teks).strip()[:MAKS_DESKRIPSI]
    return bersih or None