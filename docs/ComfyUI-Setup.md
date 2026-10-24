# 接入 ComfyUI 真实文生图

一键成片已经有了，但画面目前是**占位帧**（分镜示意图）。接上 ComfyUI 后会自动切成真实 AI 出图。

---

## 一、当前进度：**已接入并实测出图** ✅

| 环节 | 状态 |
|---|---|
| 工作流模板 `workflows/txt2img.json` | ✅ 8 节点，管线注入提示词/种子/分辨率 |
| provider 判定逻辑 | ✅ 服务与模板两项状态分别报告 |
| ComfyUI 服务端 | ✅ **ComfyUI 0.40.0 运行于 `D:\ComfyUI`**，RTX 4060 已识别 |
| PyTorch | ✅ **2.14.1+cu130**（CUDA 可用，矩阵运算通过） |
| checkpoint | ✅ **SD 1.5 已下载**（4.0GB，1145 张量，UNET+VAE 完整） |
| 真实出图 | ✅ **已产出 14 张**，管线端到端跑通 |
| 自检 | ✅ 85 项全通过（含 ComfyUI 工作流 11 项） |

一键启动：**双击 `D:\ComfyUI\..\一言片场\启动ComfyUI.bat`**，或在项目根双击 `启动ComfyUI.bat`。

体检输出：
```
出图 comfy     ✅ ComfyUI 0.40.0 ｜ txt2img.json（8 节点）
```

---

## 二、⚠️ 性能真相（重要，先看这个）

**这台机器出图很慢，约 2-3 分钟一张。** 原因不是代码，是显存：

```
GPU 总显存 8188 MB
Windows 桌面自身占用  -3.5 GB   ← explorer + 系统UI + WebView + Edge/微信/WPS/MuMu
ComfyUI 实测可用      5.7 GB
```

**8GB 独显里Windows 自己就吃掉 3.5GB**，留给 AI 的只有一半。实测每步耗时 34-68 秒。

### 提速手段与实测对比

| 配置 | 单张耗时 | 说明 |
|---|---|---|
| 768x512 / 24 步 / dpmpp_2m | **512-800 秒** | 初始配置，慢到不可用 |
| 640x384 / 14 步 / dpmpp_sde | 322-564 秒 | 中途优化 |
| **512x320 / 8 步 / euler** | **约 180 秒** | 当前配置 |

### 关键启动参数（缺一不可）

```bash
venv\Scripts\python.exe main.py --port 8188 --disable-dynamic-vram --lowvram --reserve-vram 0.5
```

| 参数 | 作用 |
|---|---|
| `--disable-dynamic-vram` | **最关键**。PyTorch 2.14 + ComfyUI 新默认会每次请求都重载模型，实测单次多花 300+ 秒 |
| `--lowvram` | 显存只有 5.7GB 可用，必须开|
| `--reserve-vram 0.5` | 留 0.5GB 余量避免 OOM |

### 想更快怎么办

1. **关掉占显存的程序**：Edge、微信、MuMu 模拟器、WPS、Nahimic 全都在抢显存，关掉能多挤出 1-2GB
2. **别开ComfyUI 界面**：只开API 服务即可（省一份显存）
3. **接受降级**：项目已有占位帧兜底，出图失败会降级，管线照常出片
4. **换模型**：FLUX 量化版在 5.7GB 显存下跑不动，别试

---

## 二、⚠️ 网络：本机可用与不可用的源

**这个环境的网络出口很不稳定**，同一分钟内某个站点可能从不通变通。以下是实测结论：

### 可用

| 源 | 用途 | 实测 |
|---|---|---|
| `mirrors.aliyun.com/pytorch-wheels/cu130/` | PyTorch cu130 wheel | ✅ 200（**装 torch 就靠它**） |
| `mirrors.aliyun.com/pypi/simple/` | 通用 pip 包 | ✅ 200 |
| `codeload.github.com` | GitHub 源码 zip | ✅ 200（**git clone 不通时用这个**） |
| `hf-mirror.com` | HuggingFace 模型 | ✅ 200（速度约 2MB/s，需续传） |
| `api.github.com` | GitHub API | ✅ 200 |

### 不可用（或间歇不可用）

