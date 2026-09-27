# TOOL NEXUS 開発仕様書

> このファイルはLIST NEXUSの開発中に設計した内容をまとめたものです。
> `git init` してこのSPEC.mdを最初のコミットに含めるところから始めてください。

## 1. プロジェクト概要

### 1.1 プロジェクト名

**TOOL NEXUS**

ローカルで動かしている複数のStreamlitツール（および任意の実行ファイル）を、
登録・起動・停止・状態確認するためのローカル専用ランチャー。

自作ツールが増えるほど「どこに何があるか」「今どれが起動しているか」が分からなくなる。
本アプリはその入口を1つにまとめる。

LIST NEXUS（SharePoint Listリンク管理）の姉妹ツールであり、UIの骨格と実装方針を共有する。

### 1.2 解決したい問題

* ツールごとに `start.bat` があり、どれがどれか分からない
* 起動しているか確認するにはタスクマネージャを見るしかない
* ポートが衝突する、どのポートを使ったか忘れる
* 複数ツールをまとめて起動したい

---

## 2. 初期ディレクトリ構成

```text
tool-nexus/
├─ app.py
├─ database.py
├─ models.py
├─ repositories.py
├─ process_utils.py      … プロセス起動・停止・探索
├─ health.py             … 死活監視
├─ port_utils.py         … 空きポートの割当
├─ styles.py
├─ constants.py
├─ requirements.txt
├─ README.md
├─ SPEC.md
├─ .gitignore
├─ .gitattributes
├─ .streamlit/config.toml
├─ setup.bat
├─ start-tool-nexus.bat
├─ data/
│  └─ .gitkeep
└─ tests/
   ├─ __init__.py
   ├─ test_repositories.py
   ├─ test_port_utils.py
   ├─ test_process_utils.py
   └─ test_health.py
```

SQLiteは `data/tool_nexus.sqlite3` に作成し、Git管理対象外とする。

---

## 3. 技術構成

* Python 3.12以上
* Streamlit
* SQLite（標準ライブラリの `sqlite3`）
* 標準ライブラリの `subprocess` / `socket` / `urllib.request`
* pytest

`requirements.txt` は `streamlit` と `pytest` のみとする。

`psutil` は**使わない**。社用PCへのパッケージ追加を避けるため、プロセス情報は
PowerShell（`Get-CimInstance` / `Get-NetTCPConnection`）の呼び出しで取得する。

### 3.1 対象環境

* Windows 11
* ローカルPC、単一ユーザー
* `127.0.0.1` のみで待ち受ける

---

## 4. 基本方針

### 4.1 担当すること

* ツールの登録・編集・削除（コマンド、作業ディレクトリ、ポート、ログ出力先）
* 起動 / 停止 / まとめて起動
* 状態表示（起動中 / 停止中）
* 起動中のStreamlitの自動検出とワンクリック登録
* ポートの自動割当

### 4.2 担当しないこと

* 常駐しての自動再起動・監視デーモン
* プロセスツリーの完全な管理
* リモートホストの管理
* Docker / Compose
* Node.js、npm

### 4.3 監視に関する割り切り

**死活監視は画面を開いている間だけ行う。**

ランチャーの目的は「使いたいときに起動できること」であり、
誰も見ていない時間に落ちていても実害がない。
常駐監視をやめることで、設計と実装が大幅に軽くなる。

---

## 5. UIコンセプト

LIST NEXUSと同じサイバーパンク風ダークテーマ、同じ画面構造を用いる。

```text
┌──────────────────────────────────────────────┐
│ TOOL NEXUS   [すべて][Streamlit][その他]  [⚙] │ ← 上部バー（全幅）
├───────────┬──────────────────────────────────┤
│ 左ナビ     │ 検索 / 追加 / まとめて起動        │
│ 種別       │ ─────────────────────────────── │
│ 状態       │ ● 在庫チェッカー  8502  [開く][停止]│
│ フィルター │ ○ 集計ツール      8503  [起動]     │
└───────────┴──────────────────────────────────┘
```

* 上部バーは全幅。その下を左ナビと本文の2列にする（Streamlitのサイドバーは使わない）
* 1行1ツールのリスト表示
* 行あたりのウィジェットは最小限にする（後述）

