"""版本号的唯一来源。

为什么单独建一个文件：以前标题、打包产物名、日志里各写各的字符串，
改版本时总会漏掉一处（尤其前后端分属两个进程入口）。
现在两边都从这里读，改一个地方就够。

**版本历史**
  2.0  队友交付基线：音乐 MV / 逐字卡拉OK / 按拍切镜，修掉界面假死等 22 个缺陷
  3.0  本地合并版：补回 V1 的 5 项性能与稳定性修复（启动提速、水彩画质三档、
       孤儿浏览器清场），界面按屏幕分辨率缩放（2K 屏字号 +33%）
  4.0  **架构换代**：Tkinter → WebView2 + FastAPI + React 19。
       - 新增 server/（22 个 API）与 web/（React 19 + TS + Tailwind 4 + Vite 8）
       - 新增 desktop.py：WebView2 原生窗口装 web/dist，CSS 动画/backdrop-filter
         这类 Tkinter 给不了的效果才做得出
       - 新增账号体系：注册/登录/手写 HS256 JWT、个人中心、按用户隔离成片

      版本号从 3.0.0 跳到 4.0.0，因为**启动方式、界面技术栈、数据模型**三样
      全变了：4.0 之前的界面代码一行也用不上，成片也多了归属关系。
      只改次位号会让队友误以为能直接覆盖升级。
  4.1  合并「剧本到分镜」拆解层：
       - 新增 `build_plan()` 与 `render_from_plan()` 双入口，共用同一套逐镜
         闭环与合成，杜绝"先拆再拍"与"一次跑完"两条路行为分叉
       - 新增 `server/plan.py`：拆 / 读 / 改剧本 / 改单镜 / 按拆解拍摄五个接口
       - 新增拆解页（`#plan/<run_id>`）：场次文字与单镜时长、画面、机位、台词
         可改，改完重算时间轴；越界不静默修正，直接拒绝并指出镜号
       - 拆解文件（`runs/<id>/plan/*.json`）成为拍摄阶段的唯一输入
  4.2  拆解层 C 期：**按场局部重拆**。
       - 新增 `POST /api/plan/{run_id}/acts/{act_name}/replan`：只重拆一场，
         其余场次的镜头一字不动，全片总时长锁定不变
       - 改剧本后 `stale` 从"只标不改"变成"标了可以按场重拆"，由人发起，
         不自动重派生——自动重派生会把逐镜调好的活儿整体冲掉
       - 该场若有手改过的镜头，默认 409 并要求 `force`，不静默覆盖
       - 镜号会随镜数变化顺移，`edited` / `stale` 的镜号跟着 `id_map` 映射，
         否则"改过"的徽章会标到别的镜头上
       - 修 `_act_units()`：一场戏常是一整句长句，按句末切会整句塞不进镜头而
         被丢弃，台词退化成"画面还在走。"这类兜底句
  4.3  清掉旧版前端：Tkinter 界面与它的一整套配套一起删除。
       - 删除 `app/gui.py` / `app/ui.py` / `app/theme.py`、入口 `yiyan_gui.py`、
         启动器 `一言片场.vbs` 与 `启动桌面版.bat` / `启动一言片场.bat`、
         Tk 专用打包脚本 `build.py`、`.venv-tk` 虚拟环境
       - 删除 8 个只服务于 Tk 界面的工具：`probe_gui_*`（4 个）、
         `shoot_gui` / `smoke_gui` / `verify_buttons` / `verify_gui_window`
       - 自检里 `[15]` 改为守 V4 的两个启动器；`[14]` 的主题令牌改为读
         `web/src/index.css`（深浅两套必须一一对应）；`[8]` 的打包规则改守
         `build_web.py`
       - 修打包缺件：`build_web.py` 原来只带模板的 `src/` 与 `studio.html`，
         漏了 `render.mjs` / `package.json` / `node_modules`——打包版会渲不出帧
  4.4  MV 入口：三种输入模式终于能在界面上选，此前只有命令行能出 MV。
       - 新增 `POST /api/media/audio`：上传音轨返回 token。边读边算哈希、边数
         字节，超 60 MB 立刻 413；落盘用 `.part` 再改名，避免半截文件占住哈希
       - token **不是路径**。正则挡格式、`resolve()` 后比对父目录挡符号链接，
         两道缺一不可——直接收客户端字符串等于把 ffmpeg 变成任意文件解析器，
         还能靠报错内容探测文件在不在
       - `RunRequest` 补 `audio` / `lyrics` / `music_role` / `bpm` / `beat_offset`
         / `auto_beats`。`music_role` 用 `Literal` 而不是 `str`：`is_bgm` 只认
         `"bgm"`，拼错一个字母就静默变成 MV，出片不报任何错
       - 创作台加模式开关、音轨拖放区、歌词框（可读 `.lrc`）、节拍行；MV 模式
         下时长栏转只读，因为总时长归音频管
       - 拆解页加音轨徽章：`onRender` 只传 run_id，徽章是唯一能确认音轨还生效
         的地方
       - 音轨随拆解落进 `plan/meta.json`，拍摄时**整组回落**（token / 歌词 /
         role / 节拍）。只回落 token 会让一份配乐拆解被悄悄拍成 MV
       - 音轨复制进 `runs/<id>/work/`：上传目录按 7 天 TTL 清理，而拆解可能几天
         后才被点「开始拍摄」，复制之后运行自带副本
       - 修 `_shot_bounds` 与 `_check_shots` 的 floor：两处都写死 2 秒，而
         `build_timeline` 用的是 `min(2, 总时长/镜数)`。4 秒音频拆 3 镜（每镜
         1.333 秒）于是「拆得出来、拍不了」。MV 模式下音频可以短到几秒，这个
         自相矛盾立刻暴露
  4.5  局部改片：改一镜不用重拍整片，改时长不用重画画面。
       - 单镜重拍 `POST /api/plan/{run_id}/shots/{shot_id}/render`：只重画这一镜，
         其余镜头复用 `work/clips` 里的旧片段，然后整片重新合成。一条 222 秒的
         MV 有 45 镜，为改一镜全渲一遍要等一小时——那"能改单镜"就只是摆设
       - 单镜重拍**不重算时间轴**（`_sync_starts` 只重排 `start`）。`_rebuild_timeline`
         为了让累加命中目标会微调镜长，而其余片段是按旧镜长渲的，镜长一动就整体
         音画错位且不报错
       - 单镜重拍**不对其余镜头重跑终检**。它们的帧缓存可能已被 `clean_frames`
         清掉，重跑只会被判"未达标"再按宁缺毋滥剔除——用一次重拍把整条片毁掉
       - 时间轴编辑 `POST /api/plan/{run_id}/recompose`：一个像素都不重画，按现在的
         镜长与顺序把已有片段对齐后重拼。新增 `compose.fit_clip()`——长了裁尾、
         短了 `tpad=stop_mode=clone` 冻结末帧，编码参数必须与 `KenBurns.make_clip`
         一致（`concat_clips` 走 stream copy，不一致直接拼不上）
  5.0  **出图/出片接入云端 + 修掉三个「提示词不生效」的老问题**。
       - 新增 `CloudImageProvider`（智谱 CogView-4）：中文提示词直接可用，
         1440×720，同步接口。此前这个类是桩，填了 Key 也不会调用
       - 新增 `CloudVideoProvider`（智谱 CogVideoX-2）：图生视频，
         1720×1080 / 5 秒，提交后轮询 `/async-result/{id}`，下载后
         ffmpeg **裁掉底部 7% 去掉「AI生成」水印**再交本地合成
       - **修 `compose_image_prompt` 丢弃 `visual`**：原来主体与动作取自
         `genre.subjects` / `genre.actions`（中文模板词），而 `visual` 只被
         当成 `stable_choice` 的哈希种子——**一个字都没进提示词**。所以
         无论用户写什么，出图永远是模板那句「一碗热汤面 + 掀帘进门」。
         现在改为：`visual` 原文放最前当主体，模板词退成追加修饰
       - **修 `sanitize_input` 误洗描述**：它本是给「给我做一条方城烩面宣传片」
         这类口语指令设计的（删时长词/删数字/按标点切段/丢短片段/无空格拼接），
         用户写的画面描述被它洗成粘连词串。现在长度 >40 字或逗号 ≥2 个就原样返回
       - **修 `theme` 截断**：`[:60]` → `[:300]`，且超长不再静默砍
       - **修「真图被丢弃」**：`_make_clip` 无条件 `render_still` 重画示意图当背景，
         `shot.frame_path` 里的真图从未被读取——日志写着"出图完成（comfy）"，
         成片里却是色块。现在按 `shot.still_provider` 判断：真图放大当背景，
         没有才重画；`require_real_image` 时无真图直接报错
       - 新增「必须真图」开关（`require_real_image`，默认开）：为真时不挂占位帧，
         云端不可用就明确报错，不再静默出色块
       - 修 `tool_guard` 拦死云端：`deep_review` 镜头一律标 `stakes=high` 导致
         每次付费调用都返回 confirm、静默降级 kenburns；勾了「必须真图」视为
         已授权付费
       - 修出片时狂弹终端窗口：新增 `pipeline/proc.py`，所有运行期子进程
         （ffmpeg / ffprobe / node / mklink）统一隐藏控制台
       - 修 `WinError 5` 崩溃：探测写完探针 png 要删，杀软占句柄导致
         `unlink` 抛错把整条流程带崩；改为尽力删除、失败忽略
       - 新增设备检测与体检报告（`pipeline/hardware.py` + `/api/device`）：
         真实探测显卡/显存/CPU/内存/磁盘/WebView2/Python/Node/ComfyUI/云端连通性，
         给出档位结论（sdxl / sd15 / none）与引导，可保存 `work/device-report.md`

       - 对齐结果写 `work/clips_fit/`，**绝不原地覆盖**源片段：重编码一次画质掉
         一次，裁短之后再也补不回来
       - 重合成复用配音：从 `work/vo/shotNN_raw.wav` 取上次的原始配音只重新
         `fit_audio`。台词没变，重合成一遍纯属浪费（云端 TTS 还要走网络），
         但镜长变了，拉伸与留白必须跟着变
       - 镜头重排 `POST /api/plan/{run_id}/shots/reorder`：只改顺序，镜号不动。
         让前端改 `id` 来实现重排，等于把片段文件名、`edited` 记录、单镜重拍的
         路径三处一起改坏。只收**完整置换**，缺项一律拒——允许缺项就是允许
         "悄悄删镜头"
       - 代码绘制模式同时拒绝单镜重拍与只重合成：它按整段分镜编译成一条视频，
         没有"只重画一镜"的入口，也没有逐镜片段可重拼。起任务前就拒，别等用户
         等完才发现白等
       - 打包补 `python_multipart` / `multipart` 到 `HIDDEN`。FastAPI 在
         `ensure_multipart_is_installed()` 里用 try/except 试导入它，静态分析当成
         可选依赖跳过——源码跑得好好的，打包版一上传音轨就 500
"""

from __future__ import annotations

#: 语义化版本。首位变动 = 功能级变化（界面/链路），次位 = 修复与优化。
__version__ = "5.0.0"

#: 中文名（界面标题、日志用）
APP_NAME = "一言片场"

#: 简短版本标签（标题栏展示，例如「一言片场 V3」）
VERSION_TAG = "V5"

#: 一句话定位
TAGLINE = "AI 视频智能生产线"

#: 完整标题：`一言片场 V3 · AI 视频智能生产线`
FULL_TITLE = f"{APP_NAME} {VERSION_TAG} · {TAGLINE}"

#: 备份/导出等场景用的文件前缀（含版本，便于区分来自哪一版）
FILE_STEM = f"YiYanStudio-{VERSION_TAG.lower()}"
