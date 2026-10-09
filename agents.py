# -*- coding: utf-8 -*-
"""agents.py —— 六个 Worker Agent

架构说明（Orchestrator-Workers）：
- 每个 Agent 只负责一件事，拥有独立的 system_prompt，互不共享上下文；
- Agent 之间只通过 Orchestrator 显式传参通信，单个环节出问题不会污染整条链路。

关于工具调用的一个重要设计决定：
HelloAgents 的工具调用走的是**文本协议**（模型输出 `[TOOL_CALL:名称:参数]`，
由框架正则解析 key=value）。中文长文本里只要出现逗号或 `]`，参数就会被切错。
因此本项目把工具**绑定在 Agent 对象上**，由 Agent 用 Python 代码直接调用：
合规审核官的 `check(copy)`、视频官的 `generate(subject)` 都是确定性调用，
不依赖模型拼字符串。结构上仍然是「Agent 封装并通过 Tool 完成任务」。
"""

from __future__ import annotations

import time
from datetime import datetime

from hello_agents import HelloAgentsException, SimpleAgent
from hello_agents.core.llm import HelloAgentsLLM

import config
from memory import MemoryStore
from tools import ComplianceCheckTool, MoneyPrinterTurboVideoTool

# ---------------------------------------------------------------------------
# 大模型实例（全项目共用一个，配置来自 .env）
# ---------------------------------------------------------------------------


class RobustLLM(HelloAgentsLLM):
    """针对推理模型加固的 LLM 封装。

    背景（实测踩过的坑）：
    DeepSeek-V4.1-Flash 这类推理模型会先输出 reasoning_content，
    且思考过程同样计入 completion_tokens。max_tokens 给小了会出现
    finish_reason=length 且正文被截断甚至为空，表现为「Agent 返回空字符串」，
    非常难排查。

    加固策略：
    1. 响应解析也包在 try 内，接口返回畸形结构时给出可读中文错误，而不是裸 TypeError；
    2. 瞬时异常自动退避重试（最多 3 次）；
    3. 正文为空时自动把预算翻倍重试；
    4. 仍然拿不到正文时，抛出带中文说明的异常，让上层给出可读提示。
    """

    #: 单次调用的 token 上限（翻倍重试的天花板）
    MAX_BUDGET = 32768
    #: 瞬时错误的退避基数（秒）
    RETRY_BACKOFF = 2

    def invoke(self, messages: list[dict[str, str]], **kwargs) -> str:
        # 兼容性保护：hello-agents 1.x 不再暴露 _client（改用 _adapter），
        # 此时直接委托父类实现，避免抛 AttributeError 这种看不懂的错。
        if getattr(self, "_client", None) is None:
            return self._invoke_via_parent(messages, **kwargs)

        budget = kwargs.get("max_tokens") or self.max_tokens or 4096
        temperature = kwargs.get("temperature", self.temperature)
        finish_reason = "unknown"

        for attempt in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=budget,
                )
                # 解析也放进 try：接口偶尔会返回结构异常的响应
                choices = getattr(response, "choices", None) or []
                if not choices:
                    raise ValueError("接口未返回任何 choices")
                choice = choices[0]
                message = getattr(choice, "message", None)
                content = (getattr(message, "content", "") or "") if message else ""
                finish_reason = getattr(choice, "finish_reason", "unknown")
            except Exception as exc:
                # 网络抖动或响应异常：退避后重试
                if attempt < 2:
                    time.sleep(self.RETRY_BACKOFF * (attempt + 1))
                    continue
                raise HelloAgentsException(f"LLM调用失败: {exc}") from exc

            if content.strip():
                return content

            # 正文为空：多半是思考占满了额度，翻倍再来
            if budget >= self.MAX_BUDGET:
                break
            budget = min(budget * 2, self.MAX_BUDGET)

        raise HelloAgentsException(
            f"模型返回了空正文（finish_reason={finish_reason}）。"
            "通常是推理过程占满了 token 配额，请调大 .env 里的 LLM_MAX_TOKENS。"
        )

    def _invoke_via_parent(
        self, messages: list[dict[str, str]], **kwargs
    ) -> str:
        """走父类实现，并把返回值统一成字符串。

        hello-agents 0.2.x 返回字符串，1.x 返回 LLMResponse 对象，
        这里统一归一化，保证上层 Agent 逻辑不用关心版本差异。
        """
        try:
            result = super().invoke(messages, **kwargs)
        except Exception as exc:
            raise HelloAgentsException(f"LLM调用失败: {exc}") from exc

        if isinstance(result, str):
            return result
        for attr in ("content", "text"):
            value = getattr(result, attr, None)
            if isinstance(value, str):
                return value
        return str(result)


