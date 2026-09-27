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
├─ platform_ops.py       … OS依存機能の切り替え（3.2）
├─ os_windows.py         … Windows実装
├─ os_linux.py           … Linux実装
├─ launch_assist.py      … ファイル選択と起動方式の推測（6.9）
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
Windowsでは PowerShell（`Get-CimInstance` / `Get-NetTCPConnection`）、Linuxでは `/proc` から取得する。

### 3.1 対象環境

* Windows 11（社用PC・メイン）
* Ubuntu（自宅の開発機）
* ローカルPC、単一ユーザー
* `127.0.0.1` のみで待ち受ける

### 3.2 OS対応

フレームワークは作らず、**OSに依存する機能だけを1枚の層で切り替える**。

```text
platform_ops.py   … 実行中のOSに応じて下のどちらかを選び、同じ関数名で公開する
├─ os_windows.py  … PowerShell / taskkill / Windows Forms
└─ os_linux.py    … /proc / シグナル / zenity・kdialog
```

| 機能 | Windows | Linux |
| --- | --- | --- |
| 起動時刻の取得・照合 | `Get-CimInstance` の `CreationDate` | `/proc/<pid>/stat` の starttime ＋ `/proc/stat` の btime |
| プロセス一覧 | `Get-CimInstance Win32_Process` | `/proc/<pid>/{stat,cmdline,comm}` |
| LISTEN中のポート | `Get-NetTCPConnection` | `/proc/net/tcp{,6}` の inode と `/proc/<pid>/fd` の突き合わせ |
| 起動（切り離し） | `CREATE_NO_WINDOW` | `start_new_session=True`（新しいプロセスグループ） |
| 子ごと停止 | `taskkill /T /F` | 子孫にSIGTERM → 猶予後に残っていればSIGKILL |
| コマンドの分解 | `shlex`（`posix=False`）＋両端のクォート除去 | `shlex`（`posix=True`） |
| venvのpython | `Scripts\python.exe` | `bin/python` |
| venvが無いときのpython | `py`（無ければ `python`） | `python3` |
| ファイル選択 | PowerShellの Windows Forms | `zenity`（無ければ `kdialog`）。どちらも無ければその旨を表示 |
| `exe` 種別として選べるもの | `.exe` | 実行権限のあるファイル（`.py` 以外） |

OSに依存しないもの（共通で使う）：PID照合（`verify_pid`）、親子の判定、検出、ヘルスチェック、
ポート割当、起動方式の推測、DB、画面。

* 両方のOS用モジュールは**どちらのOSでもimportできる**ようにする（OS専用のモジュールは関数の中でimportする）。
  Linux の `/proc` 解析は、テストで一時ディレクトリに作った疑似 `/proc` を読ませて、Windows上でも検証する
* Linuxの停止は、Windowsと違っていきなり強制終了せず、まずSIGTERMで終了処理の機会を与える
* 停止前の起動時刻の照合（6.3）はLinuxでも同じく行う（PIDはLinuxでも再利用される）
* **実行ファイルのパスはシンボリックリンクをたどらずに絶対パス化する**（`Path.resolve()` を使わない）。
  Linuxのvenvの `python` はシステムの `python3.x` へのリンクで、たどるとvenvの外のPythonになり、
  venvに入れたパッケージが見えなくなる（WSLのUbuntu 24.04で確認。Windowsのvenvは実ファイルなので起きない）
* **Linuxでは終了した子プロセスがゾンビとして残る**。ゾンビは `/proc` に残り起動時刻も同じなので、
  そのままでは照合が一致して「生きている」と誤判定される。起動した `Popen` を保持して状態確認のたびに回収し、
  念のため状態 `Z` のプロセスは存在しないものとして扱う
* 実プロセスでの確認：Windows 11、WSLのUbuntu 24.04、GitHub Actions（windows-latest / ubuntu-latest）で
  結合テストを含む全テストが通ることを確認済み

#### 検証

