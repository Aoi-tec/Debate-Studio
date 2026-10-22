#!/usr/bin/env python3
"""Debate Studio launcher — same command on Windows, macOS and Linux.

    python run.py              # 起動してブラウザを開く
    python run.py --demo       # APIキー不要のデモ（FakeProvider・課金なし）
    python run.py --port 8123  # ポート変更
    python run.py --build      # 画面（frontend）を作り直す（Node.js が必要）
    python run.py --check      # 環境チェックのみ
    python run.py --test       # 自動テストを実行

What it does:
  1. checks Python >= 3.10
  2. creates a per-OS virtualenv (.venv-win32 / .venv-darwin / .venv-linux) so a
     folder shared between Windows and Mac never mixes native binaries
  3. installs backend requirements when requirements*.txt changed
  4. builds frontend/dist only when missing or outdated (and Node.js is available)
  5. starts the server on 127.0.0.1 and opens the browser

Only the standard library is imported at module level: multiprocessing on Windows
and macOS ("spawn") re-imports this file in worker processes, so all work happens
under ``if __name__ == "__main__"``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
MIN_PY = (3, 10)
IS_WIN = os.name == "nt"
PLATFORM_TAG = "win32" if IS_WIN else ("darwin" if sys.platform == "darwin" else "linux")
VENV = ROOT / f".venv-{PLATFORM_TAG}"
INNER_FLAG = "--_inside-venv"


def say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # very old Windows consoles
        print(msg.encode("ascii", "replace").decode(), flush=True)


def fail(msg: str, code: int = 1) -> int:
    say(f"\n[エラー] {msg}\n")
    return code


# ---------------------------------------------------------------- python / venv
def python_help() -> str:
    if IS_WIN:
        return ("Python 3.10 以上をインストールしてください。\n"
                "  - https://www.python.org/downloads/ のインストーラで「Add python.exe to PATH」にチェック\n"
                "  - または PowerShell で: winget install Python.Python.3.12\n"
                "インストール後、新しいウィンドウで start.bat をもう一度実行してください。")
    if sys.platform == "darwin":
        return ("Python 3.10 以上をインストールしてください（macOS標準の python3 は 3.9 のことがあります）。\n"
                "  - https://www.python.org/downloads/macos/ のインストーラ\n"
                "  - または Homebrew で: brew install python@3.12\n"
                "インストール後、start.command をもう一度実行してください。")
    return "Python 3.10 以上と venv（例: sudo apt install python3.12 python3.12-venv）をインストールしてください。"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def in_our_venv() -> bool:
    try:
        return Path(sys.prefix).resolve() == VENV.resolve()
    except OSError:
        return False


def req_hash() -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt", "requirements-dev.txt"):
        p = BACKEND / name
        if p.exists():
            h.update(p.read_bytes())
    h.update(f"{sys.version_info[:2]}".encode())
    return h.hexdigest()


def ensure_venv(with_dev: bool) -> int:
    py = venv_python()
    if py.exists():
        ok = subprocess.run([str(py), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"],
                            capture_output=True).returncode == 0
        if not ok:  # broken (e.g. Python was upgraded/removed) → rebuild in a fresh folder
            old = VENV.with_name(f"{VENV.name}-broken-{int(time.time())}")
            say(f"仮想環境が壊れているため作り直します（古いものは {old.name} に退避）")
            VENV.rename(old)
    if not venv_python().exists():
        say(f"仮想環境を作成しています: {VENV.name}")
        r = subprocess.run([sys.executable, "-m", "venv", str(VENV)])
        if r.returncode != 0 or not venv_python().exists():
            return fail("仮想環境を作成できませんでした。\n" + python_help())
    marker = VENV / ".ds-requirements"
    want = req_hash() + (":dev" if with_dev else "")
    have = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    if have != want and not (have.endswith(":dev") and have.split(":")[0] == want.split(":")[0]):
        req = BACKEND / ("requirements-dev.txt" if with_dev else "requirements.txt")
        say("必要なPythonパッケージをインストールしています（初回は数分かかります）…")
        r = subprocess.run([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                            "-r", str(req)], cwd=str(ROOT))
        if r.returncode != 0:
            return fail("パッケージのインストールに失敗しました。ネットワーク接続を確認してもう一度実行してください。")
        marker.write_text(want, encoding="utf-8")
    return 0


# ---------------------------------------------------------------- frontend
def newest_mtime(paths: list[Path]) -> float:
    m = 0.0
    for base in paths:
        if base.is_file():
            m = max(m, base.stat().st_mtime)
        elif base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    m = max(m, p.stat().st_mtime)
    return m


def ensure_frontend(force: bool) -> int:
    dist_index = FRONTEND / "dist" / "index.html"
    sources = [FRONTEND / "src", FRONTEND / "index.html", FRONTEND / "package.json", FRONTEND / "vite.config.ts"]
    outdated = dist_index.exists() and newest_mtime(sources) > dist_index.stat().st_mtime + 1
    if dist_index.exists() and not force and not outdated:
        return 0
    npm = shutil.which("npm")
    if not npm:
        if dist_index.exists():
            say("注意: 画面のソースが更新されていますが Node.js が無いため、既存のビルド（frontend/dist）で起動します。")
            return 0
        return fail("画面（frontend/dist）がありません。Node.js 18 以上（https://nodejs.org/ の LTS）をインストールしてから再実行してください。")
    nm = FRONTEND / "node_modules"
    tag = nm / ".ds-platform"
    if nm.exists() and (not tag.exists() or tag.read_text(encoding="utf-8").strip() != f"{PLATFORM_TAG}-{platform.machine()}"):
        # node_modules from another OS/CPU contains wrong native binaries (esbuild/rollup) — reinstall
        say("別のOS用の node_modules を検出したため入れ直します…")
        shutil.rmtree(nm, ignore_errors=True)
    if not nm.exists():
        say("画面の依存パッケージをインストールしています（npm install）…")
        cmd = [npm, "ci"] if (FRONTEND / "package-lock.json").exists() else [npm, "install"]
        if subprocess.run(cmd + ["--no-audit", "--no-fund"], cwd=str(FRONTEND)).returncode != 0:
            return fail("npm install に失敗しました。")
        tag.write_text(f"{PLATFORM_TAG}-{platform.machine()}", encoding="utf-8")
    say("画面をビルドしています（npm run build）…")
    if subprocess.run([npm, "run", "build"], cwd=str(FRONTEND)).returncode != 0:
        return fail("画面のビルドに失敗しました。")
    return 0


# ---------------------------------------------------------------- server
def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def is_debate_studio(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:  # noqa: BLE001
        return False


def open_browser_when_ready(url: str, port: int) -> None:
    def run() -> None:
        for _ in range(120):
            if is_debate_studio(port):
                webbrowser.open(url)
                return
            time.sleep(0.5)
    threading.Thread(target=run, daemon=True).start()


def serve(args: argparse.Namespace) -> int:
    os.environ["DEBATE_STUDIO_HOME"] = str(ROOT)
    os.environ["DEBATE_STUDIO_ENABLE_FAKE"] = "1" if args.demo else "0"
    url = f"http://127.0.0.1:{args.port}/"
    if port_in_use(args.port):
        if is_debate_studio(args.port):
            say(f"Debate Studio は既に起動しています: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        return fail(f"ポート {args.port} は別のアプリが使用中です。例: python run.py --port 8123")
    sys.path.insert(0, str(BACKEND))
    import uvicorn  # noqa: E402  (installed in the venv)

    say("")
    say("============================================================")
    say(f"  Debate Studio を起動しました: {url}")
    say(f"  モード: {'デモ（FakeProvider・課金なし）' if args.demo else '通常'}")
    say("  終了するにはこのウィンドウで Ctrl+C を押してください")
    say("============================================================")
    if not args.no_browser:
        open_browser_when_ready(url, args.port)
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level=args.log_level)
    return 0


def run_tests() -> int:
    code = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=str(ROOT)).returncode
    npm = shutil.which("npm")
    if npm and (FRONTEND / "node_modules").exists():
        code |= subprocess.run([npm, "run", "typecheck"], cwd=str(FRONTEND)).returncode
        code |= subprocess.run([npm, "test"], cwd=str(FRONTEND)).returncode
    return code


def check() -> int:
    say(f"OS        : {platform.system()} {platform.release()} ({platform.machine()})")
    say(f"Python    : {sys.version.split()[0]} ({sys.executable})")
    say(f"仮想環境  : {VENV.name} {'OK' if venv_python().exists() else '未作成'}")
    say(f"Node.js   : {shutil.which('node') or '未検出（画面のビルドにのみ必要）'}")
    say(f"画面      : {'ビルド済み' if (FRONTEND / 'dist' / 'index.html').exists() else '未ビルド'}")
    try:
        import sqlite3
        say(f"SQLite    : {sqlite3.sqlite_version}")
    except Exception:  # noqa: BLE001
        say("SQLite    : 利用不可")
    try:
        sys.path.insert(0, str(BACKEND))
        import app.main  # noqa: F401
        say("バックエンド: 読み込みOK")
    except Exception as e:  # noqa: BLE001
        return fail(f"バックエンドを読み込めません: {type(e).__name__}: {e}")
    return 0


def run_child(cmd: list[str]) -> int:
    """Run the in-venv process and make sure it never outlives this launcher."""
    import signal

    child = subprocess.Popen(cmd)

    def stop(*_a) -> None:
        if child.poll() is None:
            child.terminate()

    if not IS_WIN:
        signal.signal(signal.SIGTERM, lambda *a: (stop(), sys.exit(0)))
        signal.signal(signal.SIGHUP, lambda *a: (stop(), sys.exit(0)))  # Terminal window closed
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Ctrl+C reaches both processes; give the server a moment to shut down cleanly
            try:
                return child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                stop()
            except KeyboardInterrupt:
                stop()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Debate Studio launcher (Windows / macOS / Linux)")
    ap.add_argument("--demo", action="store_true", help="APIキー不要のデモモード（FakeProvider）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    ap.add_argument("--build", action="store_true", help="画面を強制的に再ビルド")
    ap.add_argument("--check", action="store_true", help="環境チェックのみ")
    ap.add_argument("--test", action="store_true", help="自動テストを実行")
    ap.add_argument("--log-level", default="info")
    ap.add_argument(INNER_FLAG, action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if sys.version_info < MIN_PY:
        return fail(f"このPythonは {platform.python_version()} です。\n" + python_help())

    if not getattr(args, INNER_FLAG.lstrip("-").replace("-", "_")) and not in_our_venv():
        code = ensure_venv(with_dev=args.test)
        if code:
            return code
        if not args.check and not args.test:
            code = ensure_frontend(args.build)
            if code:
                return code
        return run_child([str(venv_python()), str(Path(__file__).resolve()), *argv, INNER_FLAG])

    if args.check:
        return check()
    if args.test:
        return run_tests()
    try:
        return serve(args)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
