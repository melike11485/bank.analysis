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

Uygulama yerelde veya Streamlit Community Cloud'da ilk kez açıldığında
`data/processed/tbb.db` bulunmuyorsa, depodaki `data/raw` TBB XLS dosyalarından
veritabanını otomatik oluşturur. Üretilen SQLite dosyası büyük olduğu için GitHub'a
eklenmez.

Streamlit Community Cloud dağıtım ayarları:

- Repository: `melike11485/bank.analysis`
- Branch: `main`
- Main file path: `src/tbb_dashboard/dashboard.py`
- Python version: `3.12`

Tarayıcıda açılan ekran ilk olarak dönemsel analizi gösterir. Dönemsel analiz,
zaman analizi ve özelleştirilebilir metrikler birbirinden bağımsız ana
sekmelerdir. Dönemsel analizde seçili bankaların tek dönem grafiği ve ilk 10
banka sıralaması bulunur. Zaman analizi; dönem seyrini, başlangıç–bitiş
karşılaştırmasını, çeyreklik değişimi ve yıllık değişimi aynı filtrelerle
gösterir. Sol filtre paneli kullanılmaz;
her ana sekme kendi rapor, metrik, dönem, banka/kurum ve grafik ayarlarını içerir.
İşaret kutulu açılır filtre; arama, alfabetik veya değer bazlı sıralama, tümünü
seçme ve seçimden çıkarma olanağı verir. Karşılaştırma düzeyi yalnızca Bankalar
ve Banka Grupları seçeneklerinden oluşur. Her ana analiz kendi veri tablosu ve veri
kalitesi alt sekmelerini kullanır; eksik kayıtlar dönem ve banka/kurum bazında
listelenir ve CSV olarak indirilebilir.
Seçili bankalar çizgi veya sütun grafiğinde
karşılaştırılabilir, daire grafikle dönem dağılımı incelenebilir.
Banka seçiminde sayı sınırı yoktur ve tüm kurumlar tek seferde seçilebilir.
Özelleştirilebilir metrikler sekmesi A–H arasında sekiz metriği formülde
birleştirebilir. Toplama, çıkarma, çarpma, bölme/oran ve yüzde oran işlemleri ile
`A+B/C` veya `(A+B)/(C-D)` gibi parantezli formüller desteklenir.
Grafiklerin altındaki veri CSV olarak indirilebilir.
