# harness-pad-4

AI エージェントの「待ち」に気づくための 4 キーのパッド、harness-pad-4 を動かすソフトウェア一式です。

Claude Code などのエージェントは、許可や返事を求めて止まります。別の作業をしていると、
止まっていることに気づけません。このパッドは、セッションごとの状態をキーの LED の色で示し、
何を求められているかをディスプレイに出します。キーを押すと、そのセッションの端末が前に出ます。

**パッドは知らせて、案内するだけです。承認や入力をパッドから送ることはありません。**
判断は、内容を読んだうえで端末で行ってください。

## 必要なもの

| | |
|---|---|
| パッド | harness-pad-4（ファームウェアは書き込み済み） |
| OS | **macOS** か **Windows 10 / 11**（Windows は試験段階。下の「Windows での制限」を参照） |
| Python | 3.9 以上 |
| エージェント | Claude Code（ほかのエージェントは「ほかのエージェントを繋ぐ」を参照） |

macOS では、hidapi を入れるために Homebrew も使います。Windows では Python のほかに要るものはありません。

## セットアップ（macOS）

```sh
brew install hidapi
git clone https://github.com/SoyaManabe/harness-pad-4.git
cd harness-pad-4
./install.sh
```

`install.sh` は次の 3 つを行います。何度実行しても構いません。

1. `daemon/.venv` に Python の専用環境を作り、依存（`hid`）を入れる
2. Claude Code のスキル `/pad` を `~/.claude/skills/pad/` に入れる
3. デーモンを常駐させる（ログイン時に自動で起動し、落ちたら再起動する）

### フックを設定する

エージェントの状態は、Claude Code のフックからデーモンへ届きます。**これだけは手で設定します。**

1. [`daemon/hooks.example.json`](daemon/hooks.example.json) の `"hooks"` の中身を、
   `~/.claude/settings.json` の `"hooks"` に統合する。すでに別のフックがあれば、消さずに並べる
2. Claude Code で `/hooks` を開くか、セッションを始め直す

フックは受け取った内容をデーモン（自分のマシンの `127.0.0.1:8787`）へ送るだけで、
エージェントの動作は変えません。デーモンが止まっていても、エージェントは止まりません。

### 動いているか確かめる

パッドを USB で繋ぎ、Claude Code で何か頼みます。

```sh
./daemon/agentpadctl status
```

```
常駐: 稼働中 (com.keyboard2026.agentpad)
  S1* BUSY     my-project       /Users/you/my-project  [8b278297]
  S2  (空き)
  S3  (空き)
  S4  (空き)
```

S1 のキーが青く光れば成功です。

## セットアップ（Windows）