* GitHub Actions で Windows と Ubuntu の両方で `pytest` を実行する
* Ubuntu では、確認用のStreamlitを実際に起動 → ヘルスチェック → 検出 → 停止する結合テストも実行する
  （Linuxの実機が手元に無い状態でも、OS依存部分を実プロセスで確かめるため）

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
| 起動コマンド | ○ | 例: `.venv\Scripts\python.exe -m streamlit run app.py`。`{port}` はポートに置き換える（6.2） |
| ポート | | 空欄なら自動割当（6.5） |
| 種別 | ○ | `streamlit` / `web` / `python` / `exe`（下表） |
| 死活監視モード | ○ | `http` / `process` / `none` |
| ログ出力先 | | 既定は作業ディレクトリ配下の `tool-nexus.log` |
| 自動起動 | | 「まとめて起動」の対象にするか |
| 説明 | | 自由記述 |

種別は「フレームワーク」ではなく**TOOL NEXUSから見た動き方**で分ける。
FlaskとFastAPIは動き方が同じ（指定ポートでHTTPを待ち受ける）ため、どちらも `web` とする。

| 種別 | 対象 | `--server.*` 自動付与 | HTTP監視の確認先 | 死活監視の既定 |
| --- | --- | --- | --- | --- |
| `streamlit` | Streamlit | ○ | `/_stcore/health` が200 | `http` |
| `web` | Flask、FastAPI（uvicorn）など | — | `/` に何らかのHTTP応答 | `http` |
| `python` | 素のPythonスクリプト | — | `/` に何らかのHTTP応答 | `process` |
| `exe` | 実行ファイル | — | `/` に何らかのHTTP応答 | `process` |

`kind` 列は文字列なので、種別を増やしてもDBの移行は要らない（7.1）。

登録時の検証：

* `health_mode == "http"` のときはポート必須（未入力はエラー）。
  黙って `process` に切り替えることはしない（ユーザーが気づかないまま監視の強度が落ちるため）
* 登録フォームで種別を `python` / `exe` にしたら、死活監視モードの既定値を `process` にする
  （そもそも上記のエラーを踏まないようにする）
* 起動コマンドに `{port}` を含むのにポートが空欄の場合もエラー（自動割当できる場合は先に割り当てる。6.5）

### 6.2 起動

`subprocess.Popen` でデタッチ起動する。

```python
argv = split_command(command)
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
    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
)
```

**`DETACHED_PROCESS` は使わない**（実機で確認）。
`CREATE_NO_WINDOW` と併用すると `CREATE_NO_WINDOW` が無視され、親（venvの `python.exe` はリダイレクタ）が
コンソール無しで起動する。その子の `python.exe` は自分用のコンソールを新規に作って**ウィンドウを表示し**、
ユーザーがそれを閉じるとツールが落ちる。
`CREATE_NO_WINDOW` だけなら非表示のコンソールが作られて子に引き継がれ、
TOOL NEXUS側のコンソールとも切り離されるため、TOOL NEXUSを閉じてもツールは動き続ける。

`argv[0]` が相対パス（例: `.venv\Scripts\python.exe`）の場合は、作業ディレクトリ基準で絶対パスに解決してから渡す。
Windowsの `CreateProcess` は相対パスの実行ファイルを `cwd` 引数ではなく呼び出し元のカレントディレクトリ基準で探すため。

起動直後にPIDの起動時刻（`CreationDate`）を取得し、`last_pid` と
`last_pid_created_at` に保存する（6.3・6.4の照合に使う）。

#### コマンド文字列の分解

`posix=True` にすると `C:\dev\x` のバックスラッシュが消えるため、`posix=False` を使う。
ただし `posix=False` はトークン両端のクォートを残す
（`"C:\Program Files\...\python.exe"` が `"` 付きのままになり、`Popen` が実行ファイルを見つけられない）。
そのため各トークンの両端のクォートを外す。

```python
def split_command(command: str) -> list[str]:
    tokens = shlex.split(command, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]
```

* 閉じていないクォートは `ValueError` になる。登録時の検証で弾く
* 既知の制約: `--name="a b"` のようにトークン途中から始まるクォート内の空白では分割されてしまう。
  空白を含む値は `--name "a b"` と別トークンに分けて書く

必ず守ること：

* `shell=True` と `eval` / `os.system` を使わない
  （PIDが取れない、エスケープ事故、入力文字列がそのまま実行される）
* `stdout=subprocess.PIPE` にしない
  （誰も読まないとバッファが詰まり子プロセスが停止する）。ログファイルか `DEVNULL` へ
