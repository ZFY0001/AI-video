# 一言片场 V5

**一句话进，成片出。** 输入一句主题、或「一句话 + 音频 + 歌词」，全自动完成：
简报 → 剧本 → 分镜 → 出图 → 质检 → 重试 → 出片 → 配音 → 字幕 → 合成。

> 版本号定义在 `app/version.py`（当前 `4.5.0`），界面标题与 `/api/meta` 都会显示。
>
> **V4 是架构换代**，不是功能叠加：
> - 界面：Tkinter → **WebView2 + React 19 + Tailwind 4**（`desktop.py` 装 `web/dist`）
> - 后端：新增 FastAPI（`server/`，27 条路由，其中拆解层 9 条）
> - 账号：注册 / 登录 / JWT / 个人中心，成片按用户隔离
>
> **4.1 加了「剧本到分镜」拆解层**：可以先只拆解、在拆解页改场次与单镜，
> 再从这份拆解文件拍摄。改完重算时间轴，越界直接拒绝并指出镜号。
>
> **4.2 加了按场局部重拆**：改了一场的文字后，可以在那一场点「重拆这一场」，
> 只让模型重写这一场的镜头，其余场次一字不动、全片总时长锁定不变。
> 这一场若有你手改过的镜头，会先问一次再覆盖。
>
> **4.3 删掉了旧版前端**：Tkinter 那一套（`app/gui.py` / `app/ui.py` /
> `app/theme.py`、`yiyan_gui.py`、`一言片场.vbs`、`build.py`、`.venv-tk`）
> 连同只服务于它的 8 个探针脚本一起移除。界面只剩 WebView2 + React 一条路径，
> 打包脚本只剩 `build_web.py`。
>
> **4.4 补回 MV 入口**：删掉 Tkinter 之后，音乐 MV 与配乐模式在界面上消失了，
> 只剩命令行能出。现在创作台多一行「模式」，选了 MV 就出现音轨与歌词，
> 拖进去就能出片。上传走 `POST /api/media/audio`，返回的 token 只当文件名解析
> （正则 + 前缀两道检查），音轨随拆解存进 `plan/meta.json`，从拆解页拍摄不会掉音轨。

技术定位见 [docs/技术方案-V1.md](docs/技术方案-V1.md)，提示词全部集中在
[docs/提示词库-V1.md](docs/提示词库-V1.md)。
**本次改动的交付说明见 [交付说明.md](交付说明.md)**（改了什么 / 接下来要做什么）。

---

## 快速开始（从源码跑，约 10 分钟）

### 你需要什么

| 项 | 版本 | 为什么 |
|---|---|---|
| **Python** | 3.10+（推荐 3.11/3.12） | 跑后端 |
| **Node.js** | 18+（推荐 20/22） | 构建 / 开发前端 |
| **Git** | 任意新版本 | 拉代码 |
| **FFmpeg** | 任意 | 出片合成；**仓库里没带**，下一步会装 |

> **本仓库不含任何 API Key**（`.env` 在 `.gitignore` 里）。
> 想出真画面需要自己申请模型服务的 Key —— 见下面第 3 步。

### 三步跑起来

```
# 1. 拉代码（克隆后的目录名是 AI-video，不是中文名）
git clone git@github.com:ZFY0001/AI-video.git
cd AI-video

# 2. 一次性装依赖 + 下载 FFmpeg（约 213 MB）
python setup.py --install --ffmpeg
python setup.py                      # 自检：看到「可以出片了」就 OK

# 3. 配自己的 Key（不配也能跑，但只能出占位帧）
copy .env.example .env
#   编辑 .env，最少填这两行：
#     MINIMAX_API_KEY=你的Key
#     MINIMAX_BASE_URL=https://api.minimax.chat
```

### 启动

```
cd web && npm ci && npm run build    # 首次或改过前端时才需要
cd .. && python -m server            # 后端，默认 8000（被占会自动往后找）
```

浏览器打开 **http://127.0.0.1:8000**；也可以双击 `启动客户端.bat` 开原生窗口。

