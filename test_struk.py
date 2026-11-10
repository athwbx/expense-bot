import sys
import json
import re
import time
from pathlib import Path
from typing import Literal, Optional

from ollama import chat
from pydantic import BaseModel

MODEL_CATAT = "gemma4:e4b-it-qat"  #  model untuk mencatat
MODEL_BACA = "GLM-OCR"  # model untuk baca foto
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
    tanggal: str
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


def tanya(pesan, suhu, label, format=None):
    """Panggil Ollama sekali, lalu catat lama waktu dan jumlah token keluar."""
    mulai = time.perf_counter()
    response = chat(
        model=MODEL,
        messages=pesan,
        format=format,
        think=False,
        options={"temperature": suhu},
    )
    detik = time.perf_counter() - mulai
    token = getattr(response, "eval_count", "?")
    print(f"[{label}] {detik:.0f} detik, {token} token keluar")
    return response.message.content


def baca_foto(path, suhu):
    prompt = Path("prompt_baca.txt").read_text(encoding="utf-8")
    pesan = [{"role": "user", "content": prompt, "images": [path]}]
    return tanya(pesan, suhu, "tahap 1")


def catat_struk(transkrip, suhu):
    aturan = Path("prompt_catat.txt").read_text(encoding="utf-8")
    pesan = [
        {"role": "system", "content": aturan},
        {"role": "user", "content": transkrip},
    ]
    teks = tanya(pesan, suhu, "tahap 2", format=Receipt.model_json_schema())
    return Receipt.model_validate_json(teks)


def hitung_baris(data):
    tanggal = format_tanggal(data.tanggal)
    baris = []
    for item in data.items:
        nama = item.description.lower()
        if any(kata in nama for kata in BUKAN_BARANG):
            continue
        baris.append({
            "Date": tanggal,
            "Bank": None,
            "Category": item.category,
            "Description": item.description,
            "Amount": item.harga - abs(item.diskon),
        })
    return baris


mulai_total = time.perf_counter()
percobaan = []
for coba in range(MAX_COBA):
    suhu = 0 if coba == 0 else 0.4
    print(f"--- percobaan {coba + 1} (temperature {suhu}) ---")
    transkrip = baca_foto(FOTO, suhu)
    data = catat_struk(transkrip, suhu)
    baris = hitung_baris(data)
    jumlah = sum(b["Amount"] for b in baris)
    selisih = None if data.total_belanja is None else abs(jumlah - data.total_belanja)
    percobaan.append({"transkrip": transkrip, "baris": baris, "jumlah": jumlah,
                      "total": data.total_belanja, "selisih": selisih})
    if selisih == 0:
        break

# pilih percobaan dengan selisih paling kecil (selisih kosong dianggap paling buruk)
terbaik = min(percobaan, key=lambda p: 10**12 if p["selisih"] is None else p["selisih"])

print()
print("[TRANSKRIP TERPILIH]")
print(terbaik["transkrip"])
print()
print("[HASIL AKHIR]")
print(json.dumps(terbaik["baris"], indent=2, ensure_ascii=False))

if terbaik["selisih"] is None:
    print(f"TOTAL TIDAK TERBACA. Jumlah baris {terbaik['jumlah']}. Cek manual.")
elif terbaik["selisih"] == 0:
    print(f"COCOK. Jumlah baris = total belanja = {terbaik['jumlah']}")
else:
    print(f"PERLU DICEK. Jumlah baris {terbaik['jumlah']}, total belanja {terbaik['total']}, selisih {terbaik['selisih']}")
print(f"Total waktu: {time.perf_counter() - mulai_total:.0f} detik")