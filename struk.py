"""Membaca foto struk menjadi baris pengeluaran.

Dua tahap, keduanya lokal lewat Ollama:
1. GLM-OCR menyalin teks dari foto.
2. Python membaca barang, harga, diskon, tanggal, dan total dari teks itu dengan aturan pasti.
   Gemma 4 hanya menilai: jenis struk (belanja atau makan), nama tempat, dan kategori.

Jumlah baris barang diperiksa terhadap total di struk. Foto diproses di memori dan tidak ditulis ke disk.
Pemakaian di terminal:
    python struk.py tes             tes parser tanpa model
    python struk.py foto_struk.jpg  jalankan seluruh alur pada satu foto
"""
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from catat_teks import Kategori, MODEL_CATAT, cari_bank, cari_tanggal, tampil_tanggal, tanya

MODEL_BACA = "glm-ocr"  # tahap 1: hanya membaca foto menjadi teks
PROMPT_BACA = "Text Recognition:"  # prompt baku GLM-OCR, jangan diubah
PROMPT_STRUK = Path(__file__).with_name("prompt_struk.txt")
HARGA_MIN = 100  # angka di bawah ini dianggap bukan harga (jumlah porsi, nomor meja)
TOLERANSI_PEMBULATAN = 99  # selisih jumlah barang dan total sampai segini dianggap pembulatan

POLA_BAYAR = re.compile(r"\b(debit|qris|kredit|credit|kartu|card)\b", re.I)
POLA_LEWAT = re.compile(
    r"\b(tunai|cash|kembali|kembalian|change|ppn|dpp|pajak|tax|service|serv|pb1|hemat|"
    r"pembayaran|bayar|payment|voucher|points?|poin|saldo|rounding|pembulatan|telp|npwp|kasir|"
    r"cashier|invoice|nota|member|trx|jalan|jl|ovo|gopay|shopeepay|linkaja|approval)\b",
    re.I,
)
POLA_TOTAL = re.compile(r"\b(total|sub\s*total|subtotal|jumlah)\b", re.I)
POLA_BUKAN_TOTAL = re.compile(r"\btotal\s+(item|items|qty|disc|diskon|hemat|potongan|point|poin)\b", re.I)
POLA_DISKON = re.compile(r"^\s*(disc|diskon|potongan|promo)\b", re.I)
POLA_RINCIAN = re.compile(r"^\s*(orig\b|\d+\s*[@x]\s*[\d.,]+\s*$)", re.I)
POLA_RINCIAN_HARGA = re.compile(r"^\s*\d+\s*[xX@]\s*[\d.,]+\s+(?P<harga>[\d.,]+)\s*$")
POLA_BARANG = re.compile(r"^(?:\d{5,14}\s+)?(?P<nama>.*?[A-Za-z].*?)\s+(?P<harga>\S+)\s*$")
POLA_TGL_STRUK = re.compile(r"(?<![\d.,/-])(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})(?![\d/])")
POLA_KUNCI = re.compile(r"\b(serv|service|pb1|ppn|pajak|tax|table|meja|sub\s*total|subtotal)\b", re.I)


class ItemKategori(BaseModel):
    nomor: int
    category: Kategori


class HasilStruk(BaseModel):
    jenis: Literal["belanja", "makan"]
    nama_tempat: str
    kategori_umum: Kategori
    items: list[ItemKategori]


def rupiah(n):
    return "Rp" + f"{n:,}".replace(",", ".")


NAMA_KOSONG = "Nama tidak terbaca"


def rapikan_nama(nama):
    """Huruf besar di awal kata, sisanya kecil. Singkatan struk dibiarkan apa adanya."""
    nama = re.sub(r"\s+", " ", nama).strip(" -.,:")
    if not nama:
        return NAMA_KOSONG
    return " ".join(k[:1].upper() + k[1:].lower() for k in nama.split(" "))


# ===== bagian Python murni: semua yang bisa dicari dengan aturan pasti =====

def ke_angka(token):
    """Ubah potongan teks jadi rupiah bulat (29,030 atau 29.030 atau 12.500,00). None kalau bukan angka."""
    t = re.sub(r"^rp\.?", "", token.strip().lstrip("-").strip("()"), flags=re.I)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+[.,]\d{2}", t):  # 12.500,00 menjadi 12.500
        t = t[:-3]
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", t):
        return int(re.sub(r"[.,]", "", t))
    if re.fullmatch(r"\d+", t):
        return int(t)
    return None


