# Универсальная основа pipeline на LangGraph

Каркас для трёх разных проектов (новостной агрегатор, автоподбор
C#-стратегий, помощник по продажам), построенный вокруг одной идеи:

## Требования

- Python 3.10+
- [langgraph](https://github.com/langchain-ai/langgraph)
- [pydantic](https://docs.pydantic.dev/)
- `anthropic` — для LLM-узлов (`llm_processor`, `llm_node_factory`)
- `feedparser` — только для примера `news_aggregator_graph`

## Установка

```bash
git clone <ссылка-на-этот-репозиторий>
cd pipeline_framework
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # заполните реальными значениями при необходимости
```

## Окружение, логирование, конфигурация

Три сквозных слоя, общих для всех проектов на этом каркасе (не нужно
переизобретать в каждом новом projects/<name>/):

- **`core/config.py`** — загружает `config.yaml` (структура) и `.env`
  (секреты). В `config.yaml` секреты не хранятся как значения — только
  как плейсхолдер `${ИМЯ_ПЕРЕМЕННОЙ}`, который разворачивается из
  `.env`/окружения рекурсивно на любой глубине. Использование:
  `from core import load_config; cfg = load_config()`.
- **`core/logging_setup.py`** — единая настройка логирования (уровень +
  вывод в консоль и опционально в файл) из секции `logging` в
  `config.yaml`. Вызывается один раз при сборке графа:
  `from core import setup_logging; setup_logging(cfg["logging"])`.
  Дальше каждый модуль просто берёт `logging.getLogger("<имя>")`.
- **`core/llm_client.py`** — универсальный LLM-клиент с fallback по
  списку эндпойнтов (любой OpenAI-совместимый `/chat/completions`).
  Не содержит промтов/парсинга — это дело конкретного узла. Конфигурация
  (модель, `base_url`, ключ, цепочка fallback) берётся из `config.yaml`
  и передаётся в узел при сборке графа, а не хардкодится в коде узла.

### Провайдеры и fallback2

Профиль в `llm.<имя>` — упорядоченный список эндпойнтов; перебираются по
очереди, пока один не ответит. Третий элемент (`fallback2`) необязателен:
если его нет, поведение прежнее. Поле `provider` у каждого эндпойнта:

- `api` (по умолчанию) — OpenAI-совместимый API, нужны `base_url`, `model`,
  `api_key` (без ключа эндпойнт пропускается). Старый формат работает как раньше.
- `claude_cli` — `claude -p --output-format json --model <model> --tools ""`.
- `codex_cli` — `codex exec --skip-git-repo-check --ephemeral --sandbox read-only -o <tmpfile> --model <model> -`.

Для CLI: `api_key` не нужен, дополнительно `timeout` (сек, по умолчанию 600)
и `executable` (путь, если CLI нет в PATH). Промпт идёт через stdin, процесс
запускается с `shell=False`, `CREATE_NO_WINDOW`, вывод перехватывается,
по таймауту убивается всё дерево процессов (`taskkill /T /F`). CLI
используются только как генератор текста: без инструментов/записи файлов,
флаги отключения защиты не применяются. Модуль — `core/cli_runner.py`.
CLI должен быть авторизован под тем же пользователем Windows.

Тесты (subprocess замокан, реальные CLI не вызываются): `python -m pytest tests`.

Добавление нового проекта с LLM и логированием — это (1) новая секция в
`config.yaml` под своим ключом, (2) новые переменные в `.env`, (3)
`setup_logging(cfg["logging"])` и `LLMClient(cfg["llm"]["<профиль>"])`
при сборке графа. Ни `core/config.py`, ни `core/logging_setup.py`, ни
`core/llm_client.py` менять не нужно — см. `config.yaml` в корне,
там есть закомментированный пример секции под новый профиль.

**узел графа — это не код, а декларация.** Разработчик описывает узел
через `NodeSpec` (роль, что читает, что пишет, конфигурация), а дальше
либо (а) эта декларация резолвится в одну из 9 типовых фабрик из
каталога, либо (б) по ней LLM генерирует код нового узла. В обоих
случаях результат — обычная python-функция `def node(state) -> dict`,
которую понимает `langgraph.graph.StateGraph`.

## Структура

```
core/                базовое ядро для нескольких проектов
  state.py            общая схема состояния (BaseNodeState + расширения)
  node_kinds.py        каталог из 9 типовых узлов + NodeSpec/NodeKind
  registry.py           реестр: NodeSpec -> скомпилированная функция узла
  llm_node_factory.py    генерация НОВОГО вида узла через LLM (custom-узлы)
  graph_builder.py       сборка StateGraph из списка NodeSpec + EdgeSpec
examples/
  news_aggregator_graph.py       проект 1 — сборка из типовых узлов
  strategy_selector_graph.py     проект 2 — retry-цикл поверх тех же узлов
  sales_assistant_graph.py       проект 3 — human-in-loop + anti-spam
```

Все три графа в `examples/` реально собираются и прогоняются (проверено
`graph.invoke(...)`), но fetch/LLM/Telegram/compile — заглушки: цель
примеров — показать сборку, а не бизнес-логику конкретного проекта.

## Каталог типовых узлов

| Вид (`NodeKind`) | Что делает | Где встречается в ваших 3 проектах |
|---|---|---|
| `collector` | тянет сырые кандидаты из внешнего источника, нормализует в `Item` | RSS (новости), 2GIS/Telegram (продажи) |
| `queue_store` | sqlite-очередь: `enqueue` кладёт, `dequeue` достаёт pending | очередь ссылок, очередь контактов, история попыток |
| `queue_preview` | read-only сводка по очереди (`GROUP BY status`) | ШАГ 2 новостей — тот же узел подходит для любого чек-пойнта |
| `llm_processor` | вызов LLM по `$item`-шаблону + разбор JSON-ответа | классификация новости, генерация C#-класса, текст предложения |
| `router` / условие на ребре | маршрутизация по полю состояния | новость А/Б, компиляция ok/fail, метрики ok/fail |
| `external_action` | эффектный шаг вовне: пишет `action_status`/`action_result` | Telegram-отправка, запись .cs + компиляция, запуск расчёта |
| `human_approval` | пауза на согласование с человеком | ШАГ 4 продаж (в бою — через `interrupt()`, не polling) |
| `retry_controller` | счётчик попыток + `ok_check(state)` → повтор/выход | ШАГ 6-8 стратегий, но применим в любом проекте |
| `rate_limiter` (router) | anti-spam гейт перед отправкой | ШАГ 5 продаж |

Одинаковый вид узла в разных проектах — это **один и тот же python-код**,
разница только в конфигурации (`NodeSpec.config`): путь к БД, промпт,
конкретная функция-адаптер. Именно это даёт "типовую реализацию для
одинаковых узлов", о которой шла речь в задаче.

## Как добавить типовой узел в цепочку

Ничего не пишется с нуля — добавляется запись в список `specs` и `edges`:

```python
specs.append(NodeSpec(
    node_id="dedup_check",
    kind=NodeKind.LLM_PROCESSOR,
    description="Проверка на дубликат перед отправкой",
    config={"prompt_template": "Это дубликат уже отправленного? $item"},
))
edges.append(EdgeSpec(source="analyze", target="dedup_check"))
```

## Как добавить НОВЫЙ вид узла руками

1. Написать `def my_kind_factory(...) -> Callable[[dict], dict]` в
   `node_kinds.py`, по образцу существующих 9.
2. Добавить значение в `NodeKind`.
3. Зарегистрировать: `registry.register_kind(NodeKind.MY_KIND, ...)`.

## Как сгенерировать новый узел через LLM

Это основной механизм из требования "должна быть возможность чётко
описать вход и выход узла, его роль и получить результат, который можно
подключить в общую схему":

```python
from core.node_kinds import NodeSpec, NodeKind, FieldSpec
from core.llm_node_factory import generate_custom_node

spec = NodeSpec(
    node_id="dedup_by_embedding",
    kind=NodeKind.CUSTOM,
    description="Сравнить current_item с недавно отправленными по "
                 "смысловой близости и пометить дубликат",
    reads=[FieldSpec(name="current_item", type="Item"),
           FieldSpec(name="llm_result", type="dict", description="уже есть summary")],
    writes=[FieldSpec(name="llm_result", type="dict",
                       description="добавляет ключ is_duplicate: bool")],
)

generated = generate_custom_node(
    spec,
    state_fields=["current_item", "llm_result", "history"],
    examples="...",  # можно вставить исходники соседних узлов для стиля
)
registry.register_compiled(spec, generated.fn)
print(generated.source_code)  # для code review перед тем, как доверять узлу
```

Внутри `generate_custom_node` — тот же паттерн, что и в проекте
"Автоподбор стратегий": сгенерировать → проверить → если не прошло,
отправить ошибку обратно в промпт и повторить (до `max_attempts`).

## Известные упрощения (что доделать для продакшена)

- **Fan-out по элементам очереди.** В примерах `items_batch` собирается,
  но LLM-обработка идёт по одному `current_item`. В реальном графе для
  обработки каждого элемента очереди отдельным проходом стоит использовать
  `langgraph.types.Send` (map-узел), а не пытаться протащить список через
  один узел.
- **Human-in-the-loop.** `human_approval_node_factory` в примере опрашивает
  (`poll_fn`) синхронно; в бою это `interrupt()` + checkpointer
  (`langgraph.checkpoint`), чтобы граф реально приостанавливался между
  запусками процесса, а не крутился в цикле ожидания.
- **Выполнение LLM-сгенерированного кода.** `llm_node_factory.py` делает
  `exec()` в текущем процессе — для прототипа ок, для продакшена
  сгенерированный код нужно гнать в изолированной среде (sandboxed
  subprocess/контейнер) и через code review перед регистрацией в графе.
- **Конкурентный доступ к sqlite.** `queue_store` открывает соединение на
  каждый вызов без WAL/блокировок — нормально для одного воркера,
  для параллельных пайплайнов нужен `PRAGMA journal_mode=WAL` и/или
  переход на нормальную БД.
- Реальные клиенты (Anthropic API, Telegram Bot API, `dotnet build`,
  2GIS API) в примерах — заглушки; сама интеграция с ними, как и было
  оговорено в задаче, не входит в объём этого фреймворка.

## Запуск примеров

```bash
python -m examples.news_aggregator_graph      # или import graph и .invoke({...})
python -m examples.strategy_selector_graph
python -m examples.sales_assistant_graph
```

## Лицензия

Не указана — добавьте файл `LICENSE` с выбранной лицензией (например, MIT)
перед публикацией, если проект предполагается открытым.
