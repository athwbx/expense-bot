import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from ollama import chat
from pydantic import BaseModel

MODEL_CATAT = "gemma4:e4b-it-qat"  # sama seperti tahap 2 di jalur foto
PROMPT_TEKS = Path(__file__).with_name("prompt_teks.txt")

BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
         "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

def muat_bank_alias():
    """Baca alias bank dari bank_alias.json (pribadi, tidak masuk git).
    Kalau belum ada, pakai bank_alias.example.json. Isinya {"alias yang diketik": "nama persis di dropdown"}."""
    folder = Path(__file__).parent
    for nama in ("bank_alias.json", "bank_alias.example.json"):
        jalur = folder / nama
        if jalur.exists():
            return json.loads(jalur.read_text(encoding="utf-8"))
    raise SystemExit("bank_alias.json belum ada. Salin bank_alias.example.json menjadi bank_alias.json lalu sesuaikan.")


NAMA_BULAN = {
    "januari": 1, "jan": 1, "februari": 2, "feb": 2, "maret": 3, "mar": 3,
    "april": 4, "apr": 4, "mei": 5, "may": 5, "juni": 6, "jun": 6,
    "juli": 7, "jul": 7, "agustus": 8, "agu": 8, "agt": 8, "aug": 8,
    "september": 9, "sep": 9, "oktober": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11, "desember": 12, "des": 12, "dec": 12,
}


def _daftar(kata):
    """Gabungkan kata jadi pola regex, yang terpanjang dulu (agustus sebelum agu)."""
    return "|".join(re.escape(k) for k in sorted(kata, key=len, reverse=True))


def pasang_bank_alias(alias):
    """Pakai daftar alias bank ini. Pencarian tidak peduli huruf besar atau kecil.
    Juga dipakai tes supaya tidak bergantung pada bank_alias.json pribadi."""
    global BANK_ALIAS, BANK_LOOKUP, POLA_BANK
    BANK_ALIAS = alias
    BANK_LOOKUP = {k.lower(): v for k, v in alias.items()}
    POLA_BANK = re.compile(rf"\b({_daftar(BANK_LOOKUP)})\b", re.I)


pasang_bank_alias(muat_bank_alias())


POLA_KEMARIN_LUSA = re.compile(r"\bkemarin lusa\b", re.I)
POLA_KEMARIN = re.compile(r"\bkemarin\b", re.I)
POLA_HARI_INI = re.compile(r"\bhari ini\b", re.I)
POLA_TGL_BULAN = re.compile(rf"\b(\d{{1,2}})\s+({_daftar(NAMA_BULAN)})\b(?:\s+(20\d{{2}})\b)?", re.I)
POLA_TGL_ANGKA = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}|\d{2}))?\b")
# Pemisah item: baris baru, titik koma, atau koma. Koma di antara dua angka (1,5jt) bukan pemisah.
POLA_PISAH = re.compile(r"[\n;]+|(?<!\d),|,(?!\d)")
POLA_NOMINAL = re.compile(r"(?<![\w.,])(rp\.?\s*)?(\d+(?:[.,]\d+)*)\s*(jt|juta|rb|ribu|k)?(?!\w)", re.I)


# Daftar kategori. Harus sama dengan dropdown Category di spreadsheet dan dengan prompt_teks.txt.
Kategori = Literal[
    "Home", "Home supplies", "Groceries", "Dine Out", "Jajan",
    "Transportation", "Personal Care", "Socials", "Date/Couple",
]


class ItemTeks(BaseModel):
    nomor: int
    description: str
    category: Kategori


class HasilTeks(BaseModel):
    items: list[ItemTeks]


def tampil_tanggal(d):
    """Untuk ditampilkan di chat. Ke Google Sheets nanti kirim objek date aslinya."""
    return f"{d.day} {BULAN[d.month - 1]} {d.year}" if isinstance(d, date) else str(d)


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


# ===== bagian Python murni: semua yang bisa dicari dengan aturan pasti =====

def rapikan(teks):
    return re.sub(r"\s+", " ", teks).strip(" ,.:-")


