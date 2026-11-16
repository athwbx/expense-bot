"""Bot Telegram pencatat pengeluaran.

Pesan teks seperti "kopi 20rb seabank" diubah menjadi baris pengeluaran oleh catat_teks.py, dan foto
struk oleh struk.py. Hasilnya ditampilkan dengan tombol konfirmasi, lalu disimpan ke Google Sheets, atau ke
pengeluaran.tsv kalau Sheets belum diatur (lihat penyimpanan.py). Foto struk dihapus dari chat setelah Simpan atau Batal.

Konfigurasi lewat environment variable atau file .env (lihat .env.example):
    TELEGRAM_TOKEN    token dari BotFather (wajib)
    ALLOWED_USER_ID   ID Telegram pengguna yang dilayani. Boleh lebih dari satu, pisahkan dengan koma.
                      Kalau kosong, bot berjalan dalam mode setup yang hanya
                      menyebutkan ID pengirim pesan.
"""
import asyncio
import logging
import os
import re
from datetime import timedelta, timezone
from typing import get_args

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from catat_teks import BANK_ALIAS, Kategori, proses_pesan, tampil_tanggal
from edit_baris import baca_deskripsi, baca_jumlah, baca_tanggal
from penyimpanan import DATA_PATH, simpan_baris, tujuan_aktif
from struk import proses_foto

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv bersifat opsional, environment variable biasa tetap dibaca
    pass

WIB = timezone(timedelta(hours=7))  # Jakarta tidak memakai daylight saving, selisih tetap cukup
BANK_PILIHAN = list(dict.fromkeys(BANK_ALIAS.values()))  # nama unik, urutan tetap
KATEGORI_PILIHAN = list(get_args(Kategori))
NAMA_BAGIAN = {"T": "tanggal", "D": "deskripsi", "J": "jumlah"}
CONTOH_BAGIAN = {"T": "kemarin, 6 agustus, atau 6/8", "D": "Nasi Goreng Percetakan Negara", "J": "71k atau 71.000"}
MAKS_PANJANG_PESAN = 500  # pesan lebih panjang ditolak sebelum membuang waktu model
MAKS_PENDING = 50  # hasil yang menunggu konfirmasi, yang paling lama dibuang bila melebihi ini
TIMEOUT_TELEGRAM = 20  # detik. Bawaan library hanya 5 detik untuk connect, terlalu ketat untuk koneksi yang lambat
JUMLAH_COBA = 3  # percobaan untuk panggilan Telegram yang gagal karena jaringan

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # URL request memuat token, jangan dicatat
log = logging.getLogger("expense-bot")


def id_diizinkan():
    """Himpunan ID Telegram yang boleh memakai bot, dari ALLOWED_USER_ID (pisahkan dengan koma).
    Kosong kalau belum diatur. ValueError kalau ada nilai yang bukan angka, supaya salah ketik tidak diam-diam menolak seseorang."""
    potongan = [p for p in re.split(r"[,;\s]+", os.environ.get("ALLOWED_USER_ID", "").strip()) if p]
    salah = [p for p in potongan if not p.isdigit()]
    if salah:
        raise ValueError(f"ALLOWED_USER_ID memuat nilai yang bukan angka: {', '.join(salah)}")
    return frozenset(int(p) for p in potongan)


def rupiah(n):
    return "Rp" + f"{n:,}".replace(",", ".")


def format_hasil(baris, catatan):
    teks = [f"Hasil baca ({len(baris)} item):", ""]
    for n, b in enumerate(baris, start=1):
        teks.append(
            f"{n}. {tampil_tanggal(b['Date'])} | {b['Bank'] or 'bank?'} | "
            f"{b['Category'] or 'kategori?'} | {b['Description']} | {rupiah(b['Amount'])}"
        )
    teks += ["", f"Total {rupiah(sum(b['Amount'] for b in baris))}"]
    # catatan "bank belum disebut" tidak perlu ditampilkan, tombol bank sudah mewakili
    teks += [f"Catatan: {c}" for c in catatan if "bank belum disebut" not in c]
    if not all(b["Bank"] for b in baris):
        teks.append("Pilih bank terlebih dahulu.")
    return "\n".join(teks)


