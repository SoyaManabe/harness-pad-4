#!/bin/bash
# harness-pad-4 のセットアップ（macOS）。何度実行してもよい。
#
#   1. Python の専用環境 (daemon/.venv) を作り、依存を入れる
#   2. Claude Code のスキル /pad を入れる
#   3. デーモンを常駐させる（ログイン時に自動で起動する）
#
# Claude Code のフック設定だけは自動では入れない。既存の設定を壊さないよう、
# 最後に表示する手順で手で統合する。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DAEMON_DIR="$ROOT/daemon"
SKILL_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/pad"
MIN_PYTHON="3.9"

die() { echo "install.sh: $*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "いまは macOS にしか対応していません"

command -v python3 >/dev/null || die "python3 が見つかりません"
python3 -c "import sys; sys.exit(sys.version_info < tuple(map(int, '$MIN_PYTHON'.split('.'))))" \
    || die "Python $MIN_PYTHON 以上が必要です（いまは $(python3 --version 2>&1)）"

command -v brew >/dev/null || die "Homebrew が見つかりません。https://brew.sh から入れてください"
[ -e "$(brew --prefix)/lib/libhidapi.dylib" ] \
    || die "hidapi が入っていません。先に次を実行してください:  brew install hidapi"

echo "==> Python の環境を用意します ($DAEMON_DIR/.venv)"
python3 -m venv "$DAEMON_DIR/.venv"
"$DAEMON_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DAEMON_DIR/.venv/bin/pip" install --quiet -r "$DAEMON_DIR/requirements.txt"

echo "==> スキル /pad を入れます ($SKILL_DIR)"
mkdir -p "$SKILL_DIR"
# スキルからこのフォルダの agentpadctl を呼べるよう、場所を書き込む
sed "s|__AGENTPADCTL__|$DAEMON_DIR/agentpadctl|g" "$ROOT/skill/SKILL.md" > "$SKILL_DIR/SKILL.md"

echo "==> デーモンを常駐させます"
"$DAEMON_DIR/agentpadctl" install

cat <<MESSAGE

セットアップが終わりました。残りは Claude Code のフック設定です。

  1. $DAEMON_DIR/hooks.example.json の "hooks" の中身を、
     ~/.claude/settings.json の "hooks" に統合する
  2. Claude Code で /hooks を開く（またはセッションを始め直す）

このフォルダを移動したときは、もう一度 ./install.sh を実行してください。
MESSAGE