* 種別が `streamlit` のときだけ `--server.*` を自動付与する
* 起動コマンド中の `{port}` は、全種別で登録済みのポートに置き換える
  （例: `python -m flask --app app run --port {port}`）。ポートをコマンドとポート欄に二重に書かずに済み、
  食い違いで監視が外れることもなくなる。ポート未登録で `{port}` があれば起動しない
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

**停止の前に、PIDの起動時刻を必ず照合する。**

ツールが落ちた後にOSがPIDを再利用していると、`taskkill /T /F` は
無関係なプロセスをツリーごと強制終了してしまう。
そのため `taskkill` を実行する前に、`last_pid` の現在の起動時刻が
`last_pid_created_at` と一致することを確認する。

* 一致した場合のみ `taskkill` を実行する
* 一致しない・PIDが存在しない・起動時刻が取れない場合は停止せず、
  「対象プロセスを特定できませんでした」と表示する。
  その上で、ポートから（6.6の検出と同じ方法で）プロセスを引き直すか、手動対応に倒す

#### ポートからの引き直し

TOOL NEXUSの外で起動されたツールや、起動記録が消えたツールは、ポートから停止対象を特定できる。

1. ツールのポートでLISTENしているPIDを `Get-NetTCPConnection` で取得する
2. 親をたどり、コマンドラインが同じツールのものとみなせる最上位の祖先を停止対象にする
   （6.6と同じ規則。venvの `python.exe` はリダイレクタで、子が本体）
3. **対象のPID・コマンドラインを画面に出し、ユーザーの確認を取ってから停止する**
   （ポートは記録ではなく現在の状態から引くため誤爆はしにくいが、強制終了なので確認を挟む）
4. 停止は通常の停止と同じく、直前に取得した起動時刻で照合してから `taskkill /T /F`

この照合は `process` モードの死活監視（6.4）とまったく同じ仕組みであり、
共通関数 `verify_pid(pid, created_at)` として1か所に実装し、テストも1か所にまとめる。

停止後はヘルスチェックまたはポート確認で解放を確認する。

### 6.4 状態表示（死活監視）

| モード | 方法 | 分かること |
| --- | --- | --- |
| `http` | `GET http://127.0.0.1:<port>/_stcore/health` | 応答すること。**対象ツールの改造が不要**。exeは `/` に何らかのHTTP応答（404等を含む）があれば生存 |
| `process` | PIDの存在確認（起動時刻と突き合わせる） | 起動していること。固まっていても生存扱い |
| `none` | 監視しない | — |

実測：`/_stcore/health` は **HTTP 200 "ok" を 1.2ms** で返す（`/healthz` も可）。
1分間隔・10ツールでも1分あたり12ms程度であり、負荷は無視できる。

`process` モードではPIDの再利用による誤判定を避けるため、
起動時に `CreationDate` を `last_pid_created_at` へ保存し、判定時に一致を確認する
（6.3の停止前照合と共通の `verify_pid` を使う）。

`Get-CimInstance Win32_Process` の `CreationDate` はCIM datetime
（例: `20260927134501.123456+540`）であり、取得方法や環境によって文字列表現が揺れる。
**保存・比較ともに秒精度のISO 8601（例: `2026-09-27T13:45:01+09:00`）に正規化してから一致判定する。**
生の文字列同士を直接比較すると偽陰性（生きているのに停止扱い、停止できない）が出る。

#### 「起動中…」の判定

状態はDBに保存しない（7.1）。「起動中…」は `last_started_at` から導出する。
`last_started_at` は状態ではなく記録なので、この方針と矛盾しない。

| 条件 | 表示 |
| --- | --- |
| ヘルス通過 | 起動中 |
| ヘルス未通過 かつ `last_started_at` から30秒以内 | 起動中…（黄） |
| ヘルス未通過 かつ 30秒超（直近に起動操作あり） | 「起動できていない可能性があります」＋ログを開く導線 |
| ヘルス未通過 かつ 起動操作の記録が古い / 無い | 停止 |

30秒を超えても「起動中…」のまま無言で回り続けることはしない。

#### 自動更新

Streamlitは画面を開いている間しか動かないため、
**フラグメントで状態表示の部分だけを定期更新する**。

```python
@st.fragment(run_every="3s")          # 再描画の間隔（固定）
def render_tool_list(...):
    healths = check_tools(...)        # ヘルスチェックは必要なときだけ
```