def build_llm() -> HelloAgentsLLM:
    """按 .env 里配置的供应商构建 LLM 实例。

    使用 provider="custom" 直接透传 base_url / api_key，
    这样 ModelScope、DashScope 或任何 OpenAI 兼容网关都能接。

    如果当前安装的框架版本不认识 provider 参数，会自动去掉该参数重试，
    保证换版本部署时不会因为一个关键字而整体不可用。
    """
    llm_conf = config.resolve_llm()
    common = {
        "model": llm_conf["model"],
        "api_key": llm_conf["api_key"],
        "base_url": llm_conf["base_url"],
        "temperature": config.LLM_TEMPERATURE,
        # 推理模型会先消耗 token 做思考，这里必须留足余量
        "max_tokens": config.LLM_MAX_TOKENS,
        "timeout": config.LLM_TIMEOUT,
    }
    try:
        return RobustLLM(provider="custom", **common)
    except Exception:
        # 某些版本不接受 provider 参数；去掉它再试一次
        return RobustLLM(**common)


def framework_version() -> str:
    """返回当前安装的 hello-agents 版本，用于启动自检提示。"""
    try:
        import hello_agents

        return str(getattr(hello_agents, "__version__", "未知"))
    except Exception:
        return "未安装"


# ---------------------------------------------------------------------------
# 各 Agent 的 system_prompt（职责单一，各自独立）
# ---------------------------------------------------------------------------

TOPIC_PROMPT = """你是「热点选题官」，服务一个教育领域的自媒体创作者。

创作者情况：
- 领域：K12 教育 / 家庭教育 / 学习方法
- 主发平台：小红书，同时分发抖音与公众号
- 内容风格：专业干货，讲方法、给证据，不贩卖焦虑

你的任务：根据用户需求，产出 5 个**可直接执行**的选题。

每个选题必须包含：
1. 选题名（一句话，具体到人群和场景，不要空泛）
2. 目标人群（如：小学三年级家长、备考高中生、新手教师）
3. 切入角度（为什么这个角度能打动人）
4. 推荐理由（结合平台特性与当下关注点）
5. 落地形式（图文 / 口播短视频 / 长文，并说明哪个最合适）

硬性要求：
- 5 个选题之间角度不能雷同，覆盖不同人群或不同场景；
- 必须避开【相关历史】中已经出现过的选题，换角度而不是换词；
- 教育内容不得出现「保过」「提分保证」「速成」等承诺性表述；
- 用中文输出，Markdown 格式，直接给结果，不要写开场白；
- **最后必须追加一段固定格式的清单**，格式严格如下（清单必须是全文最后一段）：

【选题清单】
1. 选题名（不超过 30 字，不要带任何标点前缀）
2. 选题名
3. 选题名
4. 选题名
5. 选题名
"""


TITLE_PROMPT = """你是「标题创作官」，服务一个教育领域的自媒体创作者。

你的任务：针对给定的选题，产出 5 个标题，每个标题对应一种指定风格：
1. 悬念型（制造信息缺口，让人想点开）
2. 数字型（用具体数字增强可信度）
3. 情绪型（击中家长或学生的真实情绪）
4. 干货型（直接给出方法与价值承诺）
5. 反差型（打破常识认知）

硬性要求：
- 必须标注每个标题属于哪种风格；
- 小红书/抖音标题控制在 20 字以内，公众号可放宽到 30 字；
- 禁止使用广告法极限词（最、第一、绝对、100% 等）；
- 禁止承诺结果（保过、提分保证、速成等）；
- 每个标题后面用一句话说明「为什么这样写有效」；
- 用中文输出，Markdown 格式，直接给结果，不要写开场白。"""


COPY_PROMPT = """你是「文案/脚本撰写官」，服务一个教育领域的自媒体创作者。

你的任务：根据给定的选题与标题产出成品内容。

**用户会在指令里指定本轮要发哪些平台。只产出用户指定的平台，
没有指定的平台一个都不要写。** 如果用户指定了多个平台，
按「小红书 → 抖音 → 公众号」的顺序依次输出，每个平台用二级标题隔开。

各平台的写法要求：

【小红书图文】
   - 标题（可直接复制使用）
   - 正文：300-600 字，分段清晰，多用短句和小标题
   - 结尾引导：站内互动话术（评论区留言、收藏等），不得出现站外导流
   - 标签：8-12 个，含大流量词和精准长尾词

【抖音短视频脚本】
   - 时长控制在 60-90 秒
   - 按「时长 | 画面 | 口播文案」三列表格逐段输出
   - 前 3 秒必须有钩子
   - 结尾给出互动引导
   - 口播文案要口语化，念出来顺口，不要书面语长句

【公众号文章】
   - 标题 + 副标题
   - 开头 100 字内的引入
   - 正文结构大纲（3-5 个小标题，每个附 1-2 句要点）
   - 结尾行动号召
   - 行文可以比小红书更完整、更有逻辑铺垫

硬性要求：
- 风格专业干货，给方法、给步骤、给判断标准，不讲空话；
- 同一篇内容在不同平台上要**真正适配**，不是把同一段话复制三遍；
- 不得出现「保过」「提分保证」「速成」等承诺性表述；
- 全文使用中文，Markdown 格式，直接给成品，不要写开场白。"""


