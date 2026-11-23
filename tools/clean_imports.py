# -*- coding: utf-8 -*-
"""Чистка импортов в пакете chrono: неиспользуемые имена, дубли, длинные строки.

Использование:
    python tools/clean_imports.py --dry   # показать, что будет убрано/изменено
    python tools/clean_imports.py         # применить
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(BASE, 'chrono')
LIMIT = 100


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


def alias_text(alias):
    return alias.name + (f' as {alias.asname}' if alias.asname else '')


def format_import(module, level, aliases, indent=''):
    """Одна строка, если влезает в LIMIT, иначе — в скобках по несколько имён."""
    head = f"from {'.' * level}{module} import "
    names = [alias_text(a) for a in aliases]
    one_line = head + ', '.join(names)
    if len(indent + one_line) <= LIMIT:
        return indent + one_line + '\n'
    out = [f"{indent}{head}("]
    line = indent + '    '
    for name in names:
        piece = name + ', '
        if len(line) + len(piece.rstrip()) > LIMIT and line.strip():
            out.append(line.rstrip())
            line = indent + '    '
        line += piece
    out.append(line.rstrip())
    out.append(indent + ')')
    return '\n'.join(out) + '\n'


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
        if keep and isinstance(node, ast.ImportFrom):
            edit = format_import(node.module, node.level, keep)
        elif keep:
            edit = 'import ' + ', '.join(alias_text(a) for a in keep) + '\n'
        else:
            edit = ''
        edits.append((node.lineno, node.end_lineno, [edit] if edit else []))
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
        edits.append((nodes[0].lineno, nodes[0].end_lineno,
                      [format_import(module, level, aliases)]))
        for node in nodes[1:]:
            edits.append((node.lineno, node.end_lineno, []))
        print(f"{os.path.relpath(path, BASE)}: склеены импорты из {module}")
    if dry or not edits:
        return
    for start, end, new in sorted(edits, reverse=True):
        lines[start - 1:end] = new
    open(path, 'w', encoding='utf-8', newline='\n').write(''.join(lines))


def wrap_long_imports(path, dry):
    """Оборачивает одиночные импорты длиннее LIMIT в скобки."""
    src = open(path, encoding='utf-8').read()
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    edits = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if node.end_lineno != node.lineno:
            continue
        text = lines[node.lineno - 1].rstrip('\n')
        if len(text) <= LIMIT:
            continue
        if isinstance(node, ast.ImportFrom):
            wrapped = format_import(node.module, node.level, node.names)
        else:
            wrapped = 'import (\n' + ',\n'.join('    ' + alias_text(a) for a in node.names) + '\n)\n'
        edits.append((node.lineno, node.end_lineno, [wrapped]))
        print(f"{os.path.relpath(path, BASE)}: перенос строки {node.lineno} ({len(text)} симв.)")
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
        wrap_long_imports(path, dry)
    print('dry-run' if dry else 'готово')


if __name__ == '__main__':
    main()
