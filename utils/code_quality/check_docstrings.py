"""Lists the functions whose docstring lacks a Sphinx ``:param name:`` line for a parameter, and the functions without a
return annotation.

Usage, from the repository root:
    python utils/code_quality/check_docstrings.py [path ...] [--strict]

Without a path it checks ``server/``. ``--strict`` exits with 1 when anything is listed, so the check can gate a pipeline
once the backlog is cleared.
"""

import argparse
import ast
import logging
import sys
from pathlib import Path
from typing import Iterator, List, Tuple

logger = logging.getLogger('check_docstrings')

# Parameters that need no description.
_SKIPPED = {'self', 'cls'}


def _functions(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every function and method defined in ``tree``, nested ones included.

    :param tree: a parsed module.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def check_file(path: Path) -> List[Tuple[int, str, str]]:
    """The problems of one file.

    :param path: a Python file.
    :return: ``(line, function name, problem)`` for each function with an undocumented parameter or no return type.
    """
    problems = []
    # Databricks notebooks start some lines with a %magic, which is not Python.
    source = '\n'.join('' if line.startswith('%') else line for line in path.read_text(encoding='utf-8').split('\n'))
    for func in _functions(ast.parse(source)):
        args = func.args
        names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs if a.arg not in _SKIPPED]
        names += [a.arg for a in (args.vararg, args.kwarg) if a]
        doc = ast.get_docstring(func) or ''
        missing = [n for n in names if f':param {n}:' not in doc]
        if missing:
            problems.append((func.lineno, func.name, 'no ":param" for ' + ', '.join(missing)))
        if func.returns is None and func.name != '__init__':
            problems.append((func.lineno, func.name, 'no return annotation'))
    return problems


def main() -> int:
    """Check the given paths, log each problem, and return the exit code.

    :return: 1 with ``--strict`` when a problem was found, else 0.
    """
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('paths', nargs='*', default=['server'])
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    total = 0
    for root in args.paths:
        files = [Path(root)] if Path(root).is_file() else sorted(Path(root).rglob('*.py'))
        for path in files:
            problems = check_file(path)
            total += len(problems)
            for line, name, problem in problems:
                logger.info('%s:%d %s: %s', path, line, name, problem)
    logger.info('%d problem(s)', total)
    return 1 if args.strict and total else 0


if __name__ == '__main__':
    sys.exit(main())