### 一个 Key 都不配会怎样

能跑通整条链路并出片，但画面是**占位帧**（程序画的色块 + 镜号 + 字幕），
用来验证「分镜 → 画面 → 视频 → 成片」这条流水线。**想要真画面必须配 Key。**

### 换模型（不局限一家）

默认走 **MiniMax** 最便宜档：`image-01` 出图（约 0.05 元/张）+
`MiniMax-Hailuo-2.3-Fast` 出片（约 0.32 元/秒）。
换一家只改 `.env` 四行：

```ini
IMAGE_PROVIDER=zhipu        # zhipu 或 minimax
IMAGE_MODEL=cogview-4
VIDEO_PROVIDER=zhipu
VIDEO_MODEL=cogvideox-2
```

可选模型、单价、分辨率限制都在 `GET /api/models` 里，源码在
`pipeline/model_registry.py`。写了不存在的模型名**不会崩**，会自动回落默认。

---

## 现在能跑到什么程度（诚实清单）

| 能力 | 状态 | 说明 |
|---|---|---|
| **Windows 客户端** | ✅ 可跑 | WebView2 原生窗口装 React 前端，四页导航；管线跑在本地 FastAPI 里，界面不阻塞 |
| **三种输入模式** | ✅ 可跑 | ①一句话解说短片 ②音乐 MV（音频+歌词）③配乐模式（解说+背景音乐）；创作台顶部切模式 |
| **音轨上传** | ✅ 可跑 | 拖进创作台即可，服务端 ffprobe 验真（改后缀的假音频当场拒），落盘 7 天自动清理 |
| **三种画幅** | ✅ 可跑 | 16:9 / 9:16 / 1:1，三种都真渲验证过（1920×1080 / 1080×1920 / 1080×1080） |
| **逐字卡拉OK 歌词** | ✅ 可跑 | 墨条 + 已唱字符金色 + 未唱米白，随演唱从左到右填充 |
| **按拍切镜** | ✅ 可跑 | 纯标准库节拍估计（不依赖 librosa/numpy），镜头切点吸附拍点 |
| **音乐混音** | ✅ 可跑 | 音频定时长权威；`amix` + `loudnorm` + 淡入淡出；BGM 垫底增益 0.28 |
| 一句话 → 简报/剧本/分镜 | ✅ 可跑 | 离线引擎可跑；配 `LLM_API_KEY` 后勾「真实模型」走真实大模型 |
| 逐镜头 路由 → 生成 → 质检 → 重试 闭环 | ✅ 可跑 | 决策层本地规则引擎；阈值与升级规则写死在代码里 |
| N 选 1 候选裁决 | ✅ 可跑 | 关键镜头自动出 3 候选并裁决 |
| 成片 MP4（H.264 + AAC） | ✅ 可跑 | 本地 FFmpeg，1080p；**必定带音轨**（静音时补 `anullsrc`） |
| 中文配音 | ✅ 可跑 | Windows SAPI（Microsoft Huihui），按实测语速 3.6 字/秒校准稿长 |
| SRT 字幕 | ✅ 可跑 | UTF-8 BOM；MV 模式下用**歌词时间轴**而不是解说词 |
| **代码绘制后端** | ✅ 可跑 | p5.js 水彩引擎逐帧真画。**本机拿到"真画面"的推荐路径**，约 1 秒/帧 |
| 打包为 EXE | ✅ 可跑 | `python build_web.py` → `dist/YiYanStudio-V4/`，**FFmpeg 与渲染模板都已随包** |
| **拆解层（先拆后拍）** | ✅ 可跑 | 拆解页可改场次文字、单镜时长/画面/机位/台词；改完再拍，拍的就是你看到的 |
| **按场局部重拆** | ✅ 可跑 | 只重拆这一场，其余场次一字不动；该场有你手改过的镜头时默认拒绝并列出镜号 |
| **单镜重拍** | ✅ 可跑 | 只重画这一镜，其余镜头复用上次的片段，然后整片重新合成 |
| **时间轴编辑** | ✅ 可跑 | 改镜长、挪顺序之后点「只重合成」：一个像素都不重画，只把片段对齐后重拼 |
| 历史成片管理 | ✅ 可跑 | 界面内浏览/播放/看分镜/删除 |
| **真实文生图（ComfyUI + SD1.5）** | ⚠️ 需 N 卡 | 代码完整（体检→提交→轮询→下载）；**但需要有 NVIDIA 显卡的机器**，本机跑不动 |
| **云端出图 / 云端视频** | ❌ 未实现 | 适配器是桩；**填了 key 也不会调用**（故意的防误花钱保护） |
| **Jev 决策层实调** | ⚠️ 未验证 | 需 `JEV_API_KEY`；2026-10-14 核查时官方端点返回 HTTP 451 |
| **REA 工作流逆向** | ⚠️ 未接入 | 解析层设计与提示词已就绪，代码尚未落地 |

