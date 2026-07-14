# TBB Finansal Veri Projesi

Ana geliştirme ortamı VS Code + Python sanal ortamı + GitHub'dır. Jupyter yalnızca
keşif ve tek seferlik analizlerde yardımcı araç olarak kullanılmalıdır.

## TBB'den otomatik indirme

Belirli tarih aralığını indir:

    python -m src.tbb_dashboard.download --start 2020-03 --end 2026-03

Mart 2020'den bugüne kadar yayımlanmış bütün çeyrekleri kontrol et:

    python -m src.tbb_dashboard.download

Program TBB sayfasındaki yıl kimliklerini, dönem raporunu ve rapordaki XLS bağlantılarını
kendisi keşfeder. Geçerli mevcut dosyaları tekrar indirmez. Her dosyanın SHA-256 özeti
data/raw/manifest.json dosyasına yazılır. Haziran 2026 yayımlandığında ikinci komut yeni
dönemi otomatik bulur.

## SQLite veri ambarını oluşturma

Gerekli paketi yükle:

    python3 -m pip install -r requirements.txt

İndirilen bütün dönemleri tek SQLite veritabanına aktar:

    python3 -m src.tbb_dashboard.ingest

Oluşan ana veri kaynağı:

    data/processed/tbb.db

Temel tablolar:

- observations: dönem + banka/grup + rapor + metrik seviyesinde uzun-format değerler
- load_audit: her dönemde yüklenen sayfa ve değer sayıları
- schema_audit: gerçek eksikleri, dönemsel sayfaları ve henüz yürürlüğe girmemiş
  tabloları ayırır

Yeni dönem indirildikten sonra aynı ingest komutunu yeniden çalıştırmak yeterlidir.
İlgili dönem yenilenir ve mükerrer gözlem oluşmaz.

## Dashboard'u çalıştırma

Dashboard paketlerini ilk seferde yükle:

    python3 -m pip install -r requirements.txt

Analiz ekranını başlat:

    python3 -m streamlit run src/tbb_dashboard/dashboard.py

Tarayıcıda açılan ekrandan rapor grubu, tablo, finansal metrik, kurum ve
iki ayrı karşılaştırma dönemi seçilebilir. Seçili bankalar çizgi veya sütun
grafiğinde karşılaştırılabilir; daire grafikle dönem dağılımı incelenebilir. Banka
seçiminde sayı sınırı yoktur ve tüm kurumlar tek seferde seçilebilir. Metrik
hesaplayıcı ile farklı iki metrik için oran, yüzde oranı veya fark hesaplanabilir.
Grafiklerin altındaki veri CSV olarak indirilebilir.
