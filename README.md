# Discussion_AIs — Debate Studio v1.4

AIにディスカッションさせるやつ。複数のLLMが役割（議長・提案・批判・代替案など）を持って討論し、その過程をLINE風のグループチャットでリアルタイムに観測・操作するローカルWebアプリです。
設計は `ai/DebateStudio/DESIGN_v1.4.md`、開発ルールは同フォルダの `Agent.MD` / `Skill.MD` / `Task.MD` に従っています。

## 起動（Windows / Mac 共通）

必要なのは **Python 3.10 以上** だけです（画面はビルド済みの `frontend/dist` を同梱）。Node.js は画面のソースを変更したときだけ必要です。

### Windows
1. Python をインストール: https://www.python.org/downloads/ （インストーラで **「Add python.exe to PATH」にチェック**）
   または PowerShell で `winget install Python.Python.3.12`
2. フォルダ内の **`start.bat` をダブルクリック**（APIキーなしで試すなら **`start-demo.bat`**）
3. 初回は必要なパッケージを自動でインストールし、ブラウザで http://127.0.0.1:8000 が開きます。終了はウィンドウで `Ctrl+C`

### Mac
1. Python をインストール: https://www.python.org/downloads/macos/ または `brew install python@3.12`
   （macOS に最初から入っている `python3` は 3.9 のことがあり、そのままでは動きません）
2. Finder で **`start.command` をダブルクリック**（デモは **`start-demo.command`**）
   - ZIPでダウンロードした場合など「開発元が未確認」と出たら、右クリック →「開く」
   - 実行できない場合はターミナルで `chmod +x start.command start-demo.command`
3. ブラウザで http://127.0.0.1:8000 が開きます。終了はターミナルで `Ctrl+C`

### ターミナルから（全OS共通）
```bash
python run.py              # Windows は  py run.py  でも可
python run.py --demo       # デモ（FakeProvider・課金なし）
python run.py --port 8123  # ポート変更
python run.py --check      # 環境チェック
python run.py --test       # 自動テスト
python run.py --build      # 画面を作り直す（Node.js 18 以上が必要）
```

- 仮想環境は OS ごとに別フォルダ（`.venv-win32` / `.venv-darwin` / `.venv-linux`）に作られるため、同じフォルダを Windows と Mac で共有しても衝突しません。`node_modules` も別OS用を検出すると入れ直します。
- 画面のソース（`frontend/src`）がビルドより新しく、Node.js がある場合は自動で再ビルドします。
- 画面の開発時は `cd frontend && npm install && npm run dev`（http://127.0.0.1:5173、`/api` は 8000 番へプロキシ）。

## OpenAI で使う最短手順

1. 「API接続」→「OpenAI」の **既定モデル** にモデルIDを入力し、APIキーを登録します（キーは `.env` にだけ保存され、再表示されません）。
2. 同じ画面の「使用量照会と口座閾値」で、次のどちらかを選びます。
   - Admin API Key を登録して `auto`（当月の使用量 U0 を取得し、`U0 + U0/6` を自動上限にする）
   - 手動予算（`manual_budget_tokens`）を入力する、または `disabled` にする
   - **どちらも設定しないと開始を拒否します**（設計書 7A.3 の仕様どおり）。
3. 資料を添付する場合は、「添付資料由来のテキストの送信を許可」にチェックを入れます。チェックがない接続先へは、資料の内容を送りません。
4. 「討論ルーム」→「＋ 新しい討論」で議題と資料を入力し、「開始」を押します。

## Strata などローカルLLMで使う

1. 「API接続」→「新規登録」→ プリセット **「Strata（ローカル）」** を押し、「既定モデル」を入力して登録します。モデルIDは「サーバーからモデル一覧を取得」で選べます。
   プリセットの初期値は次のとおりです。
   - `http://127.0.0.1:8080/v1`
   - キー不要
   - 口座閾値なし
   - 思考「中」
   - 同時実行数 1