ページ全体を再実行しないこと。全体の再実行は、件数が増えると体感で重くなる
（LIST NEXUSでの実測: ウィジェット641個で描画1.6秒）。

**再描画の間隔とヘルスチェックの間隔を分ける**（実機で確認）。

* フラグメントは固定の短い間隔（3秒）で再描画する
* 実際のヘルスチェックは次のときだけ行い、それ以外は前回の結果から状態を求める（`health.is_check_due`）
  * 設定 `health_interval`（既定60秒）が経過した
  * 「起動中…」のツールがある（ヘルスが通れば最長3秒で「起動中」に切り替わる）
  * 再チェック・起動・停止の直後、または未確認のツールがある
* `run_every` を状況に応じて切り替える方式（フラグメントの中から `st.rerun(scope="app")` で登録し直す）は採らない。
  前回の描画が消えずに行が重複して残った

起動/停止ボタンは状態によって切り替わるためフラグメントの中に置き、押したらフラグメントだけを再実行する。
編集・ログのダイアログはアプリ全体の描画で開くため、そのボタンだけ `st.rerun(scope="app")` する。
ダイアログは `on_dismiss` で状態を戻す（×で閉じた後、次の全体再実行で開き直さないように）。

通知メッセージは再描画で消えないよう表示期限を持たせる（成功6秒、警告・エラー30秒）。

即時確認用の「再チェック」ボタンをフラグメント内に置く。

### 6.5 ポートの自動割当

ポート未指定で登録した場合、空きポートを割り当てて**そのままDBに保存する**。
次回以降は同じポートで起動し、ブックマークやリンクが壊れないようにする。

* **割当は保存時に行う**（起動時ではない）。起動のたびに振り直すと、ブラウザのブックマークや
  LIST NEXUSに登録したリンクが毎回壊れる
* **自動割当は、割り当てたポートで待ち受けることが保証できる場合だけ**行う。
  具体的には `kind == "streamlit"`（`--server.port` を付与する）か、起動コマンドに `{port}` を含む場合。
  それ以外は振ったポートで実際に待ち受ける保証が無く、監視すると永遠に
  「起動できていない可能性」が出続けるため
* 処理の順番は「自動割当 → 検証」。自動割当できる場合はポートが空欄なら先に割り当て、
  その後に6.1の検証（httpならポート必須）を通す
* 自動割当できない（`{port}` を含まない `web` / `python` / `exe`）かつ `health_mode == "http"` かつポート空欄は、
  **検証エラーで弾く**。ユーザーに実際の待ち受けポートを入力させる
* 編集時にポート欄を空にして保存した場合は「振り直し」として扱う。
  ただし **既存のブックマークやリンクが切れる旨の注意** を表示する

避けるべき範囲（実機で確認済み）：

* `0-1023` … well-known
* `49152-65535` … Windowsの動的ポート範囲。OSが自動で使うため必ず避ける
  （`netsh int ipv4 show dynamicport tcp` で確認できる）
* 登録済みのポート
* 実際にLISTEN中のポート
* **予約済みポート**（下記）

#### 予約済みポート

TOOL NEXUS自身のポートを管理対象ツールに割り当ててはならない。
`bind()` による確認は、TOOL NEXUSが**起動中**なら自分のポートを弾けるが、
**停止中**に別のツールへそのポートを割り当てると、次回TOOL NEXUS自身が起動できなくなる
（ランチャーが起動できないのは致命的）。

* TOOL NEXUS自身は割当範囲の外の **8499** で動かす（`.streamlit/config.toml`）。
  範囲を設定で変更しても安全なように、自身のポートは常に割当候補から除外する
* 設定 `reserved_ports` を持ち、割当候補から常に除外する。
  既定は **8501**（LIST NEXUSが使用。同じPCで両方動かす前提）。
  ユーザーが手動で固定しているポートもここに足せる

既定の割当範囲は **8500-8999**（設定で変更可能）。
空き確認は実際に `bind()` して行う。

候補を抽出（サンプリング）せず、範囲内の未使用ポートを全件シャッフルして順に試す。
サンプリングは範囲が狭いと `ValueError` になり、また「空きがあるのに見つからない」原因にもなる。

```python
def pick_free_port(used: set[int], low: int = 8500, high: int = 8999) -> int:
    candidates = [p for p in range(low, high + 1) if p not in used]
    random.shuffle(candidates)
    for port in candidates:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise PortError("空きポートが見つかりませんでした")
```

