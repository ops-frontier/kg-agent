from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml


MANIFEST_NAMES = {
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "pyproject.toml", "pom.xml",
}


def analyze_manifests(root: Path, repository_names: set[str]) -> list[dict[str, Any]]:
    manifests = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name not in MANIFEST_NAMES or ".git" in path.parts:
            continue
        try:
            dependencies = parse_manifest(path)
            package_graph = parse_npm_lockfile(path) if path.name in {
                "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            } else {"packages": [], "edges": []}
            manifests.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": path.name,
                    "dependencies": [
                        {
                            **dependency,
                            "repository_dependency": dependency_repo(
                                dependency["name"], repository_names
                            ),
                        }
                        for dependency in dependencies
                    ],
                    **package_graph,
                }
            )
        except (OSError, ValueError, ET.ParseError, tomllib.TOMLDecodeError, yaml.YAMLError) as error:
            manifests.append({
                "path": path.relative_to(root).as_posix(),
                "type": path.name,
                "error": str(error),
                "dependencies": [],
                "packages": [],
                "edges": [],
            })
    return manifests


def parse_manifest(path: Path) -> list[dict[str, str]]:
    if path.name == "package.json":
        data = json.loads(path.read_text(encoding="utf-8"))
        result = []
        for scope in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            result.extend(
                {"name": name, "version": str(version), "scope": scope}
                for name, version in data.get(scope, {}).items()
            )
        return result
    if path.name == "pyproject.toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        result = _pep621_dependencies(data.get("project", {}))
        poetry = data.get("tool", {}).get("poetry", {})
        for scope, values in poetry.items():
            if scope == "dependencies" and isinstance(values, dict):
                result.extend(
                    {"name": name, "version": str(version), "scope": "dependencies"}
                    for name, version in values.items() if name.lower() != "python"
                )
        return result
    if path.name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}:
        return []
    return _maven_dependencies(path)


def parse_npm_lockfile(path: Path) -> dict[str, list[dict[str, str]]]:
    if path.name == "package-lock.json":
        return _parse_package_lock(path)
    if path.name == "pnpm-lock.yaml":
        return _parse_pnpm_lock(path)
    return _parse_yarn_lock(path)


