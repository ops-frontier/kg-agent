from pathlib import Path

from kg_collector.dependencies import analyze_manifests, parse_manifest, parse_npm_lockfile


def test_parse_scoped_package_json_and_match_repository(tmp_path: Path) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_text(
        '{"dependencies":{"@example-org/shared-lib":"^1.2.0"},"devDependencies":{"vitest":"latest"}}',
        encoding="utf-8",
    )

    result = analyze_manifests(tmp_path, {"shared-lib", "other-service"})

    assert result[0]["dependencies"][0]["repository_dependency"] == "shared-lib"
    assert result[0]["dependencies"][1]["scope"] == "devDependencies"


def test_parse_pyproject_pep621(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        '[project]\ndependencies=["requests>=2", "shared_service"]\n'
        '[project.optional-dependencies]\ndev=["pytest<10"]\n',
        encoding="utf-8",
    )

    result = parse_manifest(manifest)

    assert result == [
        {"name": "requests", "version": ">=2", "scope": "dependencies"},
        {"name": "shared_service", "version": "*", "scope": "dependencies"},
        {"name": "pytest", "version": "<10", "scope": "optional:dev"},
    ]


def test_parse_maven_dependencies(tmp_path: Path) -> None:
    manifest = tmp_path / "pom.xml"
    manifest.write_text(
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><dependencies><dependency>'
        '<groupId>com.example</groupId><artifactId>shared</artifactId><version>1.0</version>'
        '</dependency></dependencies></project>',
        encoding="utf-8",
    )

    assert parse_manifest(manifest) == [
        {"name": "com.example:shared", "version": "1.0", "scope": "compile"}
    ]


def test_parse_package_lock_dependencies(tmp_path: Path) -> None:
    lockfile = tmp_path / "package-lock.json"
    lockfile.write_text(
        '{"packages":{"":{"dependencies":{"express":"^4.18.0"}},'
        '"node_modules/express":{"version":"4.18.3","dependencies":{"body-parser":"^1.20.2"}},'
        '"node_modules/body-parser":{"version":"1.20.2"}}}',
        encoding="utf-8",
    )

    result = parse_npm_lockfile(lockfile)

    assert {tuple(package.values()) for package in result["packages"]} == {
        ("express", "4.18.3"), ("body-parser", "1.20.2"),
    }
    assert result["edges"] == [{
        "source_name": "express", "source_version": "4.18.3",
        "target_name": "body-parser", "target_version": "1.20.2",
    }]


def test_parse_legacy_package_lock_dependencies(tmp_path: Path) -> None:
    lockfile = tmp_path / "package-lock.json"
    lockfile.write_text(
        '{"lockfileVersion":1,"dependencies":{"express":{"version":"4.18.3",'
        '"dependencies":{"body-parser":{"version":"1.20.2"}}}}}',
        encoding="utf-8",
    )

    result = parse_npm_lockfile(lockfile)

    assert result["edges"] == [{
        "source_name": "express", "source_version": "4.18.3",
        "target_name": "body-parser", "target_version": "1.20.2",
    }]


def test_parse_yarn_lock_resolves_dependency_selector(tmp_path: Path) -> None:
    lockfile = tmp_path / "yarn.lock"
    lockfile.write_text(
        'express@^4.18.0:\n  version "4.18.3"\n  dependencies:\n    body-parser "^1.20.2"\n\n'
        'body-parser@^1.20.2:\n  version "1.20.2"\n',
        encoding="utf-8",
    )

    result = parse_npm_lockfile(lockfile)

    assert result["edges"][0]["target_version"] == "1.20.2"


def test_parse_pnpm_lock_dependencies(tmp_path: Path) -> None:
    lockfile = tmp_path / "pnpm-lock.yaml"
    lockfile.write_text(
        "lockfileVersion: '9.0'\nsnapshots:\n  express@4.18.3:\n"
        "    dependencies:\n      body-parser: 1.20.2\n  body-parser@1.20.2: {}\n",
        encoding="utf-8",
    )

    result = parse_npm_lockfile(lockfile)

    assert result["edges"] == [{
        "source_name": "express", "source_version": "4.18.3",
        "target_name": "body-parser", "target_version": "1.20.2",
    }]