割当範囲の設定を保存するときに検証する：

* `low <= high`
* 両方が `1024-49151` の範囲内（49152以降はWindowsの動的ポート範囲なので不可）

### 6.6 起動中ツールの検出とワンクリック登録

このPCで動いているStreamlitを列挙し、未登録のものを登録候補として表示する。

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'"   # PID・親PID・コマンドライン
Get-NetTCPConnection -State Listen                          # PID → ポート
```

この2つを突き合わせ、コマンドラインに `streamlit` を含むものを抽出する。
親子両方が現れるため、**ポートを持っている方をポート判定に使い、登録には親のコマンドラインを使う**。
親は `python.exe` とは限らない（`streamlit.exe` や `uv.exe` の子として動くことがある）ため、
プロセス名では絞らず、コマンドラインで判定する。

検出結果からは、名前（作業ディレクトリ名）・コマンド・ポートを埋めた状態で登録フォームを開く。
登録はフォームで確認してから行う。

* 作業ディレクトリはWMIから取得できない。コマンドライン中のスクリプトが絶対パスならその親、
  実行ファイルがvenv配下（`<root>\.venv\Scripts\python.exe`）ならその `<root>` を推測し、
  どちらも取れなければ空欄にしてユーザーに入力させる
* TOOL NEXUS自身と、登録済みのポートで動いているものは候補から除く

### 6.7 まとめて起動

「自動起動」が有効なツールを順に起動する。
既に起動しているものは飛ばす（二重起動しない）。

起動は非同期に見えるが、Streamlitの起動には数秒かかるため、
起動直後は「起動中…」と表示し、ヘルスが通ったら「起動中」に切り替える（判定は6.4）。

### 6.8 開く・ログ

* 「開く」… `http://127.0.0.1:<port>` を新しいタブで開く（アンカーで描画し、ウィジェットを増やさない）
* 「ログ」… ログファイルの末尾を画面に表示する（数百行程度）

### 6.9 ファイル選択と起動方式の推測

パスやコマンドの手入力をなくすため、登録フォームに「ファイルから入力」と「フォルダを選ぶ」を置く。

#### ファイル選択

ブラウザはローカルファイルの**パスを渡せない**ため、`st.file_uploader` は使えない。
TOOL NEXUSはサーバーとブラウザが同じPCで動くので、**サーバー側からWindows標準のダイアログ**を出す。

* PowerShellの `System.Windows.Forms.OpenFileDialog` / `FolderBrowserDialog` を使う
  （外部パッケージ不要。プロセス情報をPowerShellで取る方針と揃える。tkinterはStreamlitのスレッドから呼ぶと不安定）
* ブラウザの裏に隠れないよう、最前面のフォームを親にして開く
* 選べるのは `.py` と `.exe`。`.bat` / `.cmd` は対象外（cmd.exe経由の引数エスケープの問題があり、
  `shell=False` の方針とも合わないため）

#### 推測

選んだファイルから次を推測し、**フォームの入力欄に入れるだけ**にする。
最終的なコマンドは必ず画面に表示し、ユーザーが確認・修正してから登録する（推測は外れうる）。

1. **プロジェクトのルート**：ファイルの場所から上へたどり、`pyproject.toml` / `uv.lock` /
   `requirements.txt` / `.venv` / `.git` のいずれかがある最初のフォルダ。無ければファイルのフォルダ。
   作業ディレクトリにする
2. **Python環境**（ファイルの場所からルートまでをたどる）

   | 見つかったもの | 使うもの |
   | --- | --- |
   | `pyvenv.cfg` と `Scripts\python.exe` を持つフォルダ（`.venv` / `venv` / `env` を優先。名前は問わない） | そのvenvの `python.exe` |
   | `uv.lock` があり、venvが無い | `uv run python`（初回は同期で時間がかかる旨を表示する） |
   | どれも無い | `py`（無ければ `python`）。`python` はWindowsストアのエイリアスのことがあるため `py` を優先 |

   `uv.lock` とvenvが両方あればvenvを直接使う。`uv run` は起動のたびに同期が走ることがあり、
   30秒を超えると「起動できていない可能性」と誤表示されるため
