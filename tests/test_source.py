from pathlib import Path

from kg_collector.source import analyze_file, analyze_file_isolated, npm_package_name, resolve_file_imports


def test_analyze_python_symbols_and_calls(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "from client import send\n\nclass Service:\n    def run(self):\n        return send()\n",
        encoding="utf-8",
    )

    result = analyze_file(source, tmp_path, "python")

    assert result["path"] == "service.py"
    assert result["parse_has_error"] is False
    assert [item["name"] for item in result["classes"]] == ["Service"]
    assert result["functions"][0]["name"] == "run"
    assert result["functions"][0]["calls"] == ["send"]
    assert result["imports"] == ["from client import send"]


def test_analyze_typescript_symbols(tmp_path: Path) -> None:
    source = tmp_path / "client.ts"
    source.write_text(
        "import { request } from './http';\nexport function load() { return request(); }\n",
        encoding="utf-8",
    )

    result = analyze_file(source, tmp_path, "typescript")

    assert result["functions"][0]["name"] == "load"
    assert result["functions"][0]["calls"] == ["request"]
    assert result["imports"] == ["import { request } from './http';"]
    assert result["import_modules"] == ["./http"]


def test_resolve_tsx_relative_index_and_alias_imports(tmp_path: Path) -> None:
    (tmp_path / "src/components").mkdir(parents=True)
    (tmp_path / "src/hooks").mkdir()
    (tmp_path / "src/components/Search.tsx").write_text("", encoding="utf-8")
    (tmp_path / "src/components/Dialog.tsx").write_text("", encoding="utf-8")
    (tmp_path / "src/hooks/index.ts").write_text("", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text(
        '{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}}}',
        encoding="utf-8",
    )
    source_data = [
        {"path": "src/components/Search.tsx", "import_modules": ["./Dialog", "@/hooks", "react"]},
        {"path": "src/components/Dialog.tsx", "import_modules": []},
        {"path": "src/hooks/index.ts", "import_modules": []},
    ]

    resolve_file_imports(tmp_path, source_data)

    assert source_data[0]["resolved_imports"] == [
        "src/components/Dialog.tsx",
        "src/hooks/index.ts",
    ]
    assert source_data[0]["package_imports"] == ["react"]


def test_npm_package_name_handles_scopes_and_subpaths() -> None:
    assert npm_package_name("axios/lib/adapters") == "axios"
    assert npm_package_name("@scope/client/http") == "@scope/client"
    assert npm_package_name("./client") is None


def test_analyze_tsx_arrow_function_names(tmp_path: Path) -> None:
    source = tmp_path / "SearchSection.tsx"
    source.write_text(
        "const formatDate = () => 'date';\n"
        "const SearchSection: React.FC<SearchSectionProps> = () => formatDate();\n",
        encoding="utf-8",
    )

    result = analyze_file(source, tmp_path, "tsx")

    assert result["parse_has_error"] is False
    assert [item["name"] for item in result["functions"]] == ["formatDate", "SearchSection"]


def test_analyze_tsx_callback_remains_anonymous(tmp_path: Path) -> None:
    source = tmp_path / "helpers.tsx"
    source.write_text(
        "const dates = values.map((value) => formatDate(value));\n",
        encoding="utf-8",
    )

    result = analyze_file(source, tmp_path, "tsx")

    assert result["functions"][0]["name"] == "<anonymous>"


def test_analyze_tsx_function_definition_and_jsx_reference(tmp_path: Path) -> None:
    source = tmp_path / "SearchSection.tsx"
    source.write_text(
        "const SearchSection: React.FC<Props> = () => {\n"
        "  const handleViolationSearch = (value: string) => run(value);\n"
        "  return <ViolationModal onSearch={handleViolationSearch} />;\n"
        "};\n",
        encoding="utf-8",
    )

    result = analyze_file(source, tmp_path, "tsx")

    functions = {item["name"]: item for item in result["functions"]}
    assert "handleViolationSearch" in functions
    assert "handleViolationSearch" in functions["SearchSection"]["references"]
    assert "handleViolationSearch" not in functions["SearchSection"]["calls"]


def test_analyze_file_isolated(tmp_path: Path) -> None:
    source = tmp_path / "lib.rs"
    source.write_text("fn load() { request(); }\n", encoding="utf-8")

    result = analyze_file_isolated(source, tmp_path, "rust")

    assert result["parse_has_error"] is False
    assert result["functions"][0]["name"] == "load"
    assert result["functions"][0]["calls"] == ["request"]