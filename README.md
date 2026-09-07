# knowledge-graph-poc
このリポジトリは Knowledge Graph を AI で管理することができるかを確認するためのものである。

# ナレッジグラフとは

プログラミングやソフトウェア開発におけるナレッジグラフとは、ソースコード、ライブラリ、設計書、変更履歴、バグ報告などの開発に関わる情報を「要素（ノード）」と「関係性（エッジ）」のネットワーク構造として整理・データベース化したものです。

従来のテキスト検索や静的解析ツールでは見落としがちな「コードとシステム全体の複雑なつながり」をグラフ構造で表現することで、開発の精度と効率を高めます。

## ナレッジグラフの構成例

ノード（要素）: 関数、クラス、ファイル、依存ライブラリ、コミット履歴、Issue（課題）、開発者

エッジ（関係性）: 「〜を呼び出す（calls）」「〜を継承する（inherits）」「〜に依存する（depends_on）」「〜のバグを修正する（fixes）」「〜が作成した（authored_by）」

## ナレッジグラフによってプログラミング品質が上がる理由

### 影響範囲の正確な把握（破壊的変更の防止）
コードの一部を修正した際、その変更がどの関数、テストコード、外部API、あるいは別サービスにまで影響するかを網羅的に追跡できます。デプロイ後の予期せぬバグ（エンバグ）を劇的に減らせます。

### コード生成AI（LLM）の精度向上（Graph RAG）
GitHub CopilotやCursorなどのコード生成AIにナレッジグラフの構造情報を与えることで、単一のファイル内だけでなく「プロジェクト全体の設計規約や依存関係」を理解した精度の高いコードを提案させることが可能になります。

### アーキテクチャ違反・循環依存の検知
「上位レイヤーが下位レイヤーを正しく呼び出しているか」「コード間で不適切な循環依存が発生していないか」といった設計上の欠陥（コードスメル）を自動的に検出・リファクタリングできます。

### ノウハウの再利用と属人化の解消
「過去に似た機能を誰がどのように実装し、どんな不具合が発生してどう修正されたか」をコードと課題管理システムを紐付けて検索できるため、過去の失敗の再発を防ぎ、共通処理の重複実装を回避できます。

# ナレッジグラフの生成手順

GitHubの多数のリポジトリからナレッジグラフを構築するには、「データ収集（GitHub API/コード解析）」「グラフモデル定義」「グラフデータベース（Neo4j等）への保存」を行うパイプラインを作成します。

---

**ステップ1: データの抽出・解析**

複数リポジトリから3つのレイヤーのデータを並列取得します。

* **GitHubメタデータの取得（GitHub GraphQL API）**
* 各リポジトリのコミット履歴、プルリクエスト、Issue、コントリビューター（開発者）情報を取得します。


* **コード構造の構文解析（AST / LSP解析）**
* **Tree-sitter** や **SCIP**（Sourcegraphのインデックス形式）などのパーサーを使用し、ソースコードから関数、クラス、変数、インポート文、および呼び出し関係（Call Graph）をパースします。


* **リポジトリ間の依存関係の取得**
* 各リポジトリの `package.json`、`pyproject.toml`、`pom.xml` や GitHub Dependency Graph API から、リポジトリ同士や外部ライブラリとの依存関係を抽出します。



---

**ステップ2: グラフモデル（ノードとエッジ）の設計**

抽出したデータを「要素（ノード）」と「関係（エッジ）」として整理します。

* **ノードの種類**
* `Repository`（リポジトリ）
* `File`（ファイル）
* `Function`（関数） / `Class`（クラス）
* `Commit`（コミット） / `Issue`（課題）
* `User`（開発者）


* **エッジ（関係性）の種類**
* `(:Repository)-[:CONTAINS]->(:File)`
* `(:File)-[:DEFINES]->(:Function)`
* `(:Function)-[:CALLS]->(:Function)` （クロスリポジトリ含む）
* `(:User)-[:AUTHORED]->(:Commit)-[:MODIFIED]->(:File)`
* `(:Commit)-[:FIXES]->(:Issue)`
* `(:Repository)-[:DEPENDS_ON]->(:Repository)`



---

**ステップ3: グラフデータベースへの登録（Neo4j の例）**

抽出・変換したデータを **Neo4j** や **Memgraph** などのグラフデータベースへ投入します。