3. **種別とコマンド**：ファイルの中身を読んで判定する（**実行はしない**）

   | 判定 | 種別 | コマンド |
   | --- | --- | --- |
   | `import streamlit` / `from streamlit` | `streamlit` | `<python> -m streamlit run <file>` |
   | `Flask(` | `web` | `<python> -m flask --app <file> run --host 127.0.0.1 --port {port}` |
   | `FastAPI(` | `web` | `<python> -m uvicorn <module>:<変数名> --host 127.0.0.1 --port {port}` |
   | それ以外の `.py` | `python` | `<python> <file>` |
   | `.exe` | `exe` | `<file>`（作業ディレクトリはexeのフォルダ） |

   パスはルートからの相対パスで書き、空白を含むものはクォートする。
   `{port}` を含むコマンドは自動割当の対象になる（6.5）
4. **名前**：ルートのフォルダ名

---

## 7. データモデル

### 7.1 toolsテーブル

```sql
CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'streamlit',      -- streamlit | web | python | exe
    directory TEXT NOT NULL,
    command TEXT NOT NULL,
    port INTEGER,                                 -- NULL許容（exe用）
    health_mode TEXT NOT NULL DEFAULT 'http',     -- http | process | none
    log_path TEXT NOT NULL DEFAULT '',
    autostart INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    last_pid INTEGER,
    last_pid_created_at TEXT,                     -- 秒精度ISO 8601に正規化
    last_started_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

**状態（起動中かどうか）をDBに保存しない。**
保存した瞬間から嘘になる（クラッシュしてもDBは「起動中」のまま）。
現在の状態は毎回ヘルスチェックで取得し、DBには設定と履歴だけを持つ。

`last_pid` と `last_pid_created_at` は常に対で保存・消去する。
PID単体では再利用を見分けられないため、片方だけを信用しない。

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
| `reserved_ports` | `8501` | 自動割当から常に除外するポート（カンマ区切り）。TOOL NEXUS自身の8499は設定に関わらず常に除外 |

ポート範囲は保存時に `low <= high` かつ両方 `1024-49151` を検証する（6.5）。

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
* 行のウィジェットは「起動/停止」「ログ」「編集」の3つにする。**削除は編集ダイアログの中**に置く
  （3個以内の制約のため。誤操作の防止にもなる）
* ヘルスチェックはプロキシを通さない（社用PCのプロキシ設定で 127.0.0.1 への確認が外に出ないように）

### 8.3 実装上の注意（LIST NEXUSでの実測・やり直しから）

* **列幅はCSSで固定する**。Streamlitの列は比率配分のため、ウィンドウ幅を変えるとアイコン列が縮む
  （LIST NEXUSでは幅1324pxでアイコン列が20pxになり、37pxのボタンがはみ出した）。
  アイコン列は `flex: 0 0 2.3rem !important`、伸縮させる列は `flex: N 1 0 !important` と
  `min-width: 0 !important`。`min-width: 0` が無いと中身の最小幅を主張して後続の列を押し出す。
  `styles.py` の「列幅の固定」ブロックを流用する
* **1行あたりのウィジェットは3個以内**。「開く」はアンカー、ログのパス表示は `st.code`
  （標準のコピーボタンを使う）で0ウィジェットにし、枠は起動/停止などの操作ボタンに使う
  （LIST NEXUS実測: 638ウィジェットで873ms → 3個/行に減らして698ms）
* **`st.fragment` と操作ボタンの同居に注意**。フラグメント内のボタンはフラグメントだけを再実行するため、
  起動ボタンを状態表示フラグメントの中に置くと一覧全体が更新されない可能性がある。
  操作後に全体を更新するなら `st.rerun(scope="app")` を明示するか、ボタンをフラグメントの外に出す。
  どちらにするかは実機で確かめてから決める
* **`st.sidebar` は使わない**。サイドバーは画面上端から始まるため、全幅の上部バーを置けなくなる。
  左ナビは `st.columns` で自作し、`initial_sidebar_state="collapsed"` を指定する
* **`st.data_editor` を使う場合は行をIDで突き合わせる**。並べ替え後に位置で照合すると、編集が別の行に適用される。
  非表示のID列（`column_config={"ID": None}`）を持たせる
* **HTML出力は必ず `escape_html()` を通す**。TOOL NEXUSはコマンド文字列を画面に出すため特に重要
* `.streamlit/config.toml` は `client.toolbarMode = "minimal"`（Deployボタンを消す）、
  `server.address = "127.0.0.1"`、`server.port = 8499`、`browser.gatherUsageStats = false`
* アイコンだけのボタンは `use_container_width=True` にする。列幅をCSSで固定しても、
  ボタンを包む要素が内容幅に縮み、ボタンが細い楕円（幅16px）になる（実機で確認）
* 種別タブは比率ではなく固定幅（文字幅）にし、残りを余白にする。比率配分だと幅1280pxでも「Streamlit」が省略される
* 配色は `styles.py` の `:root` のCSS変数を差し替える。起動中=`--ln-accent`、停止=`--ln-muted`、起動中…=`--ln-warn`

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
* 範囲が100未満でも動作すること（範囲内の空きが1つだけでも見つけること）
* 範囲設定の検証（`low > high`、1024未満、49152以上を拒否）
* 予約済みポート（`reserved_ports` とTOOL NEXUS自身の8499）を返さない
* 自動割当は `streamlit` か `{port}` を含むコマンドのときだけ行い、それ以外はポート空欄のまま（httpなら検証エラー）

### process_utils
* コマンド文字列の分解（Windowsのパスが壊れないこと）
  * 空白入りのクォート付きパス `"C:\Program Files\Python\python.exe" -m streamlit run app.py`
  * クォート無しのバックスラッシュパス（バックスラッシュが残ること）
  * 引数側に空白が含まれる場合
  * 空文字・空白のみ
* `--server.port` の自動付与、および二重付与しないこと
* 種別が `exe` のときに `--server.*` を付与しないこと
* 起動コマンドの組み立て（実際にプロセスは起こさず、argvを検証する）
* `CreationDate` の正規化（CIM datetime・表現揺れを秒精度ISO 8601へ）
* `verify_pid`: 一致 / PID不在 / 起動時刻の不一致（PID再利用）/ 起動時刻が取れない
* 停止: 照合が取れない場合に `taskkill` を実行しないこと
* `{port}` の置き換え、ポート未登録で `{port}` があれば起動しないこと
* 検出: プロセス一覧とLISTEN一覧の突き合わせ（親子の扱い、TOOL NEXUS自身・登録済みの除外、作業ディレクトリの推測）

### launch_assist
* ルートの推測（マーカーが無い場合はファイルのフォルダ）
* venvの検出（`.venv` 以外の名前、`pyvenv.cfg` の無いフォルダを無視）、uv、どちらも無い場合
* 種別の判定（streamlit / Flask / FastAPI / 素のPython / exe）とコマンドの組み立て（相対パス、空白のクォート）

### health
* HTTPモード: 応答する / しない / タイムアウト
* processモード: `verify_pid` を使うこと（照合ロジックのテストは process_utils に集約）
* noneモード: 常に不明を返す
* 「起動中…」の導出: 30秒以内 / 30秒超で「起動できていない可能性」/ 記録が古い

### repositories
* 一時SQLite DBを用いたCRUD
* ポート重複の検出
* 設定値の読み書きと既定値
* 検証: `http` モードでポート未入力はエラー
* `last_pid` と `last_pid_created_at` を対で保存・消去すること

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
* 起動中ツールの検出とワンクリック登録、ポートからの引き直しによる停止
* 種別の追加（`web` / `python`）と `{port}` の置き換え
* ファイル選択と起動方式の推測（6.9）

### Phase 3.5
* OS依存部分の切り出し（3.2）とLinux対応
* GitHub Actions（Windows / Ubuntu）

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
* TOOL NEXUS本体が終了してもツールは動き続けるが、TOOL NEXUSを**プロセスツリーごと**終了させた場合
  （`taskkill /T`、終了時に子も止めるジョブの中で動かしている場合など）はツールも止まる（実機で確認）
* 起動はデタッチするため、TOOL NEXUSを閉じてもツールは動き続ける（意図した挙動）
* 起動コマンドで `--name="a b"` のようにトークン途中のクォートに空白を含める書き方はできない（6.2）
* `.bat` / `.cmd` は登録できない（6.9）
* ファイル選択のダイアログはTOOL NEXUSを動かしているPCの画面に出る（同じPCのブラウザから使う前提）
* 起動方式の推測はファイルの中身の簡単な判定であり、外れることがある。登録前にコマンドを確認する

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