def harga_akhir(baris):
    """Angka di ujung baris sebagai harga, atau None kalau bukan harga."""
    token = baris.split()[-1] if baris.split() else ""
    nilai = ke_angka(token)
    return nilai if nilai is not None and nilai >= HARGA_MIN else None


def prioritas_total(baris):
    """0 paling dipercaya. None kalau baris itu bukan kandidat total yang dibayar."""
    if POLA_BUKAN_TOTAL.search(baris):
        return None
    if re.search(r"\b(grand\s*total|total\s*belanja|total\s*bayar|total\s*tagihan)\b", baris, re.I):
        return 0
    if re.search(r"\b(sub\s*total|subtotal)\b", baris, re.I):
        return 2
    if re.search(r"\btotal\b", baris, re.I):
        return 1
    return 2 if re.search(r"\bjumlah\b", baris, re.I) else None


def urai_struk(teks):
    """Baca barang, diskon, total, dan nama tempat dari teks hasil OCR."""
    baris = [b.strip() for b in teks.splitlines() if b.strip()]
    items, kandidat, selesai = [], [], False
    nama_menunggu = None  # baris teks tanpa harga, mungkin nama barang yang harganya ada di baris berikutnya
    for i, b in enumerate(baris):
        if POLA_TOTAL.search(b) or (selesai and POLA_BAYAR.search(b)):
            selesai = True  # barang berhenti di blok total
            nilai = harga_akhir(b)
            if nilai is None and i + 1 < len(baris) and re.fullmatch(r"[Rp.\s]*[\d.,]+", baris[i + 1]):
                nilai = ke_angka(baris[i + 1].replace("Rp", "").strip())  # angka di baris berikutnya
            prio = prioritas_total(b) if POLA_TOTAL.search(b) else 3  # 3: baris pembayaran (debit, qris)
            if nilai is not None and prio is not None:
                kandidat.append((prio, nilai))
            continue
        if selesai or POLA_RINCIAN.match(b):
            continue
        m_rinci = POLA_RINCIAN_HARGA.match(b)  # "1x 2.000   2.000": nama ada di baris sebelumnya
        if m_rinci:
            harga = ke_angka(m_rinci.group("harga"))
            if harga is not None and harga >= HARGA_MIN:
                items.append({"nama": nama_menunggu or "", "harga": harga, "diskon": 0})
            nama_menunggu = None
            continue
        if POLA_DISKON.match(b):  # diskon menempel ke barang tepat di atasnya
            nilai = harga_akhir(b)
            if items and nilai:
                items[-1]["diskon"] += nilai
            continue
        ada_kode = re.match(r"^\d{5,14}\s+", b)  # baris berkode produk adalah barang, apa pun katanya
        if not ada_kode and (POLA_LEWAT.search(b) or POLA_BAYAR.search(b)):
            continue
        m = POLA_BARANG.match(b)
        harga = ke_angka(m.group("harga")) if m else None
        if m and harga is not None and harga >= HARGA_MIN:
            nama = re.sub(r"\s+\d{1,3}(?:\s*[x@]\s*|\s+)[\d.,]+$", "", m.group("nama"))  # buang "1 4,000" (jumlah dan harga satuan)
            nama = re.sub(r"^\d{1,3}\s+(?=[A-Za-z])", "", nama)  # buang jumlah di depan nama
            items.append({"nama": nama, "harga": harga, "diskon": 0})
            nama_menunggu = None
        elif not ada_kode and len(re.findall(r"[A-Za-z]", b)) >= 2:
            nama_menunggu = b
    terbaik = min((k[0] for k in kandidat), default=None)
    total = [k[1] for k in kandidat if k[0] == terbaik][-1] if kandidat else None  # yang terakhir di antara yang sama
    tempat = next((b for b in baris if len(re.findall(r"[A-Za-z]", b)) >= 3 and not POLA_LEWAT.search(b)), "")
    return {
        "tempat": tempat,
        "total": total,
        "items": items,
        "baris_atas": baris[:8],
        "baris_kunci": [b for b in baris if POLA_KUNCI.search(b)][:6],
    }


