# -*- coding: utf-8 -*-
"""Аудит: функции, ссылающиеся на имена, которых нет в модуле (пропущенные импорты).

Использование:
    python tools/audit_names.py           # печатает проблемы, если есть
Функция `find_missing_names(dir)` возвращает {модуль: [(функция, [имена])]} —
используется также тестами.
"""
import ast
import builtins
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(BASE, 'chrono')

BUILTINS = set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__package__', '__builtins__',
                                '__loader__', '__spec__', '__annotations__', 'TYPE_CHECKING'}


def _collect_module_names(tree):
    """Имена, доступные на уровне модуля (с учётом try/if на верхнем уровне)."""
    names = set()

    def visit_stmt(stmt):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for a in stmt.names:
                names.add(a.asname or a.name.split('.')[0])
        elif isinstance(stmt, (ast.If, ast.Try, ast.With)):
            for child in getattr(stmt, 'body', []):
                visit_stmt(child)
            for child in getattr(stmt, 'orelse', []):
                visit_stmt(child)
            for child in getattr(stmt, 'finalbody', []):
                visit_stmt(child)
            for handler in getattr(stmt, 'handlers', []):
                for child in handler.body:
                    visit_stmt(child)

    for stmt in tree.body:
        visit_stmt(stmt)
    return names


def _free(fn):
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    if fn.args.vararg:
        params.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        params.add(fn.args.kwarg.arg)
    assigned, loads = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Store):
                assigned.add(n.id)
            elif isinstance(n.ctx, ast.Load) and n.id not in BUILTINS:
                loads.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in n.args.args + n.args.kwonlyargs:
                assigned.add(a.arg)
            if n.args.vararg:
                assigned.add(n.args.vararg.arg)
            if n.args.kwarg:
                assigned.add(n.args.kwarg.arg)
            assigned.add(n.name)
        elif isinstance(n, ast.Lambda):
            for a in n.args.args + n.args.kwonlyargs:
                assigned.add(a.arg)
            if n.args.vararg:
                assigned.add(n.args.vararg.arg)
        elif isinstance(n, ast.comprehension):
            for x in ast.walk(n.target):
                if isinstance(x, ast.Name):
                    assigned.add(x.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            assigned.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                assigned.add(a.asname or a.name.split('.')[0])
    return loads - assigned - params


def find_missing_names(pkg=PKG):
    problems = {}
    for root, dirs, files in os.walk(pkg):
        if '__pycache__' in root:
            continue
        for f in sorted(files):
            if not f.endswith('.py'):
                continue
            path = os.path.join(root, f)
            src = open(path, encoding='utf-8').read()
            tree = ast.parse(src)
            mod = _collect_module_names(tree)
            hits = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    missing = sorted(_free(node) - mod)
                    if missing:
                        hits.append((node.name, missing))
            if hits:
                problems[os.path.relpath(path, BASE)] = hits
    return problems


def main():
    problems = find_missing_names()
    if not problems:
        print('проблем не найдено')
        return 0
    for module, hits in problems.items():
        print(module)
        for name, missing in hits:
            print(f'  {name}: {missing}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