たとえば、Python等で解析したデータを Cypher クエリ言語を使って登録します。

```cypher
// リポジトリ・ファイル・関数のノード作成と関連付け
MERGE (r:Repository {name: "payment-service"})
MERGE (f:File {path: "src/charge.py", repo: "payment-service"})
MERGE (fn1:Function {name: "process_payment", repo: "payment-service"})

MERGE (r)-[:CONTAINS]->(f)
MERGE (f)-[:DEFINES]->(fn1)

// 他リポジトリの共通ライブラリ関数呼び出しの記録
MERGE (fn2:Function {name: "validate_card", repo: "auth-library"})
MERGE (fn1)-[:CALLS]->(fn2)

```

---

**ステップ4: 活用（検索・分析）**

構築したナレッジグラフに対してクエリを発行し、品質向上に役立てます。

* **マルチリポジトリ影響範囲分析**
特定ライブラリの関数に変更を加える際、影響を受ける全リポジトリの関数を特定します。
```cypher
MATCH (target:Function {name: "validate_card"})<-[:CALLS*1..3]-(caller:Function)<-[:DEFINES]-(f:File)<-[:CONTAINS]-(r:Repository)
RETURN r.name, f.path, caller.name

```


* **AI・LLM連携（GraphRAG）**
GitHub Copilot や社内LLMツールと接続し、「リポジトリAの変更がリポジトリBにどう影響するか」といった複数コードベースにまたがる回答精度を高めます。

---

**構築時に使える主要オープンソース・ツール**

* **Tree-sitter**: 複数言語に対応した高速コード解析ライブラリ
* **CodeQL**: GitHub公式のコード解析エンジン（コードをDB化してクエリ検索可能）
* **Neo4j / Memgraph**: ナレッジグラフの保存・検索を行う定番データベース

# 対象のリポジトリ

対象のGitHub Organizationは環境変数 `GH_TARGET_ORGANIZATION` で指定する。

# GitHubの認証情報

`GH_PAT` には、対象Organizationのリポジトリに対して actions、code、issues、metadata、pull requestsを参照できるPATを設定する。`GH_PAT` と `GH_TARGET_ORGANIZATION` はイメージへ組み込まず、コンテナ起動時にホストの環境変数を継承させる。

GHCRへのログインには `GITHUB_TOKEN` を使用する。トークンには、プッシュ先パッケージに対する書き込み権限が必要となる。

# 構成

`docker-compose.yml` で次の3サービスを起動する。

* `neo4j`: ナレッジグラフを永続化する。
* `kg-collector`: GitHub API、Tree-sitter、Manifest解析を実行し、収集ジョブと参照用のHTTP APIを提供する。Web UIは持たない。
* `kg-agent`: React SPAをPythonサーバで配信し、`kg-collector` APIへのプロキシとAIチャットAPIを提供する。

`kg-collector` は抽出した情報をYAMLファイルへ保存せず、最初からNeo4jへノードとエッジとして登録する。`kg-agent` の画面状態はブラウザヒストリで管理され、リポジトリ詳細URLやチャットURLへ直接アクセスできる。現時点のAIチャットはモックで、入力に対して「未実装です」と応答する。認証機能は設けていない。

Neo4j のデータは Codespaces 内の `./neo4j-data` を `/data` にバインドマウントして永続化する。このディレクトリは `.gitignore` と `.dockerignore` の対象であり、Gitには含めない。

devcontainerはDocker-in-Dockerを提供する。devcontainerをrebuildしたあと、内部から `docker compose` を実行できる。

## ステップ1〜3収集ツール

`kg-collect` は `GH_PAT` を使って `GH_TARGET_ORGANIZATION` のリポジトリを列挙し、次の情報を並列収集します。

* GitHub GraphQL API: リポジトリ、直近のコミット、Pull Request、Issue、コミット作成者
* Tree-sitter: 関数、クラス、変数、import、関数内の呼び出し
* Manifest: `package.json`、`pyproject.toml`、`pom.xml` の依存関係

収集結果はNeo4jへ次のように登録する。

