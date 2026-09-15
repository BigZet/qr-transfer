# qr-transfer

Передача файлов из JupyterHub, открытого в браузере на VDI, на хост через визуальные коды на экране. Целевая версия — Python 3.10; рассматриваются QR, цветные QR и cimbar, зависимости — из внутреннего зеркала PyPI.

[Документ проекта: архитектура, ограничения и план развития](PROJECT.md).

Поставка: скачать наш репозиторий с GitHub на VDI и перенести проект в JupyterHub. Ограничения на размер исходника отправителя нет; приоритет — надёжность и пропускная способность.

[Исследование аналогов: цветные коды, анимированные QR, FEC и готовые библиотеки](RESEARCH.md).

[Полный план реализации: итерации, зависимости и критерии готовности](IMPLEMENTATION_PLAN.md).

[Отдельный эксперимент на VDI: сохранность 2/4/8 цветов, геометрия и частота](docs/experiments/01-vdi-color-channel.md).

Текущий прототип: `code.py` — генерация QR и HTML; `qr.py` — приём с экрана.

## Итерация 00

Готовы диагностика среды, браузерная тестовая страница и контрольный стенд. [Инструкция запуска в JupyterHub и на Windows](docs/usage/iteration-00.md), [результаты локальных проверок](docs/reports/environment.md).

Первый шаг в терминале JupyterHub, без сторонних зависимостей:

```bash
python3.10 tools/i00.py probe --role jupyterhub --output artifacts/i00/jupyter-before.json
```

Браузерный маршрут JupyterHub подтверждён; оставшиеся измерения передачи через VDI сохраняются открытыми.

## Итерация 01

Реализованы устанавливаемый пакет `qr_transfer`, AQR2-repeat, сборка с проверкой SHA-256, ограниченный буфер до metadata, журнал и штатное возобновление, симулятор потерь и явный AQR1-адаптер. [Инструкция и локальный пример](docs/usage/iteration-01.md) · [Спецификация](docs/specs/aqr2.md).

```bash
python -m pip install .
python -m qr_transfer --help
```

30 тестов проходят на Python 3.10 и 3.12. И01 проверяет транспорт; парольный контейнер реализован в И02, новый браузерный отправитель — И03.

## Итерация 02

Реализованы 7z/LZMA2/AES-256 со скрытыми именами, скрытый ввод пароля, внутренний манифест, проверяемая распаковка и повтор после неверного пароля без новой передачи. [Инструкция](docs/usage/iteration-02.md) · [Результаты и профили сжатия](docs/reports/container.md) · [Спецификация](docs/specs/container.md).

```bash
python -m pip install ".[container]"
python -m qr_transfer pack artifacts/i00/fixtures/tree --output artifacts/i02/prepared
```

52/52 теста прошли на Windows/Python 3.10 и 3.12. Интеграционная проверка в реальном JupyterHub остаётся открытой.

## Итерация 03

Готов браузерный отправитель и приёмник одного QR: парольный архив → компактные матрицы → автономный HTML/Canvas → захват монитора → проверка → распаковка. Есть fullscreen с верхним отступом 48 CSS px под панель VDI, пауза, шаг и настройка скорости.

[Инструкция: JupyterHub и Windows, монитор 1](docs/usage/iteration-03.md) · [Локальные результаты](docs/reports/browser-mono.md) · [Формат и ограничения](docs/specs/mono-player.md).

```bash
python -m pip install ".[sender]"
python -m qr_transfer pack artifacts/i00/fixtures/tree --output artifacts/i03/prepared
python -m qr_transfer render artifacts/i03/prepared/object.7z --descriptor artifacts/i03/prepared/descriptor.json --output artifacts/i03/player
```

Откройте `standalone.html` отдельной вкладкой браузера VDI. Команды приёма и порядок совместной проверки И00–И03 приведены в инструкции. Локально прошли 3 × 1 МиБ в Full HD; после проверки пользователя также подтверждены три приёма и распаковки малого каталога на хосте (захват 4K, архив 67 020 байт). Критерий реального Full HD/1 МиБ остаётся открытым. Возобновление живого захвата и несколько QR — И04.
