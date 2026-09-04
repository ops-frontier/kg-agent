from __future__ import annotations

import json
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


MANIFEST_NAMES = {"package.json", "pyproject.toml", "pom.xml"}


def analyze_manifests(root: Path, repository_names: set[str]) -> list[dict[str, Any]]:
    manifests = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name not in MANIFEST_NAMES or ".git" in path.parts:
            continue
        try:
            dependencies = parse_manifest(path)
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
                }
            )
        except (OSError, ValueError, ET.ParseError, tomllib.TOMLDecodeError) as error:
            manifests.append({
                "path": path.relative_to(root).as_posix(),
                "type": path.name,
                "error": str(error),
                "dependencies": [],
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
    return _maven_dependencies(path)


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