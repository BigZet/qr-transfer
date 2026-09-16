# Повторная проверка падения LT

Исследован receive-fast-3 после VDI-теста 2026-09-16. На копии SQLite повторно
восстановились 80/119 блоков. Недостающие пакеты поданы из локального архива
успешного repeat-прогона с тем же SHA-256: восстановлены 119/119 блоков,
хеш итогового архива совпал. Исходное состояние VDI не изменялось.
Это проверка журнала и LT без экрана; причина исходного падения ещё неизвестна.

## Что изменилось в приёмнике

capture.json сохраняет error.stage и error.chain: типы исключений, места вызовов
(имя файла, функция, строка), errno/winerror при наличии. Причина ошибки фонового
потока также сохраняется. Содержимое пакетов, тексты исключений и локальные
переменные в диагностику не включаются. Терминал печатает этап и типы ошибок.
Для успешного приёма error=null. Форматы передачи и состояния не изменились.

## Возобновление того же LT-плеера

Обновите репозиторий на Windows. Исходный LT-плеер должен иметь transfer ID
047d4a278f0073bebe0438ad27c32e59. Новый render создаёт другой ID и требует нового
состояния; repeat-плеер вместо LT для этой команды не подходит.

Сохраните старую попытку и работайте на копии (каталог копии должен отсутствовать):

```powershell
Copy-Item -LiteralPath artifacts/paged-vdi/receive-fast-3 -Destination artifacts/paged-vdi/receive-lt-recheck -Recurse
.\.venv-i00\Scripts\python.exe -m qr_transfer capture --transport lt --visual rgb8 --monitor 1 --state artifacts/paged-vdi/receive-lt-recheck --resume --extract-to artifacts/paged-vdi/restored-lt-recheck
```

Откройте прежний LT-плеер через /proxy/ и запустите показ. Для сопоставимого
повтора сохраните прежние настройки. При успехе введите пароль и проверьте файл:

```powershell
.\.venv-i00\Scripts\python.exe tools/paged_fixture.py verify artifacts/paged-vdi/restored-lt-recheck/test-10-2m.bin --mib 10
```

Если ошибка повторится, сохраните capture.json и терминальный вывод до следующего
resume. Не запускайте одновременно два приёмника с одним каталогом состояния.

## Локальное воспроизведение проверки журнала

Приёмник должен быть остановлен. Команда создаёт отдельное состояние из SQLite
backup, проверяет соответствие архива и завершает LT без генерации QR:

```powershell
.\.venv-i00\Scripts\python.exe tools/check_lt_resume.py artifacts/paged-vdi/receive-fast-3 artifacts/paged-vdi/receive-fast-4/object.bin artifacts/lt-investigation/new-check
```

[Сохранённый результат](../../benchmarks/vdi-paged/lt-resume.json).
