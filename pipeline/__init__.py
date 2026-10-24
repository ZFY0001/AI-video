"""一言片场 · 管线包。

模块分层（对应 docs/技术方案-V1.md 第 4 章）：
    config      配置与运行上下文
    prompts     提示词库加载（源头是 docs/提示词库-V1.md）
    llm         创意层：语言大模型客户端 + 离线创意引擎
    decide      决策层：Jev 客户端 + 本地规则引擎 + 强制升级保险
    images      执行层：出图 provider（ComfyUI / 离线占位帧 / 云端）
    video       执行层：出片 provider（FFmpeg 运动片段 / 云端视频）
    tts         执行层：配音 provider + 字幕
    compose     合成层：FFmpeg 拼接与封装
    pipeline    主流程编排
"""

__version__ = "0.1.0"
