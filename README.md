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

収集ツールはDockerイメージ内で動作し、ナレッジグラフをコンテナ内の `/knowledge` にYAMLおよびMarkdownとして作成する。`Dockerfile` はツールのインストールまでを行い、ナレッジグラフ自体は含めない。

devcontainerはDocker-in-Dockerを提供する。devcontainerをrebuildしたあと、内部からDockerイメージのbuild、run、commit、pushを実行できる。

## ステップ1収集ツール

`kg-collect` は `GH_PAT` を使って `GH_TARGET_ORGANIZATION` のリポジトリを列挙し、次の情報を並列収集します。

* GitHub GraphQL API: リポジトリ、直近のコミット、Pull Request、Issue、コミット作成者
* Tree-sitter: 関数、クラス、変数、import、関数内の呼び出し
* Manifest: `package.json`、`pyproject.toml`、`pom.xml` の依存関係

### イメージのbuildとWeb UI

収集ツールのイメージをbuildする。

```bash
docker build -t kg-collector:latest .
```

ホスト側で環境変数を設定し、コンテナを名前付きで起動する。コンテナ起動時に Web UI が `8080` ポートで開始される。Codespaces のポート転送で `8080` を開いて閲覧する。`-e` に値を書かないことで、ホストの値をコンテナへ継承する。

```bash
export GH_PAT=<read-only PAT>
export GH_TARGET_ORGANIZATION=<organization>

docker run --name kg-collector-run \
	-p 8080:8080 \
	-e GH_PAT \
	-e GH_TARGET_ORGANIZATION \
	kg-collector:latest
```

Web UI 上部の「ナレッジグラフ収集」ボタンを押すと収集がバックグラウンドで開始される。サイドバーの「依存関係探索」では、収集済みリポジトリの Call Graph を表示する。木の各項目は初期状態で折りたたまれており、項目をクリックすると下位の呼び出しを展開できる。認証は行わず、Codespaces のポート転送経由で利用する。

```bash
# Codespaces の Ports ビューで 8080 を転送する
```

GitHub GraphQL APIの1回の取得上限に合わせ、コミット、Pull Request、Issueはそれぞれ直近100件を保存する。件数はイメージ名の後ろに `--history-limit 20` のように指定して変更でき、総件数は各YAMLの `total_count` に残る。

### ナレッジグラフを含むイメージの作成とpush

収集コンテナの終了後、`/knowledge` を含む新しいイメージを作成する。起動時に渡したPATをイメージ設定へ残さないよう、commit時に認証用環境変数を空へ上書きする。

```bash
export GHCR_OWNER=<push先のGitHub userまたはorganization>
export KG_IMAGE="ghcr.io/${GHCR_OWNER}/knowledge-graph:latest"

docker commit \
	--change 'ENV GH_PAT=' \
	--change 'ENV GH_TARGET_ORGANIZATION=' \
	kg-collector-run "${KG_IMAGE}"

echo "${GITHUB_TOKEN}" | docker login ghcr.io \
	--username "${GITHUB_ACTOR}" \
	--password-stdin
docker push "${KG_IMAGE}"
```

push後は収集コンテナを削除できる。

```bash
docker rm kg-collector-run
```

### 既存ナレッジグラフの差分更新

commit済みイメージから起動すると、既存の `/knowledge` とGitHub上のリポジトリ情報を比較する。`updatedAt`、`pushedAt`、デフォルトブランチの先頭コミットOIDが一致するリポジトリはcloneと解析を省略し、変更されたリポジトリだけを置き換える。

```bash
docker pull "${KG_IMAGE}"
docker run --name kg-collector-update \
	-e GH_PAT \
	-e GH_TARGET_ORGANIZATION \
	"${KG_IMAGE}"

docker commit \
	--change 'ENV GH_PAT=' \
	--change 'ENV GH_TARGET_ORGANIZATION=' \
	kg-collector-update "${KG_IMAGE}"
docker push "${KG_IMAGE}"
docker rm kg-collector-update
```

収集を実行せずナレッジグラフだけを取り出す場合は、一時コンテナから `/knowledge` をコピーする。

```bash
docker create --name kg-export "${KG_IMAGE}"
docker cp kg-export:/knowledge ./knowledge
docker rm kg-export
```

### 出力構造

解析結果はソースのディレクトリ構造を保って保存します。

```text
knowledge/
└── <organization>/
	├── index.yaml
	└── <repository>/
		├── SUMMARY.md
		├── repository.yaml
		├── github/
		│   ├── commits.yaml
		│   ├── contributors.yaml
		│   ├── issues.yaml
		│   └── pull_requests.yaml
		├── dependencies/
		│   └── manifests.yaml
		└── code/
			└── <source path>.yaml
```

各ファイルは `schema_version` を持ち、途中で一部リポジトリの取得に失敗した場合も `index.yaml` にエラーを記録して残りの収集を継続します。生成前に対象リポジトリの古い出力を置き換えるため、削除されたソースの解析結果は残りません。

開発時のテストは次のコマンドで実行する。

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```