Python は [python.org](https://www.python.org/downloads/windows/) の 64 ビット版を入れておきます。
PowerShell かコマンドプロンプトで、次を実行します。

```bat
git clone https://github.com/SoyaManabe/harness-pad-4.git
cd harness-pad-4
python install.py
```

`python` が見つからなければ `py install.py` を試してください。
`install.py` は次の 4 つを行います。何度実行しても構いません。管理者権限は要りません。

1. `daemon\.venv` に Python の専用環境を作り、依存（`hidapi`）を入れる。
   **hidapi の本体はこのパッケージに入っているので、ほかに入れるものはありません**
2. Claude Code のスキル `/pad` を `%USERPROFILE%\.claude\skills\pad\` に入れる
3. このフォルダの場所に合わせたフック設定を `daemon\hooks.windows.json` に書き出す
4. デーモンを起動し、ログイン時に自動で起動するよう登録する
   （レジストリの `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` に `AgentPad` を足す）

最後に、パッドが見えているかを調べて結果を出します。

### フックを設定する

1. `daemon\hooks.windows.json` の `"hooks"` の中身を、`%USERPROFILE%\.claude\settings.json` の
   `"hooks"` に統合する。すでに別のフックがあれば、消さずに並べる
2. Claude Code で `/hooks` を開くか、セッションを始め直す

Windows のフックは、`curl` の代わりに同梱の [`daemon/hook_send.py`](daemon/hook_send.py) で送ります。
Claude Code がフックを動かすシェル（Git Bash か PowerShell）に左右されないようにするためです。
設定にはこのフォルダの場所が入るので、**フォルダを移動したら `install.py` をやり直し、統合し直します。**

### 動いているか確かめる

パッドを USB で繋ぎ、Claude Code で何か頼みます。

```bat
daemon\agentpadctl status
```

```
自動起動: 登録済み
デーモン: 稼働中 (PID 12345)
  S1* BUSY     my-project       C:\Users\you\my-project  [8b278297]
  S2  (空き)
  S3  (空き)
  S4  (空き)
```

S1 のキーが青く光れば成功です。光らなければ `daemon\agentpadctl check` と
`daemon\agentpadctl logs` の出力を見てください。

### Windows での制限

Windows 版は、実機で通して確かめる前の段階です。

- **端末を前に出す機能は、ウィンドウを選ぶところまでです。** タブは選べません。
  WSL の中で動かしているセッションは見つけられません。
- **デーモンが落ちても、自動では起き直りません。** `daemon\agentpadctl start` で起動します。
  ログインのたびには自動で起動します。
- ARM 版の Windows 向けには hidapi が配布されていません。x64 版の Python を使ってください。

## 使い方

### LED の色

セッションは、始まった順に空いているキー（S1〜S4）へ入ります。

| 色 | 状態 | |
|---|---|---|
| 消灯 | 空き | セッションが割り当てられていない |
| 暗い白 | 待機中 | |
| 青 | 作業中 | エージェントが動いている |
| **橙の点滅** | **入力待ち** | **許可や返事を待っている。対応が必要** |
| 緑 | 完了 | 30 秒後に待機中へ戻る |
| 赤 | エラー | |

### キー

| 操作 | 動作 |
|---|---|
| 短く押す | そのセッションの端末を前に出す |
| 長く押す（0.8 秒） | 直近に発言したセッションを、そのキーに割り当てる。先客がいれば入れ替わる |

端末を前に出す動作は、アプリによってできることが違います。macOS では次のとおりです。

| アプリ | 動作 |
|---|---|
| Terminal、iTerm2 | そのセッションのタブを選んで前に出す |
| VS Code、Cursor、Windsurf | そのフォルダを開いているウィンドウを前に出す（タブまでは選べない） |
| その他 | アプリを前に出す |

初めて押したとき、macOS が「ほかのアプリの制御」の許可を求めます。許可してください。

Windows では、そのセッションを表示しているウィンドウを前に出します。タブまでは選べません。
同じアプリが複数のウィンドウを開いていれば、題名に作業フォルダの名前を含むものを選びます。
Windows がウィンドウの切り替えを断ることがあり、そのときはタスクバーのボタンが点滅します。

### ディスプレイ

上の 4 行がキーごとの状態、下の 4 行が、対応が必要なセッションの詳細です。

```
1 BUSY api-server          ← S1〜S4 の状態と、セッション（作業フォルダの名前）
2 WAIT!my-project
3 WAIT docs

S2 WAIT 0:42    ! 1/2      ← 詳細を出しているキー、状態、待たせている時間
Force push                 ← エージェントが付けた説明
$ git push --force or      ← 求めている操作そのもの
igin main                     （入りきらなければ 1 行ずつ送る）
```

- 入力待ちかエラーのセッションが複数あれば、詳細を順番に出します（右上の `1/2`）。
- **`!` は、取り返しのつかない操作らしいことを示します**（`rm -r`、`sudo`、`git push --force` など）。
  決まった規則で調べているだけなので、**`!` が無いことは安全を意味しません。**
- 待機中のセッションは一覧に出しません。全部が待機中か空きなら、ディスプレイは消えます。

### `/pad` スキル

Claude Code の中から、パッドを操作できます。

| 入力 | 動作 |
|---|---|
| `/pad 2` | **いまのセッション**を S2 に割り当てる |
| `/pad release 2` | S2 を空ける |
| `/pad status` | 割り当ての状況を見る |
| `/pad start`、`/pad stop`、`/pad restart` | デーモンを起動・停止・再起動する |

「パッドの S3 にして」のように頼んでも動きます。

### コマンド

```sh
./daemon/agentpadctl status      # 稼働状況と割り当て
./daemon/agentpadctl restart     # 再起動（割り当ては消える）
./daemon/agentpadctl logs        # ログを追う
./daemon/agentpadctl stop        # 止める（次のログインでまた起動する）
./daemon/agentpadctl uninstall   # 常駐をやめる
```

Windows では `daemon\agentpadctl` と書きます。同じコマンドに加えて、
`daemon\agentpadctl check`（hidapi を読めるか、パッドが見えるか）があります。
ログは `%LOCALAPPDATA%\AgentPad\agentpad.log` に出ます。

## 知っておくこと

- **パッドを抜いても、デーモンは動き続けます。** 挿し直せば、その時点の状態が出ます。
- **デーモンが止まると、約 10 秒で LED とディスプレイが消えます。**
  このパッドのキーは文字を入力しないので、デーモンが無いあいだは押しても何も起きません。
- **セッションは 4 つまでです。** 5 つ目が始まると、待機中か完了のものが押し出されます。
  4 つとも作業中や入力待ちなら、5 つ目は表示されません。
- **デーモンを再起動すると、状態を忘れます。** 各セッションで次に何かが起きたときに戻ります。
  入力待ちのまま止まっているセッションは、応答するまで表示されません。
- デーモンは自分のマシン（`127.0.0.1`）からしか繋がりません。外部には何も送りません。

## うまく動かないとき

| 症状 | 確かめること |
|---|---|
| LED が点かない | `./daemon/agentpadctl status` で「稼働中」か。`./daemon/agentpadctl logs` に「パッドに接続しました」が出ているか |
| 「パッドが見つかりません」 | USB ケーブルがデータ通信に対応しているか。別のポートで試す |
| 稼働中なのにセッションが出ない | フックを設定したか。設定後に `/hooks` を開いたか |
| `libhidapi` を読めない（macOS） | `brew install hidapi` を実行してから `./install.sh` をやり直す |
| キーを押しても端末が前に出ない（macOS） | システム設定 → プライバシーとセキュリティ → オートメーション で許可されているか |
| フォルダを移動したら動かなくなった | `./install.sh`（Windows は `python install.py`）をやり直す。Windows はフックも統合し直す |
| `hid` を読めない（Windows） | `daemon\agentpadctl check` の出力を見る。`python install.py` をやり直す |
| キーを押しても端末が前に出ない（Windows） | 下の「端末が前に出ないとき（Windows）」の手順で、どこで見失っているかを調べる |
| フックがエラーになる（Windows） | Claude Code が古いと、フック設定の `args` を読めない。Claude Code を更新する |
| 起動しても応答がない（Windows） | `%LOCALAPPDATA%\AgentPad\agentpad.log` の末尾を見る。ポート 8787 をほかが使っていないか |

### 端末が前に出ないとき（Windows）

どこで見失っているかを調べられます。`C:\path\to\project` は、セッションを動かしているフォルダです。

```bat
cd daemon
.venv\Scripts\python -m agentpad.focus C:\path\to\project
```

エージェントの候補、そこから親をたどった列、選んだウィンドウが出ます。

## ほかのエージェントを繋ぐ

デーモンは、エージェントの種類によらない共通の形式でイベントを受け取ります。
エージェントが「入力待ちになった」「入力待ちが終わった」をコマンドの実行で知らせられるなら、
繋げます。形式と送り方は [EVENTS.md](EVENTS.md) にあります。

ほかのマシンで動くエージェントの状態を受け取る仕組み（ホスト名の区別）も入っていますが、
**実験段階です。** 届け方は [EVENTS.md](EVENTS.md) §6 と §7 を見てください。

## 取り除く

```sh
./daemon/agentpadctl uninstall
rm -r ~/.claude/skills/pad
```

Windows では次のとおりです。

```bat
daemon\agentpadctl uninstall
rmdir /s /q "%USERPROFILE%\.claude\skills\pad"
rmdir /s /q "%LOCALAPPDATA%\AgentPad"
```

そのあと、`~/.claude/settings.json` から追加したフックを消します。

## ライセンス

[MIT](LICENSE)
