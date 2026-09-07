# ナレッジグラフ収集仕様

この文書は `kg-collect` が GitHub Organization から情報を取得し、ソースコードと依存定義を解析して Neo4j に登録するまでの実装仕様を示す。記載内容は現在の `src/kg_collector` の挙動に基づく。

## 1. 収集処理の全体像

収集は次の順序で実行する。

1. GitHub GraphQL API で Organization が所有するリポジトリを列挙する。
2. アーカイブ状態や `--repository` 指定に基づいて対象を絞り込む。
3. Neo4j の既存 `Repository` ノードと GitHub 上のフィンガープリントを比較する。
4. 変更されたリポジトリについて、GitHub メタデータ取得、浅い clone、ソース解析、Manifest 解析を行う。
5. リポジトリ単位で既存の配下ノードを置き換え、Neo4j にノードとエッジを登録する。
6. 1件以上を再収集した場合、Organization 内の全 `Function` を使って `CALLS` エッジを再解決する。
7. 収集結果を `CollectionIndex` ノードへ保存する。

GitHub API のメタデータ取得と clone 後の解析は、リポジトリごとに `ThreadPoolExecutor` で並列実行する。デフォルトの並列数は4である。各ソースファイルの Tree-sitter 解析は別プロセスで実行し、1ファイルの上限時間を60秒とする。

## 2. 実行条件とCLIオプション

### 必須環境変数

| 変数 | 用途 |
| --- | --- |
| `GH_PAT` | GitHub GraphQL APIと`gh repo clone`の認証 |
| `GH_TARGET_ORGANIZATION` | 収集対象Organization。`--owner`で上書き可能 |

### Neo4j環境変数

| 変数 | デフォルト |
| --- | --- |
| `NEO4J_URI` | `bolt://neo4j:7687` |
| `NEO4J_USER` | `neo4j` |
| `NEO4J_PASSWORD` | `kgpassword` |
| `NEO4J_DATABASE` | `neo4j` |
| `NEO4J_TRANSACTION_RETRY_SECONDS` | `120` |

### CLIオプション

| オプション | デフォルト | 説明 |
| --- | --- | --- |
| `--owner OWNER` | `GH_TARGET_ORGANIZATION` | 収集対象Organization |
| `--repository NAME` | 指定なし | 対象をリポジトリ名で限定する。複数回指定可能 |
| `--history-limit N` | `100` | コミット、Pull Request、Issueの取得件数。1から100まで |
| `--workers N` | `4` | 同時に収集するリポジトリ数 |
| `--max-file-bytes N` | `1000000` | ソース解析対象ファイルの最大バイト数 |
| `--include-archived` | 無効 | アーカイブ済みリポジトリも収集する |

実行例:

```bash
kg-collect --owner example-org --history-limit 50 --workers 8
kg-collect --repository service-a --repository shared-library
```

## 3. GitHubメタデータの収集

