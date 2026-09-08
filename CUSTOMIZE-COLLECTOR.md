# リポジトリ固有解析

このツール自身はプロジェクトに対して汎用的でなければならない。しかし、一方でリポジトリ固有の解析を入れる必要がある。ここではファイルごとの解析ルールや Tree-sitter Query をリポジトリ側に設定する方法について述べる

既存の静的解析ツール（Semgrep、CodeQL、ESLint、Dependabotなど）でも採用されている、**リポジトリルートに専用の設定ディレクトリエリアを設けるプラクティス**が非常に効果的です。

組織内の多数のリポジトリを巡回する汎用ツールにおいて、リポジトリ固有の解析（カスタム Tree-sitter Query や抽出ルールの定義）を安全かつ柔軟に組み込むための推奨設計パターンを提案します。

---

### 1. リポジトリ側のディレクトリ構成例

リポジトリのルート（または `.github/` 配下）に `.knowledge-graph/` ディレクトリを配置させる仕様にします。

```text
my-repository/
├── .knowledge-graph/          # ナレッジグラフ巡回ツール用設定フォルダ
│   ├── config.yml             # 全体設定（対象・除外パス、設定のオン/オフ）
│   ├── queries/               # リポジトリ固有の Tree-sitter Query (.scm)
│   │   ├── react_components.scm
│   │   └── custom_events.scm
│   └── rules.yml              # クエリとグラフ構造（Node/Edge）のマッピング定義
├── src/
└── package.json

```

---

### 2. 「S式クエリ」と「グラフ要素」を結びつける宣言的マッピング（Declarative Mapping）

単に `.scm`（Tree-sitter Query）を置くだけでは、キャプチャしたトークン（例: `@comp.name`）を Neo4j のどの「ラベル」「プロパティ」「リレーション」に変換すればよいかを汎用ツール側が解釈できません。

そのため、**「Tree-sitter クエリ」と「グラフ構造」を結びつける宣言的な YAML スキーマ**を定義するのがベストプラクティスです。

#### `queries/react_components.scm`（クエリ定義）

```query
(variable_declarator
  name: (identifier) @component.name
  value: (arrow_function) @component.body
)

```

#### `rules.yml`（クエリ結果をグラフに変換する定義）

```yaml
rules:
  - id: extract_react_components
    language: tsx
    target_files: "src/components/**/*.tsx" # 対象のファイルグロブパターン
    query_file: "queries/react_components.scm"
    
    # クエリのキャプチャ結果をどうグラフ化するか
    mapping:
      # 1. 作成（またはマッチ）するノードの定義
      nodes:
        - bind: "@component.name"
          label: "ReactComponent"
          key_property: "name"      # 一意キーとするプロパティ
          properties:
            is_custom: true
            
      # 2. 既存の File ノードと作成したノードを繋ぐエッジの定義
      relationships:
        - type: "DEFINES_COMPONENT"
          from: "File"              # ツール標準の File ノード
          to: "@component.name"     # 上記で作成したノード

```

---

### 3. 設計における 4 つのベストプラクティス

#### ① デフォルトフォールバック（継承と追加）

リポジトリ側に `.knowledge-graph/` が存在しない、あるいは部分的にしか記述されていない場合でもツールが止まらないよう設計します。

* **基本動作**: ツール組み込みの「標準解析ルール（汎用的な File, Function, Class の依存抽出）」を実行。
* **拡張動作**: リポジトリ側に `.knowledge-graph/` があれば、標準ルールに**追加（Merge）**または**オーバーライド**して解析を実行。

#### ② スコープ制限と安全性の確保（セキュリティ対策）

巡回ツールが各リポジトリの設定を読み込んで自動実行するため、**セキュリティ**への配慮が必要です。

* **任意コード（Python/JS等）の実行は避ける**: リポジトリ側に任意実行可能なコード（プラグインスクリプト）を置かせると、悪意あるリポジトリによって巡回ツール側のサーバー権限が奪われるリスク（RCE）があります。
* **宣言的（Declarative）記述に限定**: 上記の例のように **YAML と S式（Tree-sitter Query）のみ** を許可することで、安全にカスタム解析を実行できます。

#### ③ ノード・リレーションの命名空間（Namespace）の分離

リポジトリ固有の解析によって作られるノードやリレーションには、汎用ルールと衝突しないようにプレフィックスを自動で付与するか、ドメイン用ラベルを強制するルールを設けるとグラフの品質が保たれます。

* 例: リポジトリ固有で生成されたラベルには `Custom:` や `App:` などのプレフィックスやプロパティ（`is_custom_rule: true`）を埋め込む。

#### ④ ドライラン（検証）コマンドの提供

開発者が自分のリポジトリで「正しく Tree-sitter クエリとマッピングが機能するか」をローカルでテストできる CLI コマンドをツール側に用意しておきます。

```bash
# 巡回ツール側の CLI を使ってローカルで試走するイメージ
kg-tool lint --repo-path ./my-repository
kg-tool parse --repo-path ./my-repository --dry-run

```

この「**設定ディレクトリエリア + S式クエリ + 宣言的グラフマッピング（YAML）**」という構成をとることで、コアツールとしての汎用性を高く保ちつつ、各チームが自由に自リポジトリ用の解析ルールを追加できる構造が実現できます。