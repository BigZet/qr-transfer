# И01: пакет, AQR2 и локальный стенд

[Спецификация](../specs/aqr2.md) · [План](../iterations/01-core-protocol.md)

И01 работает с подготовленным объектом и локальными потоками пакетов, без экрана.
Шифрование — И02, новый браузерный отправитель — И03. Для демонстрации используйте
синтетический файл И00: команда `prepare` пока **не создаёт парольный архив**.

## Установка

В Python 3.10 или 3.12 из корня репозитория:

```bash
python -m pip install .
python -m qr_transfer --help
```

Ядро не имеет runtime-зависимостей. Build использует setuptools из настроенного
источника pip. Для отдельного QR round-trip: `python -m pip install ".[qr]"`.
Также устанавливается команда `qr-transfer`. Парольного параметра CLI нет.

## Подготовка и приём

Создайте `artifacts/i01`, затем:

```bash
python -m qr_transfer prepare artifacts/i00/fixtures/binary.bin --output artifacts/i01/binary.aqs --cycles 5
python -m qr_transfer receive artifacts/i01/binary.aqs --state artifacts/i01/received
python -m qr_transfer inspect artifacts/i01/received
```

Результат — `received/object.bin`; успех транспорта: `state=object_verified`,
`content_verified=true`. Это не означает успешную расшифровку. Выходной поток и
новый каталог состояния не перезаписываются. Код возврата: 0 — проверенный объект,
2 — набор неполон/объект не проверен, 1 — ошибка команды/потока, 130 — отмена.
После Ctrl+C журнал сохраняется для продолжения.

```bash
python -m qr_transfer resume artifacts/i01/binary.aqs --state artifacts/i01/received
```

Для resume нужен поток с **тем же transfer ID**; повторная `prepare` создаёт новый.
Resume в И01 переигрывает журнал принятых пакетов, поэтому повторная проверка
линейна по размеру объекта. Восстановление после аварийного завершения процесса
ещё не реализовано; штатный Ctrl+C и закрытая неполная сессия поддерживаются.

## Потери без обратного канала

```bash
python -m qr_transfer simulate artifacts/i01/binary.aqs --output artifacts/i01/simulation --seed 42 --loss 0.15 --corrupt 0.1 --duplicate 0.2 --late 3 --burst-start 10 --burst-length 3
python -m qr_transfer simulate artifacts/i01/binary.aqs --output artifacts/i01/replay --schedule artifacts/i01/simulation/schedule.json
```

Стенд заранее формирует и сохраняет `schedule.json`, записывает получившийся
`delivered.aqs`, затем запускает приёмник. Порядок по умолчанию перемешан;
`--no-reorder` оставляет порядок. `late` и burst указаны в исходных номерах кадров
с нуля. Corrupt меняет бит в ID заголовка или payload без исправления CRC.
Для повторного запуска расписания используйте тот же исходный `.aqs`.
Вероятности относятся к кадрам, а не уникальным чанкам. При пяти циклах часть
потерь компенсируется повторами; доставка не гарантируется для произвольных потерь.
Стенд ограничен 32 МиБ входа/100 000 записей, поскольку материализует расписание.

## API и границы модулей

- `protocol`: строгие `encode/decode`, `Packet`, `TransferDescriptor`, `Limits`.
- `transfer`: `prepare_object`, ленивый `packets`, дисковый `Receiver`.
- `stream`: локальное обрамление и журналируемая `Session`.
- `interfaces`: `PreparedTransfer`, `VisualProfile`, `DecodeEvent`, `TransferState`.
  `PacketDecoder` возвращает байты пакетов; `FileTransport` для cimbar возвращает
  состояние/проверенный объект и не обязан использовать AQR2. `ContainerAdapter`
  принимает пароль через API; интерактивное получение будет в И02.
- `legacy`: явный AQR1, без автоматической распаковки и переноса filename в пути.
- `visual`: необязательный QR V40-L adapter с ленивыми импортами зависимостей.

## Проверка

```bash
python -m unittest discover -s tests -v
```

Без QR-зависимостей соответствующий visual-тест пропускается; остальные тесты
ядра работают. Локальные результаты Windows/Python 3.10 и 3.12 не подтверждают
наличие Linux wheels во внутреннем зеркале JupyterHub. Отступ/fullscreen на VDI
и оставшиеся измерения И00 продолжают проверяться отдельно.