> ### ⚠️ 关于「占位帧」
> 默认画面是 PIL 合成的**分镜示意图**（色块＋三分线＋镜头信息文字），
> 用来验证「分镜 → 画面 → 视频 → 成片」整条管线，**不是 AI 出的图**。
>
> **想要真画面，勾选「代码绘制」** —— 用 p5.js 水彩引擎逐帧真画（天空／三层山／
> 角色／前景草叶／墨线）。它不依赖显卡、不依赖任何 API，体检显示已就绪。
> 界面底栏会实时告诉你当前画面到底怎么来的。

---

## 三种输入模式

创作台 → 「模式」一行切换（`解说` / `MV` / `配乐`）。切到后两种时下方会展开音轨面板：
拖入音频、贴歌词、按需填 BPM。

| 模式 | 输入 | 行为 |
|---|---|---|
| **解说短片** | 一句话 | 时长按界面「时长」字段；Windows 中文语音配音；字幕＝解说词 |
| **音乐 MV** | 一句话 + 音频 + 歌词 | **总时长以音频为准**（时长栏变只读，显示探测到的秒数）；不出配音；按拍切镜；歌词逐字上屏 |
| **配乐模式** | 一句话 + 音频 + 歌词 | 保留配音解说；音乐压低垫底；音乐比片子短会自动循环铺满；时长仍按界面字段 |

歌词支持 `.lrc`（最准，按时间轴对齐）与 `.txt`（按字数均分并吸附拍点），
也可以直接在框里改——歌词只在浏览器里读，不上传，改了立刻生效。

音轨跟着拆解一起存进 `runs/<id>/plan/meta.json`，所以从拆解页点「开始拍摄」
不会掉音轨。上传目录本身按 7 天清理，但运行目录里存了副本，老拆解照样能重拍。

---

## 图形界面（推荐用法）

```bash
启动客户端.bat          # 原生窗口（WebView2 装 web/dist，无浏览器地址栏）
启动Web版.bat           # 本地服务 + 默认浏览器打开，改前端时用这个
启动Web版.bat rebuild   # 顺手重新构建前端再启动
```

> 两个启动器都是纯 ASCII、会先 `cd` 到脚本目录、失败时 `pause` 停住——
> 这三条都有对应自检（`tests/test_pipeline.py` 的 `[15]`），
> 因为「双击一闪就没」是这类脚本最常见也最难自查的毛病。

顶部四个导航入口：

| 入口 | 功能 |
|---|---|
| **创作台** | 选模式（解说 / MV / 配乐）→ 填主题 → 调参数 → 勾「代码绘制」→ 开始生成；实时日志（SSE 推送）、进度、成片预览 |
| **成片墙** | 浏览历史运行，播放/看分镜/下载/删除；`mine=1` 只看自己的 |
| **设置** | 填 Key 与端点（密钥掩码）；一键环境体检，三态显示 |
| **个人中心** | 账号信息与用量统计 |