def _parse_package_lock(path: Path) -> dict[str, list[dict[str, str]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = data.get("packages") or {}
    if not packages:
        nodes: list[dict[str, str]] = []
        edges: list[dict[str, str]] = []
        _walk_package_lock_dependencies(data.get("dependencies") or {}, nodes, edges)
        return {"packages": _unique_dicts(nodes), "edges": _unique_dicts(edges)}
    versions = {
        package_path: details.get("version")
        for package_path, details in packages.items()
        if package_path and isinstance(details, dict) and details.get("version")
    }
    nodes = []
    edges = []
    for package_path, version in versions.items():
        name = _node_modules_name(package_path)
        if not name:
            continue
        nodes.append({"name": name, "version": str(version)})
        details = packages[package_path]
        dependencies = {**(details.get("dependencies") or {}), **(details.get("optionalDependencies") or {})}
        for dependency_name in dependencies:
            target_version = _package_lock_dependency_version(package_path, dependency_name, versions)
            if target_version:
                edges.append(_package_edge(name, str(version), dependency_name, target_version))
    return {"packages": _unique_dicts(nodes), "edges": _unique_dicts(edges)}


def _walk_package_lock_dependencies(
    dependencies: dict[str, Any],
    nodes: list[dict[str, str]],
    edges: list[dict[str, str]],
    parent: tuple[str, str] | None = None,
) -> None:
    for name, details in dependencies.items():
        if not isinstance(details, dict) or not details.get("version"):
            continue
        version = str(details["version"])
        nodes.append({"name": name, "version": version})
        if parent:
            edges.append(_package_edge(parent[0], parent[1], name, version))
        nested = {**(details.get("dependencies") or {}), **(details.get("optionalDependencies") or {})}
        _walk_package_lock_dependencies(nested, nodes, edges, (name, version))


def _node_modules_name(package_path: str) -> str | None:
    marker = "node_modules/"
    if marker not in package_path:
        return None
    return package_path.rsplit(marker, 1)[-1]


def _package_lock_dependency_version(
    package_path: str,
    dependency_name: str,
    versions: dict[str, Any],
) -> str | None:
    directory = package_path
    while directory:
        candidate = f"{directory}/node_modules/{dependency_name}"
        if candidate in versions:
            return str(versions[candidate])
        directory = directory.rsplit("/node_modules/", 1)[0] if "/node_modules/" in directory else ""
    return str(versions.get(f"node_modules/{dependency_name}")) if versions.get(f"node_modules/{dependency_name}") else None


def _parse_pnpm_lock(path: Path) -> dict[str, list[dict[str, str]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    snapshots = data.get("snapshots") or data.get("packages") or {}
    nodes = []
    edges = []
    for key, details in snapshots.items():
        package = _pnpm_package(str(key), details)
        if not package:
            continue
        nodes.append(package)
        if not isinstance(details, dict):
            continue
        dependencies = {**(details.get("dependencies") or {}), **(details.get("optionalDependencies") or {})}
        for dependency_name, reference in dependencies.items():
            target_version = _pnpm_reference_version(reference)
            if target_version:
                edges.append(_package_edge(package["name"], package["version"], str(dependency_name), target_version))
    return {"packages": _unique_dicts(nodes), "edges": _unique_dicts(edges)}


def _pnpm_package(key: str, details: Any) -> dict[str, str] | None:
    value = key.lstrip("/")
    if value.startswith("@") and value.count("@") == 1 and "/" in value:
        name, version = value.rsplit("/", 1)
    elif "@" in value:
        name, version = value.rsplit("@", 1)
    elif "/" in value:
        name, version = value.rsplit("/", 1)
    else:
        return None
    version = version.split("(", 1)[0]
    if isinstance(details, dict):
        name = str(details.get("name") or name)
        version = str(details.get("version") or version)
    return {"name": name, "version": version} if name and version else None


def _pnpm_reference_version(reference: Any) -> str | None:
    if isinstance(reference, dict):
        reference = reference.get("version")
    if not isinstance(reference, str) or reference.startswith(("link:", "workspace:", "file:")):
        return None
    return reference.split("(", 1)[0].lstrip("/").rsplit("@", 1)[-1]


def _parse_yarn_lock(path: Path) -> dict[str, list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    current_selectors: list[tuple[str, str]] = []
    current_version: str | None = None
    dependencies: list[tuple[str, str]] = []

    def flush() -> None:
        if not current_selectors or not current_version:
            return
        records.append({
            "selectors": current_selectors.copy(),
            "version": current_version,
            "dependencies": dependencies.copy(),
        })

    in_dependencies = False
    for raw_line in path.read_text(encoding="utf-8").splitlines() + [""]:
        if raw_line and not raw_line.startswith((" ", "#")) and raw_line.endswith(":"):
            flush()
            selectors = [item.strip().strip('"\'') for item in raw_line[:-1].split(",")]
            current_selectors = [_yarn_selector(item) for item in selectors]
            current_version = None
            dependencies = []
            in_dependencies = False
        elif raw_line.startswith("  version "):
            current_version = raw_line.split(None, 1)[1].strip('"\'')
            in_dependencies = False
        elif raw_line.strip() in {"dependencies:", "optionalDependencies:"}:
            in_dependencies = True
        elif in_dependencies and raw_line.startswith("    "):
            match = re.match(r'^\s{4}("[^"]+"|\S+)\s+"?([^"\s]+)"?$', raw_line)
            if match:
                dependencies.append((match.group(1).strip('"'), match.group(2)))
        elif raw_line and not raw_line.startswith("    "):
            in_dependencies = False
    flush()
    versions_by_selector = {
        selector: record["version"]
        for record in records
        for selector in record["selectors"]
    }
    versions_by_name: dict[str, set[str]] = {}
    for (name, _), version in versions_by_selector.items():
        versions_by_name.setdefault(name, set()).add(version)
    nodes = [
        {"name": name, "version": record["version"]}
        for record in records
        for name in dict.fromkeys(selector[0] for selector in record["selectors"])
    ]
    edges = []
    for record in records:
        for source_name in dict.fromkeys(selector[0] for selector in record["selectors"]):
            for dependency_name, reference in record["dependencies"]:
                target_version = versions_by_selector.get((dependency_name, reference))
                candidates = versions_by_name.get(dependency_name, set())
                if target_version is None and len(candidates) == 1:
                    target_version = next(iter(candidates))
                if target_version:
                    edges.append(_package_edge(source_name, record["version"], dependency_name, target_version))
    return {"packages": _unique_dicts(nodes), "edges": _unique_dicts(edges)}


def _yarn_selector(selector: str) -> tuple[str, str]:
    if selector == "__metadata":
        return ("", "")
    if selector.startswith("@"):
        return tuple(selector.rsplit("@", 1))
    return tuple(selector.split("@", 1))


def _package_edge(source_name: str, source_version: str, target_name: str, target_version: str) -> dict[str, str]:
    return {
        "source_name": source_name,
        "source_version": source_version,
        "target_name": target_name,
        "target_version": target_version,
    }


def _unique_dicts(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return list({tuple(sorted(item.items())): item for item in items}.values())


def _pep621_dependencies(project: dict[str, Any]) -> list[dict[str, str]]:
    result = [_split_python_dependency(item, "dependencies") for item in project.get("dependencies", [])]
    for group, items in project.get("optional-dependencies", {}).items():
        result.extend(_split_python_dependency(item, f"optional:{group}") for item in items)
    return result


def _split_python_dependency(value: str, scope: str) -> dict[str, str]:
    separators = "<>=!~;[ "
    index = min((value.find(char) for char in separators if char in value), default=len(value))
    return {"name": value[:index], "version": value[index:].strip() or "*", "scope": scope}


def _maven_dependencies(path: Path) -> list[dict[str, str]]:
    root = ET.parse(path).getroot()
    namespace = root.tag.partition("}")[0] + "}" if "}" in root.tag else ""
    result = []
    for dependency in root.findall(f".//{namespace}dependencies/{namespace}dependency"):
        group = dependency.findtext(f"{namespace}groupId", "")
        artifact = dependency.findtext(f"{namespace}artifactId", "")
        result.append({
            "name": f"{group}:{artifact}" if group else artifact,
            "version": dependency.findtext(f"{namespace}version", "*"),
            "scope": dependency.findtext(f"{namespace}scope", "compile"),
        })
    return result


def dependency_repo(package_name: str, repository_names: set[str]) -> str | None:
    candidates = {package_name, package_name.rsplit("/", 1)[-1], package_name.rsplit(":", 1)[-1]}
    normalized = {candidate.lower().replace("_", "-") for candidate in candidates}
    for repository_name in repository_names:
        if repository_name.lower().replace("_", "-") in normalized:
            return repository_name
    return None