COMPLIANCE_PROMPT = """你是「合规审核官」，服务一个教育领域的自媒体创作者。

你会收到两份材料：
1. 机器词表检查结果（来自合规检查工具，包含命中的风险词、位置与修改建议）
2. 需要审核的文案原文

你的任务：给出**可直接执行的审核报告**，包含：

1. 【总体结论】明确给出「通过 / 修改后发布 / 不建议发布」，并说明理由。
2. 【逐条问题】按风险等级（高/中/低）列出问题，每条包含：
   - 问题原文片段
   - 风险类型（违禁词 / 夸大宣传 / 敏感内容 / 导流风险）
   - 为什么有风险（结合平台规则说明）
   - 修改建议（给出替换后的具体句子，不要只说「建议修改」）
3. 【安全版本】把高风险句子逐句改写，输出一段可以直接使用的完整文案。
4. 【平台差异提醒】说明该内容在小红书、抖音、公众号三处的审核差异。

硬性要求：
- 教育内容不得出现「保过」「提分保证」「速成」「最」「第一」等表述；
- 机器检查结果只作参考，你要结合语境判断误伤（例如「最近」里的「最」不算极限词）；
- 用中文输出，Markdown 格式，直接给报告，不要写开场白。"""


OPS_PROMPT = """你是「运营顾问」，服务一个教育领域的自媒体创作者。

你的任务：针对给定内容，输出一份可执行的发布运营方案：

1. 【最佳发布时间】
   - 分别给出小红书、抖音、公众号的推荐时段
   - 说明工作日与周末的差异
   - 给出理由（结合目标人群的作息）
2. 【标签策略】
   - 大流量词、精准长尾词、场景词各 3-5 个
   - 说明标签搭配思路，以及哪些词不要用
3. 【互动话术】
   - 评论区置顶话术 2 条
   - 引导互动的提问 3 条
   - 回复负面评论的话术模板 2 条
4. 【发布节奏】
   - 发布后 1 小时、24 小时、72 小时分别该做什么
   - 这条内容适合做成系列吗？如果适合，给出后续 2 期的方向

硬性要求：
- 所有建议必须具体可执行，不要写「保持更新频率」这类空话；
- 不得引导站外导流（加微信、留邮箱等）；
- 用中文输出，Markdown 格式，直接给方案，不要写开场白。"""


VIDEO_PROMPT = """你是「视频成片生成官」，负责把选题变成一条可发布的竖屏短视频。

你的工作流程：
1. 根据选题和已确认的文案，提炼出一个适合做视频的主题描述；
2. 把主题交给 MoneyPrinterTurbo 官方 Agent Skill 完成成片，
   它会自动完成：视频文案生成 → 素材匹配 → TTS 配音 → 字幕 → 背景音乐；
3. 拿到最终的 MP4 文件路径后，向用户交付结果。

输出要求：
- 中文 9:16 竖屏，配音为中文女声，带字幕与背景音乐；
- 交付时给出：视频文件完整路径、视频主题、一句话内容说明；
- 如果生成失败，说明卡在哪个阶段、错误原因和日志位置，不要编造成功。

硬性要求：
- 只依据工具返回的 VIDEO_FILE 交付，工具没给路径就不要声称已生成；
- 不得输出任何 API Key、Token 或配置文件内容；
- 用中文输出，直接给结果，不要写开场白。"""


# ---------------------------------------------------------------------------
# Agent 基类与六个 Worker
# ---------------------------------------------------------------------------


class WorkerAgent:
    """Worker 基类：一个 SimpleAgent + 一段独立 system_prompt。"""

    #: 展示用的中文角色名
    role = "Worker"
    #: 记录到记忆系统的类型
    memory_type = "note"

    def __init__(self, llm: HelloAgentsLLM, system_prompt: str) -> None:
        self.agent = SimpleAgent(name=self.role, llm=llm, system_prompt=system_prompt)

    def run(self, task: str) -> str:
        """执行一次任务，异常统一转成可读文本，避免中断主流程。"""
        try:
            return self.agent.run(task)
        except Exception as exc:
            return f"【{self.role}执行失败】{config.redact(str(exc))}"