def buat_keyboard(kunci, baris, pilih_bank=False):
    """Susun tombol. Pemilih bank tampil kalau ada item tanpa bank, atau setelah menekan Ganti bank."""
    bank_lengkap = all(b["Bank"] for b in baris)
    if pilih_bank or not bank_lengkap:
        tombol_bank = [
            InlineKeyboardButton(nama, callback_data=f"B|{kunci}|{i}")
            for i, nama in enumerate(BANK_PILIHAN)
        ]
        susunan = [tombol_bank[i:i + 2] for i in range(0, len(tombol_bank), 2)]  # dua per baris, nama bank panjang
        susunan.append([InlineKeyboardButton("Batal", callback_data=f"X|{kunci}")])
        if bank_lengkap:  # sedang mengganti bank, sediakan jalan kembali
            susunan[-1].insert(0, InlineKeyboardButton("Kembali", callback_data=f"K|{kunci}"))
        return InlineKeyboardMarkup(susunan)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Simpan", callback_data=f"S|{kunci}"),
         InlineKeyboardButton("Edit", callback_data=f"E|{kunci}")],
        [InlineKeyboardButton("Ganti bank", callback_data=f"G|{kunci}"),
         InlineKeyboardButton("Batal", callback_data=f"X|{kunci}")],
    ])


def kelompok(tombol_, per_baris):
    return [tombol_[i:i + per_baris] for i in range(0, len(tombol_), per_baris)]


def ringkas_baris(n, b):
    return (f"{n}. {tampil_tanggal(b['Date'])} | {b['Bank'] or 'bank?'} | {b['Category'] or 'kategori?'} | "
            f"{b['Description']} | {rupiah(b['Amount'])}")


def tandai_diedit(data):
    """Status COCOK atau PERLU DICEK dihitung sebelum edit, jadi dibuang dan diganti catatan edit."""
    data["catatan"] = [c for c in data["catatan"] if not re.search(r"COCOK|PERLU DICEK|TIDAK BISA DICEK", c)]
    if "Sudah diedit manual." not in data["catatan"]:
        data["catatan"].append("Sudah diedit manual.")


# Layar hasil: setiap fungsi mengembalikan (teks, tombol)
def layar_utama(kunci, data):
    return format_hasil(data["baris"], data["catatan"]), buat_keyboard(kunci, data["baris"])


def layar_pilih_baris(kunci, data):
    nomor = [InlineKeyboardButton(str(n), callback_data=f"R|{kunci}|{n - 1}")
             for n in range(1, min(len(data["baris"]), 60) + 1)]
    susunan = kelompok(nomor, 5) + [[InlineKeyboardButton("Kembali", callback_data=f"K|{kunci}")]]
    return (format_hasil(data["baris"], data["catatan"]) + "\n\nPilih nomor baris yang mau diedit.",
            InlineKeyboardMarkup(susunan))


def layar_pilih_bagian(kunci, data, i):
    ada_banyak = len(data["baris"]) > 1
    susunan = [
        [InlineKeyboardButton("Tanggal", callback_data=f"F|{kunci}|{i}|T"),
         InlineKeyboardButton("Bank", callback_data=f"F|{kunci}|{i}|B"),
         InlineKeyboardButton("Kategori", callback_data=f"F|{kunci}|{i}|K")],
        [InlineKeyboardButton("Deskripsi", callback_data=f"F|{kunci}|{i}|D"),
         InlineKeyboardButton("Jumlah", callback_data=f"F|{kunci}|{i}|J")],
        ([InlineKeyboardButton("Hapus baris", callback_data=f"F|{kunci}|{i}|H")] if ada_banyak else [])
        + [InlineKeyboardButton("Kembali", callback_data=f"K|{kunci}")],
    ]
    return (f"Edit baris {i + 1}:\n{ringkas_baris(i + 1, data['baris'][i])}\n\nPilih bagian yang diubah.",
            InlineKeyboardMarkup(susunan))


