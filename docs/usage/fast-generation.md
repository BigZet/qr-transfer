# Ускоренная подготовка и ранний старт на VDI

Обновите репозиторий и выполните в терминале JupyterHub из его корня:

```bash
python -m pip install ".[sender]"
python -m qr_transfer render artifacts/paged-vdi/prepared/object.7z --descriptor artifacts/paged-vdi/prepared/descriptor.json --output artifacts/paged-vdi/player-fast --transport repeat --visual rgb8 --slots 2 --interval-ms 600 --part-frames 32 --workers 0 --live --external-only
```

Команда использует архив из [теста 10 МиБ](paged-vdi-test.md). Для своего архива
замените пути object.7z и descriptor.json. Каталог player-fast должен быть новым.
Новых зависимостей по сравнению с обычным отправителем нет.

Если `/files/` показывает ошибку загрузки, используйте
[локальный сервер через Jupyter proxy](jupyter-player.md): отдельная вкладка
сама по себе не снимает изоляцию HTML в Jupyter.

- `--workers 0`: автоматический выбор, максимум четыре процесса. Учитываются
  CPU, affinity и квоты Linux в стандартных путях cgroup. Для нестандартных
  ограничений JupyterHub задайте `--workers 1`, `2` или `4` вручную.
- `--live`: index появляется после первой готовой порции. Откройте его отдельной
  вкладкой через `/files/`, **не закрывая терминал генерации**. Включите fullscreen.
- `--part-frames 32`: короткое ожидание первой порции. Для ещё более раннего
  старта можно 8, но будет больше запросов. Обычное значение по умолчанию — 256.
- Если показ догнал генерацию, прежний QR остаётся на экране до готовности
  следующего. Ориентир времени цикла учитывает наблюдаемые задержки; это не ACK
  приёмника. Ранний старт совмещает подготовку с передачей.
- `--generator segno` уже выбран по умолчанию: локально он быстрее qrcode.
  На четырёх процессах подготовка ускорилась примерно втрое, но результат VDI
  зависит от выделенных CPU. Размеры QR, выбор маски и коррекция не упрощались.

Без `--live` можно использовать тот же параллелизм и дождаться полной подготовки.
Live совместим только с `--external-only`; standalone требует всех матриц.
Для LT замените транспорт на `lt` и установите `".[sender,fec]"`. Repair-пакеты
также требуют генерации QR; лимиты архива и параметры LT не изменены.

## Приём и проверка

Новый render создаёт новый ID передачи: используйте новое состояние приёмника.
На Windows (монитор 1), затем нажмите Старт в браузере VDI:

```powershell
.\.venv-i00\Scripts\python.exe -m qr_transfer capture --transport repeat --visual rgb8 --monitor 1 --state artifacts/paged-vdi/receive-fast-1 --extract-to artifacts/paged-vdi/restored-fast-1
.\.venv-i00\Scripts\python.exe tools/paged_fixture.py verify artifacts/paged-vdi/restored-fast-1/test-10m.bin --mib 10
```

Последняя команда относится к синтетическому файлу 10 МиБ. Ожидается verified=true.
Проверьте, что index открылся до завершения render; сохраните prepare.json,
JSON плеера и отчёты приёмника. В prepare.json есть workers, first_part_seconds
и prepare_seconds. В плеере paging.generation_waiting отражает ожидание генератора.

При ошибке Python плеер показывает остановку генерации. Ожидание одной порции
ограничено десятью минутами; после восстановления доступен Старт. Если Python
завершился аварийно, генерация не возобновляется: создайте новый render и состояние.
Не удаляйте папку parts-* во время показа. refresh_player обновляет готовый HTML,
но ускорение подготовки проверяется новым render.

[Локальные измерения](../reports/qr-generation.md).
