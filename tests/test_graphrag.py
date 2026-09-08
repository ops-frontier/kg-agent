import pytest

from kg_agent.graphrag import GitHubFileClient, GraphRAGAgent


class FakeNeo4jSession:
    def __init__(self, records):
        self.records = records
        self.questions = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def run(self, query, question, result_limit):
        self.questions.append(question)
        assert result_limit > 0
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
    def __init__(self, plans=None):
        self.url = None
        self.body = None
        self.plans = iter(plans or [])

    def post(self, url, json, timeout):
        self.url = url
        self.body = json
        assert timeout == 90
        if json["generationConfig"].get("responseMimeType") == "application/json":
            try:
                plan = next(self.plans)
            except StopIteration:
                plan = {"queries": []}
            return PlanResponse(plan)
        return FakeResponse()


class PlanResponse(FakeResponse):
    def __init__(self, plan):
        self.plan = plan

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": __import__("json").dumps(self.plan)}]}}]}


class ChunkResponse(FakeResponse):
    def __init__(self, text, finish_reason):
        self.text = text
        self.finish_reason = finish_reason

    def json(self):
        return {
            "candidates": [{
                "content": {"parts": [{"text": self.text}]},
                "finishReason": self.finish_reason,
            }],
        }


class ChunkedSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def post(self, url, json, timeout):
        self.requests.append(json)
        return next(self.responses)


class FakeGitHubClient:
    def __init__(self, contents, search_results=None):
        self.contents = contents
        self.requests = []
        self.search_results = search_results or {}
        self.searches = []

    def fetch(self, repository, path):
        self.requests.append((repository, path))
        return self.contents[(repository, path)]

    def search(self, query, limit):
        self.searches.append((query, limit))
        return self.search_results.get(query, [])[:limit]


class GitHubResponse:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        return self.content[:limit]


class GitHubSearchResponse(GitHubResponse):
    def read(self):
        return self.content