| 源 | 实测 |
|---|---|
| `github.com`（git clone） | ❌ 502 CONNECT tunnel failed / 证书吊销错误 |
| `download.pytorch.org` / `download-r2.pytorch.org` | ❌ ReadTimeout |
| `huggingface.co` 官方 | ❌ 超时（用 hf-mirror） |
| `pypi.org` 官方 | ❌ ReadTimeout（用阿里云） |
| `pypi.tuna.tsinghua.edu.cn` | ⚠️ 时通时不通 |
| gitee 上的 ComfyUI 镜像 | ❌ 不存在（404 且需登录） |

### 两个必备技巧

**1. git clone 报证书错误时**
```bash
git -c http.schannelCheckRevoke=false clone --depth 1 <url>
```

**2. git clone 报 502 时，改下 zip**
```bash
curl -L -o src.zip https://codeload.github.com/<org>/<repo>/zip/refs/heads/master
```
解压后把内层目录展平即可。

**3. ���模型下载中断，用断点续传**
```bash
curl -L -C - --retry 20 --retry-delay 5 --retry-all-errors -o model.safetensors <url>
```
`-C -` 会从已有字节继续，不用重头下。

---

## 三、接入步骤（实测可用命令）

### 步骤 1 · 部署 ComfyUI

已装到 `D:\ComfyUI`（C 盘仅 34GB，torch+模型约 13GB，D 盘更宽裕）。

```bash
# 源码（git 不通时用 zip）
cd /d
curl -L -o comfyui_src.zip https://codeload.github.com/Comfy-Org/ComfyUI/zip/refs/heads/master
python -c "import zipfile; zipfile.ZipFile('comfyui_src.zip').extractall('.')"
# 解压后目录会是 ComfyUI-master，需要展平到 D:\ComfyUI

# venv（用 3.12.14，不要用项目的 3.11.9）
cd /d/ComfyUI
C:\Users\ZFY\AppData\Roaming\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe -m venv venv
```

### 步骤 2 · 装依赖（**必须用阿里云源**）

```bash
# PyTorch（download.pytorch.org 在本机不通）
venv\Scripts\python.exe -m pip install torch torchvision torchaudio \
  --index-url https://mirrors.aliyun.com/pytorch-wheels/cu130/ \
  --extra-index-url https://mirrors.aliyun.com/pypi/simple/ \
  --timeout 120 --retries 8

# 其余依赖
venv\Scripts\python.exe -m pip install -r requirements.txt \
  -i https://mirrors.aliyun.com/pypi/simple/ --timeout 120 --retries 10
```

实测装到 `torch-2.14.1 + torchvision-0.29.1 + torchaudio-2.11.0`，约 14 分钟。

### 步骤 3 · 下载模型

放到 `D:\ComfyUI\models\checkpoints\`：

| 模型 | 体积 | 8GB 显存 | 说明 |
|---|---|---|---|
| **SD 1.5** | 4.3GB | ✅ 轻松 | 先用这个验证链路，5-8 秒/张 |
| SDXL Base 1.0 | 6.9GB | ⚠️ 需 `--lowvram` | 画质更好，30-60 秒/张 |
| FLUX（量化） | 12GB | ❌ 吃力 | 暂不建议 |

```bash
cd /d/ComfyUI/models/checkpoints
curl -L -C - --retry 20 --retry-delay 5 --retry-all-errors \
  -o v1-5-pruned-emaonly.safetensors \
  https://hf-mirror.com/runwayml/stable-diffusion-v1-5/resolve/main/v1-5-pruned-emaonly.safetensors
