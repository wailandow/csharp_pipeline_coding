"""
visualize_graphs.py

Универсальный скрипт: находит в проекте все скомпилированные LangGraph-графы
и рисует их через pyvis в HTML-файлы (по одному на граф + index.html).

Поиск графов: в корне проекта рекурсивно ищутся файлы graph*.py, каждый
импортируется, и в модуле берутся ВСЕ объекты-графы (по типу, а не по имени
переменной). Так подхватываются и projects/<имя>/graph.py, и графы других
проектов с похожей структурой.

Номера шагов (step0, step1, step3b ...) берутся из имени модуля/функции узла
(например, step0_prompt_builder.py) или, если там нет, из id узла.

Запуск:
    python visualize_graphs.py                          # проект = папка скрипта
    python visualize_graphs.py --root D:\\other_project  # другой проект
    python visualize_graphs.py --out docs/graphs --open
    python visualize_graphs.py --exclude examples --exclude tests   # по умолчанию

Импорт graph.py выполняет его побочные эффекты (config.yaml, .env, логирование),
поэтому для каждого проекта они должны быть настроены. Проекты с общими именами
пакетов (например, `core`) запускайте отдельными вызовами с разными --root.
"""
from __future__ import annotations

import argparse
import html
import importlib
import os
import re
import sys
import webbrowser
from pathlib import Path

from langgraph.pregel import Pregel
from pyvis.network import Network

START, END = "__start__", "__end__"

COLOR_START = "#2e7d32"
COLOR_END = "#c62828"
COLOR_NODE = "#1565c0"

DEFAULT_EXCLUDE = ["examples", "tests", "test", "venv", ".venv", "env",
                   "node_modules", "site-packages", "__pycache__", "build", "dist"]

STEP_RE = re.compile(r"(?<![a-z0-9])step[_-]?(\d+[a-z]?)(?![a-z0-9]*\d)", re.IGNORECASE)

VIS_OPTIONS = """
{
  "layout": {"hierarchical": {"enabled": true, "direction": "UD",
             "sortMethod": "directed", "levelSeparation": 110,
             "nodeSpacing": 200}},
  "physics": {"enabled": false},
  "edges": {"smooth": {"type": "cubicBezier", "roundness": 0.5},
            "font": {"size": 13, "align": "middle", "background": "#ffffff"}},
  "nodes": {"font": {"color": "#ffffff", "size": 15}},
  "interaction": {"hover": true, "navigationButtons": true}
}
"""


# ---------- поиск графов ----------

def find_graph_files(root: Path, exclude: set[str]) -> list[Path]:
    files = []
    for path in root.rglob("graph*.py"):
        rel_parts = path.relative_to(root).parts[:-1]
        if any(p in exclude or p.startswith(".") for p in rel_parts):
            continue
        files.append(path)
    return sorted(files)


def module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    return ".".join(parts)


def graphs_in_module(module) -> list[tuple[str, Pregel]]:
    """Все скомпилированные графы, определённые/доступные в модуле."""
    found, seen = [], set()
    for attr, value in vars(module).items():
        if isinstance(value, Pregel) and id(value) not in seen:
            seen.add(id(value))
            found.append((attr, value))
    return found


# ---------- номера шагов ----------

def _node_callable(compiled: Pregel, node_id: str):
    try:
        bound = compiled.nodes[node_id].bound
    except Exception:
        return None
    return getattr(bound, "func", None) or getattr(bound, "afunc", None) or bound


def step_of(compiled: Pregel, node_id: str) -> tuple[str | None, str]:
    """(номер шага, откуда взят) — по модулю/имени функции узла, затем по id."""
    fn = _node_callable(compiled, node_id)
    candidates = []
    if fn is not None:
        candidates += [getattr(fn, "__module__", "") or "", getattr(fn, "__qualname__", "") or ""]
    candidates.append(node_id)
    for text in candidates:
        m = STEP_RE.search(text.split(".")[-1] if text is not node_id and "." in text else text)
        if not m and "." in text:
            for part in text.split("."):
                m = STEP_RE.search(part)
                if m:
                    break
        if m:
            return m.group(1).lower(), text
    return None, ""