另有**拆解页**（`#plan/<run_id>`，从创作台的拆解入口进）：改场次文字与单镜时长、
画面、机位、台词，改完重算时间轴；越界不静默修正，直接指出镜号。
改完某一场的文字后，那一场会出现「重拆这一场」。

**音乐 MV / 配乐**：创作台「模式」一行切过去，下方展开音轨面板——拖入音频、
贴歌词（可读 `.lrc`）、按需填 BPM 与首拍。详见上文「三种输入模式」。

**界面主题**：右上角一键切浅色/深色，立刻生效并持久化到 `localStorage`；
令牌表在 `web/src/index.css`（`@theme` + `:root` 浅色，`[data-theme="dark"]` 深色），
深浅两套必须一一对应——少一个令牌就会有一块地方在深色下白得刺眼，
这条也有自检（`[14]`）。

---

## 命令行用法（进阶/批处理）

```bash
# 环境体检（配了 key 必跑；会真发一次最小请求验证）
python yiyan.py --check-env

# 离线跑通一条 15 秒片（不需要任何 API key）
python yiyan.py "给我的家乡方城烩面做一条 15 秒宣传片" --duration 15

# ★ 想要"真画面"而不是占位帧 —— 加 --painted
python yiyan.py "介绍一下城市郑州" --duration 15 --painted

# 接入真实大模型（先在 .env 填 LLM_API_KEY，或界面里填）
python yiyan.py "城市夜景宣传片" --duration 30 --live

# 竖版短视频
python yiyan.py "新品手机发布" --aspect 9:16 --duration 15

# 音乐 MV（音频 + 歌词 → 逐字卡拉OK）
python yiyan.py "夜里的城市" --audio "歌.m4a" --lyrics-file "词.lrc" --painted

# 竖屏 MV
python yiyan.py "夜里的城市" --aspect 9:16 --audio "歌.m4a" --lyrics-file "词.lrc" --painted

# 长片：降帧率 + 编码后清帧（3 分钟 1080p 的帧缓存约 2.4GB）
python yiyan.py "..." --painted --painted-fps 12 --clean-frames
```

常用参数：`--audio` `--lyrics` `--lyrics-file` `--music-role {song,bgm}`
`--bpm` `--beat-offset` `--no-beats` `--painted` `--painted-fps` `--clean-frames`

---

## 三条降级路径（本项目的可靠性设计）

管线**不依赖任何单一外部服务**，每一层都有自己的兜底：

| 层 | 首选 | 不可用时自动降级为 | 触发条件 |
|---|---|---|---|
| 创意层 | 语言大模型 | 内置离线创意引擎（题材词库） | 无 `LLM_API_KEY` 或未勾「真实模型」 |
| 决策层 | Jev 官方 API | 本地规则引擎（阈值与升级规则一致） | 无 `JEV_API_KEY`，或 Jev 调用失败 |
| 出图层 | 本地 ComfyUI | 离线占位帧合成 / 代码绘制 | ComfyUI 未响应或缺少工作流模板 |
| 出片层 | 云端图生视频 | FFmpeg Ken Burns 运动片段 | 未配置云端 API（**默认拒绝付费调用**） |
| 配音层 | 云端 TTS | Windows SAPI → 静音轨 | 未配置云端 TTS / SAPI 不可用 |

> **注意创意层是例外**：它**没有兜底**。明确勾了「真实模型」但 key/端点填错时，
> 跑片会**直接失败并报错**，而不是偷偷用离线文案糊过去。
> 这是有意的 —— 否则用户永远看不出 key 到底有没有生效。

**付费护栏**：云端生成前必过 `tool_guard`，需同时具备「质检证据 + 预算校验」才允许执行。
默认配置下，管线**一次付费调用都不会发生**（云端适配器目前根本没接入）。

---

## 需要配什么

**一个 key 都不配也能出片**（离线引擎 + Windows 中文语音 + 占位帧/代码绘制）。