class TopicAgent(WorkerAgent):
    """热点选题官。"""

    role = "热点选题官"
    memory_type = "topic"

    def __init__(self, llm: HelloAgentsLLM) -> None:
        super().__init__(llm, TOPIC_PROMPT)


class TitleAgent(WorkerAgent):
    """标题创作官。"""

    role = "标题创作官"
    memory_type = "title"

    def __init__(self, llm: HelloAgentsLLM) -> None:
        super().__init__(llm, TITLE_PROMPT)


class CopyAgent(WorkerAgent):
    """文案/脚本撰写官。"""

    role = "文案撰写官"
    memory_type = "copy"

    def __init__(self, llm: HelloAgentsLLM) -> None:
        super().__init__(llm, COPY_PROMPT)


class OpsAgent(WorkerAgent):
    """运营顾问。"""

    role = "运营顾问"
    memory_type = "ops"

    def __init__(self, llm: HelloAgentsLLM) -> None:
        super().__init__(llm, OPS_PROMPT)


class ComplianceAgent(WorkerAgent):
    """合规审核官：先跑词表工具，再让模型结合语境出报告。"""

    role = "合规审核官"
    memory_type = "review"

    def __init__(self, llm: HelloAgentsLLM, tool: ComplianceCheckTool) -> None:
        super().__init__(llm, COMPLIANCE_PROMPT)
        self.tool = tool

    def check(self, copy_text: str, platform: str = "") -> tuple[str, str]:
        """返回 (机器检查结果, 审核报告)。

        机器检查是确定性的，不依赖模型；模型只负责语境判断与改写。
        """
        machine = self.tool.run({"content": copy_text, "platform": platform})
        task = (
            f"【机器检查结果】\n{machine}\n\n"
            f"【需要审核的文案原文】\n{copy_text}\n\n"
            f"目标平台：{platform or '小红书'}\n"
            "请给出完整审核报告。"
        )
        return machine, self.run(task)


class VideoAgent(WorkerAgent):
    """视频成片生成官：封装并直接调用 MoneyPrinterTurbo 工具。"""

    role = "视频成片生成官"
    memory_type = "video"

    def __init__(self, llm: HelloAgentsLLM, tool: MoneyPrinterTurboVideoTool) -> None:
        super().__init__(llm, VIDEO_PROMPT)
        self.tool = tool

    def generate(self, subject: str, extra_args: str = "") -> str:
        """调用视频工具渲染成片。"""
        return self.tool.run({"subject": subject, "extra_args": extra_args})

    def generate_with_aspect(
        self, subject: str, aspect: str = "9:16", extra_args: str = ""
    ) -> str:
        """按指定画面比例调用视频工具渲染成片。"""
        return self.tool.run(
            {"subject": subject, "aspect": aspect, "extra_args": extra_args}
        )

    def polish_subject(self, topic: str, copy_text: str = "") -> str:
        """把选题打磨成一句更适合做视频的主题描述。

        模型失败时退回原始选题，保证渲染流程不被卡住。
        """
        task = (
            f"选题：{topic}\n"
            f"已有文案摘要：{copy_text[:300]}\n\n"
            "请只输出一句适合做 9:16 竖屏短视频的主题描述（不超过 40 字），"
            "不要任何解释、标点前缀或换行。"
        )
        try:
            result = self.agent.run(task).strip().strip('"').splitlines()[0]
            return result[:60] or topic
        except Exception:
            return topic


def build_agents(
    memory: MemoryStore | None = None,
    tools: dict | None = None,
) -> dict[str, WorkerAgent]:
    """构建全部 Worker Agent。

    统一在这里注入大模型与工具，方便 Orchestrator 集中管理。
    """
    memory = memory or MemoryStore()
    tools = tools or {
        "compliance": ComplianceCheckTool(),
        "video": MoneyPrinterTurboVideoTool(),
    }
    llm = build_llm()

    return {
        "topic": TopicAgent(llm),
        "title": TitleAgent(llm),
        "copy": CopyAgent(llm),
        "compliance": ComplianceAgent(llm, tools["compliance"]),
        "ops": OpsAgent(llm),
        "video": VideoAgent(llm, tools["video"]),
    }


if __name__ == "__main__":
    # 自检：只验证 Agent 能否构建成功、prompt 是否装配到位，不调用大模型
    agents = build_agents()
    print(f"已构建 {len(agents)} 个 Agent：")
    for key, worker in agents.items():
        prompt = worker.agent.system_prompt or ""
        print(f"  - {key:11s} {worker.role:10s} system_prompt {len(prompt)} 字")
    print("时间：", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