# ---------- отрисовка ----------

def build_network(compiled: Pregel, title: str) -> Network:
    drawable = compiled.get_graph()
    net = Network(height="95vh", width="100%", directed=True,
                  heading=title, cdn_resources="in_line")
    net.set_options(VIS_OPTIONS)

    for node_id in drawable.nodes:
        if node_id == START:
            net.add_node(node_id, label="START", shape="ellipse", color=COLOR_START)
        elif node_id == END:
            net.add_node(node_id, label="END", shape="ellipse", color=COLOR_END)
        else:
            step, source = step_of(compiled, node_id)
            label = f"step{step}\n{node_id}" if step else node_id
            tip = f"{node_id}" + (f"\nшаг: step{step} (из {source})" if step else "")
            net.add_node(node_id, label=label, title=tip, shape="box", color=COLOR_NODE)

    for edge in drawable.edges:
        label = edge.data if isinstance(edge.data, str) else ""
        net.add_edge(edge.source, edge.target, label=label,
                     dashes=bool(edge.conditional),   # условные переходы — пунктиром
                     arrows="to", title=label or "переход")
    return net


def write_index(out_dir: Path, items: list[tuple[str, Path]]) -> Path:
    links = "\n".join(
        f'<li><a href="{html.escape(p.name)}">{html.escape(ref)}</a></li>'
        for ref, p in items
    )
    index = out_dir / "index.html"
    index.write_text(
        '<!doctype html><meta charset="utf-8"><title>Графы проекта</title>'
        '<body style="font-family:sans-serif;margin:2em">'
        f"<h2>Графы проекта</h2><p>Пунктир — условные переходы.</p><ul>{links}</ul></body>",
        encoding="utf-8",
    )
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Визуализация LangGraph-графов проекта в HTML (pyvis)")
    parser.add_argument("--root", default=str(Path(__file__).parent),
                        help="корень проекта (по умолчанию — папка скрипта)")
    parser.add_argument("--out", default=None, help="папка вывода (по умолчанию <root>/graphs_html)")
    parser.add_argument("--exclude", action="append", default=None,
                        help=f"имя папки, которую не сканировать; можно повторять "
                             f"(по умолчанию: {', '.join(DEFAULT_EXCLUDE[:3])}, ...)")
    parser.add_argument("--open", action="store_true", help="открыть index.html в браузере")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out_dir = Path(args.out).resolve() if args.out else root / "graphs_html"
    out_dir.mkdir(parents=True, exist_ok=True)
    exclude = set(DEFAULT_EXCLUDE) | set(args.exclude or [])

    # graph.py обычно читает config.yaml/шаблоны относительно проекта
    os.chdir(root)
    sys.path.insert(0, str(root))

    done: list[tuple[str, Path]] = []
    for path in find_graph_files(root, exclude):
        mod_name = module_name(root, path)
        try:
            module = importlib.import_module(mod_name)
            graphs = graphs_in_module(module)
        except Exception as exc:  # один сломанный проект не должен ронять остальные
            print(f"[SKIP] {mod_name}: {type(exc).__name__}: {exc}")
            continue
        if not graphs:
            print(f"[--]   {mod_name}: графов нет")
            continue
        for attr, compiled in graphs:
            ref = f"{mod_name}:{attr}"
            out_file = out_dir / f"{ref.replace(':', '__').replace('.', '_')}.html"
            net = build_network(compiled, title=ref)
            # write_html в pyvis пишет в системной кодировке — пишем сами в utf-8
            out_file.write_text(net.generate_html(notebook=False), encoding="utf-8")
            print(f"[OK]   {ref} -> {out_file}")
            done.append((ref, out_file))

    if not done:
        print("Графы не найдены.")
        return 1
    index = write_index(out_dir, done)
    print(f"Индекс: {index}")
    if args.open:
        webbrowser.open(index.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