| Key | 作用 | 不配的后果 |
|---|---|---|
| `LLM_API_KEY` + `LLM_BASE_URL` + `LLM_MODEL` | 创意层真实大模型（**只影响文案，不影响画面**） | 走内置离线引擎，文案较平 |
| `JEV_API_KEY` + `JEV_BASE_URL` | 决策层 Jev API | 走本地规则引擎 |
| `TTS_API_KEY` + `TTS_BASE_URL` | 云端配音 | 用 Windows SAPI（够用） |
| `COMFYUI_BASE_URL` | 真实文生图 | 占位帧 / 代码绘制 |
| `VIDEO_API_KEY` + `VIDEO_API_BASE_URL` | 云端视频 | 未实现适配器，填了也不生效 |

**DeepSeek 填法**（端点这栏怎么写都行，程序会自动规整）：

```
LLM_BASE_URL = https://api.deepseek.com      （或 .../v1）
LLM_MODEL    = deepseek-chat
LLM_API_KEY  = sk-...
```

填完 **先在界面勾「真实模型」，再点「环境体检」** 确认连通，然后才生成。

### 本机依赖

- Windows、Python 3.13（`.venv` 就是 3.13；C 扩展是 `cp313`，换版本会 ABI 不匹配）
- **FFmpeg**（`bin\ffmpeg.exe` + `bin\ffprobe.exe`，共 426 MB）
- 代码绘制额外需要 **Node.js ≥ 18 + Edge/Chrome 任一**

> ⚠️ **从 git clone 下来的话，`bin/` 和 `node_modules/` 是空的**——这两样因为体积
> 太大（213 MB 单文件超过 GitHub 上限）故意没进仓库。先跑一次环境准备：
>
> ```powershell
> python setup.py                  # 体检：缺什么、怎么补，都会列出来
> python setup.py --install --ffmpeg   # 自动装依赖 + 下载 FFmpeg
> ```
>
> 装好后 `python setup.py` 应显示「可以出片了」。
> 直接拿打包好的 zip 的用户不需要这一步（FFmpeg 已随包）。

---

## 目录结构

```
一言片场/
├── 启动客户端.bat            # 原生窗口（WebView2 装 web/dist）
├── 启动Web版.bat             # 本地服务 + 浏览器；带 rebuild 参数
├── 启动ComfyUI.bat           # 可选：接真实文生图时启动 ComfyUI
├── desktop.py               # 桌面外壳：同进程起 uvicorn + pywebview 开窗
├── yiyan.py                 # 命令行入口（离线/接模型都能跑）
├── build_web.py             # 打包脚本（PyInstaller → dist/YiYanStudio-V4/）
├── setup.py                 # 环境准备：体检 / 装依赖 / 下载 FFmpeg
├── requirements.txt         # 运行依赖（Pillow + PyYAML + FastAPI + pywebview）
├── 交付说明.md               # 交接说明（改了什么 / 接下来要做什么）
├── app/                     # 引擎层
│   ├── engine.py            # 引擎桥接：线程化管线 + 事件回调 + 历史扫描 + 自检
│   └── version.py           # 版本号唯一来源
├── server/                  # FastAPI 后端（24 条路由）
│   ├── app.py               # 应用装配、静态资源、SSE 日志流
│   ├── plan.py              # 拆解层：拆 / 读 / 改剧本 / 改单镜 / 按场重拆 / 拍摄
│   ├── auth.py              # 注册登录、手写 HS256 JWT、归属校验
│   ├── history.py           # 成片墙与可见性规则（只写一份）
│   ├── jobs.py              # 任务队列（同一时刻只跑一个）
│   └── media.py             # 成片/封面/字幕的媒体通道
├── web/                     # React 19 + TS + Tailwind 4 + Vite 前端
│   ├── src/pages/           # 创作台 / 成片墙 / 拆解页 / 设置 / 个人中心 / 登录
│   ├── src/lib/api.ts       # 全部接口与类型（含 ApiError 的 409 分支）
│   └── dist/                # 构建产物（客户端装的就是它）
├── pipeline/                # 管线代码
│   ├── config.py            # 配置与运行上下文（含 EXE 形态路径解析）
│   ├── net.py               # 统一出网口（本机地址绕过系统代理）
│   ├── prompts.py           # 提示词库加载器（真相源头是 docs/）
│   ├── llm.py               # 创意层：大模型客户端 + 离线创意引擎
│   ├── genre.py             # 离线引擎题材词库 + 输入清洗
│   ├── decide.py            # 决策层：Jev 客户端 + 本地规则 + 强制升级保险
│   ├── images.py            # 出图 provider（ComfyUI / 占位帧 / 云端）
│   ├── image_render.py      # 占位帧合成
│   ├── beats.py             # 节拍估计（纯标准库）
│   ├── lyrics.py            # 歌词解析与对齐（.lrc 优先）
│   ├── video.py             # 出片 provider（FFmpeg Ken Burns / 云端）
│   ├── tts.py               # 配音 provider + SRT 字幕
│   ├── compose.py           # 合成层（混音 / loudnorm）
│   ├── painted.py           # 代码绘制后端：分镜 → p5.js 水彩场景
│   └── pipeline.py          # 主流程编排
├── prompts/prompts.yaml     # 由文档自动抽取生成（勿手改）
├── vendor/painted-template/ # 代码绘制引擎（p5.js + p5.brush + render.mjs，MIT）
├── tools/                   # 端到端验证工具（见下）
├── tests/test_pipeline.py   # 管线自检（476 项）
├── docs/                    # 技术方案 / 提示词库 / 使用手册 / 事实核查
└── workflows/               # ComfyUI 工作流模板（API 格式，8 节点）
```

