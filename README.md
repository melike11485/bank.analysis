# TBB Finansal Veri Projesi

Ana geliştirme ortamı VS Code + Python sanal ortamı + GitHub'dır. Jupyter yalnızca
keşif ve tek seferlik analizlerde yardımcı araç olarak kullanılmalıdır.

## TBB'den otomatik indirme

Belirli dokuz dönemi indir:

    python -m src.tbb_dashboard.download --start 2024-03 --end 2026-03

Mart 2024'ten bugüne kadar yayımlanmış bütün çeyrekleri kontrol et:

    python -m src.tbb_dashboard.download --start 2024-03

Program TBB sayfasındaki yıl kimliklerini, dönem raporunu ve rapordaki XLS bağlantılarını
kendisi keşfeder. Geçerli mevcut dosyaları tekrar indirmez. Her dosyanın SHA-256 özeti
data/raw/manifest.json dosyasına yazılır. Haziran 2026 yayımlandığında ikinci komut yeni
dönemi otomatik bulur.
