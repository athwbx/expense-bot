import json
import re
import sys
import time
from pathlib import Path
from typing import Literal, Optional

from ollama import chat
from pydantic import BaseModel

MODEL_BACA = "glm-ocr"             # tahap 1: hanya membaca foto menjadi teks
MODEL_CATAT = "gemma4:e4b-it-qat"  # tahap 2: menyusun teks menjadi data
PROMPT_BACA = "Text Recognition:"  # prompt baku GLM-OCR, jangan diubah
FOTO = sys.argv[1] if len(sys.argv) > 1 else "struk.jpg"
MAX_COBA = 2

BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
         "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]
BUKAN_BARANG = ("total", "disc", "ppn", "dpp", "tunai", "kembalian", "qris")


class Item(BaseModel):
    description: str
    category: Literal[
        "Home", "Home supplies", "Groceries", "Dine Out", "Jajan",
        "Transportation", "Personal Care", "Socials", "Date/Couple",
    ]
    harga: int
    diskon: int


class Receipt(BaseModel):
    jenis: Literal["belanja", "makan"]
    nama_tempat: str
    tanggal: Optional[str] = None
    subtotal: Optional[int] = None
    service: Optional[int] = None
    pajak: Optional[int] = None
    diskon_total: Optional[int] = None
    total_belanja: Optional[int] = None
    items: list[Item]


def format_tanggal(teks: str) -> str:
    angka = re.findall(r"\d+", teks)
    if len(angka) < 3:
        return teks
    hari, bulan, tahun = angka[:3]
    if len(tahun) == 2:
        tahun = "20" + tahun
    try:
        return f"{int(hari)} {BULAN[int(bulan) - 1]} {tahun}"
    except (ValueError, IndexError):
        return teks


def tanya(model, pesan, suhu, label, format=None, think=None):
    """Panggil Ollama sekali, lalu catat lama waktu dan jumlah token keluar."""
    mulai = time.perf_counter()
    response = chat(
        model=model,
        messages=pesan,
        format=format,
        think=think,
        options={"temperature": suhu},
    )
    detik = time.perf_counter() - mulai
    token = getattr(response, "eval_count", "?")
    print(f"[{label}] {detik:.0f} detik, {token} token keluar")
    return response.message.content


def baca_foto(path):
    """Tahap 1: GLM-OCR menyalin teks dari foto."""
    pesan = [{"role": "user", "content": PROMPT_BACA, "images": [path]}]
    return tanya(MODEL_BACA, pesan, 0, "tahap 1, baca foto")


def catat_struk(transkrip, suhu):
    """Tahap 2: Gemma 4 menyusun salinan teks menjadi data terstruktur."""
    aturan = Path("prompt_catat.txt").read_text(encoding="utf-8")
    pesan = [
        {"role": "system", "content": aturan},
        {"role": "user", "content": transkrip},
    ]
    teks = tanya(MODEL_CATAT, pesan, suhu, "tahap 2, catat",
                 format=Receipt.model_json_schema(), think=False)
    return Receipt.model_validate_json(teks)


def hitung_baris(data):
    tanggal = format_tanggal(data.tanggal) if data.tanggal else None
    if data.jenis == "makan":
        return [{
            "Date": tanggal,
            "Bank": None,
            "Category": "Dine Out",
            "Description": data.nama_tempat,
            "Amount": data.total_belanja,
        }]
    items = [i for i in data.items
             if not any(kata in i.description.lower() for kata in BUKAN_BARANG)]
    # Struk beda-beda: Alfamart menulis harga kotor lalu diskon di bawahnya,
    # Hari Hari menulis harga yang sudah bersih. Total struk yang memutuskan.
    kotor = sum(i.harga for i in items)
    harga_sudah_bersih = data.total_belanja is not None and kotor == data.total_belanja
    baris = []
    for item in items:
        potongan = 0 if harga_sudah_bersih else abs(item.diskon)
        baris.append({
            "Date": tanggal,
            "Bank": None,
            "Category": item.category,
            "Description": item.description,
            "Amount": item.harga - potongan,
        })
    return baris


def periksa(data, baris):
    """Kembalikan (angka_pembanding, selisih). Selisih None artinya tidak bisa dicek."""
    if data.total_belanja is None:
        return None, None
    if data.jenis == "makan":
        if data.subtotal is None:
            return None, None
        harusnya = (data.subtotal - (data.diskon_total or 0)
                    + (data.service or 0) + (data.pajak or 0))
        selisih = abs(harusnya - data.total_belanja)
        return harusnya, (0 if selisih <= 100 else selisih)  # toleransi pembulatan
    jumlah = sum(b["Amount"] for b in baris)
    return jumlah, abs(jumlah - data.total_belanja)


mulai_total = time.perf_counter()
transkrip = baca_foto(FOTO)  # foto cukup dibaca sekali

percobaan = []
for coba in range(MAX_COBA):
    suhu = 0 if coba == 0 else 0.4
    print(f"--- mencatat, percobaan {coba + 1} (temperature {suhu}) ---")
    data = catat_struk(transkrip, suhu)
    print("tanggal dari model:", data.tanggal)
    for i in data.items:
        print(f"   {i.description[:28]:28} harga={i.harga:>7} diskon={i.diskon:>6}")
    print("jumlah harga:", sum(i.harga for i in data.items), "| total belanja:", data.total_belanja)
    baris = hitung_baris(data)
    jumlah, selisih = periksa(data, baris)
    percobaan.append({"baris": baris, "jumlah": jumlah,
                      "total": data.total_belanja, "selisih": selisih})
    if selisih == 0:
        break

# pilih percobaan dengan selisih paling kecil (selisih kosong dianggap paling buruk)
terbaik = min(percobaan, key=lambda p: 10**12 if p["selisih"] is None else p["selisih"])

print()
print("[TRANSKRIP DARI GLM-OCR]")
print(transkrip)
print()
print("[HASIL AKHIR]")
print(json.dumps(terbaik["baris"], indent=2, ensure_ascii=False))

if terbaik["selisih"] is None:
    print("TIDAK BISA DICEK (total atau subtotal tidak terbaca). Cek manual.")
elif terbaik["selisih"] == 0:
    print(f"COCOK. Hitungan {terbaik['jumlah']} = total belanja {terbaik['total']}")
else:
    print(f"PERLU DICEK. Hitungan {terbaik['jumlah']}, total belanja {terbaik['total']}, selisih {terbaik['selisih']}")
print(f"Total waktu: {time.perf_counter() - mulai_total:.0f} detik")