```

> 下载约需 35 分钟且会中断多次，**务必带 `-C -` 断点续传**。

### 步骤 4 · 启动（本机实测参数）

```bash
cd /d/ComfyUI
venv\Scripts\python.exe main.py --port 8188 --disable-dynamic-vram --lowvram --reserve-vram 0.5
```

或直接双击项目里的 `启动ComfyUI.bat`（已内置这些参数）。

启动成功会看到：
```
Set vram state to: LOW_VRAM
Device: cuda:0 NVIDIA GeForce RTX 4060 Laptop GPU
To see the GUI go to: http://127.0.0.1:8188
```

### 步骤 5 · 校验接入

```bash
cd "C:\Users\ZFY\Desktop\AI video\一言片场"
python yiyan.py --check-env
```

看到这一行就成了：

```
出图 comfy     ✅ ComfyUI 0.40.0 ｜ txt2img.json（8 节点）
```

体检输出会**同时报出服务与模板两项状态**（例如「未响应；txt2img.json（8 节点）」），
一眼就能看出是服务没起还是模板没准备好。

### 步骤 6 · 跑一条片

```bash
python yiyan.py "给我的家乡方城烩面做一条 15 秒宣传片" --duration 15
```

3 个镜头 × 约 180 秒/张 ≈ **10-15 分钟**，请耐心等待。
日志里会看到 `comfy 出图完成` 说明走的是真实出图；若看到 `comfy 出图失败，已降级下一provider`
说明 ComfyUI 没响应或超时，会自动用占位帧，成片仍会产出。

---

## 四、显存与分辨率注意

8GB 显存下，模板默认的 1920x1080 **会 OOM**。两个办法：

**办法 A**：改模板分辨率（推荐，改一处即可）
编辑 `workflows/txt2img.json` 的节点 `4`：
```json
"width": 1280,
"height": 720
```

**办法 B**：让管线按显存自动降级（更省心）
在 `pipeline/images.py` 的 `_load_graph` 里，按实际可用显存给分辨率封顶。

> 注意：成片最终分辨率由 FFmpeg 合成阶段决定，出图分辨率低于成片会被放大，清晰度略降。1280x720 出图 → 1080p 成片，日常宣传片够用。

---

## 五、可选增强

| 想做什么 | 怎么做 |
|---|---|
| 角色一致性 | 装 IPAdapter 节点，参考图控制角色外观 |
| 面部保持 | 装 ReActor 节点 |
| 更省显存 | 启动加 `--reserve-vram 1` |
| 更快预览 | 启动加 `--fast` （牺牲细节） |

装节点推荐用 **ComfyUI-Manager**（一键装/更新）：

```bash
pip install -r manager_requirements.txt
python main.py --enable-manager
```

---

## 六、常见问题

**Q：日志报 `missing_node_type: Node 'ID #_meta' has no class_type`？**
模板里的 `_meta` / `_note` 说明字段被 ComfyUI 当成节点了。
管线提交前会自动剥离这些字段（`pipeline/images.py` 的 `_load_graph`），若你手工调模板后报这个错，检查是否绕过了管线直接提交了原始 JSON。

**Q：出图很慢（2-3 分钟一张）？**
正常，见第二节「性能真相」。核心是显存被 Windows 占了 3.5GB。
关掉 Edge/微信/MuMu/WPS 能提速。

**Q：报 `ComfyUI 等待超时（600s）`？**
ComfyUI 被别的任务占着排队。清队列：
```bash
curl -X POST http://127.0.0.1:8188/interrupt
curl -X POST http://127.0.0.1:8188/queue -H "Content-Type: application/json" -d "{\"clear\":true}"
```

**Q：启动报 `Torch not compiled with CUDA enabled`？**
装了 CPU 版。必须用官方源：`https://download.pytorch.org/whl/cu130`
（阿里云的 pytorch-wheels 镜像会给错包，装出来是 `+cpu`）。

**Q：启动报 `No module named 'comfy_aimdo'` 等？**
requirements 没装全。逐个装（避开批量删除）：
```bash
for p in comfy-aimdo comfy-kitchen av kornia spandrel pydantic pydantic-settings PyOpenGL comfy-angle scipy; do
  venv\Scripts\python.exe -m pip install $p -i https://mirrors.aliyun.com/pypi/simple/ --no-deps
done
```

**Q：pip 装依赖报 `SystemExit: 1`？**
批量安装时会触发批量删除保护。改为**逐个安装 + `--no-deps`**。

**Q：启动报 `No module named 'torch_npu'` / `hip` 警告？**
正常，那是 ComfyUI 在探测其他后端（昇腾/ROCm），忽略即可。关键看这行：
```
Found comfy_kitchen backend cuda: {'available': True, ...}
```

**Q：界面能开但点 Run 没反应？**
checkpoint 没放对。确认在 `models/checkpoints/`，且 Load Checkpoint 下拉框能选中。

---

*模板文件：`workflows/txt2img.json`；启动器：`启动ComfyUI.bat`；判定逻辑见 `pipeline/images.py` 的 `ComfyUIProvider`。*