def cari_tanggal_struk(teks, tanggal_pesan):
    """Tanggal transaksi dalam urutan hari, bulan, tahun. None kalau tidak ada atau tidak masuk akal."""
    for m in POLA_TGL_STRUK.finditer(teks):
        hari, bulan, tahun = int(m.group(1)), int(m.group(2)), int(m.group(3))
        tahun += 2000 if tahun < 100 else 0
        try:
            d = date(tahun, bulan, hari)
        except ValueError:
            continue
        if date(2015, 1, 1) <= d <= tanggal_pesan + timedelta(days=1):
            return d
    return None


def tentukan_harga(items, total):
    """Struk berbeda-beda: harga di kolom kanan ada yang sudah dikurangi diskon, ada yang masih kotor.
    Total di struk yang menentukan: pakai cara yang jumlahnya paling dekat dengan total."""
    jumlah_harga = sum(i["harga"] for i in items)
    jumlah_diskon = sum(i["diskon"] for i in items)
    if total is not None:
        selisih_bersih = abs(jumlah_harga - total)
        selisih_kotor = abs(jumlah_harga - jumlah_diskon - total)
        if min(selisih_bersih, selisih_kotor) <= TOLERANSI_PEMBULATAN:
            return "bersih" if selisih_bersih <= selisih_kotor else "kotor"
    if any(i["diskon"] >= i["harga"] for i in items):  # diskon sebesar harga atau lebih: harganya pasti sudah bersih
        return "bersih"
    if total is not None:  # tidak ada yang pas (ada yang salah baca): ambil yang paling dekat ke total
        return "bersih" if selisih_bersih <= selisih_kotor else "kotor"
    return "kotor" if jumlah_diskon else "bersih"


# ===== bagian model: hanya menilai jenis, nama tempat, dan kategori =====

def baca_foto(data):
    """Tahap 1: GLM-OCR menyalin teks dari foto (bytes), tanpa menulis file."""
    pesan = [{"role": "user", "content": PROMPT_BACA, "images": [data]}]
    return tanya(MODEL_BACA, pesan, 0, "tahap 1, baca foto")


def tentukan_kategori_struk(info, daftar):
    """Tahap 2: Gemma 4 menerima bagian atas struk dan daftar barang tanpa angka."""
    aturan = PROMPT_STRUK.read_text(encoding="utf-8")
    isi = ["Bagian atas struk:", *info["baris_atas"], "", "Baris lain yang relevan:",
           *(info["baris_kunci"] or ["(tidak ada)"]), "", "Daftar barang:",
           *[f"{n}. {k}" for n, k in enumerate(daftar, start=1)]]
    pesan = [{"role": "system", "content": aturan}, {"role": "user", "content": "\n".join(isi)}]
    teks = tanya(MODEL_CATAT, pesan, 0, "tahap 2, kategori struk",
                 format=HasilStruk.model_json_schema(), think=False)
    return HasilStruk.model_validate_json(teks)


# ===== merakit hasil =====