* `Repository`, `File`, `Function`, `Class`, `Commit`, `PullRequest`, `Issue`, `User`, `Manifest`, `Dependency` ノードを作成する。
* `CONTAINS`, `DEFINES`, `CALLS`, `HAS_COMMIT`, `HAS_PULL_REQUEST`, `HAS_ISSUE`, `AUTHORED`, `HAS_MANIFEST`, `DECLARES`, `DEPENDS_ON`, `FIXES` エッジを作成する。
* `updatedAt`, `pushedAt`, デフォルトブランチの先頭コミットOIDが一致するリポジトリはcloneと解析を省略し、変更されたリポジトリだけを置き換える。

### docker compose による起動

ホスト側で環境変数を設定する。`NEO4J_PASSWORD` を省略した場合は `kgpassword` を使う。

```bash
export GH_PAT=<read-only PAT>
export GH_TARGET_ORGANIZATION=<organization>
export NEO4J_PASSWORD=<neo4j password>
```

Neo4j、収集API、kg-agent Web UIを起動する。

```bash
docker compose up --build
```

Codespaces の Ports ビューでは通常 `8080` だけを転送して利用する。その他のポートは、各サービスへ直接接続してデバッグする場合に限り転送する。

* `8080`: kg-agent のWeb UI
* `8081`: kg-collector のAPI（直接デバッグ用）
* `7474`: Neo4j HTTP API（直接デバッグ用）
* `7687`: Neo4j Bolt接続（直接デバッグ用）

Codespaces の Ports ビューで `8080` を開いて kg-agent を利用する。収集管理画面では収集状況と対象リポジトリを確認し、収集をバックグラウンドで開始できる。リポジトリ詳細には先頭コミットID、最終コミット日時、ノード数、Call Graphを表示する。AIチャット画面は入力に対してモック応答を返す。

`kg-collector` は次の認証なしAPIを提供する。

* `POST /api/collect`: 収集ジョブを開始する。
* `GET /api/status`: 収集状況をServer-Sent Eventsで配信する。
* `GET /api/repositories`: 収集済みリポジトリの一覧を返す。
* `GET /api/repositories/{name}`: リポジトリ情報とノード数を返す。
* `GET /api/graph/{name}`: Call Graphを返す。

GitHub GraphQL APIの1回の取得上限に合わせ、コミット、Pull Request、Issueはそれぞれ直近100件を保存する。件数は `kg-collector` コンテナ内で `kg-collect --history-limit 20` のように指定して変更できる。

### Neo4j の確認

Neo4j Browser は kg-agent Web UI のサイドバーから開く。接続先は自動設定される。手動で接続する場合、Codespacesでは `bolt+s://<8080番の転送URLのホスト名>:443`、ローカルでは `bolt://localhost:8080` を指定する。ユーザー名は `neo4j`、パスワードは `NEO4J_PASSWORD` で指定した値を使用する。Codespacesで転送が必要なポートは `8080` のみで、`7474` と `7687` をブラウザ向けに転送する必要はない。

登録状況はCypherで確認できる。

```cypher
MATCH (r:Repository)
RETURN r.full_name, r.source_files, r.functions, r.dependencies
ORDER BY r.full_name
```

関数呼び出しの影響範囲は次のように検索できる。

```cypher
MATCH (target:Function {name: "validate_card"})<-[:CALLS*1..3]-(caller:Function)<-[:DEFINES]-(f:File)<-[:CONTAINS]-(r:Repository)
RETURN r.full_name, f.path, caller.name
```

### 差分更新

再度「ナレッジグラフ収集」を実行すると、Neo4j上の `Repository` ノードとGitHub上のリポジトリ情報を比較する。`updatedAt`、`pushedAt`、デフォルトブランチの先頭コミットOIDが一致するリポジトリはcloneと解析を省略し、変更されたリポジトリだけを置き換える。

Neo4jのデータを破棄して作り直す場合は、サービスを停止して `./neo4j-data` を削除する。

```bash
docker compose down
rm -rf neo4j-data
```

途中で一部リポジトリの取得に失敗した場合も、収集ステータスにエラーを記録して残りの収集を継続する。生成前に対象リポジトリの古いノードを置き換えるため、削除されたソースの解析結果は残らない。

開発時のテストは次のコマンドで実行する。

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

React SPAだけを開発する場合は `kg_agent` ディレクトリで `npm install` と `npm run dev` を実行する。本番用SPAは `kg-agent` イメージのビルド時に生成される。
