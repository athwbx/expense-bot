"""Bot Telegram pencatat pengeluaran.

Pesan teks seperti "kopi 20rb seabank" diubah menjadi baris pengeluaran oleh
catat_teks.py, ditampilkan dengan tombol konfirmasi, lalu disimpan ke pengeluaran.tsv (dipisah tab, bisa ditempel ke Google Sheets).

Konfigurasi lewat environment variable atau file .env (lihat .env.example):
    TELEGRAM_TOKEN    token dari BotFather (wajib)
    ALLOWED_USER_ID   ID Telegram satu-satunya pengguna yang dilayani.
                      Kalau kosong, bot berjalan dalam mode setup yang hanya
                      menyebutkan ID pengirim pesan.
"""
import asyncio
import csv
import logging
import os
from datetime import timedelta, timezone
from pathlib import Path

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

from catat_teks import BANK_ALIAS, proses_pesan, tampil_tanggal

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv bersifat opsional, environment variable biasa tetap dibaca
    pass

DATA_PATH = Path(__file__).with_name("pengeluaran.tsv")  # dipisah tab: Google Sheets memecah kolom otomatis saat ditempel
KOLOM = ["Date", "Bank", "Category", "Description", "Amount"]
WIB = timezone(timedelta(hours=7))  # Jakarta tidak memakai daylight saving, selisih tetap cukup
BANK_PILIHAN = list(dict.fromkeys(BANK_ALIAS.values()))  # nama unik, urutan tetap
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
    """ID Telegram yang boleh memakai bot, atau None kalau belum diatur."""
    nilai = os.environ.get("ALLOWED_USER_ID", "").strip()
    return int(nilai) if nilai.isdigit() else None


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
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Simpan", callback_data=f"S|{kunci}"),
        InlineKeyboardButton("Ganti bank", callback_data=f"G|{kunci}"),
        InlineKeyboardButton("Batal", callback_data=f"X|{kunci}"),
    ]])


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


def simpan_data(baris):
    """Tambahkan baris ke pengeluaran.tsv. Pemisah kolom adalah tab, tanggal ditulis ISO (yyyy-mm-dd)."""
    if not DATA_PATH.exists():
        with open(DATA_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter="\t").writerow(KOLOM)
    with open(DATA_PATH, "a", newline="", encoding="utf-8") as f:
        tulis = csv.writer(f, delimiter="\t")
        for b in baris:
            tulis.writerow([b["Date"].isoformat(), b["Bank"], b["Category"] or "",
                            b["Description"], b["Amount"]])


def simpan_pending(data_bot, baris, catatan):
    """Simpan hasil di memori sampai dikonfirmasi. Kembalikan kunci untuk tombol."""
    data_bot["n"] += 1
    kunci = str(data_bot["n"])
    pending = data_bot["pending"]
    pending[kunci] = {"baris": baris, "catatan": catatan}
    while len(pending) > MAKS_PENDING:  # dict menjaga urutan masuk, jadi yang pertama paling lama
        pending.pop(next(iter(pending)))
    return kunci


async def kenalan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Penangan /start. Pada mode setup (ALLOWED_USER_ID kosong) hanya menyebut ID pengirim."""
    boleh = context.application.bot_data["boleh"]
    user = update.effective_user
    pesan = update.effective_message
    if boleh is None:
        await pesan.reply_text(
            f"Mode setup. ID Telegram kamu: {user.id}\n"
            "Isi ALLOWED_USER_ID di file .env dengan angka ini, lalu jalankan ulang bot."
        )
    elif user.id == boleh:
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
    kunci = simpan_pending(data_bot, baris, catatan)
    await coba_ulang(
        status.edit_text, format_hasil(baris, catatan), reply_markup=buat_keyboard(kunci, baris)
    )


async def terima_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Foto struk belum didukung. Saat ini hanya pesan teks.")


async def tombol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()  # callback query harus dijawab, sesuai dokumentasi resmi
    except NetworkError:  # tombol lama yang baru ditekan setelah jaringan pulih, jawabannya sudah kedaluwarsa
        pass
    data_bot = context.application.bot_data
    if query.from_user.id != data_bot["boleh"]:
        return
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
    elif aksi == "K":  # kembali dari pemilih bank
        await coba_ulang(
            query.edit_message_reply_markup, reply_markup=buat_keyboard(kunci, data["baris"])
        )
    elif aksi == "S":
        try:
            simpan_data(data["baris"])
        except OSError:
            log.exception("gagal menulis %s", DATA_PATH.name)
            await query.message.reply_text(
                f"Gagal menyimpan. Pastikan {DATA_PATH.name} tidak sedang dibuka di aplikasi lain, "
                "lalu tekan Simpan lagi."
            )
            return  # data tetap menunggu, tombol Simpan bisa ditekan lagi
        pending.pop(kunci, None)  # tidak ada await di antara simpan dan pop, jadi tidak bisa tersimpan dua kali
        await coba_ulang(
            query.edit_message_text,
            f"Tersimpan {len(data['baris'])} baris ke {DATA_PATH.name}.\n"
            + format_hasil(data["baris"], []),
        )
    elif aksi == "X":
        pending.pop(kunci, None)
        await coba_ulang(query.edit_message_text, "Dibatalkan, tidak ada yang disimpan.")


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
    boleh = id_diizinkan()
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
    app.bot_data.update(lock=asyncio.Lock(), pending={}, n=0, boleh=boleh)
    app.add_error_handler(tangani_error)

    app.add_handler(CommandHandler("start", kenalan))
    if boleh is None:
        log.warning("ALLOWED_USER_ID belum diatur, bot berjalan dalam mode setup.")
        app.add_handler(MessageHandler(filters.ALL, kenalan))
    else:
        # UpdateType.MESSAGE: pesan yang diedit tidak diproses ulang
        hanya_aku = filters.User(user_id=boleh) & filters.UpdateType.MESSAGE
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & hanya_aku, terima_teks))
        app.add_handler(MessageHandler(filters.PHOTO & hanya_aku, terima_foto))
        app.add_handler(CallbackQueryHandler(tombol))
    log.info("Bot berjalan. Hentikan dengan Ctrl+C.")
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


if __name__ == "__main__":
    main()