def proses_transkrip(teks, tanggal_pesan, caption="", kategorikan=None):
    """Teks OCR jadi baris pengeluaran. Kembalikan (baris, catatan). Caption foto boleh memuat bank dan tanggal."""
    kategorikan = kategorikan or tentukan_kategori_struk
    tgl_caption, sisa = cari_tanggal(caption, tanggal_pesan) if caption else (None, "")
    bank, _ = cari_bank(sisa) if caption else (None, "")
    info = urai_struk(teks)
    items, total = info["items"], info["total"]
    if not items and total is None:
        return [], ["Struk tidak terbaca: tidak ada barang atau total yang bisa dikenali."]
    tanggal = tgl_caption or cari_tanggal_struk(teks, tanggal_pesan) or tanggal_pesan
    daftar = [rapikan_nama(i["nama"]) for i in items] or [info["tempat"] or "struk"]
    hasil = kategorikan(info, daftar)
    kategori = {i.nomor: i.category for i in hasil.items}
    tempat = hasil.nama_tempat.strip() or rapikan_nama(info["tempat"])

    def baris(deskripsi, kat, jumlah):
        return {"Date": tanggal, "Bank": bank, "Category": kat, "Description": deskripsi, "Amount": jumlah}

    if hasil.jenis == "makan":
        if total is None:
            return [], [f"Struk makan di {tempat}, tetapi total tidak terbaca. Catat manual."]
        return ([baris(tempat, "Dine Out", total)],
                [f"Struk makan di {tempat}, dicatat satu baris sebesar total {rupiah(total)}. Rincian menu tidak dicatat."])
    if not items:
        return ([baris(tempat, hasil.kategori_umum, total)],
                [f"Barang tidak terbaca. Dicatat satu baris sebesar total {rupiah(total)}. PERLU DICEK."])

    mode = tentukan_harga(items, total)
    hasil_baris = []
    for n, i in enumerate(items, start=1):
        jumlah = i["harga"] if mode == "bersih" else max(i["harga"] - i["diskon"], 0)
        hasil_baris.append(baris(rapikan_nama(i["nama"]), kategori.get(n) or hasil.kategori_umum, jumlah))
    jumlah_baris = sum(b["Amount"] for b in hasil_baris)
    if total is None:
        status = "TIDAK BISA DICEK, total struk tidak terbaca."
    elif jumlah_baris == total:
        status = f"COCOK, jumlah barang sama dengan total {rupiah(total)}."
    elif abs(jumlah_baris - total) <= TOLERANSI_PEMBULATAN:
        status = f"COCOK, selisih {rupiah(abs(jumlah_baris - total))} dianggap pembulatan (total {rupiah(total)})."
    else:
        status = (f"PERLU DICEK, jumlah barang {rupiah(jumlah_baris)}, total struk {rupiah(total)}, "
                  f"selisih {rupiah(abs(jumlah_baris - total))}.")
    catatan = [f"Struk belanja {tempat}, {len(hasil_baris)} barang. {status}"]
    if mode == "kotor" and any(i["diskon"] for i in items):
        catatan.append("Diskon sudah dikurangkan dari harga barang.")
    return hasil_baris, catatan


def proses_foto(data, tanggal_pesan, caption=""):
    """Foto struk (bytes) jadi baris pengeluaran. Kembalikan (baris, catatan). Foto tidak disimpan ke disk."""
    return proses_transkrip(baca_foto(data), tanggal_pesan, caption)


# ===== tes tanpa model: python struk.py tes =====

CONTOH_A = """TOKO CONTOH A
JL CONTOH NO 1
Telp. 0812 3456 7890
==========
PENJUALAN 0102 00004 0091 08/02/26
173700 17:26:15
10000001 SUSU UHT 1L 12,500
1 @ 16,300
disc toko (-)(1) -3,800
10000002 BAWANG PUTIH /KG 4,821
orig 7,378
disc toko (-) -2,557
10000003 TEH BOTOL 350ML 5,000
10000004 SABUN CUCI 800ML 18,000
__________
4 ITEMS TOTAL 40,321
Debit QRIS 40,321
Anda Menghemat
6,357
"""
CONTOH_B = """TOKO CONTOH B
Teh Pucuk 350ML 1 4,000 4,000
Sabun Cuci Piring 1 9,000 9,000
Disc. -1,000
Total Belanja 12,000
Tgl. 05-03-2025 10:00:00
"""
CONTOH_C = """WARUNG CONTOH
Table 5
1 Nasi Goreng 30,000
1 Es Teh 8,000
Subtotal 38,000
Serv 5% 1,900
PB1 10% 3,990
Total 43,890
Tgl 02-03-2025 12:30
"""


CONTOH_D = """TOKO CONTOH D
90000001 SUNCO REF 1LT 20,500
90000002 YOGURT BLUEBERRY 80 6,485
90000003 BON CABE LV 15 2,500
15 ITEMS TOTAL 29,485
"""


CONTOH_E = """Alfamart Jogja
Jl. Kenangan
Jogja
08897482739
18.7.2024 08:49   #281
Cimory
1x 2.000     2.000
Cimory hazelnut
1x 9.000     9.000
Frestea madu
1x 8.000     8.000
Ice cream aice
1x 5.000     5.000
Kanzler
1x 10.000    10.000
Le Minerale
1x 4.000     4.000
__________
Total   Rp38.000
Tunai   Rp38.000
Lunas
"""
CONTOH_F = """TOKO CONTOH F
Teh Pucuk 350ML
2x 3.500    7.000
Disc  -500
Sabun
1x 9.000    9.000
1x 3.000    3.000
Total Belanja 18.500
"""