---

## 自检与验证

```bash
.venv\Scripts\python tests/test_pipeline.py   # 476 项：清洗/病句/提示词库/决策阈值/契约/
                                            #   节奏/时间轴/主题令牌/启动器/打包规则/端到端
python yiyan.py --check-env              # 环境体检
python yiyan.py --audit-prompts          # 提示词库与文档是否同步
cd web && npx tsc --noEmit               # 前端类型检查（零错误再提交）
```

> 自检用的是**项目内的 `.venv`**（3.13）。换解释器跑会踩到 C 扩展的 ABI 不匹配：
> 装的是 `cp313` 的 `.pyd`，在 3.14 下 `from PIL import Image` 直接失败。
> 症状是「明明装了 Pillow 却说没有 `_imaging`」——这不是缺包，是版本对不上。

### 端到端验证工具（会真渲染/真跑片，共 123 项断言）

| 工具 | 项数 | 验什么 |
|---|---|---|
| `tools/verify_painted_audio.py` | 13 | 代码绘制成片真的有音轨、帧缓存按签名清理 |
| `tools/verify_music_mix.py` | 12 | 混音增益/淡入淡出/loudnorm（FFT 量比值） |
| `tools/verify_beats.py` | 22 | BPM 与首拍估计精度（合成节拍轨） |
| `tools/verify_lyrics.py` | 21 | `.lrc`/`.txt` 对齐、字幕改歌词 |
| `tools/verify_karaoke.py` | 17 | **逐字卡拉OK真的画上去了**（暖色像素单调增长） |
| `tools/verify_aspect.py` | 20×3 | 三种画幅的帧尺寸、成片分辨率、字条位置 |
| `tools/verify_llm_live.py` | 18 | 真实大模型通路（模拟 OpenAI 兼容服务，会先移开 `.env`） |
| `tools/verify_painted.py` | — | 代码绘制端到端（真渲染，约 3 分钟） |
| `tools/verify_exe_form.py` | — | 模拟 EXE 形态验证（只读解包目录） |

### 关于那些"真机才出问题"的缺陷

4.3 删掉的那批 `probe_gui_*` 探针，当年是为了定位 Tkinter 界面的假死
（"点几下就卡死"）而写的：单看每个方法的耗时都只有几十毫秒，完全正常，
只有"主线程跑满 CPU + 内存无界增长"这个特征才能把元凶逼出来。

