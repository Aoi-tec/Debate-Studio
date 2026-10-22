#Debate Studio v1.4

複数のLLMが役割（議長・提案・批判・代替案など）を持って討論し、その過程をLINE風のグループチャットでリアルタイムに観測・操作するローカルWebアプリです。


## 起動（Mac / Linux）

```bash
# 1) バックエンド（Python 3.10 以上）
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt

# 2) 画面をビルド（Node.js 18 以上。frontend/dist があれば省略可）
cd frontend && npm install && npm run build && cd ..

# 3) 起動 → ブラウザで http://127.0.0.1:8000
.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

`scripts/run.sh` でも起動できます（初回は .venv を自動作成）。
**APIキーなしで試す場合**は `DEMO=1 scripts/run.sh`。オフラインの FakeProvider（課金なし）が「Fake（デモ用）」接続として現れます。

画面の開発時は `cd frontend && npm run dev`（http://127.0.0.1:5173、`/api` は 8000 番へプロキシ）。

## OpenAI で使う最短手順

1. 「API接続」→「OpenAI」の **既定モデル** にモデルIDを入力し、APIキーを登録します（キーは `.env` にだけ保存され、再表示されません）。
2. 同じ画面の「使用量照会と口座閾値」で、次のどちらかを選びます。
   - Admin API Key を登録して `auto`（当月の使用量 U0 を取得し、`U0 + U0/6` を自動上限にする）
   - 手動予算（`manual_budget_tokens`）を入力する、または `disabled` にする
   - **どちらも設定しないと開始を拒否します**（設計書 7A.3 の仕様どおり）。
3. 資料を添付する場合は、「添付資料由来のテキストの送信を許可」にチェックを入れます。チェックがない接続先へは、資料の内容を送りません。
4. 「討論ルーム」→「＋ 新しい討論」で議題と資料を入力し、「開始」を押します。

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
.venv/bin/python -m pytest            # バックエンド 50件（FakeProvider・HTTPモックのみ。外部API・課金なし）
cd frontend && npm run typecheck && npm test
```

## セキュリティ上の前提

- 想定は**単一ユーザー・ローカル運用**です。`127.0.0.1` 以外からのアクセスは拒否します。
- 起動ごとにランダムなセッションCookie（HttpOnly / SameSite=Strict）を発行し、変更系のAPIでは `X-Requested-With` ヘッダーと Origin を検査します。
- 共有サーバーとして公開する場合は、ユーザー別の認可と秘密管理、TLS、SSRF対策を別途追加してください（設計書 §12）。
- アップロードしたファイルは `data/uploads/` にランダムな名前で保存されます（Web非公開・Git管理外）。
- 解析は別プロセスで実行し、タイムアウトを設けています。メモリ上限は Linux でのみ有効で、macOS では効きません。

## 未対応・制約（Task.MD の確認事項も参照）

- OCR / VLM（画像・画像PDFの読み取り）はオプション扱いで未実装です。該当箇所は「未解析」と明示し、読めたものとしては扱いません。
- 下流モデルの品質検証は、5問の簡易プローブによる最低限の検査です。設計書が推奨する「過去の議題20件以上での比較評価」は別途必要で、モデル間の同等精度は保証しません。
- OpenAI / Anthropic の組織Usage APIは公開仕様に基づいて実装していますが、**実際の管理者キーでの動作は未確認**です。
- 実APIでの手動E2E試験（課金を伴うもの）は未実施です。モックとFakeProviderで検証しています。