---

## 6. 機能要件

### 6.1 ツールの登録・編集・削除

登録項目：

| 項目 | 必須 | 説明 |
| --- | --- | --- |
| 名前 | ○ | 表示名 |
| 作業ディレクトリ | ○ | `cwd` に渡す。存在チェックを行う |
| 起動コマンド | ○ | 例: `.venv\Scripts\python.exe -m streamlit run app.py` |
| ポート | | 空欄なら自動割当（6.5） |
| 種別 | ○ | `streamlit` / `exe` |
| 死活監視モード | ○ | `http` / `process` / `none` |
| ログ出力先 | | 既定は作業ディレクトリ配下の `tool-nexus.log` |
| 自動起動 | | 「まとめて起動」の対象にするか |
| 説明 | | 自由記述 |

### 6.2 起動

`subprocess.Popen` でデタッチ起動する。

```python
argv = shlex.split(command, posix=False)   # Windowsのパスを壊さない
if port and "--server.port" not in argv:
    argv += ["--server.port", str(port)]
if "--server.address" not in argv:
    argv += ["--server.address", "127.0.0.1"]
if "--server.headless" not in argv:
    argv += ["--server.headless", "true"]

proc = subprocess.Popen(
    argv,
    cwd=directory,
    stdout=log_file, stderr=subprocess.STDOUT,
    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
)
```

必ず守ること：

* `shell=True` と `eval` / `os.system` を使わない
  （PIDが取れない、エスケープ事故、入力文字列がそのまま実行される）
* `stdout=subprocess.PIPE` にしない
  （誰も読まないとバッファが詰まり子プロセスが停止する）。ログファイルか `DEVNULL` へ
* 種別が `streamlit` のときだけ `--server.*` を自動付与する
* 既にコマンドに同じ引数が書かれていれば二重付与しない

### 6.3 停止

**`Popen.pid` は親プロセスであり、ポートを持つのは子プロセスである。**

実測（LIST NEXUSで確認）：

```text
bash(27208) ─ python(42288)   ← Popen.pid が返すのはこれ。ポートを持たない
                └ python(12380)   ← 実際に 8501 をLISTENしている
```

したがって停止は必ず子を含めて行う。

```bat
taskkill /PID <親PID> /T /F
```

停止後はヘルスチェックまたはポート確認で解放を確認する。

### 6.4 状態表示（死活監視）

| モード | 方法 | 分かること |
| --- | --- | --- |
| `http` | `GET http://127.0.0.1:<port>/_stcore/health` | 応答すること。**対象ツールの改造が不要** |
| `process` | PIDの存在確認（起動時刻と突き合わせる） | 起動していること。固まっていても生存扱い |
| `none` | 監視しない | — |

実測：`/_stcore/health` は **HTTP 200 "ok" を 1.2ms** で返す（`/healthz` も可）。
1分間隔・10ツールでも1分あたり12ms程度であり、負荷は無視できる。

`process` モードではPIDの再利用による誤判定を避けるため、
起動時に `CreationDate` を保存し、判定時に一致を確認する。

#### 自動更新

Streamlitは画面を開いている間しか動かないため、
**フラグメントで状態表示の部分だけを定期更新する**。

```python
@st.fragment(run_every=settings.health_interval)   # 既定 "60s"
def render_status(tools):
    ...
```

ページ全体を再実行しないこと。全体の再実行は、件数が増えると体感で重くなる
（LIST NEXUSでの実測: ウィジェット641個で描画1.6秒）。

即時確認用の「再チェック」ボタンをフラグメント内に置く。

### 6.5 ポートの自動割当

ポート未指定で登録した場合、空きポートを割り当てて**そのままDBに保存する**。
次回以降は同じポートで起動し、ブックマークやリンクが壊れないようにする。

避けるべき範囲（実機で確認済み）：

* `0-1023` … well-known
* `49152-65535` … Windowsの動的ポート範囲。OSが自動で使うため必ず避ける
  （`netsh int ipv4 show dynamicport tcp` で確認できる）