def susun_tanggal(hari, bulan, tahun, tanggal_pesan):
    """Susun objek date. Tahun kosong: pakai tahun pesan, tapi mundur setahun kalau jatuh di masa depan."""
    try:
        if tahun is None:
            hasil = date(tanggal_pesan.year, bulan, hari)
            if hasil > tanggal_pesan:
                hasil = date(tanggal_pesan.year - 1, bulan, hari)
            return hasil
        tahun = int(tahun)
        return date(2000 + tahun if tahun < 100 else tahun, bulan, hari)
    except ValueError:
        return None


def cari_tanggal(teks, tanggal_pesan):
    """Kembalikan (tanggal atau None, teks tanpa potongan tanggal)."""
    for pola, selisih in ((POLA_KEMARIN_LUSA, -2), (POLA_KEMARIN, -1), (POLA_HARI_INI, 0)):
        m = pola.search(teks)
        if m:
            return tanggal_pesan + timedelta(days=selisih), teks[:m.start()] + " " + teks[m.end():]
    m = POLA_TGL_BULAN.search(teks)
    if m:
        hari, bulan, tahun = int(m.group(1)), NAMA_BULAN[m.group(2).lower()], m.group(3)
    else:
        m = POLA_TGL_ANGKA.search(teks)
        if not m:
            return None, teks
        hari, bulan, tahun = int(m.group(1)), int(m.group(2)), m.group(3)
    hasil = susun_tanggal(hari, bulan, tahun, tanggal_pesan)
    if hasil is None:  # misalnya 31/02, anggap bukan tanggal
        return None, teks
    return hasil, teks[:m.start()] + " " + teks[m.end():]


def cari_bank(teks):
    """Kembalikan (nama bank sesuai dropdown atau None, teks tanpa kata bank)."""
    m = POLA_BANK.search(teks)
    if not m:
        return None, teks
    return BANK_LOOKUP[m.group(1).lower()], teks[:m.start()] + " " + teks[m.end():]


def baca_nominal(angka, satuan, ada_rp):
    """Ubah potongan teks jadi rupiah bulat. None kalau bukan nominal."""
    satuan = (satuan or "").lower()
    if satuan:  # dengan satuan, koma atau titik = desimal (1,5jt, 27,5rb)
        try:
            nilai = float(angka.replace(",", "."))
        except ValueError:
            return None
        return round(nilai * (1_000_000 if satuan in ("jt", "juta") else 1000))
    # tanpa satuan, titik atau koma hanya boleh jadi pemisah ribuan (27.000, 1,500,000)
    if re.fullmatch(r"\d+", angka):
        nilai = int(angka)
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", angka):
        nilai = int(re.sub(r"[.,]", "", angka))
    else:
        return None
    # angka kecil tanpa satuan (misalnya jumlah porsi) bukan nominal, kecuali ada "Rp"
    return nilai if (nilai >= 1000 or ada_rp) else None


def cari_nominal(teks):
    """Kembalikan (nominal atau None, teks tanpa nominal, jumlah kandidat nominal)."""
    kandidat = []
    for m in POLA_NOMINAL.finditer(teks):
        nilai = baca_nominal(m.group(2), m.group(3), bool(m.group(1)))
        if nilai is not None:
            kandidat.append((nilai, m.span()))
    if not kandidat:
        return None, teks, 0
    nilai, (awal, akhir) = kandidat[-1]  # kalau ada beberapa, pakai yang terakhir
    return nilai, teks[:awal] + " " + teks[akhir:], len(kandidat)


def urai_pesan(pesan, tanggal_pesan=None):
    """Baca nominal, bank, dan tanggal dari pesan. Satu baris, atau dipisah titik koma atau koma = satu item.
    Kembalikan (daftar item, daftar catatan)."""
    tanggal_pesan = tanggal_pesan or date.today()
    catatan, item = [], []
    bank_terlihat, tanggal_terlihat = set(), set()
    for baris in POLA_PISAH.split(pesan):
        baris = baris.strip()
        if not baris:
            continue
        tgl, sisa = cari_tanggal(baris, tanggal_pesan)
        bank, sisa = cari_bank(sisa)
        nominal, sisa, jumlah = cari_nominal(sisa)
        keterangan = rapikan(sisa)
        if bank:
            bank_terlihat.add(bank)
        if tgl:
            tanggal_terlihat.add(tgl)
        if nominal is None:
            if keterangan:  # baris yang hanya berisi bank atau tanggal sah, dipakai untuk baris lain
                catatan.append(f"tidak ada nominal di '{baris}', baris dilewati")
            continue
        if jumlah > 1:
            catatan.append(f"ada {jumlah} angka mirip nominal di '{baris}', dipakai yang terakhir ({nominal})")
        item.append({"tanggal": tgl, "bank": bank, "nominal": nominal, "keterangan": keterangan})
    # bank atau tanggal yang cuma disebut sekali berlaku untuk semua item yang belum punya
    bank_umum = next(iter(bank_terlihat)) if len(bank_terlihat) == 1 else None
    tanggal_umum = next(iter(tanggal_terlihat)) if len(tanggal_terlihat) == 1 else tanggal_pesan
    for i in item:
        i["bank"] = i["bank"] or bank_umum
        i["tanggal"] = i["tanggal"] or tanggal_umum
    return item, catatan


