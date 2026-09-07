from pathlib import Path

from kg_collector.source import analyze_file, analyze_file_isolated


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