import asyncio
import csv
import logging
import os
from datetime import timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from catat_teks import BANK_ALIAS, proses_pesan, tampil_tanggal

CSV_PATH = Path(__file__).with_name("pengeluaran.csv")
KOLOM = ["Date", "Bank", "Category", "Description", "Amount"]
WIB = timezone(timedelta(hours=7))  # Jakarta tidak pakai daylight saving, jadi cukup selisih tetap
BANK_PILIHAN = list(dict.fromkeys(BANK_ALIAS.values()))  # nama unik, urutan tetap

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # supaya token di URL tidak ikut tercetak
log = logging.getLogger("expense-bot")


def id_diizinkan():
    """ID Telegram yang boleh memakai bot, dibaca dari environment variable."""
    nilai = os.environ.get("ALLOWED_USER_ID", "").strip()
    return int(nilai) if nilai.isdigit() else None


def rupiah(n):
    return "Rp" + f"{n:,}".replace(",", ".")


def format_hasil(baris, catatan):
    teks = [f"hasil baca ({len(baris)} item):", ""]
    for n, b in enumerate(baris, start=1):
        teks.append(
            f"{n}. {tampil_tanggal(b['Date'])} | {b['Bank'] or 'bank?'} | "
            f"{b['Category'] or 'kategori?'} | {b['Description']} | {rupiah(b['Amount'])}"
        )
    teks += ["", f"total {rupiah(sum(b['Amount'] for b in baris))}"]
    # catatan "bank belum disebut" tidak perlu, tombol bank sudah mewakili
    teks += [f"catatan: {c}" for c in catatan if "bank belum disebut" not in c]
    if not all(b["Bank"] for b in baris):
        teks.append("pilih bank dulu yaa.")
    return "\n".join(teks)


def buat_keyboard(kunci, baris):
    bank_lengkap = all(b["Bank"] for b in baris)
    awalan = "semua: " if bank_lengkap else ""
    baris_bank = [
        InlineKeyboardButton(awalan + nama, callback_data=f"B|{kunci}|{i}")
        for i, nama in enumerate(BANK_PILIHAN)
    ]
    baris_aksi = [InlineKeyboardButton("Batal", callback_data=f"X|{kunci}")]
    if bank_lengkap:  # Simpan baru muncul kalau semua baris sudah punya bank
        baris_aksi.insert(0, InlineKeyboardButton("Simpan", callback_data=f"S|{kunci}"))
    return InlineKeyboardMarkup([baris_bank, baris_aksi])


def simpan_csv(baris):
    """Tambah baris ke pengeluaran.csv. Tanggal ditulis ISO (yyyy-mm-dd) supaya mudah dibaca ulang."""
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(KOLOM)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        tulis = csv.writer(f)
        for b in baris:
            tulis.writerow([b["Date"].isoformat(), b["Bank"], b["Category"] or "",
                            b["Description"], b["Amount"]])


async def kenalan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start. Kalau ALLOWED_USER_ID belum di-set, bot hanya menyebut ID pengirim."""
    boleh = id_diizinkan()
    user = update.effective_user
    if boleh is None:
        await update.message.reply_text(
            f"ID Telegram kamu: {user.id}\n"
            "set ALLOWED_USER_ID dengan angka ini, lalu jalankan ulang bot."
        )
    elif user.id == boleh:
        await update.message.reply_text(
            "siap. kirim pengeluaran, contoh:\nkopi 20rb seabank\n"
            "atau beberapa sekaligus: bensin 30k, ketoprak 40k kemarin"
        )


async def terima_teks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pesan = update.message
    data_bot = context.application.bot_data
    status = await pesan.reply_text("sedang diproses...")
    tanggal_pesan = pesan.date.astimezone(WIB).date()
    try:
        # satu per satu: Ollama di laptop ini tidak kuat melayani beberapa pesan bersamaan
        async with data_bot["lock"]:
            baris, catatan = await asyncio.to_thread(proses_pesan, pesan.text, tanggal_pesan)
    except Exception:
        log.exception("gagal memproses pesan")
        await status.edit_text("waduh, gagal diproses. cek apakah Ollama jalan, lalu coba lagi yaa.")
        return
    if not baris:
        await status.edit_text(
            "nggak ada nominal yang kebaca. contoh: kopi 20rb seabank\n" + "\n".join(catatan)
        )
        return
    data_bot["n"] += 1
    kunci = str(data_bot["n"])
    data_bot["pending"][kunci] = {"baris": baris, "catatan": catatan}
    await status.edit_text(format_hasil(baris, catatan), reply_markup=buat_keyboard(kunci, baris))


async def terima_foto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("foto struk belum didukung di versi ini, baru teks dulu yaa.")


async def tombol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # tombol harus selalu dijawab, kata dokumentasi resminya
    if query.from_user.id != id_diizinkan():
        return
    bagian = query.data.split("|")
    aksi, kunci = bagian[0], bagian[1]
    pending = context.application.bot_data["pending"]
    data = pending.get(kunci)
    if data is None:
        await query.edit_message_text(
            "data ini sudah diproses atau kedaluwarsa (bot mungkin sempat restart). kirim ulang pesannya yaa."
        )
        return
    try:
        if aksi == "B":
            nama = BANK_PILIHAN[int(bagian[2])]
            for b in data["baris"]:
                b["Bank"] = nama
            await query.edit_message_text(
                format_hasil(data["baris"], data["catatan"]),
                reply_markup=buat_keyboard(kunci, data["baris"]),
            )
        elif aksi == "S":
            simpan_csv(pending.pop(kunci)["baris"])  # pop dulu, jadi tekan dua kali tidak menyimpan dua kali
            await query.edit_message_text(
                f"tersimpan {len(data['baris'])} baris ke {CSV_PATH.name}.\n"
                + format_hasil(data["baris"], [])
            )
        elif aksi == "X":
            pending.pop(kunci)
            await query.edit_message_text("dibatalkan, nggak ada yang disimpan.")
    except BadRequest as e:
        if "not modified" not in str(e).lower():  # menekan bank yang sama dua kali itu wajar
            raise


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_TOKEN belum di-set (lihat langkah di chat).")
    boleh = id_diizinkan()
    # concurrent_updates: tombol tetap responsif selagi Gemma bekerja, lock di atas yang menjaga antrean
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.bot_data.update(lock=asyncio.Lock(), pending={}, n=0)

    app.add_handler(CommandHandler("start", kenalan))
    if boleh is None:
        log.warning("ALLOWED_USER_ID belum di-set, bot hanya akan menyebut ID pengirim.")
        app.add_handler(MessageHandler(filters.ALL, kenalan))
    else:
        hanya_aku = filters.User(user_id=boleh)
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & hanya_aku, terima_teks))
        app.add_handler(MessageHandler(filters.PHOTO & hanya_aku, terima_foto))
        app.add_handler(CallbackQueryHandler(tombol))
    log.info("bot jalan. berhenti dengan Ctrl+C.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()