现在界面换成 React，这类问题换了个形式存在（长任务堵住 SSE、整表重渲染），
但定位思路没变：**看证据，不看感觉**。所以保留的不是探针，而是自检里
那几条"会真的起子进程/真的读文件"的断言——它们比调方法可靠得多。

---

## 打包与分发

```bash
cd web && npm run build && cd ..      # 先出 web/dist，客户端装的就是它
pip install pyinstaller
python build_web.py                  # 产物：dist/YiYanStudio-V4/
```

> **打包踩坑（必读）**
>
> 1. PyInstaller 6.x 的**单文件模式在路径含非 ASCII 字符时会报**
>    `Could not load PyInstaller's embedded PKG archive`，现象是**双击毫无反应、
>    进程空转不退出也不报错**。所以默认目录模式，`build_web.py` 还会先把源码
>    复制到 ASCII 临时目录再构建，从源头规避。
> 2. **代码绘制的模板要整套带**：`render.mjs`、`package.json`、`node_modules`
>    缺一样，打包版就渲不出帧，而源码模式永远复现不出来（模板就在项目里、可写、
>    依赖就在旁边）。`[8]` 那条自检专门盯这个。
> 3. 分发时**整个 `YiYanStudio-V4/` 文件夹一起拷走**，别漏 `_internal`。

**打包前务必确认**：

1. `.env` 里**没有明文 Key**（当前已清空，`.gitignore` 已排除）
2. `runs/`、`work/`、`__pycache__/` 已清理
3. 打包产物在**纯英文路径**下双击跑一次，确认能开窗、能体检、能出片

分发给别人后：

