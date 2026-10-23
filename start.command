#!/bin/bash
# Debate Studio - macOS launcher (double-click in Finder). Options: ./start.command --demo
cd "$(dirname "$0")" || exit 1
# Finder-launched shells may miss Homebrew / python.org paths
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    "$c" run.py "$@"
    status=$?
    echo
    read -r -p "Enterキーでウィンドウを閉じます… " _
    exit $status
  fi
done
echo
echo "[エラー] Python 3.10 以上が見つかりません（macOS標準の python3 は 3.9 の場合があります）。"
echo "  https://www.python.org/downloads/macos/ からインストールするか、 brew install python@3.12"
echo
read -r -p "Enterキーでウィンドウを閉じます… " _
exit 1
