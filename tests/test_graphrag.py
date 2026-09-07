from kg_agent.graphrag import GraphRAGAgent


class FakeNeo4jSession:
    def __init__(self, records):
        self.records = records
        self.questions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def run(self, query, question):
        self.questions.append(question)
        if "'file' AS result_type" in query:
            return self.records.get("files", [])
        assert "CALLS|REFERENCES*1..5" in query
        return self.records.get("impacts", [])


class FakeDriver:
    def __init__(self, records):
        self.neo4j_session = FakeNeo4jSession(records)

    def session(self, database):
        assert database == "neo4j"
        return self.neo4j_session


class FakeResponse:
    ok = True
    status_code = 200

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": "影響範囲です。"}]}}]}


class FakeAuthorizedSession:
    def __init__(self):
        self.url = None
        self.body = None

    def post(self, url, json, timeout):
        self.url = url
        self.body = json
        assert timeout == 90
        return FakeResponse()


def make_agent(records=None, *, files=None, impacts=None):
    if records is not None:
        impacts = records
    return GraphRAGAgent(
        driver=FakeDriver({"files": files or [], "impacts": impacts or []}),
        database="neo4j",
        session=FakeAuthorizedSession(),
        project="sample-project",
        location="asia-northeast1",
        model="gemini-2.5-flash",
    )


def test_answer_uses_neo4j_impact_paths_as_vertex_context() -> None:
    records = [{
        "target_id": "repo-a:target",
        "target_name": "target",
        "target_repository": "owner/repo-a",
        "target_file": "src/a.py",
        "target_line": 10,
        "caller_id": "repo-b:caller",
        "caller_name": "caller",
        "caller_repository": "owner/repo-b",
        "caller_file": "src/b.py",
        "caller_line": 20,
        "depth": 2,
    }]
    agent = make_agent(records)

    result = agent.answer("repo-a の target の影響範囲は？")

    assert result == {
        "message": "影響範囲です。",
        "sources": [{
            "repository": "owner/repo-b",
            "file": "src/b.py",
            "function": "caller",
            "line": 20,
            "depth": 2,
        }],
    }
    assert agent.driver.neo4j_session.questions == [
        "repo-a の target の影響範囲は？",
        "repo-a の target の影響範囲は？",
    ]
    assert "repo-b" in agent.session.body["contents"][0]["parts"][0]["text"]
    assert agent.session.url.startswith("https://asia-northeast1-aiplatform.googleapis.com/v1/")


def test_answer_keeps_target_context_when_no_callers_exist() -> None:
    records = [{
        "target_id": "repo-a:target",
        "target_name": "target",
        "target_repository": "owner/repo-a",
        "caller_id": None,
        "depth": None,
    }]

    result = make_agent(records).answer("target の影響範囲は？")

    assert result["message"] == "影響範囲です。"
    assert result["sources"] == []


def test_answer_includes_matching_file_as_vertex_context_and_source() -> None:
    agent = make_agent(files=[{
        "result_type": "file",
        "file_id": "owner/repo:src/SearchSection.tsx",
        "file_name": "SearchSection.tsx",
        "file": "src/SearchSection.tsx",
        "language": "tsx",
        "repository": "owner/repo",
        "definitions": [{"type": "Function", "name": "SearchSection", "line": 12}],
    }])

    result = agent.answer("SearchSection.tsxというファイルはありますか")

    assert result["sources"] == [{
        "repository": "owner/repo",
        "file": "src/SearchSection.tsx",
        "function": "",
        "line": None,
        "depth": 0,
    }]
    prompt = agent.session.body["contents"][0]["parts"][0]["text"]
    assert '"file_name": "SearchSection.tsx"' in prompt
    assert '"repository": "owner/repo"' in prompt