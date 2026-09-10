# AIエージェント

kg-agent は、Neo4j に保存されたコードの構造情報と GitHub 上のソースコードを検索し、Vertex AI で回答を生成する GraphRAG エージェントである。React 製の Web UI、Python 製の HTTP サーバ、GraphRAG の検索・回答生成処理から構成される。

## 処理フロー

1. Web UI が質問と直近の会話履歴を `POST /api/chat` へ送信する。
2. 質問にファイル名が含まれる場合、Neo4j から該当ファイル、定義シンボル、import 関係を検索する。
3. 質問に関数名が含まれる場合、Neo4j の `CALLS` と `REFERENCES` を最大5段たどり、呼び出し元と影響範囲を検索する。
4. 質問と検索結果からソース本文のキーワード検索が必要と判断した場合、GitHub REST API の Code Search を実行する。
5. Neo4j または Code Search の結果に含まれるファイルを GitHub Contents API から取得する。
6. Vertex AI が現在の結果を評価し、未調査の論点があれば追加の検索語を最大5件生成する。
7. 探索上限へ達するか追加検索が不要になるまで検索を繰り返す。
8. 会話履歴、Neo4j の検索結果、GitHub のソースコードを根拠として日本語の回答を生成する。

回答には、根拠として利用したリポジトリ、ファイル、関数、行番号、呼び出し距離が付与される。検索結果がない場合や静的解析だけでは断定できない場合は、その制約を回答に含める。

## チャット履歴

Web UI はチャット履歴をブラウザのローカルストレージの `kg-agent.chat-history` キーへ保存する。ページを再読み込みしても履歴は復元される。

後続の質問では、直近20件のユーザーとエージェントのメッセージを会話コンテキストとして使用する。各メッセージは最大8,000文字まで API へ送信される。履歴はサーバには永続化されない。「チャットをクリア」ボタンを押すと、画面上の履歴、会話コンテキスト、ローカルストレージのデータが削除される。

## HTTP API

### `POST /api/chat`

リクエストは JSON で送信する。

```json
{
	"message": "submit_order の変更影響を教えてください",
	"history": [
		{"role": "user", "text": "delivery-api を調べてください"},
		{"role": "agent", "text": "delivery-api の概要です。"}
	]
}
```

`message` は必須である。`history` は省略でき、`role` が `user` または `agent` の有効なメッセージだけが利用される。サーバでも直近20件、各8,000文字に制限する。リクエスト本文の上限は200,000バイトである。

レスポンスの Content-Type は `application/x-ndjson` で、処理状況と結果を1行1イベントで返す。

```json
{"type":"progress","stage":"search","iteration":1,"queries":["submit_order"]}
{"type":"result","message":"回答本文","sources":[]}
```

進捗の `stage` は `search`、`github`、`github_complete`、`planning`、`answering` のいずれかである。処理に失敗した場合は `{"type":"error","error":"..."}` を返す。

## 環境変数

### 必須設定

| 環境変数 | 説明 |
| --- | --- |
| `GCP_SA_KEY_JSON` | Vertex AI を呼び出すサービスアカウントキーの JSON 文字列。Google Cloud の `cloud-platform` スコープで利用する。 |
| `GH_PAT` | GitHub Code Search と Contents API に使用する Personal Access Token。対象リポジトリの Contents 読み取り権限が必要。 |

`GCP_PROJECT_ID` が未指定の場合は、`GCP_SA_KEY_JSON` 内の `project_id` も必須となる。必須設定の検証は、最初のチャット要求でエージェントを初期化するときに行われる。