def make_agent(records=None, *, files=None, impacts=None, plans=None, max_iterations=5, max_results=100, github_client=None, max_github_files=10):
    if records is not None:
        impacts = records
    return GraphRAGAgent(
        driver=FakeDriver({"files": files or [], "impacts": impacts or []}),
        database="neo4j",
        session=FakeAuthorizedSession(plans),
        project="sample-project",
        location="asia-northeast1",
        model="gemini-2.5-flash",
        max_iterations=max_iterations,
        max_results=max_results,
        github_client=github_client,
        max_github_files=max_github_files,
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


def test_answer_uses_chat_history_as_search_and_answer_context() -> None:
    agent = make_agent()

    agent.answer(
        "その呼び出し元は？",
        history=[
            {"role": "user", "text": "delivery-api の submit_order を調べて"},
            {"role": "agent", "text": "submit_order は orders.py にあります。"},
        ],
    )

    contextual_question = agent.driver.neo4j_session.questions[0]
    assert "delivery-api の submit_order" in contextual_question
    assert "その呼び出し元は？" in contextual_question
    prompt = agent.session.body["contents"][0]["parts"][0]["text"]
    assert "submit_order は orders.py にあります。" in prompt


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


def test_answer_repeats_until_planner_has_no_more_queries() -> None:
    progress = []
    agent = make_agent(
        impacts=[{
            "target_id": "repo-a:target",
            "target_name": "target",
            "target_repository": "owner/repo-a",
            "caller_id": None,
            "depth": None,
        }],
        plans=[{"queries": ["related helper"]}, {"queries": []}],
        max_iterations=4,
        max_results=10,
    )

    agent.answer("target の影響範囲は？", progress.append)

    assert agent.driver.neo4j_session.questions == [
        "target の影響範囲は？",
        "target の影響範囲は？",
        "related helper",
        "related helper",
    ]
    assert [item["stage"] for item in progress] == [
        "search", "planning", "search", "planning", "answering",
    ]
    assert progress[-1]["iterations"] == 2


def test_answer_fetches_graph_files_from_github_for_further_planning() -> None:
    github = FakeGitHubClient({
        ("owner/repo", "src/SearchSection.tsx"): "const handleViolationSearch = () => run();",
    })
    progress = []
    agent = make_agent(
        files=[{
            "result_type": "file",
            "file_id": "owner/repo:src/SearchSection.tsx",
            "file_name": "SearchSection.tsx",
            "file": "src/SearchSection.tsx",
            "repository": "owner/repo",
            "definitions": [],
        }],
        plans=[{"queries": []}],
        github_client=github,
    )

    agent.answer("SearchSection.tsx を調べて", progress.append)

    assert github.requests == [("owner/repo", "src/SearchSection.tsx")]
    assert "handleViolationSearch" in agent.session.body["contents"][0]["parts"][0]["text"]
    assert [item["stage"] for item in progress] == [
        "search", "github", "github_complete", "planning", "answering",
    ]


def test_answer_uses_github_code_search_when_planner_requests_it() -> None:
    query = '"FEATURE_FLAG" repo:owner/repo'
    github = FakeGitHubClient(
        {("owner/repo", "src/flags.py"): 'FEATURE_FLAG = "new-checkout"'},
        {query: [("owner/repo", "src/flags.py")]},
    )
    progress = []
    agent = make_agent(
        plans=[
            {"queries": [], "github_queries": [query]},
            {"queries": [], "github_queries": []},
        ],
        github_client=github,
    )

    result = agent.answer("FEATURE_FLAG が使われている場所を調べて", progress.append)

    assert github.searches == [(query, 10)]
    assert github.requests == [("owner/repo", "src/flags.py")]
    assert result["sources"] == [{
        "repository": "owner/repo",
        "file": "src/flags.py",
        "function": "",
        "line": None,
        "depth": 0,
    }]
    assert 'FEATURE_FLAG = \\"new-checkout\\"' in agent.session.body["contents"][0]["parts"][0]["text"]
    assert [item["stage"] for item in progress] == [
        "search", "planning", "search", "github", "github_complete", "planning", "answering",
    ]


def test_github_file_client_uses_pat_and_encodes_path(monkeypatch) -> None:
    captured = {}

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return GitHubResponse(b"export const value = 1")

    monkeypatch.setattr("kg_agent.graphrag.urllib.request.urlopen", urlopen)

    content = GitHubFileClient("secret-token", 100).fetch(
        "owner/repo",
        "src/path with spaces/file.tsx",
    )

    assert content == "export const value = 1"
    assert captured["request"].get_header("Authorization") == "Bearer secret-token"
    assert captured["request"].full_url.endswith("src/path%20with%20spaces/file.tsx")
    assert captured["timeout"] == 30


def test_github_file_client_rejects_oversized_files(monkeypatch) -> None:
    monkeypatch.setattr(
        "kg_agent.graphrag.urllib.request.urlopen",
        lambda request, timeout: GitHubResponse(b"123456"),
    )

    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        GitHubFileClient("secret-token", 5).fetch("owner/repo", "large.tsx")


def test_github_file_client_searches_code_with_pat(monkeypatch) -> None:
    captured = {}

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return GitHubSearchResponse(
            b'{"items":[{"path":"src/search.py","repository":{"full_name":"owner/repo"}}]}'
        )

    monkeypatch.setattr("kg_agent.graphrag.urllib.request.urlopen", urlopen)

    results = GitHubFileClient("secret-token", 100).search('"error code" language:python', 5)

    assert results == [("owner/repo", "src/search.py")]
    assert captured["request"].get_header("Authorization") == "Bearer secret-token"
    assert captured["request"].get_header("Accept") == "application/vnd.github+json"
    assert captured["request"].full_url.endswith(
        "search/code?q=%22error+code%22+language%3Apython&per_page=5"
    )
    assert captured["timeout"] == 30


def test_generate_continues_after_max_tokens() -> None:
    session = ChunkedSession([
        ChunkResponse("回答の前半。", "MAX_TOKENS"),
        ChunkResponse("回答の後半。", "STOP"),
    ])
    agent = GraphRAGAgent(
        driver=FakeDriver({}),
        database="neo4j",
        session=session,
        project="sample-project",
        location="global",
        model="gemini-2.5-flash",
        max_output_tokens=8192,
        max_output_chunks=3,
    )

    assert agent._generate("質問") == "回答の前半。回答の後半。"
    assert len(session.requests) == 2
    assert session.requests[0]["generationConfig"]["maxOutputTokens"] == 8192
    assert session.requests[1]["contents"][-2] == {
        "role": "model",
        "parts": [{"text": "回答の前半。"}],
    }