def kategori_palsu(jenis=None):
    """Pengganti model untuk tes: semua barang Groceries."""
    def fungsi(info, daftar):
        j = jenis or ("makan" if "WARUNG" in info["tempat"].upper() else "belanja")
        return HasilStruk(jenis=j, nama_tempat=info["tempat"].title(), kategori_umum="Groceries",
                          items=[ItemKategori(nomor=n, category="Groceries") for n in range(1, len(daftar) + 1)])
    return fungsi


def jalankan_tes():
    from catat_teks import BANK_ALIAS, pasang_bank_alias
    alias_asli = BANK_ALIAS
    pasang_bank_alias({"seabank atha": "Seabank Atha", "cash": "Cash"})
    hari_ini = date(2026, 11, 10)
    gagal = 0

    def cek(nama, kondisi, rincian=""):
        nonlocal gagal
        print(("OK    " if kondisi else "GAGAL ") + nama + ("" if kondisi else f"  {rincian}"))
        gagal += 0 if kondisi else 1

    # A. harga kolom kanan sudah bersih, diskon menempel ke barang di atasnya, nomor telepon bukan barang
    baris, cat = proses_transkrip(CONTOH_A, hari_ini, kategorikan=kategori_palsu())
    cek("A: 4 barang", len(baris) == 4, [b["Description"] for b in baris])
    cek("A: jumlah per barang", [b["Amount"] for b in baris] == [12500, 4821, 5000, 18000], [b["Amount"] for b in baris])
    cek("A: COCOK", "COCOK" in cat[0] and "Rp40.321" in cat[0], cat)
    cek("A: tanggal dari struk", baris[0]["Date"] == date(2026, 2, 8), baris[0]["Date"])
    cek("A: nama barang dirapikan", baris[0]["Description"] == "Susu Uht 1l", baris[0]["Description"])
    # B. harga kolom kanan masih kotor, diskon dikurangkan
    baris, cat = proses_transkrip(CONTOH_B, hari_ini, kategorikan=kategori_palsu())
    cek("B: diskon dikurangkan", [b["Amount"] for b in baris] == [4000, 8000], [b["Amount"] for b in baris])
    cek("B: COCOK dan catatan diskon", "COCOK" in cat[0] and len(cat) == 2, cat)
    cek("B: tanggal 05-03-2025", baris[0]["Date"] == date(2025, 3, 5), baris[0]["Date"])
    cek("B: nama tanpa jumlah dan harga satuan", baris[0]["Description"] == "Teh Pucuk 350ml", baris[0]["Description"])
    # C. struk makan: satu baris sebesar total, Dine Out
    baris, cat = proses_transkrip(CONTOH_C, hari_ini, kategorikan=kategori_palsu())
    cek("C: satu baris Dine Out total", len(baris) == 1 and baris[0]["Amount"] == 43890 and baris[0]["Category"] == "Dine Out", baris)
    # D. jumlah barang tidak sama dengan total
    salah = CONTOH_A.replace("TEH BOTOL 350ML 5,000", "TEH BOTOL 350ML 6,000")
    baris, cat = proses_transkrip(salah, hari_ini, kategorikan=kategori_palsu())
    cek("D: PERLU DICEK", "PERLU DICEK" in cat[0] and "selisih Rp1.000" in cat[0], cat)
    # E. barang tidak terbaca tapi total ada: satu baris total
    baris, cat = proses_transkrip("TOKO X\nbaris rusak ###\nTotal Belanja 55,000\n", hari_ini, kategorikan=kategori_palsu())
    cek("E: satu baris total", len(baris) == 1 and baris[0]["Amount"] == 55000 and "PERLU DICEK" in cat[0], (baris, cat))
    # F. total tidak ada
    baris, cat = proses_transkrip("TOKO X\nSUSU UHT 1L 12,500\nTEH BOTOL 5,000\n", hari_ini, kategorikan=kategori_palsu())
    cek("F: TIDAK BISA DICEK", len(baris) == 2 and "TIDAK BISA DICEK" in cat[0], cat)
    # G. caption memuat bank dan tanggal, tanggal caption menang
    baris, _ = proses_transkrip(CONTOH_A, hari_ini, "seabank atha kemarin", kategorikan=kategori_palsu())
    cek("G: bank dan tanggal dari caption", baris[0]["Bank"] == "Seabank Atha" and baris[0]["Date"] == date(2026, 11, 9), (baris[0]["Bank"], baris[0]["Date"]))
    # H. tanpa tanggal di struk: pakai tanggal pesan
    baris, _ = proses_transkrip("TOKO X\nSUSU UHT 1L 12,500\nTotal 12,500\n", hari_ini, kategorikan=kategori_palsu())
    cek("H: tanggal pesan sebagai cadangan", baris[0]["Date"] == hari_ini, baris[0]["Date"])
    # I. selisih pembulatan kecil tetap COCOK
    baris, cat = proses_transkrip("TOKO X\nSUSU UHT 1L 12,550\nTotal 12,500\n", hari_ini, kategorikan=kategori_palsu())
    cek("I: pembulatan", "COCOK" in cat[0] and "pembulatan" in cat[0], cat)
    # J. tidak ada apa pun yang terbaca
    baris, cat = proses_transkrip("### ??? ###", hari_ini, kategorikan=kategori_palsu())
    cek("J: struk tidak terbaca", baris == [] and "tidak terbaca" in cat[0], cat)
    # L. regresi dari struk asli: nama barang berisi kata seperti REF dan BON, atau berakhir angka
    baris, cat = proses_transkrip(CONTOH_D, hari_ini, kategorikan=kategori_palsu())
    cek("L: tiga barang utuh", [b["Description"] for b in baris] == ["Sunco Ref 1lt", "Yogurt Blueberry 80", "Bon Cabe Lv 15"], [b["Description"] for b in baris])
    cek("L: COCOK", "COCOK" in cat[0], cat)
    # M. format nama di baris sendiri, harga di baris berikutnya (struk Alfamart)
    baris, cat = proses_transkrip(CONTOH_E, hari_ini, kategorikan=kategori_palsu("belanja"))
    cek("M: nama barang bukan baris harga", [b["Description"] for b in baris] == ["Cimory", "Cimory Hazelnut", "Frestea Madu", "Ice Cream Aice", "Kanzler", "Le Minerale"], [b["Description"] for b in baris])
    cek("M: jumlah dan COCOK", [b["Amount"] for b in baris] == [2000, 9000, 8000, 5000, 10000, 4000] and "COCOK" in cat[0], cat)
    cek("M: tanggal 18.7.2024", baris[0]["Date"] == date(2024, 7, 18), baris[0]["Date"])
    baris, cat = proses_transkrip(CONTOH_F, hari_ini, kategorikan=kategori_palsu("belanja"))
    cek("N: jumlah 2x, diskon, dan baris tanpa nama", [(b["Description"], b["Amount"]) for b in baris] == [("Teh Pucuk 350ml", 6500), ("Sabun", 9000), ("Nama tidak terbaca", 3000)] and "COCOK" in cat[0], (baris, cat))
    # K. format angka
    cek("K: ke_angka", [ke_angka(x) for x in ("29,030", "29.030", "1.234.567", "Rp 12.500,00".replace(" ", ""), "(6,590)", "-2,557", "26", "4,5")]
        == [29030, 29030, 1234567, 12500, 6590, 2557, 26, None])
    pasang_bank_alias(alias_asli)
    print(f"\n{'SEMUA LOLOS' if not gagal else str(gagal) + ' GAGAL'}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tes":
        jalankan_tes()
    elif len(sys.argv) > 1:
        mulai = time.perf_counter()
        transkrip = baca_foto(Path(sys.argv[1]).read_bytes())
        print("[TRANSKRIP DARI GLM-OCR]\n" + transkrip + "\n")
        baris, catatan = proses_transkrip(transkrip, date.today())
        print(json.dumps(baris, indent=2, ensure_ascii=False, default=tampil_tanggal))
        for c in catatan:
            print("CATATAN:", c)
        print(f"Total waktu: {time.perf_counter() - mulai:.0f} detik")
    else:
        print("Pemakaian: python struk.py tes   atau   python struk.py foto_struk.jpg")