### Vertex AI

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GCP_PROJECT_ID` | サービスアカウント JSON の `project_id` | Vertex AI を利用する Google Cloud プロジェクト。指定すると認証 JSON の値を上書きする。 |
| `GCP_LOCATION` | `us-central1` | Vertex AI のリージョン。`global` の場合はグローバル API エンドポイントを使用する。 |
| `GCP_MODEL` | `gemini-2.5-flash` | 回答生成と追加検索計画に利用する Vertex AI のモデル名。 |
| `GRAPHRAG_MAX_OUTPUT_TOKENS` | `8192` | Vertex AI の1回の応答で生成できる最大トークン数。最小値は1。 |
| `GRAPHRAG_MAX_OUTPUT_CHUNKS` | `8` | 出力上限で回答が終了した場合に、続きを含めて生成する最大チャンク数。最小値は1。JSON形式の検索計画は継続生成しない。 |

### GraphRAG の探索

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GRAPHRAG_MAX_ITERATIONS` | `5` | 検索と追加検索計画を繰り返す最大回数。最小値は1。 |
| `GRAPHRAG_MAX_RESULTS` | `100` | 1回の質問で保持する Neo4j 検索結果の累計上限。最小値は1。 |
| `GRAPHRAG_MAX_GITHUB_FILES` | `10` | 1回の質問で GitHub から取得するファイル数の上限。最小値は1。 |
| `GRAPHRAG_MAX_GITHUB_FILE_BYTES` | `200000` | GitHub から取得する1ファイルあたりの最大バイト数。超過したファイルは回答コンテキストへ追加しない。最小値は1。 |

1回の探索で Vertex AI が生成できる Neo4j と GitHub Code Search の追加検索語は合計最大5件である。Code Search は、ソース本文にしかない識別子、文字列、設定キー、エラーメッセージなどの検索が必要な場合に使用する。この値と、関数の影響経路をたどる最大距離5は現在固定値であり、環境変数では変更できない。

### Neo4j

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `NEO4J_URI` | `bolt://neo4j:7687` | GraphRAG の検索に利用する Bolt URI。 |
| `NEO4J_USER` | `neo4j` | Neo4j のユーザー名。 |
| `NEO4J_PASSWORD` | `kgpassword` | Neo4j のパスワード。 |
| `NEO4J_DATABASE` | `neo4j` | GraphRAG がクエリを実行するデータベース名。 |
| `NEO4J_URL` | `http://neo4j:7474` | Web UI から Neo4j Browser を利用するための HTTP プロキシ転送先。GraphRAG の検索接続には使用しない。 |
| `NEO4J_BOLT_HOST` | `neo4j` | Neo4j Browser 用 WebSocket プロキシの接続先ホスト。 |
| `NEO4J_BOLT_PORT` | `7687` | Neo4j Browser 用 WebSocket プロキシの接続先ポート。 |

### Web サーバとコレクタ接続

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `PORT` | `8080` | kg-agent の HTTP 待受ポート。 |
| `STATIC_DIR` | `src/kg_agent/dist` | React SPA の静的ファイルを配信するディレクトリ。コンテナでは `/opt/kg-agent/dist` を使用する。 |
| `COLLECTOR_URL` | `http://kg-collector:8081` | 収集状態、リポジトリ、グラフ、収集開始 API のプロキシ転送先。末尾の `/` は除去される。 |

## Docker Compose 設定例

`docker-compose.yml` は主要な環境変数を kg-agent コンテナへ渡す。起動前にホスト側で認証情報を設定する。

```bash
export GH_PAT=<read-only PAT>
export GCP_SA_KEY_JSON="$(cat /path/to/service-account.json)"
export GCP_LOCATION=asia-northeast1
export GCP_MODEL=gemini-2.5-flash
export GRAPHRAG_MAX_ITERATIONS=5
export GRAPHRAG_MAX_RESULTS=100
export GRAPHRAG_MAX_GITHUB_FILES=10
export GRAPHRAG_MAX_GITHUB_FILE_BYTES=200000
export GRAPHRAG_MAX_OUTPUT_TOKENS=8192
export GRAPHRAG_MAX_OUTPUT_CHUNKS=8
docker compose up --build
```

環境変数を変更した場合は、kg-agent コンテナを再作成して反映する。

```bash
docker compose up --build --force-recreate kg-agent
```

## セキュリティと制約

- kg-agent 自体には認証・認可機能がない。外部公開する場合は、リバースプロキシなどでアクセス制御を追加する。
- `GCP_SA_KEY_JSON` と `GH_PAT` は秘密情報として扱い、ファイル、イメージ、Git 管理対象へ保存しない。
- GitHub から取得したコードと Neo4j の検索結果はプロンプト内ではデータとして扱い、そこに含まれる命令には従わないよう Vertex AI へ指示する。
- 関数呼び出し関係は静的解析結果であり、動的ディスパッチや実行時に決まる依存関係を完全には検出できない。
- チャット履歴は利用中のブラウザとオリジンに保存される。別のブラウザ、端末、オリジンとは共有されない。