* 登録済みのポート
* 実際にLISTEN中のポート

既定の割当範囲は **8500-8999**（設定で変更可能）。
空き確認は実際に `bind()` して行う。

```python
def pick_free_port(used: set[int], low: int = 8500, high: int = 8999) -> int:
    for port in random.sample(range(low, high + 1), 100):
        if port in used:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise PortError("空きポートが見つかりませんでした")
```

### 6.6 起動中ツールの検出とワンクリック登録

このPCで動いているStreamlitを列挙し、未登録のものを登録候補として表示する。

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'"   # PID・親PID・コマンドライン
Get-NetTCPConnection -State Listen                          # PID → ポート
```

この2つを突き合わせ、コマンドラインに `streamlit` を含むものを抽出する。
親子両方が現れるため、**ポートを持っている方をポート判定に使い、登録には親のコマンドラインを使う**。

検出結果からは、名前（作業ディレクトリ名）・コマンド・ポートを埋めた状態で登録できる。

### 6.7 まとめて起動

「自動起動」が有効なツールを順に起動する。
既に起動しているものは飛ばす（二重起動しない）。

起動は非同期に見えるが、Streamlitの起動には数秒かかるため、
起動直後は「起動中…」と表示し、ヘルスが通ったら「起動中」に切り替える。

### 6.8 開く・ログ

* 「開く」… `http://127.0.0.1:<port>` を新しいタブで開く（アンカーで描画し、ウィジェットを増やさない）
* 「ログ」… ログファイルの末尾を画面に表示する（数百行程度）

---

## 7. データモデル

### 7.1 toolsテーブル

```sql
CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'streamlit',      -- streamlit | exe
    directory TEXT NOT NULL,
    command TEXT NOT NULL,
    port INTEGER,                                 -- NULL許容（exe用）
    health_mode TEXT NOT NULL DEFAULT 'http',     -- http | process | none
    log_path TEXT NOT NULL DEFAULT '',
    autostart INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    last_pid INTEGER,
    last_started_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

**状態（起動中かどうか）をDBに保存しない。**
保存した瞬間から嘘になる（クラッシュしてもDBは「起動中」のまま）。
現在の状態は毎回ヘルスチェックで取得し、DBには設定と履歴だけを持つ。

`port` をNULL許容にし、`health_mode` を最初から列として持つこと。
この2つがあれば、後からexe対応を足しても移行が不要になる。

### 7.2 settingsテーブル

```sql
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

設定項目：

| キー | 既定値 | 説明 |
| --- | --- | --- |
| `health_interval` | `60s` | 死活監視の間隔 |
| `port_range_low` | `8500` | 自動割当の下限 |
| `port_range_high` | `8999` | 自動割当の上限 |
| `default_log_dir` | 空 | 空ならツールの作業ディレクトリ配下 |
| `health_timeout` | `2.0` | ヘルスチェックのタイムアウト（秒） |

---

## 8. 画面

### 8.1 一覧

1行1ツール。行の構成：

```text
● 在庫チェッカー                    8502   [開く][ログ]  [停止] [編集] [削除]
  C:\dev\tool-a   streamlit   最終起動 2026-09-27 13:45
```

* 状態は色付きの丸（起動中=シアン、停止=グレー、起動中…=黄）
* 「開く」はアンカーで描画する（ウィジェットを増やさない）
* 停止中は「起動」、起動中は「停止」を出す

### 8.2 設定画面

右上の⚙から全画面の設定へ。左ナビ＋本文の2列。

* **ツールの管理** … 並べ替え、まとめて削除
* **検出** … 起動中のStreamlit一覧、ワンクリック登録
* **動作設定** … ヘルス間隔、ポート範囲、ログ既定パス
* **データ** … バックアップ（JSON）、復元、DBの場所

---

## 9. セキュリティ要件

* `127.0.0.1` のみで待ち受ける
* 登録されたコマンドは `shell=False` かつリスト形式で実行する
* `eval` / `exec` / `os.system` を使わない
* 作業ディレクトリと実行ファイルの存在を起動前に確認する
* HTML出力時はユーザー入力をエスケープする
* 認証情報やトークンを保存しない

