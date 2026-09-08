from __future__ import annotations

import json
import posixpath
from pathlib import Path
import re
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
SOURCE_SCHEMA_VERSION = 3
MODULE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


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
        "schema_version": SOURCE_SCHEMA_VERSION,
        "path": path.relative_to(root).as_posix(),
        "language": language,
        "parse_has_error": True,
        "parse_error": f"Tree-sitter worker exited with status {process.returncode}",
        "classes": [],
        "functions": [],
        "variables": [],
        "imports": [],
        "import_modules": [],
    }


def _analyze_file(path: Path, root: Path, language: str) -> dict[str, Any]:
    source = path.read_bytes()
    tree = get_parser(language).parse(source)
    functions = []
    classes = []
    imports = []
    import_modules = []
    variables = []

    for node in walk(tree.root_node):
        if node.type in FUNCTION_TYPES:
            functions.append(symbol(node, source, include_calls=True))
        elif node.type in CLASS_TYPES:
            classes.append(symbol(node, source))
        elif node.type in IMPORT_TYPES:
            imports.append(node_text(node, source, 500))
            source_node = node.child_by_field_name("source")
            if language in {"javascript", "typescript", "tsx"} and source_node is not None:
                import_modules.append(node_text(source_node, source, 500).strip("'\""))
        elif node.type in VARIABLE_TYPES:
            name_node = node.child_by_field_name("name") or node.child_by_field_name("left")
            if name_node is not None:
                variables.append({"name": node_text(name_node, source, 200), "line": node.start_point.row + 1})

    return {
        "schema_version": SOURCE_SCHEMA_VERSION,
        "path": path.relative_to(root).as_posix(),
        "language": language,
        "parse_has_error": tree.root_node.has_error,
        "classes": classes,
        "functions": functions,
        "variables": variables,
        "imports": imports,
        "import_modules": import_modules,
    }


def resolve_file_imports(root: Path, source_data: list[dict[str, Any]]) -> None:
    known_paths = {item["path"] for item in source_data}
    configs = load_module_configs(root)
    for item in source_data:
        resolved = []
        for module in item.get("import_modules", []):
            target = resolve_module_path(item["path"], module, known_paths, configs)
            if target is not None and target not in resolved:
                resolved.append(target)
        item["resolved_imports"] = resolved


def resolve_module_path(
    source_path: str,
    module: str,
    known_paths: set[str],
    configs: list[tuple[str, str, dict[str, list[str]]]],
) -> str | None:
    source_directory = posixpath.dirname(source_path)
    bases = []
    if module.startswith("."):
        bases.append(posixpath.normpath(posixpath.join(source_directory, module)))
    else:
        for config_directory, base_url, paths in configs:
            if not path_is_within(source_path, config_directory):
                continue
            for pattern, targets in paths.items():
                wildcard = match_alias(pattern, module)
                if wildcard is None:
                    continue
                for target in targets:
                    mapped = target.replace("*", wildcard)
                    bases.append(posixpath.normpath(posixpath.join(config_directory, base_url, mapped)))
        if module.startswith("@/"):
            source_parts = source_path.split("/")
            if "src" in source_parts:
                src_index = len(source_parts) - 1 - source_parts[::-1].index("src")
                bases.append("/".join(source_parts[:src_index + 1] + [module[2:]]))
    for base in bases:
        for candidate in module_candidates(base):
            if candidate in known_paths:
                return candidate
    return None


def module_candidates(base: str) -> Iterator[str]:
    yield base
    if not posixpath.splitext(base)[1]:
        for extension in MODULE_EXTENSIONS:
            yield f"{base}{extension}"
        for extension in MODULE_EXTENSIONS:
            yield f"{base}/index{extension}"


def load_module_configs(root: Path) -> list[tuple[str, str, dict[str, list[str]]]]:
    configs = []
    for path in root.rglob("*"):
        if path.name not in {"tsconfig.json", "jsconfig.json"} or "node_modules" in path.parts:
            continue
        try:
            data = json.loads(strip_json_comments(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        options = data.get("compilerOptions") or {}
        paths = options.get("paths") or {}
        if not isinstance(paths, dict):
            continue
        relative_directory = path.parent.relative_to(root).as_posix()
        configs.append((
            "" if relative_directory == "." else relative_directory,
            str(options.get("baseUrl", ".")),
            {
                pattern: [target for target in targets if isinstance(target, str)]
                for pattern, targets in paths.items()
                if isinstance(pattern, str) and isinstance(targets, list)
            },
        ))
    return sorted(configs, key=lambda item: len(item[0]), reverse=True)


def strip_json_comments(value: str) -> str:
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    value = re.sub(r"(^|\s)//.*$", r"\1", value, flags=re.MULTILINE)
    return re.sub(r",\s*([}\]])", r"\1", value)


def match_alias(pattern: str, module: str) -> str | None:
    if "*" not in pattern:
        return "" if pattern == module else None
    prefix, suffix = pattern.split("*", 1)
    if module.startswith(prefix) and module.endswith(suffix):
        return module[len(prefix):len(module) - len(suffix) if suffix else None]
    return None


def path_is_within(path: str, directory: str) -> bool:
    return not directory or path == directory or path.startswith(f"{directory}/")


def walk(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def symbol(node: Node, source: bytes, include_calls: bool = False) -> dict[str, Any]:
    name_node = (
        function_name_node(node)
        if node.type == "arrow_function"
        else node.child_by_field_name("name")
    )
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
        result["references"] = sorted(
            {
                node_text(descendant, source, 200)
                for descendant in walk(node)
                if is_function_reference(descendant)
            }
        )
    return result


def function_name_node(node: Node) -> Node | None:
    current = node.parent
    while current is not None:
        if current.type == "variable_declarator":
            return current.child_by_field_name("name")
        if current.type in FUNCTION_TYPES or current.type in CALL_TYPES:
            return None
        current = current.parent
    return None


def is_function_reference(node: Node) -> bool:
    if node.type != "identifier" or node.parent is None:
        return False
    parent = node.parent
    for index, child in enumerate(parent.children):
        if child.id != node.id:
            continue
        field = parent.field_name_for_child(index)
        if field in {"name", "pattern"}:
            return False
        if parent.type in CALL_TYPES and field in {"function", "name"}:
            return False
        return True
    return False


def call_name(node: Node, source: bytes) -> str:
    target = node.child_by_field_name("function") or node.child_by_field_name("name")
    return node_text(target, source, 300) if target else ""


def node_text(node: Node | None, source: bytes, limit: int) -> str:
    if node is None:
        return ""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")[:limit]