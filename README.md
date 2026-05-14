# Satellite Environment Downloader

Web app Streamlit untuk mengambil data satelit dan lingkungan per negara, provinsi, kabupaten/kota, atau boundary kustom seperti kelurahan. App ini sekarang cocok dipasang di VPS dan mendukung `background job`, sehingga proses bisa tetap berjalan meski browser ditutup.

## Fitur utama

- pilih wilayah dari `GAUL` atau `boundary kustom` (`GeoJSON` / `Earth Engine table asset`)
- preset `Kelurahan Kota Bandung`
- pilih variabel seperti suhu, kelembaban, radiasi matahari, curah hujan, `NDVI`, `EVI`, polusi, dan gelombang
- frekuensi `daily`, `weekly`, `monthly`
- mode `daily-first`: output weekly/monthly dirangkum dari data harian
- cache per grup data
- `background job` untuk VPS
- riwayat job dan download hasil langsung dari web app

## Kebutuhan

- Python 3.10+
- akun Google Earth Engine
- Google Cloud project yang sudah aktif untuk Earth Engine

Referensi resmi:

- [Earth Engine auth](https://developers.google.com/earth-engine/guides/auth)
- [Earth Engine access](https://developers.google.com/earth-engine/guides/access)

## Instalasi lokal

```bash
cd satellite_dengue_excel
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Operasional VPS yang disarankan

App ini paling aman dijalankan sebagai service `systemd`, lalu diakses lewat `SSH tunnel`. Dengan cara ini app tidak terbuka untuk publik dan tetap hidup walau sesi SSH biasa terputus.

### 1. Clone dari GitHub ke VPS

Contoh:

```bash
git clone https://github.com/USERNAME/REPO.git
cd REPO/satellite_dengue_excel
```

### 2. Buat virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Auth Earth Engine di VPS

Jalankan sekali:

```bash
earthengine authenticate --auth_mode=localhost
earthengine set_project YOUR_PROJECT_ID
```

Kalau VPS tidak punya browser lokal, paling mudah lakukan auth dengan tunnel atau sesi yang bisa membuka browser dari komputer Anda.

### 4. Jalankan manual

```bash
./scripts/start_streamlit.sh
```

Default app akan bind ke `127.0.0.1:8501` melalui [.streamlit/config.toml](/Users/rifkyfauzi/Documents/New%20project/satellite_dengue_excel/.streamlit/config.toml).

### 5. Akses privat dari komputer Anda

Di komputer Anda:

```bash
ssh -L 8501:127.0.0.1:8501 USER@IP_VPS
```

Lalu buka:

```text
http://127.0.0.1:8501
```

Dengan pola ini:

- app tidak perlu dibuka ke publik
- hanya Anda yang bisa mengakses lewat SSH
- koneksi lebih aman dan sederhana

## Menjalankan sebagai service `systemd`

Contoh unit file sudah disiapkan di:

[deploy/systemd/satellite-environment-downloader.service](/Users/rifkyfauzi/Documents/New%20project/satellite_dengue_excel/deploy/systemd/satellite-environment-downloader.service)

Salin lalu sesuaikan `User`, `WorkingDirectory`, dan `ExecStart`.

Contoh langkah:

```bash
sudo cp deploy/systemd/satellite-environment-downloader.service /etc/systemd/system/
sudo nano /etc/systemd/system/satellite-environment-downloader.service
sudo systemctl daemon-reload
sudo systemctl enable satellite-environment-downloader
sudo systemctl start satellite-environment-downloader
sudo systemctl status satellite-environment-downloader
```

Kalau service sudah aktif, app tetap berjalan walau:

- browser Anda ditutup
- sesi SSH biasa terputus

## Background job

Di web app sekarang ada dua mode:

- `Generate di browser`
- `Jalankan di background VPS`

Mode background akan:

- membuat job di folder `satellite_jobs/`
- menjalankan worker Python terpisah
- tetap lanjut walau browser ditutup
- menyimpan log, status, dan path workbook hasil

Riwayat job bisa dilihat lagi di bagian `Riwayat Job VPS`.

## Download hasil dari VPS ke PC

Ada beberapa cara mudah:

1. paling praktis: unduh langsung dari tombol download di `Riwayat Job VPS`
2. lewat `scp`:

```bash
scp USER@IP_VPS:/path/ke/file.xlsx .
```

3. lewat `rsync`:

```bash
rsync -av USER@IP_VPS:/path/ke/folder_hasil/ .
```

Karena app menyimpan path workbook final di status job, Anda tidak perlu menebak lokasi file hasil.

## Struktur runtime penting

- `satellite_cache_runs/`:
  cache per grup data dan workbook final
- `satellite_jobs/`:
  status, log, dan riwayat job background
- `boundary_inputs/`:
  salinan boundary yang diupload lewat web
- `sample_boundaries/`:
  boundary contoh yang ikut disimpan di repo, termasuk preset `Kelurahan Kota Bandung`

Folder-folder ini sudah diabaikan di `.gitignore`, jadi aman untuk repo GitHub.

## Push ke GitHub

Paling aman: jadikan folder `satellite_dengue_excel` sebagai repo tersendiri.

Dengan begitu:

- file proyek lain di luar app tidak ikut terbawa
- cache/job/log lokal tidak ikut ter-push
- struktur repo menjadi jauh lebih bersih

Urutan amannya:

```bash
cd "/Users/rifkyfauzi/Documents/New project/satellite_dengue_excel"
git init
git status
git add .
git commit -m "Prepare VPS-ready satellite environment downloader"
git branch -M main
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```

Kalau repo GitHub sudah ada dan tidak kosong, lakukan `git pull --rebase origin main` dulu sebelum `git push`.

## Dataset utama

- [ERA5 Hourly](https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_HOURLY)
- [CHIRPS Daily](https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY)
- [MODIS MOD09GA](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD09GA)
- [Sentinel-5P NO2](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_NO2)
- [Sentinel-5P CO](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_CO)
- [Sentinel-5P Aerosol Index](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_AER_AI)
- [FAO GAUL level2](https://developers.google.com/earth-engine/datasets/catalog/FAO_GAUL_2015_level2)
