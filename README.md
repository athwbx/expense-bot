# expense-bot

Bot Telegram untuk mencatat pengeluaran pribadi dari pesan teks singkat. Berjalan lokal di laptop biasa, gratis, dan memakai model bahasa lewat [Ollama](https://ollama.com).

Contoh pesan: `bensin 30k, ketoprak 40k kemarin`

## Status

- Pesan teks ke baris pengeluaran: berfungsi, diuji di Windows.
- Konfirmasi lewat tombol Telegram (pilih bank, Simpan, Batal): berfungsi.
- Penyimpanan ke berkas TSV lokal (dipisah tab, bisa ditempel ke Google Sheets): berfungsi.
- Penyimpanan ke Google Sheets: opsional, baru ditulis dan masih dalam pengujian.
- Foto struk: eksperimen terpisah (`test_struk_glm_ocr.py`), belum terhubung ke bot.

## Cara kerja

Prinsip utamanya: **hal yang bisa dicari dengan aturan pasti dikerjakan Python, model hanya menilai.** Model bahasa kecil sering salah di angka, jadi nominal tidak pernah dikirim ke model.

```
Pesan Telegram
      |
      v
bot_expense.py      menerima pesan, hanya melayani satu ID Telegram
      |
      v
catat_teks.py       Python: baca nominal, bank, dan tanggal
      |
      v
Gemma 4 (Ollama)    menerima daftar keterangan bernomor tanpa nominal,
      |             menentukan kategori dan merapikan deskripsi
      v
Tombol konfirmasi   pilih bank bila belum disebut, lalu Simpan atau Batal
      |
      v
Google Sheets (atau pengeluaran.tsv bila belum diatur)
```

## Contoh

Pesan: `kopi 20rb cash; bensin 30rb bca kemarin` (dijalankan pada 10 Nov 2026)

Hasil di `pengeluaran.tsv` (kolom dipisah tab, ditampilkan di sini dengan spasi):

```
Date        Bank      Category        Description  Amount
2026-11-09  Cash      Jajan           Kopi         20000
2026-11-09  BCA Atha  Transportation  Bensin       30000
```

Karena dipisah tab, baris-baris ini bisa disalin dan ditempel ke Google Sheets, dan kolomnya langsung terpecah.

Kategori ditentukan model, jadi bisa meleset. Kategori yang tidak dikenal merek atau namanya bisa dikoreksi dengan menambah petunjuk di `prompt_teks.txt`.

## Format pesan

- **Nominal:** `27rb`, `27k`, `27.000`, `27000`, `rp27.000`, `1,5jt`. Angka kecil tanpa satuan (misalnya jumlah porsi) diabaikan, kecuali ditulis dengan `Rp`.
- **Bank:** dikenali dari alias di `bank_alias.json` (huruf besar atau kecil tidak berpengaruh). Kalau tidak disebut, bot menampilkan tombol pilihan bank.
- **Tanggal:** `hari ini`, `kemarin`, `kemarin lusa`, `6 agustus`, `6/8` (hari/bulan), atau `6 agustus 2025`. Tanpa tanggal, dipakai tanggal pesan (zona waktu UTC+7).
- **Banyak item:** pisahkan dengan koma, titik koma, atau baris baru. Bank atau tanggal yang disebut sekali berlaku untuk semua item di pesan itu, kecuali item punya sendiri.

## Kebutuhan

- Windows (satu-satunya sistem yang diuji)
- Python 3.12
- Ollama, dikembangkan dengan versi 0.40.14
- Model `gemma4:e4b-it-qat`
- Bot Telegram dari [@BotFather](https://t.me/BotFather)

## Instalasi

Di PowerShell:

```
git clone https://github.com/athwbx/expense-bot.git
cd expense-bot
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
ollama pull gemma4:e4b-it-qat
Copy-Item .env.example .env
Copy-Item bank_alias.example.json bank_alias.json
```

Sesuaikan `bank_alias.json` dengan bank atau dompet digital milikmu (lihat bagian Konfigurasi), lalu isi `.env`:

- `TELEGRAM_TOKEN`: token dari BotFather. Jangan dibagikan, kalau bocor cabut dengan `/revoke` di BotFather.
- `ALLOWED_USER_ID`: ID Telegram yang boleh memakai bot. Kalau belum tahu, kosongkan dulu, jalankan bot, kirim `/start`, lalu salin ID yang dibalas bot ke `.env` dan jalankan ulang.

## Menjalankan

```
python bot_expense.py
```

Laptop harus menyala dan skrip harus berjalan supaya bot membalas. Bot memakai polling, jadi tidak butuh alamat publik. Hentikan dengan Ctrl+C.

## Google Sheets (opsional)

Tanpa konfigurasi, hasil ditulis ke `pengeluaran.tsv`. Untuk menulis langsung ke Google Sheets:

1. Buat project di Google Cloud Console, lalu aktifkan Google Sheets API dan Google Drive API.
2. Buat service account dan unduh key JSON-nya. Simpan sebagai `service_account.json` di folder proyek (sudah masuk `.gitignore`).
3. Bagikan spreadsheet ke alamat `client_email` di file itu, sebagai Editor.
4. Isi `.env`: `GOOGLE_SHEET_ID` (bagian URL di antara `/d/` dan `/edit`), `GOOGLE_SHEET_TAB` (nama tab, bawaannya `Pengeluaran`), dan `GOOGLE_SERVICE_ACCOUNT_FILE` bila nama atau lokasi filenya berbeda.

Kolom yang diisi adalah A sampai E (Date, Bank, Category, Description, Amount), tepat di bawah baris terakhir kolom A. Kolom lain tidak disentuh. Nilai Bank dan Category harus sama persis dengan pilihan di spreadsheet. Kalau penulisan ke Sheets gagal, baris dialihkan ke `pengeluaran.tsv` supaya tidak hilang, dan bot memberi tahu alasannya.

## Pengujian

```
python catat_teks.py tes
python catat_teks.py "kopi 20rb seabank"
```

Perintah pertama menjalankan 26 kasus untuk pembacaan nominal, bank, dan tanggal, tanpa memanggil model. Perintah kedua memanggil model dan mencetak hasilnya.

## Konfigurasi

- **Kategori:** daftar dan contohnya ada di `prompt_teks.txt`. Daftar yang sama juga tertulis di kelas `ItemTeks` di `catat_teks.py`, jadi ubah keduanya bersamaan.
- **Bank:** `bank_alias.json`, berisi pasangan `"alias yang diketik": "nama bank"`. Nama di sisi kanan harus sama persis dengan pilihan yang kamu pakai di spreadsheet. Alias yang lebih panjang didahulukan, jadi `seabank atha` menang atas `seabank`. Berkas ini bersifat pribadi dan masuk `.gitignore`. Yang ikut repo hanya `bank_alias.example.json`. Bot harus dijalankan ulang setelah berkas ini diubah.

## Performa

Diukur sekali di laptop pengembang (Intel i5-10500H, RAM 24 GB, GTX 1650 Max-Q 4 GB VRAM): sekitar 11 sampai 12 detik per pesan teks, dan sekitar 32 detik untuk pesan pertama setelah model lama tidak dipakai. Angka ini hanya satu pengukuran, bukan patokan.

## Privasi dan keamanan

- Semua pemrosesan berjalan di laptop sendiri, tidak ada layanan AI berbayar atau cloud.
- Bot hanya merespons satu ID Telegram, pesan dari orang lain diabaikan.
- Isi pesan tidak ditulis ke log.
- `.env`, `bank_alias.json`, `pengeluaran.tsv`, dan kunci layanan Google masuk `.gitignore`.

## Batasan yang diketahui

- Hasil yang menunggu konfirmasi disimpan di memori. Kalau bot dijalankan ulang, pesan lama kedaluwarsa dan perlu dikirim ulang.
- Belum ada tombol untuk mengganti kategori, koreksi dilakukan manual di Sheets atau berkas TSV.
- Panggilan ke Telegram yang timeout diulang sampai 3 kali. Kalau koneksi terus gagal, hasil tidak muncul di chat dan pesan perlu dikirim ulang.
- Satu pesan diproses satu per satu karena keterbatasan laptop.
- Hanya satu pengguna yang didukung.

## Rencana

- Foto struk dengan GLM-OCR, dengan angka dibaca Python, bukan model
- Perintah `/undo`, pencegahan duplikat, dan ringkasan harian
- Memindahkan daftar kategori ke berkas konfigurasi

## Eksperimen foto struk

`test_struk_glm_ocr.py` mencoba alur dua tahap: GLM-OCR menyalin teks dari foto, lalu Gemma 4 mencatat. Skrip ini masih berupa percobaan: model yang membaca barang, harga, dan total, dan diskon pada struk tertentu bisa menempel ke barang yang salah. Pelajaran dari percobaan ini adalah alasan angka diserahkan ke Python di jalur teks. Skrip ini berdiri sendiri dan tidak dipakai oleh bot.

## Lisensi

Belum ditentukan.