# ===== bagian model: hanya menilai kategori dan merapikan keterangan =====

def tentukan_kategori(daftar_keterangan):
    """Gemma 4 menerima daftar bernomor tanpa nominal, jadi tidak bisa mengubah angka."""
    aturan = PROMPT_TEKS.read_text(encoding="utf-8")
    daftar = "\n".join(f"{n}. {k}" for n, k in enumerate(daftar_keterangan, start=1))
    pesan = [
        {"role": "system", "content": aturan},
        {"role": "user", "content": "Daftar:\n" + daftar},
    ]
    teks = tanya(MODEL_CATAT, pesan, 0, "catat teks",
                 format=HasilTeks.model_json_schema(), think=False)
    hasil = HasilTeks.model_validate_json(teks)
    return {i.nomor: i for i in hasil.items}


def proses_pesan(pesan, tanggal_pesan=None):
    """Pesan teks jadi baris sheet. Kembalikan (baris, catatan)."""
    item, catatan = urai_pesan(pesan, tanggal_pesan)
    if not item:
        return [], catatan + ["tidak ada nominal yang terbaca"]
    hasil = tentukan_kategori([i["keterangan"] or "(tanpa keterangan)" for i in item])
    baris = []
    for n, i in enumerate(item, start=1):
        dari_model = hasil.get(n)
        if dari_model is None:
            catatan.append(f"model tidak mengembalikan nomor {n}, kategori dikosongkan")
        if not i["keterangan"]:
            catatan.append(f"item {n} ({i['nominal']}) tidak ada keterangan")
        if i["bank"] is None:
            catatan.append(f"item {n} ({i['nominal']}) bank belum disebut")
        baris.append({
            "Date": i["tanggal"],
            "Bank": i["bank"],
            "Category": dari_model.category if dari_model else None,
            "Description": (dari_model.description if dari_model else i["keterangan"]),
            "Amount": i["nominal"],
        })
    return baris, catatan


# ===== tes tanpa model: python catat_teks.py tes =====

