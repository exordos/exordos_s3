# Мониторинг S3-инстансов

Ноды S3-инстанса отправляют метрики в платформенную VictoriaMetrics элемента
`observability`. Пока этот элемент не развёрнут, ничего не отправляется:
vmagent базового образа ждёт, пока начнёт резолвиться
`victoria-storage.local.genesis-core.tech`.

Метрикам RustFS нужен образ ноды на `exordos_base` 1.3.1 или новее: только там
vmagent принимает OTLP на `127.0.0.1:8430`. Нода на более старом образе тоже
получает новый `rustfs.env` и перезапускает RustFS, но метрики молча теряются;
они пойдут, когда инстанс переедет на версию с новым образом.

## Что собирается

| Источник | Путь | Метки, по которым виден инстанс |
|---|---|---|
| node_exporter базового образа | vmagent опрашивает раз в 15 с | `instance` — имя хоста ноды, `s3aas-dp-<uuid инстанса>-node-<суффикс>` |
| RustFS | OTLP-push в локальный vmagent раз в 30 с | `exordos_s3_instance`, `exordos_project` |

node_exporter покрывает саму ноду, включая диск данных, смонтированный в
`/var/lib/rustfs/data`. RustFS добавляет свой взгляд: ёмкость кластера, диски,
запросы, объекты и бакеты (`rustfs_cluster_*`, `rustfs_system_drive_*`,
`rustfs_node_disk_*` и другие).

Метки RustFS берёт из атрибутов, которые контрольная плоскость пишет в
`rustfs.env` (`OTEL_RESOURCE_ATTRIBUTES`). У рядов по нодам есть ещё
`server`/`drive` (эндпоинт диска в RustFS) и `network.local.address` (адрес
ноды), а RustFS добавляет `rustfs.cluster.id` (снова uuid инстанса) и
`collection_scope`: `local` — собственные диски ноды, `cluster` — её взгляд на
весь кластер. Каждая нода кластерного инстанса отдаёт общекластерные ряды
`rustfs_cluster_*`, поэтому брать надо один из них, а не сумму.

## Заполненность дисков

Запись останавливает самый полный диск: RustFS отказывает в записи, как только
хоть одному диску erasure-сета не хватает места под свой шард или в пуле
остаётся меньше 1%. Как считает `df`, без блоков, отведённых под root:

```promql
max by (exordos_s3_instance) (
  label_replace(
    100 * (
      node_filesystem_size_bytes{mountpoint="/var/lib/rustfs/data"}
      - node_filesystem_free_bytes{mountpoint="/var/lib/rustfs/data"}
    ) / (
      node_filesystem_size_bytes{mountpoint="/var/lib/rustfs/data"}
      - node_filesystem_free_bytes{mountpoint="/var/lib/rustfs/data"}
      + node_filesystem_avail_bytes{mountpoint="/var/lib/rustfs/data"}
    ),
    "exordos_s3_instance", "$1", "instance", "s3aas-dp-(.+)-node-.+"
  )
)
```

То же со стороны RustFS, по дискам:

```promql
max by (exordos_s3_instance) (
  100 * rustfs_system_drive_used_bytes / rustfs_system_drive_total_bytes
)
```

Место под объекты с учётом erasure coding, как кластер видит одна нода:

```promql
max by (exordos_s3_instance) (rustfs_cluster_capacity_free_bytes)
```

На диске данных лежит и `/var/log` (bind-mount той же ФС), так что логи ноды
тоже занимают его место.

## Дашборд

Элемент `s3_dashboard` кладёт дашборд **S3 instance** в папку **S3** общей
Grafana элемента observability, с выбором проекта и инстанса:

- ёмкость: заполненность диска по нодам, место под объекты, сырое и полезное место;
- здоровье: ноды и диски онлайн, офлайн и на лечении;
- сохранность данных: сколько ещё дисков можно потерять, объекты в очереди на
  восстановление, отказы кворума записи, недоступные ноды, ошибки между нодами,
  свободные inode;
- учёт usage: последний цикл сканера, когда usage бакетов сохранялся и сошёлся
  ли подсчёт, проваленные проверки квот. От него зависят квоты и счётчики
  объектов; после апгрейда с 1.0.0-beta.4 там `never`;
- бакеты: размер, число объектов и заполнение квоты каждого бакета;
- трафик и операции: запросы по статусу и S3-операциям, доля 5xx, средняя
  латентность, отданные байты;
- процесс: память RustFS против RAM ноды, перезапуски и OOM kill.

Элемент зависит от элемента `observability`, ставить после него.

## Замечания

- **Эндпоинт корневой намеренно.** RustFS (1.0.0, версия в образе)
  выключает stdout-экспортёр, только если задан `RUSTFS_OBS_ENDPOINT`; с одним
  `RUSTFS_OBS_METRIC_ENDPOINT` он печатает все метрики в stdout, а значит в
  журнал. Трейсы и логи выключены явно — vmagent принимает только метрики, а
  `RUSTFS_OBS_LOG_STDOUT_ENABLED` оставляет логи в журнале.
- **Латентность запросов — только средняя.** RustFS 1.0.0 раскладывает
  `rustfs_http_server_request_duration_seconds` по границам в миллисекундах, а
  пишет секунды, так что все запросы попадают в первый бакет и квантили ничего
  не значат; делить `_sum` на `_count`.
- **Число объектов и бакетов приходит рывками.** RustFS 1.0.0 публикует
  `rustfs_cluster_usage_*` и `rustfs_cluster_buckets_total`, только когда
  снимок использования сошёлся, поэтому запрашивать их через `last_over_time`.
- **Без элемента observability RustFS молчит.** Неудачные отправки
  отбрасываются без записи в лог, на готовность это не влияет.
- **Смена меток или эндпоинта перезапускает RustFS** на всех нодах разом, как
  любое изменение `rustfs.env`.
