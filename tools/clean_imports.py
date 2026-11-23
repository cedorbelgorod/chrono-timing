# -*- coding: utf-8 -*-
"""Чистка импортов в пакете chrono: убирает неиспользуемые имена и склеивает дубли.

Использование:
    python tools/clean_imports.py --dry   # показать, что будет убрано
    python tools/clean_imports.py         # применить
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(BASE, 'chrono')


def module_files():
    for root, _dirs, files in os.walk(PKG):
        if '__pycache__' in root:
            continue
        for name in sorted(files):
            if name.endswith('.py'):
                yield os.path.join(root, name)


def used_names(tree):
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Global):
            used.update(node.names)
    return used


def import_line(node, aliases):
    names = ', '.join(a.name + (f' as {a.asname}' if a.asname else '') for a in aliases)
    if isinstance(node, ast.ImportFrom):
        return f"from {'.' * node.level}{node.module or ''} import {names}\n"
    return 'import ' + names + '\n'


def prune(path, dry):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    used = used_names(tree)
    lines = src.splitlines(keepends=True)
    edits = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        keep, drop = [], []
        for alias in node.names:
            local = alias.asname or alias.name.split('.')[0]
            (keep if local in used else drop).append(alias)
        if not drop:
            continue
        edits.append((node.lineno, node.end_lineno, [import_line(node, keep)] if keep else []))
        print(f"{os.path.relpath(path, BASE)}: убрать {', '.join(a.name for a in drop)}")
    if dry or not edits:
        return
    for start, end, new in sorted(edits, reverse=True):
        lines[start - 1:end] = new
    open(path, 'w', encoding='utf-8', newline='\n').write(''.join(lines))


def merge_imports(path, dry):
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    groups = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            groups.setdefault((node.level, node.module), []).append(node)
    edits = []
    for (level, module), nodes in groups.items():
        if len(nodes) < 2:
            continue
        aliases, seen = [], set()
        for node in nodes:
            for a in node.names:
                local = a.asname or a.name
                if local in seen:
                    continue
                seen.add(local)
                aliases.append(a)
        first = nodes[0]
        edits.append((first.lineno, first.end_lineno,
                      [f"from {'.' * level}{module} import " +
                       ', '.join(a.name + (f' as {a.asname}' if a.asname else '') for a in aliases) + '\n']))
        for node in nodes[1:]:
            edits.append((node.lineno, node.end_lineno, []))
        print(f"{os.path.relpath(path, BASE)}: склеены импорты из {module}")
    if dry or not edits:
        return
    for start, end, new in sorted(edits, reverse=True):
        lines[start - 1:end] = new
    open(path, 'w', encoding='utf-8', newline='\n').write(''.join(lines))


def main():
    dry = '--dry' in sys.argv
    for path in module_files():
        prune(path, dry)
        merge_imports(path, dry)
    print('dry-run' if dry else 'готово')


if __name__ == '__main__':
    main()
