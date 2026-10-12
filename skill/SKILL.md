---
name: pad
description: エージェント監督パッド（AgentPad）の操作。このセッションを S1〜S4 のキーに割り当てる、スロットを空ける、割り当て状況を見る、デーモンを起動・停止・再起動する。「パッドの S2 に割り当てて」「パッドのデーモンを起動して」などで使う。
argument-hint: "[1-4 | release N | status | start | stop | restart | install]"
allowed-tools: Bash(__AGENTPADCTL__ *)
---

AgentPad のデーモンを `__AGENTPADCTL__` で操作する。

引数: `$ARGUMENTS`

引数を次のように読み替えて、コマンドを 1 回だけ実行する。引数が無ければ `status`。

| 引数 | 実行するコマンド |
|---|---|
| `1`〜`4` | `agentpadctl assign <N> ${CLAUDE_SESSION_ID} "$PWD"` — **このセッション**を S<N> へ。先客がいれば入れ替わる |
| `release <N>` | `agentpadctl release <N>` — S<N> を空ける |
| `status` | `agentpadctl status` |
| `start` / `stop` / `restart` | `agentpadctl <同じ語>` |
| `install` / `uninstall` | `agentpadctl <同じ語>` — 常駐（ログイン時の自動起動）の登録と解除 |

自然文で頼まれた場合（「S3 にして」「パッドを止めて」）も、上の表のどれかに当てはめる。
当てはまらなければ実行せず、何をしたいのか聞く。

結果はコマンドの出力をそのまま短く伝える。失敗したら出力のエラー文を伝え、
デーモンに繋がらない場合は `status` で稼働状況を確認する。

`stop` と `uninstall` は LED が消えるので、実行したらその旨を一言添える。
