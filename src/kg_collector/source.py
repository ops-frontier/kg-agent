from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from threading import Lock
from typing import Any, Iterator

from tree_sitter import Node
from tree_sitter_language_pack import get_parser


LANGUAGES = {
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cs": "c_sharp",
    ".go": "go", ".java": "java", ".js": "javascript", ".jsx": "javascript",
    ".kt": "kotlin", ".kts": "kotlin", ".php": "php", ".py": "python",
    ".rb": "ruby", ".rs": "rust", ".swift": "swift", ".ts": "typescript",
    ".tsx": "tsx",
}
IGNORED_DIRECTORIES = {
    ".git", ".idea", ".mypy_cache", ".next", ".pytest_cache", ".tox", ".venv",
    ".vscode", "build", "coverage", "dist", "node_modules", "target", "vendor",
}
FUNCTION_TYPES = {
    "function_definition", "function_declaration", "method_definition",
    "method_declaration", "function_item", "arrow_function", "constructor_declaration",
}
CLASS_TYPES = {
    "class_definition", "class_declaration", "interface_declaration", "struct_item",
    "trait_item", "enum_declaration",
}
IMPORT_TYPES = {
    "import_statement", "import_declaration", "import_from_statement", "use_declaration",
    "using_directive", "require_expression",
}
VARIABLE_TYPES = {
    "variable_declarator", "const_declaration", "static_item", "assignment",
}
CALL_TYPES = {"call", "call_expression", "invocation_expression"}
PARSER_LOCK = Lock()


def iter_source_files(root: Path, max_file_bytes: int) -> Iterator[tuple[Path, str]]:
    for path in root.rglob("*"):
        if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        language = LANGUAGES.get(path.suffix.lower())
        if language and path.stat().st_size <= max_file_bytes:
            yield path, language


def analyze_file(path: Path, root: Path, language: str) -> dict[str, Any]:
    with PARSER_LOCK:
        return _analyze_file(path, root, language)


def analyze_file_isolated(path: Path, root: Path, language: str) -> dict[str, Any]:
    process = subprocess.run(
        [sys.executable, "-m", "kg_collector.parser_worker", str(path), str(root), language],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if process.returncode == 0:
        return json.loads(process.stdout)
    return {
        "schema_version": 1,
        "path": path.relative_to(root).as_posix(),
        "language": language,
        "parse_has_error": True,
        "parse_error": f"Tree-sitter worker exited with status {process.returncode}",
        "classes": [],
        "functions": [],
        "variables": [],
        "imports": [],
    }


def _analyze_file(path: Path, root: Path, language: str) -> dict[str, Any]:
    source = path.read_bytes()
    tree = get_parser(language).parse(source)
    functions = []
    classes = []
    imports = []
    variables = []

    for node in walk(tree.root_node):
        if node.type in FUNCTION_TYPES:
            functions.append(symbol(node, source, include_calls=True))
        elif node.type in CLASS_TYPES:
            classes.append(symbol(node, source))
        elif node.type in IMPORT_TYPES:
            imports.append(node_text(node, source, 500))
        elif node.type in VARIABLE_TYPES:
            name_node = node.child_by_field_name("name") or node.child_by_field_name("left")
            if name_node is not None:
                variables.append({"name": node_text(name_node, source, 200), "line": node.start_point.row + 1})

    return {
        "schema_version": 1,
        "path": path.relative_to(root).as_posix(),
        "language": language,
        "parse_has_error": tree.root_node.has_error,
        "classes": classes,
        "functions": functions,
        "variables": variables,
        "imports": imports,
    }


def walk(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def symbol(node: Node, source: bytes, include_calls: bool = False) -> dict[str, Any]:
    name_node = node.child_by_field_name("name")
    result: dict[str, Any] = {
        "name": node_text(name_node, source, 200) if name_node else "<anonymous>",
        "kind": node.type,
        "start_line": node.start_point.row + 1,
        "end_line": node.end_point.row + 1,
    }
    if include_calls:
        result["calls"] = sorted(
            {
                call_name(descendant, source)
                for descendant in walk(node)
                if descendant.type in CALL_TYPES
            }
            - {""}
        )
    return result


def call_name(node: Node, source: bytes) -> str:
    target = node.child_by_field_name("function") or node.child_by_field_name("name")
    return node_text(target, source, 300) if target else ""


def node_text(node: Node | None, source: bytes, limit: int) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")[:limit]