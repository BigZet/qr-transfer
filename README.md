# QR Transfer

Передача файлов и каталогов из JupyterHub на VDI на Windows-хост через экран.
Отправитель сжимает данные в парольный 7z-архив и показывает последовательность
QR; приёмник захватывает монитор, собирает архив, проверяет SHA-256 и распаковывает.
Прямое соединение между отправителем и приёмником не требуется.

**main** — рабочий пакет и инструкции. **[discover](https://github.com/BigZet/qr-transfer/tree/discover)** —
исследования, прототипы, измерения и история экспериментов.

## Возможности

- Файлы и каталоги, 7z/LZMA2/AES-256, скрытый ввод пароля.
- Моно, 4 и 8 цветов; один или два QR, настраиваемый интервал.
- Параллельная генерация и начало показа после первой порции.
- Ограниченный кэш браузера, контроль целостности порций, оценка времени цикла.
- Возобновление приёмника, проверка архива и безопасная распаковка.
- Python 3.10 (основной), 3.12; отправитель Linux/JupyterHub, приёмник Windows.

## Быстрый старт

Из корня репозитория, в терминале JupyterHub:

```bash
python -m pip install ".[sender]"
python -m qr_transfer pack /path/to/file-or-directory --output artifacts/send/prepared
python -m qr_transfer render artifacts/send/prepared/object.7z --descriptor artifacts/send/prepared/descriptor.json --output artifacts/send/player --transport repeat --visual rgb8 --slots 2 --interval-ms 300 --workers 0 --part-frames 32 --live --external-only
```

При pack задайте пароль. Каталоги вывода должны быть новыми. После появления
index.html можно начинать показ, оставив генерацию работающей. Во втором терминале:

```bash
python tools/serve_player.py artifacts/send/player --port 8765
```

Откройте выведенный путь `/user/.../proxy/8765/index.html` на том же домене
JupyterHub. Требуется включённое расширение jupyter-server-proxy. Подробнее:
[браузер и JupyterHub](docs/usage/jupyter.md).

На Windows из корня репозитория:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[receiver]"
.\.venv\Scripts\python.exe -m qr_transfer monitors
.\.venv\Scripts\python.exe -m qr_transfer capture --transport repeat --visual rgb8 --monitor 1 --state artifacts/receive/session-1 --extract-to artifacts/receive/restored-1
```

Выберите свой монитор, запустите приёмник, затем нажмите **Старт** в плеере.
Включите полный экран; верхний отступ оставьте под панель VDI. После приёма
введите пароль архива. Для нечёткого/искажённого цвета переключите обе стороны
на mono и увеличьте интервал до 600 мс.

## Статус и ограничения

Проверен один полный VDI-прогон: RGB8, два QR, repeat, 300 мс, захват 4K,
10 МиБ восстановлены точно за 10 мин 49 с; понадобилось два цикла.
Это результат конкретного окружения, не гарантированная скорость.
[Исходный отчёт](https://github.com/BigZet/qr-transfer/blob/discover/docs/reports/vdi-paged.md).

- Repeat: архив до 64 МиБ; LT: до 16 МиБ и 256 блоков.
- Исходные/распакованные данные: до 256 МиБ и 10 000 записей.
- Standalone: до 8 МиБ матриц, несовместим с ранним стартом.
- LT доступен как экспериментальный транспорт. Падение приёмника в крупном
  VDI-прогоне пока не воспроизведено; для основного сценария используйте repeat.
- Full HD и сравнительная скорость цветов требуют дополнительных измерений.
- Оценка времени — до конца цикла; отправитель не получает подтверждений от хоста.

## Документация

- [Использование, продолжение и диагностика](docs/usage/transfer.md)
- [Открытие плеера в JupyterHub](docs/usage/jupyter.md)
- [Архитектура и форматы](PROJECT.md)
- [Разработка и проверка](CONTRIBUTING.md)
- [Исследовательский план](https://github.com/BigZet/qr-transfer/blob/discover/IMPLEMENTATION_PLAN.md)

Старые code.py/qr.py сохранены для совместимости и регрессионной проверки AQR1.
Для обычного использования запускайте `python -m qr_transfer`.
