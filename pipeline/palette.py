"""项目色板：只改配色，不改题材、文案与分镜。

题材（genre）本身带一套调色板，但"烩面"既可以拍成暖黄烟火气，也可以拍成
冷色纪实。色板是用户可撤销的视觉选择，不应该迫使他换题材（换题材会连带
改变解说词素材与画面装饰）。

实现上只是复制一份 Genre 并替换 `palette` / `accents`：题材识别、提示词、
文案素材全部保持原样，绘制层按新配色出图。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from .genre import Genre


@dataclass(frozen=True)
class Palette:
    id: str
    label: str
    hint: str
    palette: tuple[tuple[str, str], ...] = ()
    accents: tuple[str, ...] = ()


#: `auto` = 跟随题材自带配色，不覆盖。
PRESETS: tuple[Palette, ...] = (
    Palette("auto", "跟随题材", "使用题材自带的调色板"),
    Palette(
        "warm", "暖阳",
        "琥珀、麦色与暖白，适合食物、人物与烟火气",
        palette=(("#2A1608", "#5A2E10"), ("#8A4318", "#C97430"), ("#E8A24A", "#F6D08A")),
        accents=("#F6D08A", "#C97430", "#FFFFFF"),
    ),
    Palette(
        "cool", "冷夜",
        "深蓝、青灰与冷白，适合城市夜景与科技感",
        palette=(("#08131F", "#123049"), ("#1E5A78", "#3E93B0"), ("#8FD0DC", "#F2C879")),
        accents=("#8FD0DC", "#3E93B0", "#F2C879"),
    ),
    Palette(
        "film", "胶片",
        "低饱和的土色与奶白，适合纪实与回忆",
        palette=(("#1A1712", "#3A3226"), ("#6B5A44", "#A08A6A"), ("#D8C39E", "#F2E7D0")),
        accents=("#D8C39E", "#A08A6A", "#F2E7D0"),
    ),
    Palette(
        "ink", "水墨",
        "近黑、灰阶与宣纸白，适合国风与古建",
        palette=(("#0E1012", "#22262A"), ("#4A5054", "#8A9094"), ("#C9CFD2", "#F4F6F7")),
        accents=("#F4F6F7", "#8A9094", "#C9CFD2"),
    ),
    Palette(
        "vivid", "高对比",
        "深底 + 亮色，适合发布会与短视频封面",
        palette=(("#07090C", "#141A22"), ("#2B3A4A", "#4E6A8A"), ("#E8F1FF", "#FFFFFF")),
        accents=("#FFFFFF", "#4E6A8A", "#E8F1FF"),
    ),
)

_BY_ID = {item.id: item for item in PRESETS}


def resolve(palette_id: str | None) -> str:
    """非法/空值回落到 auto；接口层仍会先做一次 422 校验。"""
    key = str(palette_id or "").strip()
    return key if key in _BY_ID else "auto"


def apply(genre: Genre, palette_id: str | None) -> Genre:
    preset = _BY_ID.get(resolve(palette_id))
    if preset is None or preset.id == "auto" or not preset.palette:
        return genre
    return dataclasses.replace(genre, palette=preset.palette, accents=preset.accents)


def catalog() -> list[dict]:
    return [
        {
            "id": item.id,
            "label": item.label,
            "hint": item.hint,
            "colors": [dark for dark, _light in item.palette] + [item.accents[0]] if item.palette else [],
        }
        for item in PRESETS
    ]