GitHub GraphQL APIの仕様は[GitHub GraphQL API documentation](https://docs.github.com/en/graphql)を参照する。

### リポジトリ列挙

`repositoryOwner.repositories`を名前の昇順で50件ずつページングする。`ownerAffiliations: OWNER`を指定するため、Organizationが所有するリポジトリだけが対象となる。

列挙時には、名前、URL、SSH URL、private/archive/forkフラグ、更新日時、push日時、デフォルトブランチ名、その先頭コミットOIDを取得する。この情報は差分更新の判定にも使用する。

### リポジトリ詳細

再収集対象ごとに1回のGraphQLクエリで次を取得する。

| 対象 | 取得範囲と順序 |
| --- | --- |
| Commit | デフォルトブランチの先頭から直近`history-limit`件 |
| Pull Request | `UPDATED_AT`降順で直近`history-limit`件 |
| Issue | `UPDATED_AT`降順で直近`history-limit`件。Pull RequestはGitHubの`issues`接続には含まれない |
| Topic | 先頭30件 |
| PR/IssueのLabel | 各項目の先頭30件 |
| Commitに関連するPR | 各Commitの先頭10件 |

Commitのauthorから、サンプル内コミット数を集計したcontributor一覧も生成する。committer情報はAPIから取得するが、現在はNeo4jへ保存しない。

GitHub APIリクエストのタイムアウトは60秒である。HTTP `429`, `500`, `502`, `503`, `504`、通信エラー、タイムアウト、不完全なレスポンスは最大6回試行する。`Retry-After`があればその値を使い、それ以外は指数バックオフする。

## 4. ソースコード解析

### 対応言語と参照仕様

パーサーは[Tree-sitter](https://tree-sitter.github.io/tree-sitter/)と[tree-sitter-language-pack](https://github.com/Goldziher/tree-sitter-language-pack)を使用する。言語ごとの厳密な構文ノード仕様は、次のgrammarリポジトリにある`grammar.js`および`node-types.json`を参照する。

| 言語 | 拡張子 | Tree-sitter grammar |
| --- | --- | --- |
| C | `.c`, `.h` | [tree-sitter-c](https://github.com/tree-sitter/tree-sitter-c) |
| C++ | `.cc`, `.cpp` | [tree-sitter-cpp](https://github.com/tree-sitter/tree-sitter-cpp) |
| C# | `.cs` | [tree-sitter-c-sharp](https://github.com/tree-sitter/tree-sitter-c-sharp) |
| Go | `.go` | [tree-sitter-go](https://github.com/tree-sitter/tree-sitter-go) |
| Java | `.java` | [tree-sitter-java](https://github.com/tree-sitter/tree-sitter-java) |
| JavaScript / JSX | `.js`, `.jsx` | [tree-sitter-javascript](https://github.com/tree-sitter/tree-sitter-javascript) |
| Kotlin | `.kt`, `.kts` | [tree-sitter-kotlin](https://github.com/fwcd/tree-sitter-kotlin) |
| PHP | `.php` | [tree-sitter-php](https://github.com/tree-sitter/tree-sitter-php) |
| Python | `.py` | [tree-sitter-python](https://github.com/tree-sitter/tree-sitter-python) |
| Ruby | `.rb` | [tree-sitter-ruby](https://github.com/tree-sitter/tree-sitter-ruby) |
| Rust | `.rs` | [tree-sitter-rust](https://github.com/tree-sitter/tree-sitter-rust) |
| Swift | `.swift` | [tree-sitter-swift](https://github.com/alex-pinkus/tree-sitter-swift) |
| TypeScript | `.ts` | [tree-sitter-typescript](https://github.com/tree-sitter/tree-sitter-typescript) |
| TSX | `.tsx` | [tree-sitter-typescript](https://github.com/tree-sitter/tree-sitter-typescript) |

`.hpp`, `.cxx`, `.mjs`, `.cjs`, `.mts`, `.cts`など、表にない拡張子は現在の実装では解析しない。

### 共通のAST抽出規則

実装は言語別Tree-sitter Queryを持たず、全言語に対して次のASTノード型を共通適用する。

| 種類 | 認識するASTノード型 | 保存内容 |
| --- | --- | --- |
| Function | `function_definition`, `function_declaration`, `method_definition`, `method_declaration`, `function_item`, `arrow_function`, `constructor_declaration` | 名前、kind、開始・終了行、呼び出し名 |
| Class | `class_definition`, `class_declaration`, `interface_declaration`, `struct_item`, `trait_item`, `enum_declaration` | 名前、kind、開始・終了行 |
| Import | `import_statement`, `import_declaration`, `import_from_statement`, `use_declaration`, `using_directive`, `require_expression` | 文のテキスト。先頭500文字 |
| Variable | `variable_declarator`, `const_declaration`, `static_item`, `assignment` | 名前または左辺の先頭200文字、開始行 |
| Call | `call`, `call_expression`, `invocation_expression` | `function`または`name`フィールドの先頭300文字 |

シンボル名はASTノードの`name`フィールドから取得する。該当フィールドがない場合は`<anonymous>`とする。行番号は1始まりである。ソースのデコード不能なバイトは置換文字へ変換する。

Function配下を再帰走査し、Callの文字列表現を重複排除・ソートして`calls`プロパティへ保存する。たとえば`send()`は`send`、`client.send()`はgrammarが返す式に応じて`client.send`となる。この処理は型情報、import先、レシーバー型、オーバーロードを解決しない。また、入れ子の関数内にある呼び出しが外側の関数にも含まれる場合がある。

Importは`File.imports`プロパティとして保存する。Variableは解析結果には生成するが、現在のグラフモデルには`Variable`ノードもFileプロパティもないためNeo4jへ保存しない。

### 言語ごとの実効的な認識

共通ノード型と各grammarのノード名が一致した要素だけを収集する。このため、拡張子が対応表に含まれていても、その言語の全構文を収集できるとは限らない。

| 言語 | 主に認識される定義・呼び出し | 現在の注意点 |
| --- | --- | --- |
| Python | `function_definition`, `class_definition`, `call` | 関数とメソッドは同じ型として扱う |
| JavaScript / JSX | `function_declaration`, `method_definition`, `arrow_function`, `class_declaration`, `call_expression` | 変数へ代入したarrow functionは名前フィールドがなく`<anonymous>`になる場合がある |
| TypeScript / TSX | JavaScript相当のノード型 | interfaceは`interface_declaration`としてClass化する |
| Rust | `function_item`, `struct_item`, `trait_item`, `call_expression` | structとtraitもClass化する |
| C / C++ | grammarが共通型に一致する関数定義・call expression | declarationや複雑なdeclaratorの名前抽出はgrammarのフィールドに依存する |
| C# | `method_declaration`, `class_declaration`, `interface_declaration`, `invocation_expression` | constructorなどは共通型に一致する場合だけ対象 |
| Go | `function_declaration`, `method_declaration`, `call_expression` | `type_declaration`自体はClass型一覧にないため、一般的なstruct宣言をClass化できない場合がある |
| Java | `method_declaration`, `constructor_declaration`, `class_declaration`, `interface_declaration` | 一般的な`method_invocation`はCall型一覧にないため、呼び出しを収集できない場合がある |
| Kotlin / PHP / Ruby / Swift | grammarが共通型に一致する要素 | 専用マッピングがないため、grammarのノード名が異なる構文は収集しない |

`parse_has_error`にはTree-sitterのルートノードが構文エラーを含むかを保存する。構文エラーがあってもパーサーが結果を返せば、取得できたシンボルは保存する。

## 5. Manifestと依存関係の解析

リポジトリ配下を再帰走査し、ファイル名が`package.json`、`pyproject.toml`、`pom.xml`のいずれかであるファイルを解析する。

### package.json

[npm package.json specification](https://docs.npmjs.com/cli/configuring-npm/package-json)を基礎とし、次のセクションのキーと値をそのまま依存名・バージョンとして取得する。

* `dependencies`
* `devDependencies`
* `peerDependencies`
* `optionalDependencies`

`bundledDependencies`、`bundleDependencies`、`overrides`、workspace定義は依存として抽出しない。

### pyproject.toml

[PEP 621](https://peps.python.org/pep-0621/)の次の項目を解析する。

* `[project].dependencies`: scopeは`dependencies`
* `[project.optional-dependencies].<group>`: scopeは`optional:<group>`

依存文字列の最初の`<`, `>`, `=`, `!`, `~`, `;`, `[`, 空白で名前とバージョン指定を分割する。指定がなければバージョンを`*`とする。これは完全なPEP 508パーサーではないため、複雑なURL要件や環境マーカーは文字列分割として扱う。

[Poetry dependency specification](https://python-poetry.org/docs/dependency-specification/)については`[tool.poetry.dependencies]`だけを追加で解析し、`python`キーを除外する。Poetry dependency groupsなど、その他のPoetryセクションは解析しない。

### pom.xml

[Maven POM reference](https://maven.apache.org/pom.html)にある`dependencies/dependency`を名前空間対応で再帰検索する。

* 名前: `groupId:artifactId`。`groupId`がなければ`artifactId`
* バージョン: `version`。省略時は`*`
* scope: `scope`。省略時は`compile`

`dependencyManagement`配下も検索式に一致すれば抽出される。親POM、property、BOMからのバージョン解決は行わず、`${...}`も文字列のまま保存する。

### Organization内リポジトリとの照合

依存名について、次の候補を作る。

* 依存名全体
* 最後の`/`より後ろ。npmのscoped packageを想定
* 最後の`:`より後ろ。MavenのartifactIdを想定

候補と対象Organizationのリポジトリ名を小文字化し、`_`を`-`へ置換して完全一致させる。一致した場合は`repository_dependency`を設定し、`Repository -[:DEPENDS_ON]-> Repository`を作成する。外部パッケージの配布元リポジトリをGitHub等から探索する処理はない。

## 6. ノード仕様

すべての主要ノードラベルには、表のIDプロパティに一意制約を作成する。ネストした非スカラー値は`<property>_json`というJSON文字列として保存し、`None`は保存しない。

### Repository

| 項目 | 仕様 |
| --- | --- |
| 一意キー | `full_name`。例: `example/service` |
| 収集元 | GitHub GraphQL API |
| 作成条件 | 対象に選ばれ、収集がNeo4j書き込みまで成功した場合 |
| 主なプロパティ | `owner`, `name`, `full_name`, `description`, `url`, `sshUrl`, `isPrivate`, `isArchived`, `isFork`, `createdAt`, `updatedAt`, `pushedAt`, `default_branch`, `head_oid`, `diskUsage`, `primaryLanguage`, `licenseInfo`, `topics` |
| 集計値 | `source_files`, `functions`, `classes`, `manifests`, `dependencies` |

同一Organization内依存の参照先がまだ収集されていない場合、`full_name`, `owner`, `repository`だけを持つstub `Repository`が先に作成される。対象リポジトリ自身を収集すると詳細プロパティが追加される。

### File

| 項目 | 仕様 |
| --- | --- |
| ID | `<owner/repository>:<relative-path>` |
| 収集元 | cloneしたファイルとTree-sitter解析結果 |
| 主なプロパティ | `repository`, `path`, `language`, `parse_has_error`, `parse_error`, `imports` |

### Function

| 項目 | 仕様 |
| --- | --- |
| ID | `<owner/repository>:<relative-path>:<start-line>:<name>` |
| 収集元 | 共通Function ASTノード型 |
| 主なプロパティ | `owner`, `repository`, `file`, `name`, `kind`, `start_line`, `end_line`, `calls`, `external` |

通常の定義は`external=false`である。呼び出し先を一意に解決できない場合、`<owner/repository>::external::<called-name>`をIDとする`external=true`の参照用Functionを作成する。

### Class

| 項目 | 仕様 |
| --- | --- |
| ID | `<owner/repository>:<relative-path>:<start-line>:<name>` |
| 収集元 | 共通Class ASTノード型 |
| 主なプロパティ | `owner`, `repository`, `file`, `name`, `kind`, `start_line`, `end_line` |

### Commit

| 項目 | 仕様 |
| --- | --- |
| ID | `<owner/repository>:commit:<oid>` |
| 収集元 | デフォルトブランチの履歴 |
| 主なプロパティ | `oid`, `messageHeadline`, `committedDate`, `url`, `additions`, `deletions`, `changedFilesIfAvailable`, author情報、`pull_request_numbers` |

`pull_request_numbers`はプロパティとして保存する。CommitからPullRequestへのエッジは現在作成しない。

### PullRequest

| 項目 | 仕様 |
| --- | --- |
| ID | `<owner/repository>:pull_request:<number>` |
| 収集元 | GitHub Pull Request |
| 主なプロパティ | `number`, `title`, `state`, `url`, `createdAt`, `updatedAt`, `mergedAt`, `closedAt`, `baseRefName`, `headRefName`, `additions`, `deletions`, `changedFiles`, `labels`, author情報 |

### Issue

| 項目 | 仕様 |
| --- | --- |
| ID | `<owner/repository>:issue:<number>` |
| 収集元 | GitHub Issue |
| 主なプロパティ | `number`, `title`, `state`, `url`, `createdAt`, `updatedAt`, `closedAt`, `labels`, author情報 |

### User

| 項目 | 仕様 |
| --- | --- |
| ID | `login`、`email`、`name`の優先順。すべて空なら`unknown` |
| 収集元 | Commit authorの集計、およびCommit、Pull Request、Issueのauthor |
| 主なプロパティ | `login`, `name`, `email`, `commits_in_sample` |

`author_user_id`が`unknown`の項目には`AUTHORED`を作成しない。GitHub loginのない異なる人物が同じemailまたはnameを持つ場合は同じUserへ統合される可能性がある。

### Manifest

| 項目 | 仕様 |
| --- | --- |
| ID | `<owner/repository>:manifest:<relative-path>` |
| 収集元 | 対応する3種類のManifest |
| 主なプロパティ | `owner`, `repository`, `path`, `type`, `error` |

構文エラーなどで解析できなかった場合もManifestノードを作り、`error`を保存する。この場合Dependencyは作成しない。

### Dependency

| 項目 | 仕様 |
| --- | --- |
| ID | `<manifest-id>:dependency:<scope>:<name>` |
| 収集元 | Manifest内の各依存宣言 |
| 主なプロパティ | `owner`, `repository`, `manifest_id`, `name`, `version`, `scope`, `repository_dependency` |

同じManifest、scope、依存名の重複宣言は同じDependencyへ統合される。

### CollectionIndex

| 項目 | 仕様 |
| --- | --- |
| 一意キー | `owner` |
| 収集元 | 収集実行結果 |
| 主なプロパティ | `owner`, `generated_at`, `data` |

`data`はschema version、owner、生成日時、リポジトリ別の成功・失敗結果をJSON文字列で保持する。これは管理用ノードであり、他ノードとのエッジは作成しない。

## 7. エッジ仕様

| エッジ | 方向 | 生成条件 |
| --- | --- | --- |
| `CONTAINS` | `Repository -> File` | ソース解析対象Fileごとに作成 |
| `DEFINES` | `File -> Function`または`File -> Class` | Fileから抽出した各定義に作成 |
| `CALLS` | `Function -> Function` | Functionの`calls`文字列を後述の規則で解決して作成 |
| `HAS_COMMIT` | `Repository -> Commit` | 取得範囲内のCommitごとに作成 |
| `HAS_PULL_REQUEST` | `Repository -> PullRequest` | 取得範囲内のPull Requestごとに作成 |
| `HAS_ISSUE` | `Repository -> Issue` | 取得範囲内のIssueごとに作成 |
| `AUTHORED` | `User -> Commit/PullRequest/Issue` | author IDが`unknown`でない場合に作成 |
| `HAS_MANIFEST` | `Repository -> Manifest` | 検出したManifestごとに作成。解析エラー時も作成 |
| `DECLARES` | `Manifest -> Dependency` | 正常に解析できた依存宣言ごとに作成 |
| `DEPENDS_ON` | `Repository -> Repository` | 依存名が同一Organization内のリポジトリ名に一致した場合に作成 |
| `FIXES` | `Commit -> Issue` | Commit見出しが修正キーワードとIssue番号に一致し、そのIssueノードも取得範囲内に存在する場合に作成 |

### CALLSの解決規則

`CALLS`は全Functionを読み、呼び出し名の完全一致で次の優先順に解決する。

1. 呼び出し元と同じリポジトリ、同じファイル、同じ名前のFunctionがあれば、そのすべてへ接続する。
2. 同じファイルに候補がなく、Organization全体で同名Functionが1件だけなら、リポジトリをまたいでそのFunctionへ接続する。
3. 候補が0件または複数件なら、呼び出し元リポジトリに外部参照Functionを作成して接続する。

名前空間、モジュール、class、import、引数型は考慮しない。`client.send`と`send`も異なる名前として扱う。外部参照Functionと`CALLS`は1,000件ずつNeo4jへ書き込む。

### FIXESの解決規則

Commitの`messageHeadline`だけを、大文字小文字を区別せず次の形式で検索する。

```text
fix #12
fixes #12
fixed #12
close #12
closes #12
closed #12
resolve #12
resolves #12
resolved #12
```

キーワードと`#番号`の間は空白である必要がある。PR本文、Issue本文、Commit本文、`owner/repository#12`形式は検索しない。該当Issueが`history-limit`の取得範囲外などでNeo4jに存在しない場合、`FIXES`は作成されない。

## 8. 収集から除外される条件

### リポジトリ

次の場合は収集しない。

* `isArchived=true`で、`--include-archived`を指定していない。
* `--repository`を指定しており、その名前に含まれない。
* Organizationの所有物ではなく、member/collaboratorとしてアクセスできるだけである。

指定した`--repository`が存在しない、またはarchive除外に該当する場合は、他の収集を開始せずエラー終了する。forkとprivateリポジトリは自動除外しない。privateリポジトリはPATに必要な権限が必要である。

### ソースファイル

次の場合はFileノードを作成しない。

* 対応表にない拡張子である。
* サイズが`--max-file-bytes`を超える。デフォルトは1,000,000バイトで、同値は含む。
* パスのいずれかの部分が次の名前に一致する。

```text
.git  .idea  .mypy_cache  .next  .pytest_cache  .tox  .venv  .vscode
build  coverage  dist  node_modules  target  vendor
```

`.gitignore`や独自ignoreファイルは参照しない。シンボリックリンク、生成物、テストコードを種類だけで除外する規則もない。

### Manifest

Manifest走査の除外ディレクトリは`.git`だけである。ソース解析とは除外規則を共有しないため、`node_modules`、`vendor`、`dist`などに対応名のManifestがあれば解析対象になる。また、`--max-file-bytes`はManifestに適用しない。

次の場合はDependencyを作成しない。

* Manifest名が対応する3種類ではない。
* JSON、TOML、XMLとして解析できない、または読み込みに失敗した。この場合Manifest自体はerror付きで保存する。
* 対応Manifest内でも、解析対象外のセクションにだけ依存が記載されている。

## 9. 差分更新と置換

既存`Repository`の次の3値がGitHubの列挙結果とすべて一致する場合、変更なしとしてGitHub詳細取得、clone、ソース解析、Manifest解析、Neo4j置換を省略する。

* `updatedAt`
* `pushedAt`
* デフォルトブランチ先頭の`head_oid`

いずれかが違う場合は再収集する。書き込みトランザクション内で、その`repository`プロパティを持つRepository以外の既存ノードを`DETACH DELETE`し、そのRepositoryから出る既存`DEPENDS_ON`を削除してから、新しいノードとエッジを作成する。これにより削除されたソース、履歴範囲から外れたCommit/PR/Issue、削除された依存宣言は残らない。

Userは`repository`プロパティを持たないため置換時に削除されない。Organizationから削除・移管されたリポジトリを検出してNeo4jから削除する処理もない。`--repository`による部分収集では、選択されなかったリポジトリの直前の収集結果を`CollectionIndex`へ引き継ぐ。

1件以上を再収集した場合だけ`CALLS`を再解決する。変更なしの実行では既存`CALLS`を保持する。

## 10. エラー時の動作

| エラー | 動作 |
| --- | --- |
| `GH_PAT`またはowner未指定 | 収集前に終了コード2で終了 |
| `history-limit`が範囲外 | 収集前に終了コード2で終了 |
| Organization列挙失敗 | 収集前に終了コード1で終了 |
| 個別リポジトリのAPI取得・clone・解析・DB書き込み失敗 | そのリポジトリをfailedとして記録し、他のリポジトリを継続 |
| parser workerが非ゼロ終了 | error付きFileを作成し、シンボルなしで継続 |
| parser workerが60秒でタイムアウト | そのリポジトリ全体をfailedとして扱う |
| Tree-sitterが構文エラーを含むツリーを返す | `parse_has_error=true`でFileと取得できたシンボルを保存 |
| Manifest解析失敗 | error付きManifestを保存し、Dependencyなしで継続 |

個別失敗が1件でもあれば全体の終了コードは1となる。Web UIから実行した場合も失敗内容を収集ステータスと`CollectionIndex`へ残す。

Neo4j接続開始時は2秒間隔で最大30回ready checkを行う。ドライバーのトランザクション再試行時間はデフォルト120秒である。

## 11. 既知の制約

* ソース解析は静的な構文抽出であり、型推論、名前解決、ビルド、マクロ展開を行わない。
* 言語別の専用ASTマッピングがないため、対応拡張子でもFunction、Class、Callを網羅できない言語がある。
* `CALLS`のOrganization全体での名前一致は、同名関数が複数あると外部参照へ退避する。実際の呼び出し先を保証しない。
* 浅いcloneはデフォルトブランチの作業ツリーだけを取得する。GitHub上の履歴はGraphQL APIから別に収集する。
* Git submoduleの初期化、Git LFSオブジェクトの明示的な取得、ビルド生成は行わない。
* Commit、Pull Request、Issueは最大100件のサンプルであり、全履歴ではない。
* TopicとLabelは各30件、Commitに関連するPull Requestは各10件までである。
* `DEPENDS_ON`は名前のヒューリスティック一致であり、package registryのメタデータやlockfileを参照しない。
* lockfile、Gradle、Go modules、Cargo、Bundler、Composerなどは現在解析しない。

