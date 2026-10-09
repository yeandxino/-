# 自媒体智能运营助手

一个面向**教育领域自媒体创作者**的多 Agent 协作系统。从选题到成片，六个专职 Agent 依次接力，把「今天发什么」变成一套可交付的成品。

主要面向小红书，同时产出抖音与公众号版本，内容风格为专业干货。

---

## 零基础使用（推荐，不需要懂代码）

只需要三步：

1. **双击项目里的 `启动.bat`**，浏览器会在几秒后自动打开操作页面
   （那个黑色窗口不要关，用完再关掉它即可）；
   熟悉命令行的话也可以执行 `streamlit run app.py`；
2. 在页面**左侧**选好「你的领域 / 要发到哪些平台 / 内容风格 / 视频画面比例」——
   这些设置会**自动保存**，下次打开还是上次的选择，不用重复填；
3. 在页面**中间**写下这次想做什么，点「开始生成」，
   结果出来后每份都能一键复制、一键下载。

**它是一个可以长期反复用的工具，不是一次性的：**

- 每次生成的内容都会自动存档，页面底部的「历史记录」随时能翻回以前的内容；
- 账号档案（领域、平台、风格）和历史选题会持续累积，越用越贴合你的账号定位；
- 已经生成过的选题会被自动避开，不会反复给你同一批题。

---

## 一、六个 Agent 各自负责什么

| Agent | 职责 | 产出 |
|---|---|---|
| 热点选题官 | 结合人设与历史选题，产出 5 个可执行选题 | 选题名、目标人群、切入角度、推荐理由、落地形式 |
| 标题创作官 | 5 种风格各出一个标题 | 悬念 / 数字 / 情绪 / 干货 / 反差，附效果说明 |
| 文案撰写官 | 一次性产出三平台成品 | 小红书图文、抖音分镜脚本、公众号文章 |
| 合规审核官 | 词表检查 + 语境判断 | 风险分级、逐句改写、三平台审核差异 |
| 运营顾问 | 发布策略 | 发布时间、标签策略、互动话术、1/24/72 小时节奏 |
| 视频成片生成官 | 调用 MoneyPrinterTurbo 出片 | 中文 9:16 竖屏 MP4（配音 + 字幕 + 背景音乐） |

编排采用 **Orchestrator-Workers** 模式：每个 Agent 有独立的 `system_prompt`，互不共享上下文，只通过总调度显式传参。单个环节出错不会污染整条链路。

---

## 二、目录结构

```
my-project/
├── main.py            # 总调度 + 命令行入口
├── agents.py          # 6 个 Agent 的 system_prompt 与构建函数
├── tools.py           # 三个自定义工具：合规检查 / 记忆 / 视频生成
├── memory.py          # JSON 记忆系统（短期 5 条 + 长期可检索）
├── config.py          # 环境变量、路径、常量集中管理
├── check_llm.py       # 接口连通性自检
├── server.py          # FastAPI 对外接口（可选）
├── app.py             # Streamlit 网页界面（可选）
├── .env               # 凭证（已加入 .gitignore，切勿提交）
├── .env.example       # 配置模板
└── data/
    ├── memory.json        # 运行时自动生成
    ├── banned_words.txt   # 违禁词库，可自行增删
    └── outputs/           # 每轮产物按时间戳落盘
```

---

## 三、快速开始

### 1. 环境要求