def layar_pilih_nilai(kunci, data, i, kode):
    """Pemilih bank (kode B) atau kategori (kode K) untuk satu baris."""
    pilihan, aksi = (BANK_PILIHAN, "N") if kode == "B" else (KATEGORI_PILIHAN, "C")
    tombol_ = [InlineKeyboardButton(nama, callback_data=f"{aksi}|{kunci}|{i}|{j}") for j, nama in enumerate(pilihan)]
    susunan = kelompok(tombol_, 2) + [[InlineKeyboardButton("Kembali", callback_data=f"K|{kunci}")]]
    bagian = "bank" if kode == "B" else "kategori"
    return f"Pilih {bagian} baru untuk baris {i + 1}:\n{ringkas_baris(i + 1, data['baris'][i])}", InlineKeyboardMarkup(susunan)


def layar_ketik(kunci, data, i, kode):
    b = data["baris"][i]
    sekarang = {"T": tampil_tanggal(b["Date"]), "D": b["Description"], "J": rupiah(b["Amount"])}[kode]
    teks = (f"Edit baris {i + 1}, {NAMA_BAGIAN[kode]}. Nilai sekarang: {sekarang}\n"
            f"Ketik nilai baru, contoh: {CONTOH_BAGIAN[kode]}\nTekan Kembali untuk membatalkan.")
    return teks, InlineKeyboardMarkup([[InlineKeyboardButton("Kembali", callback_data=f"K|{kunci}")]])


async def coba_ulang(fungsi, *args, **kwargs):
    """Jalankan panggilan edit ke Telegram, ulangi kalau jaringan timeout.
    Hanya untuk edit, karena mengulang edit aman (hasilnya sama), mengulang kirim pesan bisa dobel."""
    for percobaan in range(1, JUMLAH_COBA + 1):
        try:
            return await fungsi(*args, **kwargs)
        except BadRequest as e:  # harus sebelum NetworkError, BadRequest adalah turunannya
            if "not modified" in str(e).lower():  # percobaan sebelumnya ternyata sudah masuk
                return None
            raise
        except NetworkError:  # termasuk TimedOut
            if percobaan == JUMLAH_COBA:
                raise
            log.warning("Telegram tidak terjangkau, mencoba lagi (%d/%d)", percobaan, JUMLAH_COBA)
            await asyncio.sleep(2 * percobaan)


def simpan_pending(data_bot, baris, catatan, pemilik, foto=None):
    """Simpan hasil di memori sampai dikonfirmasi. Hanya pemilik (pengirim pesan) yang boleh menekan tombolnya.
    foto: (chat_id, message_id) pesan foto struk yang dihapus setelah Simpan atau Batal. Kembalikan kunci untuk tombol."""
    data_bot["n"] += 1
    kunci = str(data_bot["n"])
    pending = data_bot["pending"]
    pending[kunci] = {"baris": baris, "catatan": catatan, "pemilik": pemilik, "foto": foto}
    while len(pending) > MAKS_PENDING:  # dict menjaga urutan masuk, jadi yang pertama paling lama
        pending.pop(next(iter(pending)))
    return kunci