1. 对方解压到**纯英文路径**（如 `D:\YiYanStudio\`）——含中文的路径会导致双击无反应
2. 双击 `YiYanStudio.exe`
3. 右上角「环境体检」确认「环境就绪，可以出片」
4. 写一句话 → 想要真画面就勾「代码绘制」→「开始生成」→「播放成片」

---

## 关键设计决策（都是踩过坑换来的）

1. **提示词只有一个真相源头**：`docs/提示词库-V1.md`。代码不许写死提示词，
   改动流程是「改文档 → `python tools/extract_prompts.py` → 校验」。
2. **阈值与升级规则写死在代码里，之上再加保险**：无论决策层返回什么，
   `apply_escalation` 都会把重试动作**只升不降**。模型可以建议，管线负责兜底。
3. **运动只作用于背景，文字层单独叠加**：文字若跟着 Ken Burns 一起缩放，
   标题和字幕会被推出画外。现在背景做运动、文字以原始分辨率 overlay。
4. **文字层只画一次**：出片用的输入必须是干净背景，否则会叠成重影。
5. **zoompan 不用状态依赖表达式**：`z='min(zoom+步长,上限)'` 从第 2 帧起行为不可靠；
   改用帧号 `on` 直接算缩放值，逐帧确定。
6. **解说词有节奏硬约束**：按实测语速做全局字数预算分配，长句在标点处切分，
   保证不出现空镜。**装不下的整句会被丢弃**，所以镜头别太短（4 秒镜头约 16 字）。
7. **配音越界不静默**：配音超过镜头时长且已到 1.35× 限速上限时，写入人工关注清单。
8. **打包后路径必须重新解析**：单文件 EXE 里 `__file__` 指向临时目录。
   `config.resolve_root()` 在 EXE 形态下改用 EXE 所在目录，`video.py` 按
   「随包 → 项目 bin/ → PATH」三级定位 ffmpeg。
9. **降级不是故障**：ComfyUI 没启动、云端 Key 没配都属于设计内降级。
   环境体检用 ok/warn/bad 三态而不是两态，避免把正常状态说成故障。
10. **随包资源与用户数据是两条路径**：`ROOT` 是用户数据区，随包数据走
    `config.resource_dir(name)` —— 曾经到处写 `ROOT / "workflows"`，
    打包后必然报「缺少工作流模板」。
11. **本机地址必须绕过系统代理**：Windows 开了系统代理时，`urllib` 会把对
    `127.0.0.1` 的请求也发给代理，返回 502 —— 表现是"ComfyUI 未响应"，
    排查方向整个是错的。见 `pipeline/net.py`。
12. **绝对像素尺寸按"短边"归一，不是按高**：场景里有一批按 1080 标定的绝对尺寸
    （角色身量、草叶、字幕字号）。三个受支持画幅短边都是 1080，所以换算系数
    恒为 1、横竖屏输出一致；换 720p/1440p 时才缩放。

---

## 已知限制

- **代码绘制慢**：约 1.0–1.8 秒/帧。15 秒片 ≈ 6 分钟、3 分钟片 ≈ 75 分钟。
  好消息是**可断点续渲**（中断后重跑接着没渲完的帧继续），且可 `--painted-fps` 降帧率
- **真实文生图需要 N 卡**：本机只有 Intel Arc 核显，跑 SD 1.5 不实用。
  代码是完整的，换一台有 ≥6GB 显存的 N 卡机器即可
- **云端出图/视频适配器未实现**（故意的防误花钱保护）
- 单条 15 秒片（3 镜）占位帧模式出片约 43 秒
- 占位帧合成器只认 6 类题材（美食/城市/人物/产品/国风/通用），其他回落通用版式
- 离线创意引擎的解说词是**模板化**的，真实创意质量需配 `LLM_API_KEY` 才能评估
- **长片（3 分钟 / 30 镜）尚未实测**，时间轴与字幕累积误差待验证
- 卡拉OK 字体 `Ma Shan Zheng` 未随包，会回退到楷体，观感打折
- 场景层只有一套结构（天空→三层山→主体→前景→字幕），镜头间差异主要靠机位缩放，
  整体观感偏统一
- `render.mjs` 的 `--window-size=1920,1080` 仍写死（当前不影响，截的是 canvas）
- 引擎指南写着「歌词不要写在画面上」，与本项目的逐字卡拉OK**直接冲突** ——
  我们是**显式选择覆盖**它的（MV 场景必须上屏歌词）
- **无版本控制历史**：项目此前没有 `.git`，现已提供 `.gitignore`，建议尽快 `git init`

---

## 合规边界

- 只逆向**自己拥有或合法授权**的目标，不破解付费 / DRM 内容
- 第三方工作流遵守其开源协议（GPL / MIT 等），注明出处
- REA 输出保留证据链，关键结论人工抽查

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [交付说明.md](交付说明.md) | **本次改动清单 + 接下来要做什么（P0/P1/P2/P3）** |
| [docs/技术方案-V1.md](docs/技术方案-V1.md) | 分层架构与设计取舍 |
| [docs/提示词库-V1.md](docs/提示词库-V1.md) | 提示词唯一真相源头 |
| [docs/使用手册.md](docs/使用手册.md) | 界面操作详解 |
| [docs/ComfyUI-Setup.md](docs/ComfyUI-Setup.md) | 接真实文生图的完整步骤 |
| [docs/事实核查-V1.md](docs/事实核查-V1.md) | 外部事实核查记录 |

---

## 许可

[MIT](LICENSE) © 2026 一言片场团队

第三方组件：`vendor/painted-template` 来自
[tuzhechen2005/opus-video-skills](https://github.com/tuzhechen2005/opus-video-skills)（MIT），
其依赖 p5.js（LGPL-2.1）、p5.brush（MIT）、puppeteer-core（Apache-2.0）。
完整声明见 [LICENSE](LICENSE) 末尾。

## 参与

```powershell
.venv\Scripts\python tests/test_pipeline.py    # 管线自检：476 项，约 1 分钟
cd web; npx tsc --noEmit; npm run build; cd .. # 前端：类型零错误 + 能构建
```

改完这两条要全绿再提交。想连真实链路再跑一遍端到端工具
（`tools/verify_*.py`，会真渲染，见上表）——它们慢，但能抓到单测抓不到的东西。