- Python 3.11
- [uv](https://docs.astral.sh/uv/)（视频引擎强制要求）
- FFmpeg（MoneyPrinterTurbo 会自动下载，一般无需手动安装）

### 2. 安装依赖

```powershell
cd C:\Users\86188\Documents\my-project
pip install -r requirements.txt
```

### 3. 配置 .env

复制 `.env.example` 为 `.env` 并填写。**所有 Key 只从环境变量读取，代码里没有硬编码。**

```ini
# 大模型：靠前的不可用时自动回退到后面的
LLM_PROVIDER_ORDER=modelscope,dashscope

MODELSCOPE_API_KEY=ms-xxxxxxxx
MODELSCOPE_BASE_URL=https://api-inference.modelscope.cn/v1
MODELSCOPE_MODEL=deepseek-ai/DeepSeek-V4.1-Flash

DASHSCOPE_API_KEY=sk-xxxxxxxx
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen-plus

# 推理模型的思考过程也计入 token，给足余量
LLM_MAX_TOKENS=8192

# 视频引擎
MPT_ROOT=D:/workspace/MoneyPrinterTurbo
MPT_SKILL_DIR=D:/workspace/MoneyPrinterTurbo/skill
MPT_VIDEO_SOURCE=coverr
MPT_COVERR_API_KEY=xxxxxxxx
MPT_TASK_TIMEOUT=3600

# 国内必配：直连 pypi.org 装依赖会慢到不可用
MPT_PYPI_MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple

# 素材检索词（英文短词）。Coverr 是精确匹配，长短语一律搜不到
MPT_VIDEO_TERMS=education,study,homework,student,learning,writing
```

### 4. 先做一次连通性自检

```powershell
python check_llm.py            # 逐家测试
python check_llm.py --models   # 附带列出可用模型 ID，排查模型名写错
```

### 5. 运行

```powershell
# 命令行
python main.py "孩子写作业总拖拉，家长怎么引导"
python main.py "初中生手机管理" --pick 2     # 选用第 2 个选题
python main.py "高中生时间管理" --no-video   # 跳过视频渲染

# 网页界面
streamlit run app.py

# 对外 API
python server.py     # http://127.0.0.1:8000/docs
```

---

## 四、MoneyPrinterTurbo Skill 集成说明

### 4.1 这是什么

[MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) 是开源的 AI 短视频自动生成工具，输入主题即可自动完成视频文案生成、素材匹配、TTS 配音、字幕添加与背景音乐合成，最终输出可用的 MP4。

它提供了官方 Agent Skill（`name: moneyprinterturbo-video`，本项目对接时版本为 **1.3.2**），任何具备终端、网络、文件系统访问能力的 AI Agent 都可以通过它完成安装、配置与渲染。

官方 Skill 说明：https://raw.githubusercontent.com/harry0703/MoneyPrinterTurbo/main/docs/skill/SKILL.md

### 4.2 本项目如何集成

`tools.py` 里的 `MoneyPrinterTurboVideoTool` 封装了整条调用链，由 `agents.py` 的 `VideoAgent` 持有并调用。

核心命令（与官方要求完全一致）：

```bash
uv run --no-project --python 3.11 python mpt_agent.py --subject "<视频主题>" --root D:/workspace/MoneyPrinterTurbo -- --video-source coverr --video-aspect 9:16 --language zh-CN --voice-name zh-CN-XiaoxiaoNeural-Female
```

关键点：**脚本名必须用相对的 `mpt_agent.py`，靠工作目录定位**。官方明确说明，Windows 上终端校验器会吃掉绝对路径里的反斜杠，写绝对路径会直接失败。因此工具的 `cwd` 固定设为 `MPT_SKILL_DIR`。

### 4.3 自动处理的事情

| 场景 | 本项目的处理 |
|---|---|
| Skill 文件不存在 | 自动从官方仓库下载 `SKILL.md` 与 `mpt_agent.py`（三路镜像：raw / jsdelivr / fastly） |
| 首次运行 | 由官方 helper 自动下载整个项目到 `MPT_ROOT`（几百 MB） |
| 缺少 Coverr Key | 自动写入 `config.toml` 的 `coverr_api_keys`，**全程不打印内容** |
| 缺少大模型 Key | 自动把本项目的大模型配置透传为 `MPT_LLM_*` 环境变量 |
| 退出码 10 | Key 本地就有则自动补配置后重跑一次；补不了才一次性问用户 |
| 终端输出被截断 | 只读一次 `latest-result.json` 判定结果 |
| 失败 | 只回报短错误 + 日志尾部 15 行，不灌整份日志 |

### 4.4 退出码语义

| 退出码 | 含义 | 处理方式 |
|---|---|---|
| 0 | 成功 | 从 `MPT_RESULT` 块提取 `VIDEO_FILE` 交付 |
| 1 | 失败 | 读取 `MPT_ERROR` 与日志尾部后回报 |
| 10 | 缺凭证 | 解析 `MPT_NEEDS_INPUT`，能自动补的补，剩下的汇总成一次请求 |

### 4.5 默认输出规格

- 画面比例可选：`9:16`（抖音/小红书竖屏）、`3:4`（小红书图文感）、`1:1`、`4:3`、`16:9`（公众号/B站）
- Edge TTS 中文女声（`zh-CN-XiaoxiaoNeural-Female`）
- 自动字幕（`subtitle_provider = "edge"`，**不会**触发 Whisper 大模型下载）
- 背景音乐 + Coverr 免费素材
- 更多参数可通过 `--video-args` 原样透传给 MoneyPrinterTurbo CLI

> **关于 3:4 和 4:3**：MoneyPrinterTurbo 原生只接受 `9:16`、`16:9`、`1:1` 三种比例。
> `3:4` 会先用 `9:16` 渲染，`4:3` 会先用 `16:9` 渲染，然后再用 ffmpeg
> **补模糊背景边**转换成目标比例——只补边、不裁画面，内容不会丢。
> 实测 1080×1920（9:16）转换后为 1440×1920，正是标准 3:4。

### 4.6 安装位置为什么不在 D 盘根目录

本机 `D:\` 根目录属性为 `ReadOnly, Hidden, System`，ACL 只授予普通用户 `ReadAndExecute` 权限，**无法在根目录创建文件夹**（提权同样被拒）。实测 `D:\workspace`、`D:\86188` 可写，因此默认安装到：

```
D:\workspace\MoneyPrinterTurbo\   # 项目本体（首次运行自动下载）
D:\workspace\mpt-skill\           # SKILL.md + mpt_agent.py
```

**为什么 Skill 目录要独立在外面？** 官方 helper 的安装校验要求安装根目录「不存在」或「是空目录」或「已是有效项目」。如果把 `skill` 子目录放进安装根目录，helper 会检测到「目录存在、有内容、但不是有效项目」并直接报错拒绝安装（实测报错：`installation directory exists but is not a valid project`）。

想换位置，改 `.env` 里的 `MPT_ROOT` 与 `MPT_SKILL_DIR` 即可。

---

## 五、记忆系统

落地为 `data/memory.json`，读写用「临时文件 + 原子替换」，进程中断不会写坏文件。文件损坏时自动备份为 `*.corrupt-时间戳.bak` 并重建。

- **短期记忆**：最近 5 条，直接拼进 Agent 的 prompt；
- **长期记忆**：全部历史，用「中文单字 + 二元组」轻量分词做检索；
- **人设档案**：领域 / 平台 / 风格偏好，保证人设一致；
- **选题去重**：用序列相似度拦截「换词不换题」的伪新选题。

---

## 六、常见问题

**Q：Agent 返回空字符串，界面一片空白？**

大概率是推理模型的 token 被思考过程吃光了。`DeepSeek-V4.1-Flash` 这类模型的 `reasoning_content` **同样计入 `completion_tokens`**，实测 `max_tokens=2048` 时正文只剩 229 字就被截断。本项目已做三层防护：`LLM_MAX_TOKENS` 默认 8192、正文为空时自动翻倍重试、响应解析异常给出中文提示。仍不够就继续调大该值。

**Q：工具调用为什么不让模型自动触发？**

HelloAgents 的工具调用走文本协议（模型输出 `[TOOL_CALL:名称:参数]`，框架用正则解析 `key=value`）。中文长文案里只要出现逗号，参数就会被切错位。因此本项目把工具绑定在 Agent 对象上、由代码直接调用（`ComplianceAgent.check()`、`VideoAgent.generate()`），去掉模型拼字符串这个不稳定环节。

**Q：Coverr 的 Key 为什么没有环境变量入口？**

MoneyPrinterTurbo 官方只为 Pexels 提供了 `MPT_PEXELS_API_KEY`，Coverr 只能写进 `config.toml` 的 `coverr_api_keys`。本项目会在渲染前自动写入，且从不打印该文件内容。

**Q：视频卡在"下载素材"阶段，日志里全是 `found 0 videos`？**

Coverr 走的是**精确 / 短语匹配**，不是模糊搜索。实测：

```
education / homework / study / student / learning   → 返回素材
third grade homework / homework routine / task list kid → 返回 0 条
```

模型自动生成的英文检索词往往又长又具体（如 `homework task breakdown`），在 Coverr 上一个都搜不到。本项目默认用 `MPT_VIDEO_TERMS` 提供一组短关键词；要更贴合内容，可在 `.env` 里改这组词，或用 `--video-args "--video-terms study,child,book"` 临时覆盖。

**Q：首次安装依赖卡住几十分钟？**

大概率是直连 PyPI 太慢（实测 7 分钟只下载 1.3 MB）。`.env` 里已默认配置 `MPT_PYPI_MIRROR` 走清华镜像，实测提速约 25 倍。若镜像也不稳，可换成阿里云 `https://mirrors.aliyun.com/pypi/simple/`。

**Q：PowerShell 里中文显示乱码？**

Windows 默认代码页是 GBK。`main.py` 已在入口强制 UTF-8 输出；手动跑其他脚本时可先执行 `$env:PYTHONIOENCODING="utf-8"`。

**Q：视频渲染要多久？**

首次运行需要下载项目本体（几百 MB）并安装依赖，之后再跑通常 5-20 分钟。超时上限由 `MPT_TASK_TIMEOUT` 控制，默认 1200 秒。渲染期间不要中断，官方要求终端超时至少 20 分钟。

**Q：可以并发跑多个视频吗？**

不可以。官方要求不要并发执行视频任务，`server.py` 用互斥锁把整条流水线串行化了。

---

## 七、安全说明

- 所有密钥只从环境变量 / `.env` 读取，代码中无任何硬编码；
- `.env` 已加入 `.gitignore`，不会进入版本库；
- `config.redact()` 会对 subprocess 输出、异常信息、工具返回值统一脱敏；
- 服务默认只监听 `127.0.0.1`，不会把带凭证的接口暴露到局域网；
- 永远不会打印 API Key、Token 或完整的 `config.toml`。

> 若密钥曾在对话、截图或公开渠道中出现过，建议在各自后台轮换一次。