async def kenalan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Penangan /start. Pada mode setup (ALLOWED_USER_ID kosong) hanya menyebut ID pengirim."""
    boleh = context.application.bot_data["boleh"]
    user = update.effective_user
    pesan = update.effective_message
    if not boleh:
        await pesan.reply_text(
            f"Mode setup. ID Telegram kamu: {user.id}\n"
            "Tambahkan angka ini ke ALLOWED_USER_ID di file .env (pisahkan dengan koma kalau lebih dari satu orang), lalu jalankan ulang bot."
        )
    elif user.id in boleh:
        await pesan.reply_text(
            "Siap. Kirim pengeluaran, contoh:\nkopi 20rb seabank\n"
            "atau beberapa sekaligus: bensin 30k, ketoprak 40k kemarin"
        )


async def terima_teks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pesan = update.message
    data_bot = context.application.bot_data
    if len(pesan.text) > MAKS_PANJANG_PESAN:
        await pesan.reply_text(f"Pesan terlalu panjang (maksimal {MAKS_PANJANG_PESAN} karakter).")
        return
    menunggu = data_bot.setdefault("menunggu", {}).get(pesan.from_user.id)
    if menunggu:  # pengguna sedang mengedit satu baris: teks ini adalah nilai barunya, bukan pengeluaran baru
        await terapkan_ketikan(pesan, context, menunggu)
        return
    status = await pesan.reply_text("Sedang diproses...")
    tanggal_pesan = pesan.date.astimezone(WIB).date()
    try:
        # satu per satu: Ollama di laptop tidak kuat melayani beberapa permintaan bersamaan
        async with data_bot["lock"]:
            baris, catatan = await asyncio.to_thread(proses_pesan, pesan.text, tanggal_pesan)
    except Exception as e:
        log.exception("gagal memproses pesan")
        await coba_ulang(
            status.edit_text,
            f"Gagal memproses pesan ({type(e).__name__}). Pastikan Ollama berjalan, atau lihat log di terminal.",
        )
        return
    if not baris:
        await coba_ulang(
            status.edit_text,
            "Tidak ada nominal yang terbaca. Contoh: kopi 20rb seabank\n" + "\n".join(catatan),
        )
        return
    log.info("pesan diproses: %d item", len(baris))  # isi pesan sengaja tidak dicatat
    kunci = simpan_pending(data_bot, baris, catatan, pesan.from_user.id)
    await coba_ulang(
        status.edit_text, format_hasil(baris, catatan), reply_markup=buat_keyboard(kunci, baris)
    )


async def terapkan_ketikan(pesan, context, menunggu):
    """Terapkan nilai yang diketik pengguna (tanggal, deskripsi, atau jumlah) ke baris yang sedang diedit."""
    data_bot = context.application.bot_data
    data = data_bot["pending"].get(menunggu["kunci"])
    i, kode = menunggu["baris"], menunggu["kode"]
    if data is None or i >= len(data["baris"]):
        data_bot["menunggu"].pop(pesan.from_user.id, None)
        await pesan.reply_text("Data yang mau diedit sudah kedaluwarsa. Kirim ulang pesan pengeluarannya.")
        return
    hari_ini = pesan.date.astimezone(WIB).date()
    if kode == "J":
        nilai, kolom, galat = baca_jumlah(pesan.text), "Amount", "Jumlah tidak terbaca. Contoh: 71k atau 71.000."
    elif kode == "T":
        nilai, kolom, galat = baca_tanggal(pesan.text, hari_ini), "Date", "Tanggal tidak dikenali. Contoh: kemarin, 6 agustus, atau 6/8."
    else:
        nilai, kolom, galat = baca_deskripsi(pesan.text), "Description", "Deskripsi kosong."
    if nilai is None:
        await pesan.reply_text(galat + " Coba lagi, atau tekan Kembali di pesan hasil.")
        return  # tetap menunggu nilai yang benar
    data["baris"][i][kolom] = nilai
    tandai_diedit(data)
    data_bot["menunggu"].pop(pesan.from_user.id, None)
    try:
        await pesan.delete()  # rapikan chat, boleh gagal
    except Exception:
        pass
    teks, markup = layar_utama(menunggu["kunci"], data)
    chat_id, message_id = menunggu["bubble"]
    await coba_ulang(context.bot.edit_message_text, chat_id=chat_id, message_id=message_id, text=teks, reply_markup=markup)


async def hapus_foto(bot, data):
    """Hapus pesan foto struk dari chat. True kalau berhasil atau tidak ada foto yang perlu dihapus."""
    foto = data.get("foto")
    if not foto:
        return True
    try:
        await bot.delete_message(chat_id=foto[0], message_id=foto[1])
        return True
    except Exception as e:  # Telegram bisa menolak, misalnya pesan lebih dari 48 jam
        log.warning("foto struk tidak bisa dihapus otomatis: %s", e)
        return False


async def terima_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pesan = update.message
    data_bot = context.application.bot_data
    berkas = pesan.photo[-1] if pesan.photo else pesan.document  # foto terbesar, atau gambar yang dikirim sebagai file
    antre = data_bot["lock"].locked()
    status = await pesan.reply_text(
        ("Antre, menunggu proses sebelumnya. " if antre else "") + "Membaca struk, bisa beberapa menit..."
    )
    try:
        file = await berkas.get_file()
        data = bytes(await file.download_as_bytearray())  # di memori saja, foto tidak ditulis ke disk
    except Exception as e:
        log.exception("gagal mengunduh foto")
        await coba_ulang(status.edit_text, f"Gagal mengunduh foto ({type(e).__name__}). Coba kirim ulang.")
        return
    tanggal_pesan = pesan.date.astimezone(WIB).date()
    try:
        async with data_bot["lock"]:
            baris, catatan = await asyncio.to_thread(proses_foto, data, tanggal_pesan, pesan.caption or "")
    except Exception as e:
        log.exception("gagal memproses foto")
        await coba_ulang(
            status.edit_text,
            f"Gagal memproses foto ({type(e).__name__}). Pastikan Ollama berjalan dan model glm-ocr sudah terpasang, "
            "atau lihat log di terminal.",
        )
        return
    if not baris:
        await coba_ulang(status.edit_text, "\n".join(catatan) + "\nFoto tidak dihapus otomatis.")
        return
    log.info("foto diproses: %d baris", len(baris))
    kunci = simpan_pending(data_bot, baris, catatan, pesan.from_user.id, foto=(pesan.chat_id, pesan.message_id))
    await coba_ulang(
        status.edit_text, format_hasil(baris, catatan), reply_markup=buat_keyboard(kunci, baris)
    )


async def tombol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()  # callback query harus dijawab, sesuai dokumentasi resmi
    except NetworkError:  # tombol lama yang baru ditekan setelah jaringan pulih, jawabannya sudah kedaluwarsa
        pass
    data_bot = context.application.bot_data
    if query.from_user.id not in data_bot["boleh"]:
        return
    data_bot.setdefault("menunggu", {}).pop(query.from_user.id, None)  # menekan tombol apa pun membatalkan edit yang menunggu ketikan
    bagian = query.data.split("|")
    aksi, kunci = bagian[0], bagian[1]
    pending = data_bot["pending"]
    data = pending.get(kunci)
    if data is None:
        await coba_ulang(
            query.edit_message_text,
            "Data ini sudah diproses atau kedaluwarsa (bot mungkin sempat dijalankan ulang). "
            "Kirim ulang pesannya.",
        )
        return
    if data["pemilik"] != query.from_user.id:  # tombol hasil pesan orang lain, abaikan
        return
    if aksi == "B":  # pilih bank untuk semua item
        nama = BANK_PILIHAN[int(bagian[2])]
        for b in data["baris"]:
            b["Bank"] = nama
        await coba_ulang(
            query.edit_message_text,
            format_hasil(data["baris"], data["catatan"]),
            reply_markup=buat_keyboard(kunci, data["baris"]),
        )
    elif aksi == "G":  # tampilkan pemilih bank
        await coba_ulang(
            query.edit_message_reply_markup,
            reply_markup=buat_keyboard(kunci, data["baris"], pilih_bank=True),
        )
    elif aksi == "K":  # kembali ke layar hasil dari pemilih bank atau menu edit
        teks, markup = layar_utama(kunci, data)
        await coba_ulang(query.edit_message_text, teks, reply_markup=markup)
    elif aksi == "E":  # buka menu edit: pilih baris dulu kalau lebih dari satu
        teks, markup = layar_pilih_baris(kunci, data) if len(data["baris"]) > 1 else layar_pilih_bagian(kunci, data, 0)
        await coba_ulang(query.edit_message_text, teks, reply_markup=markup)
    elif aksi == "R":  # baris dipilih
        teks, markup = layar_pilih_bagian(kunci, data, int(bagian[2]))
        await coba_ulang(query.edit_message_text, teks, reply_markup=markup)
    elif aksi == "F":  # bagian yang diedit dipilih
        i, kode = int(bagian[2]), bagian[3]
        if i >= len(data["baris"]):
            return
        if kode in ("B", "K"):
            teks, markup = layar_pilih_nilai(kunci, data, i, kode)
        elif kode == "H":
            if len(data["baris"]) > 1:  # baris terakhir tidak bisa dihapus, pakai Batal
                del data["baris"][i]
                tandai_diedit(data)
            teks, markup = layar_utama(kunci, data)
        else:  # T, D, J: tunggu ketikan berikutnya dari pengguna ini
            data_bot["menunggu"][query.from_user.id] = {
                "kunci": kunci, "baris": i, "kode": kode,
                "bubble": (query.message.chat_id, query.message.message_id),
            }
            teks, markup = layar_ketik(kunci, data, i, kode)
        await coba_ulang(query.edit_message_text, teks, reply_markup=markup)
    elif aksi in ("N", "C"):  # bank atau kategori baru untuk satu baris
        i, j = int(bagian[2]), int(bagian[3])
        if i < len(data["baris"]):
            if aksi == "N":
                data["baris"][i]["Bank"] = BANK_PILIHAN[j]
            else:
                data["baris"][i]["Category"] = KATEGORI_PILIHAN[j]
            tandai_diedit(data)
        teks, markup = layar_utama(kunci, data)
        await coba_ulang(query.edit_message_text, teks, reply_markup=markup)
    elif aksi == "S":
        if data.get("menyimpan"):  # Simpan ditekan lagi selagi penyimpanan pertama masih berjalan
            return
        data["menyimpan"] = True
        try:
            tujuan, peringatan = await asyncio.to_thread(simpan_baris, data["baris"])
        except OSError:
            data["menyimpan"] = False
            log.exception("gagal menulis %s", DATA_PATH.name)
            await query.message.reply_text(
                f"Gagal menyimpan. Pastikan {DATA_PATH.name} tidak sedang dibuka di aplikasi lain, "
                "lalu tekan Simpan lagi."
            )
            return  # data tetap menunggu, tombol Simpan bisa ditekan lagi
        pending.pop(kunci, None)
        teks = f"Tersimpan {len(data['baris'])} baris ke {tujuan}.\n"
        if peringatan:
            teks += f"Peringatan: {peringatan}\n"
        if not await hapus_foto(context.bot, data):
            teks += "Foto struk belum bisa dihapus otomatis, hapus manual dari chat.\n"
        await coba_ulang(query.edit_message_text, teks + format_hasil(data["baris"], []))
    elif aksi == "X":
        pending.pop(kunci, None)
        teks = "Dibatalkan, tidak ada yang disimpan."
        if not await hapus_foto(context.bot, data):
            teks += " Foto struk belum bisa dihapus otomatis, hapus manual dari chat."
        await coba_ulang(query.edit_message_text, teks)


async def tangani_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Penangkap terakhir supaya error tak terduga tercatat dan tidak mematikan bot."""
    if isinstance(context.error, NetworkError):  # library mengulang sendiri, cukup satu baris
        log.warning("koneksi ke Telegram terganggu: %s", context.error)
        return
    log.error("error tidak tertangani", exc_info=context.error)


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_TOKEN belum diatur. Salin .env.example menjadi .env, "
            "isi nilainya, lalu jalankan lagi."
        )
    try:
        boleh = id_diizinkan()
    except ValueError as e:
        raise SystemExit(str(e))
    # concurrent_updates: tombol tetap responsif selagi model bekerja, lock yang menjaga antrean
    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(True)
        .connect_timeout(TIMEOUT_TELEGRAM)
        .read_timeout(TIMEOUT_TELEGRAM)
        .write_timeout(TIMEOUT_TELEGRAM)
        .pool_timeout(TIMEOUT_TELEGRAM)
        .get_updates_connect_timeout(TIMEOUT_TELEGRAM)
        .build()
    )
    app.bot_data.update(lock=asyncio.Lock(), pending={}, n=0, boleh=boleh, menunggu={})
    app.add_error_handler(tangani_error)

    app.add_handler(CommandHandler("start", kenalan))
    if not boleh:
        log.warning("ALLOWED_USER_ID belum diatur, bot berjalan dalam mode setup.")
        app.add_handler(MessageHandler(filters.ALL, kenalan))
    else:
        # UpdateType.MESSAGE: pesan yang diedit tidak diproses ulang
        hanya_aku = filters.User(user_id=boleh) & filters.UpdateType.MESSAGE  # semua ID yang diizinkan
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & hanya_aku, terima_teks))
        app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & hanya_aku, terima_foto))
        app.add_handler(CallbackQueryHandler(tombol))
    log.info("Penyimpanan: %s", tujuan_aktif())
    log.info("Bot berjalan. Hentikan dengan Ctrl+C.")
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()