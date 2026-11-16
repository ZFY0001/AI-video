"""迭代2残留审计探针：复用路径（只重合成 / 单镜重拍）的片段指纹是否还能命中。

背景：W05 把 `image_sha256` 纳入片段指纹，而 `shot_from_dict()` 在从磁盘读回
分镜时会清空运行期字段（含 image_sha256）。若复用路径直接用读回的分镜算指纹，
就会与拍摄时写下的指纹不一致，被判 stale 而拒绝重合成。

只读探针 + 临时目录，不触碰任何真实项目。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline import clipcache, models as m  # noqa: E402
from pipeline.genre import pick_genre  # noqa: E402
from pipeline.pipeline import Pipeline  # noqa: E402


def fake_pipe(work: Path) -> Pipeline:
    pipe = object.__new__(Pipeline)
    pipe.genre = pick_genre("food")
    pipe.palette_id = "auto"
    pipe.preview = False
    pipe.work_scale = 2
    pipe.still_scale = 2.0
    pipe.fixture_scale = 1.0
    pipe.guides = False
    pipe.video_chain = []
    pipe.image_chain = []
    pipe._video_size = (1920, 1080)
    pipe._workflow_hash = "wf-probe"
    pipe.only_shot = 0
    pipe.stopped = False
    pipe.should_stop = lambda: False
    pipe.ctx = SimpleNamespace(work_dir=work, project_dir=work,
                               out_dir=work / "output", notes=[])
    pipe.log = SimpleNamespace(event=lambda *a, **k: None)
    return pipe


def main() -> int:
    out: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="probe-clipstate-") as td:
        work = Path(td)
        brief = m.ProjectBrief(theme="深夜食堂", aspect_ratio="16:9")
        script = m.Script(title="深夜食堂", logline="一碗面", tone="温暖",
                          target_audience="夜归人", hook="蒸汽")
        shot = m.Shot(id=1, act="第一幕", visual="蒸汽升起的面碗", camera="特写",
                      narration="一碗面的温度", duration=6.0, start=0.0,
                      prompt_image="steam over noodle bowl", acceptance="蒸汽清晰")
        board = m.Storyboard(project={}, shots=[shot])
        pipe = fake_pipe(work)

        # 拍摄阶段：选中图摘要已绑定，指纹按真实 sha 写下。
        shot.image_sha256 = "sha-selected-image"
        shot.frame_path = str(work / "frames" / "shot01_a1.png")
        shot.frame_source = "offline"
        key_shot = pipe._clip_key(shot, brief, board)
        clips = work / "clips"
        clips.mkdir(parents=True, exist_ok=True)
        clip = clips / "shot01.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-clip")
        clipcache.write_key(clip, key_shot, provider="kenburns")

        # 复用阶段：分镜从磁盘读回（plan/storyboard.json 不含运行期证据）。
        plan_json = json.loads(json.dumps(board.to_dict(), ensure_ascii=False))
        plan_json["shots"][0].pop("image_sha256", None)
        reloaded = m.storyboard_from_dict(plan_json)
        key_reload = pipe._clip_key(reloaded.shots[0], brief, reloaded)
        out["sha_after_reload"] = reloaded.shots[0].image_sha256
        out["key_match_before_restore"] = key_shot == key_reload
        out["clip_state_before_restore"] = clipcache.state(clip, key_reload)

        def gate(label: str, fn) -> None:
            """只关心"指纹闸门"是否放行；后续 ffmpeg/合成失败属探针 harness 边界。"""
            try:
                fn()
                out[label] = "passed"
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                blocked = ("旧片段与当前分镜不一致" in msg
                           or "但它们的片段还是旧的" in msg
                           or "缺少缓存指纹" in msg)
                out[label] = "stale-rejected" if blocked else "passed-gate"
                out[f"{label}_downstream"] = f"{type(exc).__name__}: {msg[:160]}"

        # 场景 A：没有 output/storyboard.json（证据不可读）→ 必须如实拒绝。
        gate("recompose_no_evidence", lambda: Pipeline._recompose(pipe, brief, script, reloaded))

        # 场景 B：正常拍摄后合成会写下 output/storyboard.json，复用路径应能命中。
        out_dir = work / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "storyboard.json").write_text(
            json.dumps(board.to_dict(), ensure_ascii=False), encoding="utf-8")
        reloaded_b = m.storyboard_from_dict(plan_json)
        gate("recompose_with_evidence", lambda: Pipeline._recompose(pipe, brief, script, reloaded_b))
        out["sha_after_restore"] = reloaded_b.shots[0].image_sha256
        out["key_match_after_restore"] = (
            pipe._clip_key(reloaded_b.shots[0], brief, reloaded_b) == key_shot)

        # 单镜重拍：目标镜之外的镜头同样要过指纹校验。
        shot2 = m.Shot(id=2, act="第一幕", visual="老板递面", camera="中景",
                       narration="趁热", duration=6.0, start=6.0,
                       prompt_image="chef hands over bowl", acceptance="手部清晰")
        shot2.image_sha256 = "sha-second"
        board2 = m.Storyboard(project={}, shots=[shot, shot2])
        clip2 = clips / "shot02.mp4"
        clip2.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-clip2")
        clipcache.write_key(clip2, pipe._clip_key(shot2, brief, board2), provider="kenburns")
        # 指纹含总镜数：两镜场景下镜1的指纹要按两镜重算，否则是探针自身口径不一致。
        clipcache.write_key(clip, pipe._clip_key(shot, brief, board2), provider="kenburns")
        (out_dir / "storyboard.json").write_text(
            json.dumps(board2.to_dict(), ensure_ascii=False), encoding="utf-8")
        pipe.only_shot = 2
        reloaded2 = m.storyboard_from_dict(json.loads(json.dumps(board2.to_dict(), ensure_ascii=False)))
        gate("single_shot_with_evidence",
             lambda: Pipeline._render_single_shot(pipe, brief, script, reloaded2))

        # 内容真的改过时，恢复证据不能掩盖失效：仍须拒绝。
        edited_json = json.loads(json.dumps(board2.to_dict(), ensure_ascii=False))
        edited_json["shots"][0]["visual"] = "完全不同的画面"
        edited = m.storyboard_from_dict(edited_json)
        pipe._restore_reuse_state(edited)
        out["content_edited_state"] = clipcache.state(
            clip, pipe._clip_key(edited.shots[0], brief, edited))
        out["content_edited_sha_restored"] = edited.shots[0].image_sha256

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())