2. 「編成」で各Agentの「API（主）」を Strata にします。
3. トークンの扱いは次のとおりです。
   - **1発言の上限**は「本文の上限（設定画面・既定1500）＋思考の予算（接続ごと・既定4096）」です。思考で本文が押し出されない作りです。
   - 上限で途切れた場合は、上限を2倍にして1回だけ再生成します。それでも本文が空なら、理由を表示して停止します。
   - **予算はAgentごとに分離**されます（既定）。全員で1つの枠を食い合うことはありません。クラウド口座の実際の共有上限に合わせたい場合だけ「接続全体で共有」を選んでください。
   - **同時実行数 1** にすると、独立提案も1人ずつ Strata に送られます。ローカルサーバーが同時リクエストでコンテキストを分け合う場合でも、各Agentが毎回コンテキストを丸ごと使えます。
   - 本文に混ざる `<think>…</think>` は除去します。思考の内容は表示せず、他のAgentにも渡しません（画面には思考トークン数だけ表示）。
   - コンテキスト長を超えたときは、古い履歴を要約して1回だけ再試行します。

## 主な機能（設計書との対応）

| 機能 | 実装 |
|---|---|
| 議長1＋一般Agent3（最小4人）、±で増減、議長は削除不可 | `config/agents.json`、編成画面 |
| API接続：固定ID・ENV変数で参照、名前・方式は変更可、キーは`.env`のみ | `config/api_profiles.json`、`.env` |
| 役割：`roles/<id>/role.json`＋`Agent.MD`（システム指示として送信） | 役割画面（差分を確認してから取り込み） |
| 独立提案は並列 → 全員完了まで待機 → 議長の整理 → 順次討論 | `services/debate.py` |
| 増分出力をSSEで配信、`Last-Event-ID`で重複なく再接続 | `GET /api/sessions/{id}/events` |
| 議長だけが資料を解析し、一般AgentにはテキストのContext Packetだけを配布 | `services/chair.py`、`services/packets.py` |
| 送信直前に、原本・URL・チャンク・base64・tool定義・権限外の抜粋が含まれていないか検査 | `services/context.py` |
| 根拠不足 → 議長が検索 → 見つからなければユーザーへ差し戻し → 回答を再検証 → 依存ターンだけ再開 | `services/evidence.py`、資料不足カード |
| ラウンド終了ごとに中間報告を出し、続行／方向修正／終了の判断を待つ（待機中はLLMを呼ばない） | 中間報告カード |
| Agent×Tierの予算、口座閾値、主→下流→ローカルの自動切替、品質ゲート | `services/budget.py`、`services/llm_runner.py` |
| 履歴圧縮（8,000 tokens超・ラウンド末・直近6件は原文）。完全ログは保持 | `services/compression.py` |
| 対応形式：txt / md / csv / json / pdf / docx / xlsx / pptx / 画像（画像は内容未読） | `extractors/` |

## テスト

```bash
python run.py --test                  # バックエンド 52件＋（Node.jsがあれば）画面の型検査・テスト
```

`.github/workflows/ci.yml` は push 時に **Windows / macOS / Linux × Python 3.10 / 3.13** で同じテストを実行します。

## セキュリティ上の前提

- 想定は**単一ユーザー・ローカル運用**です。`127.0.0.1` 以外からのアクセスは拒否します。
- 起動ごとにランダムなセッションCookie（HttpOnly / SameSite=Strict）を発行し、変更系のAPIでは `X-Requested-With` ヘッダーと Origin を検査します。
- 共有サーバーとして公開する場合は、ユーザー別の認可と秘密管理、TLS、SSRF対策を別途追加してください（設計書 §12）。
- アップロードしたファイルは `data/uploads/` にランダムな名前で保存されます（Web非公開・Git管理外）。
- 解析は別プロセス（spawn）で実行し、タイムアウトを設けています。プロセスのメモリ上限は Linux でのみ有効で、Windows / macOS では効きません。

## 未対応・制約（Task.MD の確認事項も参照）

- OCR / VLM（画像・画像PDFの読み取り）はオプション扱いで未実装です。該当箇所は「未解析」と明示し、読めたものとしては扱いません。
- 下流モデルの品質検証は、5問の簡易プローブによる最低限の検査です。設計書が推奨する「過去の議題20件以上での比較評価」は別途必要で、モデル間の同等精度は保証しません。
- OpenAI / Anthropic の組織Usage APIは公開仕様に基づいて実装していますが、**実際の管理者キーでの動作は未確認**です。
- 実APIでの手動E2E試験（課金を伴うもの）は未実施です。モックとFakeProviderで検証しています。