def jalankan_tes():
    alias_asli = BANK_ALIAS
    pasang_bank_alias({"seabank": "Seabank Atha", "bca": "BCA Atha", "cash": "Cash",
                       "seabank dida": "Seabank Dida", "jago saving": "Jago Saving"})
    hari_ini = date(2026, 11, 10)
    kasus = [
        # (pesan, tanggal pesan, [(nominal, bank, tanggal, keterangan), ...])
        ("tianlala 27rb seabank", hari_ini, [(27000, "Seabank Atha", date(2026, 11, 10), "tianlala")]),
        ("kopi 27k bca kemarin", hari_ini, [(27000, "BCA Atha", date(2026, 11, 9), "kopi")]),
        ("sewa kos 1,5jt cash", hari_ini, [(1500000, "Cash", date(2026, 11, 10), "sewa kos")]),
        ("bensin 27.000 cash 6 agustus", hari_ini, [(27000, "Cash", date(2026, 8, 6), "bensin")]),
        ("makan siang 35.000 seabank 6/8", hari_ini, [(35000, "Seabank Atha", date(2026, 8, 6), "makan siang")]),
        ("tiket 2 orang 50rb bca", hari_ini, [(50000, "BCA Atha", date(2026, 11, 10), "tiket 2 orang")]),
        ("beras 5kg 70rb", hari_ini, [(70000, None, date(2026, 11, 10), "beras 5kg")]),
        ("parkir 2rb cash", hari_ini, [(2000, "Cash", date(2026, 11, 10), "parkir")]),
        ("pulsa rp50.000 bca", hari_ini, [(50000, "BCA Atha", date(2026, 11, 10), "pulsa")]),
        ("es teh 1.500", hari_ini, [(1500, None, date(2026, 11, 10), "es teh")]),
        ("nasi 2 porsi 40000 seabank", hari_ini, [(40000, "Seabank Atha", date(2026, 11, 10), "nasi 2 porsi")]),
        ("kemarin lusa jajan 10rb", hari_ini, [(10000, None, date(2026, 11, 8), "jajan")]),
        ("28 desember kopi 20rb", date(2026, 1, 5), [(20000, None, date(2025, 12, 28), "kopi")]),
        ("kopi 20rb 6 agustus 2025 bca", hari_ini, [(20000, "BCA Atha", date(2025, 8, 6), "kopi")]),
        ("kopi 20rb\nbensin 30rb\nseabank kemarin", hari_ini, [
            (20000, "Seabank Atha", date(2026, 11, 9), "kopi"),
            (30000, "Seabank Atha", date(2026, 11, 9), "bensin")]),
        ("kopi 20rb cash; bensin 30rb bca", hari_ini, [
            (20000, "Cash", date(2026, 11, 10), "kopi"),
            (30000, "BCA Atha", date(2026, 11, 10), "bensin")]),
        ("bensin 30k, ketoprak 40k kemarin", hari_ini, [
            (30000, None, date(2026, 11, 9), "bensin"),
            (40000, None, date(2026, 11, 9), "ketoprak")]),
        ("10 nov 2026 kopi 20k, air putih 5k, pijet 200k", date(2026, 11, 12), [
            (20000, None, date(2026, 11, 10), "kopi"),
            (5000, None, date(2026, 11, 10), "air putih"),
            (200000, None, date(2026, 11, 10), "pijet")]),
        ("kopi 20k,bensin 30k seabank", hari_ini, [
            (20000, "Seabank Atha", date(2026, 11, 10), "kopi"),
            (30000, "Seabank Atha", date(2026, 11, 10), "bensin")]),
        ("sewa 1,5jt, kopi 20rb, bca", hari_ini, [
            (1500000, "BCA Atha", date(2026, 11, 10), "sewa"),
            (20000, "BCA Atha", date(2026, 11, 10), "kopi")]),
        ("kopi 20rb BCA", hari_ini, [(20000, "BCA Atha", date(2026, 11, 10), "kopi")]),
        ("kopi 20rb Seabank Dida", hari_ini, [(20000, "Seabank Dida", date(2026, 11, 10), "kopi")]),
        ("kopi 20rb seabank", hari_ini, [(20000, "Seabank Atha", date(2026, 11, 10), "kopi")]),
        ("kopi 20rb JAGO SAVING", hari_ini, [(20000, "Jago Saving", date(2026, 11, 10), "kopi")]),
        ("kopi", hari_ini, []),
        ("31/02 kopi 20rb", hari_ini, [(20000, None, date(2026, 11, 10), "31/02 kopi")]),
    ]
    gagal = 0
    for pesan, tgl_pesan, harapan in kasus:
        item, catatan = urai_pesan(pesan, tgl_pesan)
        hasil = [(i["nominal"], i["bank"], i["tanggal"], i["keterangan"]) for i in item]
        if hasil == harapan:
            print("OK    ", repr(pesan))
        else:
            gagal += 1
            print("GAGAL ", repr(pesan))
            print("   harapan:", harapan)
            print("   hasil  :", hasil)
    pasang_bank_alias(alias_asli)
    print(f"\n{len(kasus) - gagal} dari {len(kasus)} lolos")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "tes":
        jalankan_tes()
    else:
        pesan = " ".join(sys.argv[1:]) or "tianlala 27rb seabank"
        mulai = time.perf_counter()
        baris, catatan = proses_pesan(pesan)
        print(json.dumps(baris, indent=2, ensure_ascii=False, default=tampil_tanggal))
        for c in catatan:
            print("CATATAN:", c)
        print(f"Total waktu: {time.perf_counter() - mulai:.0f} detik")