---

## 10. テスト要件

pytestで以下を検証する。UIの自動テストは必須としない。

### port_utils
* 使用中ポートを避ける
* 登録済みポートを避ける
* 範囲外を返さない
* 空きが無い場合に例外を送出する

### process_utils
* コマンド文字列の分解（Windowsのパスが壊れないこと）
* `--server.port` の自動付与、および二重付与しないこと
* 種別が `exe` のときに `--server.*` を付与しないこと
* 起動コマンドの組み立て（実際にプロセスは起こさず、argvを検証する）

### health
* HTTPモード: 応答する / しない / タイムアウト
* processモード: PID存在、PID再利用の検出（起動時刻の不一致）
* noneモード: 常に不明を返す

### repositories
* 一時SQLite DBを用いたCRUD
* ポート重複の検出
* 設定値の読み書きと既定値

---

## 11. LIST NEXUSからの流用方針

同じ作者・同じ思想のため、以下はほぼそのまま持ち込める。

| 流用するもの | 内容 |
| --- | --- |
| `database.py` | 接続、PRAGMA、トランザクション、移行の書き方 |
| `styles.py` | 配色、行レイアウト、列幅固定のCSS、エスケープ方針 |
| 画面構造 | 上部バー＋左ナビ＋本文の2列、設定画面 |
| 表示の制約 | 1行あたりのウィジェットを3個以内に抑える |
| ドキュメント | README構成、実装上の判断メモの書き方 |

**コピーせず作り直すもの**: モデルとリポジトリ（対象が違うため）。

---

## 12. 実測値（設計の根拠）

LIST NEXUS（Streamlit 1.60）で計測した値。

| 項目 | 値 |
| --- | --- |
| Streamlit 1プロセスのメモリ | 実体 37.4MB + 親 4.9MB ≒ **42MB** |
| 待機時のCPU | 1コアの約1.8%（ブラウザを閉じればほぼ0） |
| `/_stcore/health` の応答 | HTTP 200 / **1.2ms** |
| 全体再実行のコスト | ウィジェット641個で約1.6秒 |
| Windowsの動的ポート範囲 | 49152 から 16384個 |

10ツール同時起動でもメモリは約400MB。立ち上げっぱなしを避けるほどの負荷ではない。

---

## 13. 実装優先順位

### Phase 1
* SQLite、モデル、リポジトリ
* ツールのCRUD
* 起動・停止（親子PIDの扱いを含む）

### Phase 2
* ヘルスチェック（httpモード）とフラグメントによる自動更新
* 一覧表示、開く、ログ表示

### Phase 3
* ポート自動割当
* まとめて起動
* 起動中ツールの検出とワンクリック登録

### Phase 4
* 設定画面
* processモード / exe対応
* バックアップと復元
* README、setup.bat、start-tool-nexus.bat

---

## 14. 既知の制約（README記載予定）

* 画面を開いている間しか監視しない
* `process` モードでは「固まっているが生きている」状態を検出できない
* 管理できるのはこのPC上のプロセスのみ
* 停止は強制終了（`taskkill /F`）であり、ツール側の終了処理は走らない
* 起動はデタッチするため、TOOL NEXUSを閉じてもツールは動き続ける（意図した挙動）

---

## 15. 実装を依頼するときの指示文

```text
このディレクトリのSPEC.mdを読み、TOOL NEXUSを実装してください。

Python、Streamlit、SQLiteを使用してください。psutilを含む外部パッケージは追加しないでください。
プロセス情報が必要な場合はPowerShellの呼び出しで取得してください。

subprocessは必ずリスト形式・shell=Falseで実行し、eval/os.systemは使わないでください。
Popen.pidは親プロセスであり、ポートを持つのは子プロセスである点に注意してください。

作業開始時にGitの状態を確認し、Gitリポジトリでなければ初期化してください。
実装は意味のある単位でコミットし、完了時にpytestを実行してgit statusをクリーンにしてください。

UIはLIST NEXUS（姉妹プロジェクト）と同じ構造・配色にしてください。
1行あたりのウィジェット数を抑え、定期更新はst.fragmentで部分的に行ってください。
```
