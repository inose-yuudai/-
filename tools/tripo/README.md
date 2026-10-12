# Tripo キャラクター生成パイプライン

写真の人物を、ゲームの画風に合わせたリグ付き 3D モデルにし、モーションまで付ける。
どう作るか・なぜそうするかは [`docs/characters/member_a.md`](../../docs/characters/member_a.md) を参照。

## 準備

### 1. Tripo の API キー

Tripo の API プラットフォームで API キー（`tsk_` で始まる）を発行し、環境変数 `TRIPO_API_KEY` に入れる。
API のクレジットは Web 版の会員クレジットと別のことがあるので、`balance` で残高を確認してから始める。

### 2. 参考画像を置く

`tools/tripo/refs/` に次の名前で置く。このフォルダは `.gitignore` 済みで、コミットされない。

| ファイル | 中身 |
|---|---|
| `style_hero.png` | 画風の参考: 主人公 |
| `style_chibi.webp` | 画風の参考: ちびキャラ |
| `person_1.png` / `person_2.webp` / `person_3.webp` | 人物の写真 |

拡張子が違う場合は `characters/member_a.json` のパスも合わせて変える。プロンプトは「1〜2 枚目が画風、3〜5 枚目が人物」という順番を前提にしているので、並び順は変えない。

### 3. 実行する場所

- **自分の PC**: `pip install -r tools/tripo/requirements.txt` を実行して、環境変数を設定する。出力をそのまま Unity に入れられるので、いちばん手軽。
- **Claude Code のクラウド環境**: 環境設定で次の 2 つが必要。どちらも新しいセッションから有効になる。
  - ネットワークの許可リストに `api.tripo3d.ai` を追加する。生成物のダウンロードで止まった場合は、エラーに出たホスト名も追加する。
  - `TRIPO_API_KEY` を登録する。

## 実行

リポジトリのルートで実行する。段階ごとに止まるので、**出力を目で見てから次へ進む**。

```sh
P="python tools/tripo/tripo_pipeline.py -c tools/tripo/characters/member_a.json"

$P balance                 # 残高
$P concept                 # 立ち絵 → out/member_a/concept/  ← 似ているか確認。だめならもう一度
$P concept --proportion chibi   # ちび版も見比べたいとき
$P views --image tools/tripo/out/member_a/concept/concept_02.png   # 選んだ立ち絵で 4 方向
$P model                   # 3D モデル → out/member_a/model/
$P rig                     # リグ付き FBX → out/member_a/rig/
$P anim                    # モーション → out/member_a/anim/member_a@idle.fbx など
$P status                  # これまでの記録
```

- `views --image` を省くと、最後に作った立ち絵を使う。
- 4 方向生成がうまくいかないときは `model --image <立ち絵>` で、立ち絵 1 枚から直接作れる。
- タスクは `out/member_a/state.json` に記録される。途中で止まっても、同じコマンドを実行すれば続きから再開し、クレジットを二重に使わない。
- 作り直すときは `--force` を付ける。その段階より後ろの記録は消えるので、後ろの段階もやり直しになる。

## 設定（`characters/member_a.json`）

| 項目 | 内容 |
|---|---|
| `proportion` | `hero`（主人公と同じ 5〜6 頭身）/ `chibi`（2.5 頭身） |
| `concept.prompt` | 立ち絵のプロンプト（英語、1024 文字まで）。`{proportion}` の部分は `proportion_prompts` に置き換わる |
| `concept.params.model_version` | 画像生成モデル。似ないときは `gpt_image_1.5` なども試す |
| `model` | 3D 生成のパラメータ。中身はそのまま API に渡る。ポリゴンが多すぎる / 形が汚いときは `"smart_low_poly": true` を試す |
| `rig.spec` | `mixamo` のままにする（Unity の Humanoid や Mixamo のモーションとそのまま使える） |
| `anim.animations` | 付けるモーション。1 モーションにつき 1 タスク・1 ファイルになる |

## うまくいかないとき

| 症状 | 対処 |
|---|---|
| 立ち絵が似ていない | 人物の写真を、顔がはっきり写ったものに差し替える。`model_version` を変える。プロンプトで髪型と眼鏡をさらに強調する |
| 画風が合わない | 画風の参考を、キャラだけを大きく写した画像にする（UI や背景が写っていると画風が混ざる） |
| `rig` で「リグを入れられない」 | 立ち絵の腕が体から離れていない。A ポーズを強調したプロンプトで concept からやり直す |
| 眼鏡がつぶれる | 眼鏡なしで作り、Unity で頭のボーンの子に眼鏡のメッシュを付ける（着せ替えアイテムにもなる） |
| `banned` で終わる | 内容審査で止められた。写真を差し替えて試す |
