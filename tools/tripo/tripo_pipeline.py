#!/usr/bin/env python3
"""Tripo API で「写真の人物 → ゲーム用のリグ付き 3D モデル + モーション」を作るパイプライン.

段階ごとに実行し、段階の間で人が目で確認する（クレジットを無駄にしないため）。

  balance  残高を表示
  concept  参考画像から立ち絵を生成する          → 似ているか・画風が合っているかを確認
  views    立ち絵から前・左・後・右の 4 方向画像   → 破綻がないかを確認
  model    4 方向画像から 3D モデルを生成
  rig      ボーンを入れる（Mixamo 互換の骨格）
  anim     プリセットのモーションを流し込む
  status   これまでのタスクと出力ファイルを表示

作成したタスクは out/<id>/state.json に記録する。途中で止まっても、もう一度同じコマンドを
実行すれば作成済みのタスクの完了を待つところから再開し、新しいタスクは作らない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

k_DEFAULT_BASE_URL = "https://api.tripo3d.ai/v2/openapi"
k_SCRIPT_DIR = Path(__file__).resolve().parent
k_HTTP_TIMEOUT_SEC = 60
k_TASK_TIMEOUT_SEC = 20 * 60
k_POLL_MIN_SEC = 3
k_POLL_MAX_SEC = 15
k_MAX_PROMPT_CHARS = 1024
k_TERMINAL_STATUSES = ("success", "failed", "cancelled", "banned", "expired")
k_IMAGE_TYPES = {".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".webp": "webp"}
# multiview_to_model はこの順番で 4 枚を受け取る
k_VIEW_NAMES = ("front", "left", "back", "right")
# 段階をやり直したら、その結果を使っている後ろの段階は作り直しが必要になる
k_DOWNSTREAM_KEYS = {
    "views": ("model", "rigcheck", "rig", "anim:"),
    "model": ("rigcheck", "rig", "anim:"),
    "rig": ("anim:",),
}


class TripoError(RuntimeError):
    pass


class TripoClient:
    """Tripo REST API の薄いラッパー.

    公式 SDK (tripo3d) は HTTPS_PROXY を見ずに直接つなぐので、プロキシ必須の環境では使えない。
    requests は環境変数のプロキシと CA 証明書をそのまま使う。
    """

    def __init__(self, api_key: str, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api = requests.Session()
        self._api.headers["Authorization"] = f"Bearer {api_key}"
        # 出力ファイルは署名付き URL。Authorization を付けると拒否されることがあるので別セッションにする
        self._cdn = requests.Session()

    def get_balance(self) -> dict:
        return self._call("GET", "/user/balance")

    def upload_image(self, path: Path) -> dict:
        """画像をアップロードし、タスクの file / files にそのまま入れられる形で返す."""
        file_type = k_IMAGE_TYPES.get(path.suffix.lower())
        if file_type is None:
            raise TripoError(f"対応していない画像形式です（png / jpg / webp のみ）: {path}")
        mime = "image/jpeg" if file_type == "jpg" else f"image/{file_type}"
        with path.open("rb") as f:
            data = self._call("POST", "/upload", files={"file": (path.name, f, mime)})
        return {"type": file_type, "file_token": data["image_token"]}

    def create_task(self, body: dict) -> str:
        return self._call("POST", "/task", json=body)["task_id"]

    def wait_task(self, task_id: str) -> dict:
        deadline = time.monotonic() + k_TASK_TIMEOUT_SEC
        showing_progress = False
        while True:
            task = self._call("GET", f"/task/{task_id}")
            status = task.get("status")
            if status in k_TERMINAL_STATUSES:
                if showing_progress:
                    print(file=sys.stderr)
                if status != "success":
                    detail = task.get("error_msg") or task.get("error_code") or ""
                    raise TripoError(f"タスク {task_id} が {status} で終わりました {detail}".rstrip())
                return task
            if time.monotonic() > deadline:
                raise TripoError(f"タスク {task_id} が時間内に終わりませんでした。同じコマンドで待ち直せます")
            print(f"\r  {task_id}  {status}  {task.get('progress', 0):3d}%", end="", file=sys.stderr, flush=True)
            showing_progress = True
            left_sec = task.get("running_left_time") or 0
            time.sleep(min(max(k_POLL_MIN_SEC, left_sec * 0.5), k_POLL_MAX_SEC))

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        with self._cdn.get(url, stream=True, timeout=k_HTTP_TIMEOUT_SEC) as resp:
            resp.raise_for_status()
            with part.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        part.replace(dest)
        print(f"  保存: {_display_path(dest)}")
        return dest

    def _call(self, method: str, path: str, **kwargs) -> dict:
        resp = self._api.request(method, self._base_url + path, timeout=k_HTTP_TIMEOUT_SEC, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            raise TripoError(f"HTTP {resp.status_code}: {resp.text[:300]}") from None
        if resp.status_code != 200 or body.get("code") != 0:
            message = " ".join(str(v) for v in (body.get("message"), body.get("suggestion")) if v)
            raise TripoError(f"HTTP {resp.status_code} code={body.get('code')}: {message}")
        return body["data"]


class Pipeline:
    def __init__(self, client: TripoClient | None, config_path: Path, proportion: str | None) -> None:
        self._client = client
        self._config_path = config_path
        self._config = json.loads(config_path.read_text(encoding="utf-8"))
        self._id = self._config["id"]
        self._out_dir = k_SCRIPT_DIR / "out" / self._id
        self._state_path = self._out_dir / "state.json"
        self._state: dict = (
            json.loads(self._state_path.read_text(encoding="utf-8")) if self._state_path.exists() else {}
        )
        self._proportion = proportion or self._config["proportion"]

    # ---- 段階 ----------------------------------------------------------------

    def balance(self) -> None:
        data = self._api().get_balance()
        print(f"残高: {data.get('balance')}  （処理中で確保されている分: {data.get('frozen')}）")

    def concept(self) -> None:
        params = self._config["concept"]
        refs = [_resolve(p) for p in self._config["style_refs"] + self._config["person_refs"]]
        _require_files(refs)
        proportion_text = self._config["proportion_prompts"][self._proportion]
        prompt = params["prompt"].replace("{proportion}", proportion_text)
        if len(prompt) > k_MAX_PROMPT_CHARS:
            raise TripoError(f"プロンプトが {len(prompt)} 文字あります（上限 {k_MAX_PROMPT_CHARS}）")

        # 立ち絵は何枚か作って比べるので、毎回新しい番号で作る（待ち途中・保存前のものがあればそれを再開）
        pending = next(
            (
                k
                for k, r in self._state.items()
                if k.startswith("concept_") and (r["status"] == "pending" or (r["status"] == "success" and "files" not in r))
            ),
            None,
        )
        count = sum(1 for k in self._state if k.startswith("concept_"))
        key = pending or f"concept_{count + 1:02d}"

        def build() -> dict:
            body = {"type": "generate_image", "prompt": prompt, "files": [self._api().upload_image(p) for p in refs]}
            body.update(params.get("params", {}))
            return body

        print(f"立ち絵を生成します（{key} / 等身: {self._proportion}）")
        task = self._run_task(key, build, force=False)
        output = task["output"]
        url = output.get("generated_image") or output.get("image")
        if not url:
            raise TripoError(f"生成画像の URL がありません: {output}")
        path = self._api().download(url, self._out_dir / "concept" / f"{key}{_url_ext(url, '.png')}")
        self._finish(key, [path])
        print("\n次: 画像を見て、似ていて画風も合っていれば")
        print(f"  {self._command('views')} --image {_display_path(path)}")
        print("合っていなければ concept をもう一度（プロンプトや model_version を変えてもよい）")

    def views(self, image: Path | None, force: bool) -> None:
        source = image or self._latest_concept()
        _require_files([source])

        def build() -> dict:
            return {"type": "generate_multiview_image", "file": self._api().upload_image(source)}

        print(f"4 方向画像を生成します（元: {_display_path(source)}）")
        task = self._run_task("views", build, force)
        if task is None:
            return
        output = task["output"]
        urls = output.get("generate_multiview_image") or output
        paths = []
        for name in k_VIEW_NAMES:
            url = urls.get(f"{name}_view_url")
            if not url:
                raise TripoError(f"{name} の画像がありません: {output}")
            paths.append(self._api().download(url, self._out_dir / "views" / f"{name}{_url_ext(url, '.png')}"))
        self._finish("views", paths)
        print(f"\n次: 4 枚を見て、顔・髪・服が向きによって変わっていなければ  {self._command('model')}")

    def model(self, single_image: Path | None, force: bool) -> None:
        params = self._config["model"]
        if single_image is not None:
            _require_files([single_image])

            def build() -> dict:
                return {"type": "image_to_model", "file": self._api().upload_image(single_image), **params}

            print(f"1 枚の画像から 3D モデルを生成します（{_display_path(single_image)}）")
        else:
            views = self._files("views", "views を先に実行してください")

            def build() -> dict:
                return {"type": "multiview_to_model", "files": [self._api().upload_image(p) for p in views], **params}

            print("4 方向画像から 3D モデルを生成します")
        task = self._run_task("model", build, force)
        if task is None:
            return
        output = task["output"]
        url = output.get("pbr_model") or output.get("model") or output.get("base_model")
        if not url:
            raise TripoError(f"モデルの URL がありません: {output}")
        paths = [self._api().download(url, self._out_dir / "model" / f"{self._id}{_url_ext(url, '.glb')}")]
        if output.get("rendered_image"):
            preview_url = output["rendered_image"]
            paths.append(self._api().download(preview_url, self._out_dir / "model" / f"preview{_url_ext(preview_url, '.webp')}"))
        self._finish("model", paths)
        print(f"\n次: モデルを見て、指・眼鏡・髪の崩れが許せる範囲なら  {self._command('rig')}")

    def rig(self, force: bool) -> None:
        model_task_id = self._done_task_id("model")

        print("リグを入れられるか確認します")
        check = self._run_task(
            "rigcheck",
            lambda: {"type": "animate_prerigcheck", "original_model_task_id": model_task_id},
            force,
        )
        if check is not None:
            self._finish("rigcheck", [])
        check_output = self._state["rigcheck"]["output"]
        if not check_output.get("riggable"):
            raise TripoError(
                "リグを入れられないと判定されました。腕が体から離れた A ポーズ / T ポーズの立ち絵で"
                " model からやり直してください"
            )

        params = {"rig_type": check_output.get("rig_type") or "biped", **self._config["rig"]}
        print(f"リグを入れます（{params}）")
        task = self._run_task(
            "rig",
            lambda: {"type": "animate_rig", "original_model_task_id": model_task_id, **params},
            force,
        )
        if task is None:
            return
        url = task["output"]["model"]
        path = self._api().download(url, self._out_dir / "rig" / f"{self._id}{_url_ext(url, '.fbx')}")
        self._finish("rig", [path])
        print(f"\n次: モーションを付ける  {self._command('anim')}")

    def anim(self, force: bool) -> None:
        rig_task_id = self._done_task_id("rig")
        params = dict(self._config["anim"])
        names: list[str] = params.pop("animations")
        # 1 モーション 1 タスクにする。リストに足したモーションだけが新しく作られ、
        # Unity 側でも 1 ファイル 1 クリップになって扱いやすい
        for name in names:
            key = f"anim:{name}"
            print(f"モーション {name}")
            task = self._run_task(
                key,
                lambda name=name: {
                    "type": "animate_retarget",
                    "original_model_task_id": rig_task_id,
                    "animation": name,
                    **params,
                },
                force,
            )
            if task is None:
                continue
            url = task["output"]["model"]
            label = name.split(":")[-1]
            # Unity の「モデル名@アニメ名.fbx」の命名規則に合わせる
            path = self._api().download(url, self._out_dir / "anim" / f"{self._id}@{label}{_url_ext(url, '.fbx')}")
            self._finish(key, [path])
        print(f"\n完了。Unity に取り込むファイル: {_display_path(self._out_dir / 'rig')} と {_display_path(self._out_dir / 'anim')}")

    def status(self) -> None:
        if not self._state:
            print("まだタスクはありません")
            return
        for key, record in self._state.items():
            print(f"{key:24s} {record['status']:8s} {record['task_id']}")
            for file in record.get("files", []):
                print(f"{'':24s} {_display_path(_resolve(file))}")

    # ---- 共通処理 ------------------------------------------------------------

    def _run_task(self, key: str, build: Callable[[], dict], force: bool) -> dict | None:
        """key のタスクを完了させて返す。既に完了してファイルも保存済みなら None を返す."""
        record = self._state.get(key)
        if record is not None and not force:
            if record["status"] == "success" and "files" in record:
                print(f"  {key} は作成済みなのでスキップ（作り直すなら --force）")
                return None
            if record["status"] in ("pending", "success"):
                # 待ち途中で止まった / 保存前に止まった。新しく作らずに続きから
                print(f"  作成済みのタスク {record['task_id']} の続きから")
                return self._wait(key, record["task_id"])

        body = build()
        task_id = self._api().create_task(body)
        self._drop_downstream(key)
        self._state[key] = {"task_id": task_id, "type": body["type"], "status": "pending"}
        self._save()
        return self._wait(key, task_id)

    def _wait(self, key: str, task_id: str) -> dict:
        try:
            task = self._api().wait_task(task_id)
        except TripoError:
            self._state[key]["status"] = "failed"
            self._save()
            raise
        self._state[key].update(status="success", output=task["output"])
        self._state[key].pop("files", None)
        self._save()
        return task

    def _finish(self, key: str, paths: list[Path]) -> None:
        self._state[key]["files"] = [os.path.relpath(p, k_SCRIPT_DIR) for p in paths]
        self._save()

    def _drop_downstream(self, key: str) -> None:
        prefixes = k_DOWNSTREAM_KEYS.get(key, ())
        stale = [k for k in self._state if any(k == p or (p.endswith(":") and k.startswith(p)) for p in prefixes)]
        for k in stale:
            del self._state[k]
        if stale:
            print(f"  {key} を作り直すので、後ろの段階の記録を消しました: {', '.join(stale)}")

    def _files(self, key: str, missing_message: str) -> list[Path]:
        record = self._state.get(key)
        if record is None or record["status"] != "success" or "files" not in record:
            raise TripoError(missing_message)
        paths = [_resolve(f) for f in record["files"]]
        _require_files(paths)
        return paths

    def _done_task_id(self, key: str) -> str:
        record = self._state.get(key)
        if record is None or record["status"] != "success":
            raise TripoError(f"{key} がまだ完了していません")
        return record["task_id"]

    def _latest_concept(self) -> Path:
        done = [k for k, r in self._state.items() if k.startswith("concept_") and r["status"] == "success" and r.get("files")]
        if not done:
            raise TripoError("立ち絵がありません。concept を実行するか --image で画像を指定してください")
        return self._files(sorted(done)[-1], "")[0]

    def _command(self, stage: str) -> str:
        return f"python {_display_path(Path(__file__))} -c {_display_path(self._config_path)} {stage}"

    def _api(self) -> TripoClient:
        if self._client is None:
            raise TripoError("TRIPO_API_KEY が設定されていません（README を参照）")
        return self._client

    def _save(self) -> None:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)


def _resolve(path: str) -> Path:
    """設定ファイル内の相対パスは tools/tripo からの相対."""
    p = Path(path)
    return p if p.is_absolute() else k_SCRIPT_DIR / p


def _require_files(paths: list[Path]) -> None:
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise TripoError("ファイルがありません:\n  " + "\n  ".join(missing))


def _url_ext(url: str, default: str) -> str:
    return Path(urlparse(url).path).suffix or default


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", required=True, type=Path, help="キャラクター設定 JSON")
    parser.add_argument("stage", choices=("balance", "concept", "views", "model", "rig", "anim", "status"))
    parser.add_argument("--image", type=Path, help="views: 元にする立ち絵 / model: 1 枚から作る場合の画像")
    parser.add_argument("--proportion", help="concept: 設定の proportion を上書き（hero / chibi）")
    parser.add_argument("--force", action="store_true", help="作成済みでも作り直す（クレジットを使う）")
    args = parser.parse_args()

    api_key = os.environ.get("TRIPO_API_KEY")
    client = TripoClient(api_key, os.environ.get("TRIPO_BASE_URL", k_DEFAULT_BASE_URL)) if api_key else None
    pipeline = Pipeline(client, args.config, args.proportion)
    try:
        if args.stage == "balance":
            pipeline.balance()
        elif args.stage == "concept":
            pipeline.concept()
        elif args.stage == "views":
            pipeline.views(args.image, args.force)
        elif args.stage == "model":
            pipeline.model(args.image, args.force)
        elif args.stage == "rig":
            pipeline.rig(args.force)
        elif args.stage == "anim":
            pipeline.anim(args.force)
        else:
            pipeline.status()
    except (TripoError, requests.RequestException) as e:
        print(f"\nエラー: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
