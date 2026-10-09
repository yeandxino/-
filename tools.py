# -*- coding: utf-8 -*-
"""tools.py —— 自定义工具集

包含三个 HelloAgents Tool：
1. ComplianceCheckTool        —— 违禁词 / 夸大宣传 / 敏感内容三层审核
2. MemoryTool                 —— 记忆读写的工具化封装，供 Agent 主动调用
3. MoneyPrinterTurboVideoTool —— 调用官方 Agent Skill 生成成片视频

所有 Tool 都遵循「防御性编程」原则：
- 入参名兼容多种写法（大模型经常把 subject 写成 topic、content 写成 text）；
- 任何异常都转成可读的中文错误字符串返回，绝不抛出，避免炸掉整条 Agent 链路；
- 返回值一律经 config.redact() 脱敏，绝不泄露密钥。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from hello_agents.tools import Tool, ToolParameter

import config
from memory import MemoryStore

# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def pick(parameters: dict[str, Any], names: Iterable[str], default: str = "") -> str:
    """从参数字典里按候选名依次取值。

    大模型调用工具时参数名经常飘（subject / topic / title 混用），
    统一在这里兜住，避免因为一个字段名对不上就整条链路失败。
    """
    for name in names:
        value = parameters.get(name)
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            text = " ".join(str(item) for item in value)
        elif isinstance(value, dict):
            text = json.dumps(value, ensure_ascii=False)
        else:
            text = str(value)
        text = text.strip()
        if text:
            return text
    return default


def safe_error(prefix: str, exc: BaseException) -> str:
    """把异常压成一行中文错误信息并脱敏。"""
    return f"{prefix}：{config.redact(str(exc))}"


# ---------------------------------------------------------------------------
# 词库：违禁词 / 风险词
# ---------------------------------------------------------------------------

# 词库文件格式：`类别|词`，以 # 开头的行为注释
BANNED_WORDS_TEMPLATE = """# 违禁词 / 风险词库
# 格式：类别|词（每行一个，用英文竖线分隔）
# 类别可选：极限词 / 教育夸大 / 承诺保证 / 医疗健康 / 诱导引流 / 情绪夸张

# ---------- 广告法极限词 ----------
极限词|最
极限词|最佳
极限词|最好
极限词|最强
极限词|第一
极限词|顶级
极限词|极致
极限词|唯一
极限词|首个
极限词|首选
极限词|独家
极限词|国家级
极限词|世界级
极限词|王牌
极限词|史无前例
极限词|前所未有
极限词|绝无仅有
极限词|万能
极限词|100%
极限词|百分百
极限词|绝对
极限词|永久
极限词|无风险
极限词|零风险

# ---------- 教育行业夸大宣传 ----------
教育夸大|保过
教育夸大|包过
教育夸大|必过
教育夸大|保分
教育夸大|保offer
教育夸大|保录取
教育夸大|包上
教育夸大|包升学
教育夸大|提分保证
教育夸大|一定考上
教育夸大|稳上
教育夸大|速成
教育夸大|三天学会
教育夸大|七天精通
教育夸大|零基础速成
教育夸大|一周逆袭
教育夸大|名师保证
教育夸大|提分神器
教育夸大|考试必中
教育夸大|押题必中

# ---------- 效果承诺 ----------
承诺保证|保证提分
承诺保证|保证效果
承诺保证|无效退款
承诺保证|立即见效
承诺保证|立竿见影
承诺保证|一夜
承诺保证|躺赚
承诺保证|暴富
承诺保证|稳赚

# ---------- 医疗健康（教育号蹭热点常踩）----------
医疗健康|治疗
医疗健康|根治
医疗健康|治愈
医疗健康|药到病除
医疗健康|包治
医疗健康|抗抑郁

# ---------- 诱导引流（平台重点打击）----------
诱导引流|加微信
诱导引流|微信号
诱导引流|加V
诱导引流|VX
诱导引流|私信我领
诱导引流|扫码进群
诱导引流|点击链接
诱导引流|站外
诱导引流|私域
诱导引流|留邮箱

# ---------- 情绪夸张 / 低质标题党 ----------
情绪夸张|震惊
情绪夸张|惊呆
情绪夸张|速看
情绪夸张|马上删
情绪夸张|不转不是
情绪夸张|删前速看
"""

# 各类别的风险等级与修改建议
CATEGORY_ADVICE: dict[str, tuple[str, str]] = {
    "极限词": ("高", "广告法明令禁止的绝对化用语，改成相对表述，如「更」「较为」「相对」。"),
    "教育夸大": ("高", "教育培训不得承诺结果，改成「帮助」「提升可能性」「多数学生反馈」等非承诺表述。"),
    "承诺保证": ("高", "效果承诺属于高风险违规，删除承诺，改为客观描述学习方法。"),
    "医疗健康": ("高", "教育账号不得涉及诊疗功效，删除或改为「心理疏导」「作息建议」等非医疗表述。"),
    "诱导引流": ("中", "站外导流会被限流，改为「评论区留言」「主页看合集」等站内动作。"),
    "情绪夸张": ("低", "标题党用词会被降权，改成具体、可验证的信息点。"),
}

# 误伤白名单：这些词后面跟特定字时属于正常用法，不是极限词。
# 实测「第一步」「第一项」会被词表误判为广告法极限词，
# 误伤率一高，审核模型的注意力就会被浪费在假阳性上。
SAFE_FOLLOWERS: dict[str, tuple[str, ...]] = {
    "第一": ("步", "项", "名", "次", "天", "周", "课", "章", "节", "条",
             "页", "遍", "轮", "组", "本", "阶段", "时间", "学期", "单元"),
    "最": ("近", "后", "初", "终", "新"),
}


def _is_false_positive(content: str, word: str, index: int) -> bool:
    """判断某个命中是不是正常用法的误伤。"""
    followers = SAFE_FOLLOWERS.get(word)
    if not followers:
        return False
    after = content[index + len(word): index + len(word) + 4]
    return any(after.startswith(follower) for follower in followers)


def _load_banned_words(path: Path) -> dict[str, str]:
    """读取词库文件，返回 {词: 类别}。

    文件不存在时自动生成一份默认词库，保证开箱即用。
    """
    if not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(BANNED_WORDS_TEMPLATE, encoding="utf-8")
        except OSError:
            pass

    text = ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    # 文件不可用或为空时退回内置模板，保证工具始终可用
    if not text.strip():
        text = BANNED_WORDS_TEMPLATE

    mapping: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        category, _, word = line.partition("|")
        word = word.strip()
        if word:
            mapping[word] = category.strip() or "其他"
    return mapping


# ---------------------------------------------------------------------------
# 工具一：合规审核
# ---------------------------------------------------------------------------


class ComplianceCheckTool(Tool):
    """违禁词 / 夸大宣传 / 敏感内容三层审核工具。"""

    def __init__(self, words_file: Path = config.BANNED_WORDS_FILE) -> None:
        super().__init__(
            name="compliance_check",
            description=(
                "检查文案中是否存在违禁词、夸大宣传、敏感或导流内容，"
                "并按风险等级给出修改建议。输入需要审核的完整文案。"
            ),
        )
        self.words_file = Path(words_file)

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="content",
                type="string",
                description="需要审核的文案或脚本全文",
                required=True,
            ),
            ToolParameter(
                name="platform",
                type="string",
                description="目标平台（小红书/抖音/公众号），用于提示平台侧风险",
                required=False,
                default="",
            ),
        ]

    def run(self, parameters: dict[str, Any]) -> str:
        try:
            content = pick(
                parameters,
                ("content", "text", "script", "copy", "body", "article"),
            )
            platform = pick(parameters, ("platform", "channel", "target"), "")

            if not content:
                return "【审核失败】没有拿到需要审核的文案，请把正文放进 content 参数。"

            words = _load_banned_words(self.words_file)

            # 逐个词扫描，记录全部出现位置，并区分真实命中与误伤
            hits: dict[str, list[int]] = {}
            ignored: dict[str, list[int]] = {}
            for word in words:
                start = 0
                while True:
                    index = content.find(word, start)
                    if index < 0:
                        break
                    bucket = ignored if _is_false_positive(content, word, index) else hits
                    bucket.setdefault(word, []).append(index)
                    start = index + len(word)

            lines: list[str] = [
                f"【合规审核结果】字数 {len(content)}，命中风险词 {len(hits)} 个"
                f"（另有 {len(ignored)} 个疑似误伤已自动排除）。"
            ]
            if platform:
                lines.append(f"目标平台：{platform}")

            if not hits:
                lines.append("结论：未发现风险词，可进入下一步。")
                if ignored:
                    detail = "、".join(
                        f"{word}（{len(positions)} 次）"
                        for word, positions in ignored.items()
                    )
                    lines.append(f"已排除的疑似误伤：{detail}")
                lines.append("提醒：机器检查只覆盖词表范围，发布前仍建议人工过一遍语境。")
                return "\n".join(lines)

            # 按风险等级分组：高 → 中 → 低
            order = {"高": 0, "中": 1, "低": 2}
            grouped: dict[str, list[tuple[str, str, list[int]]]] = {}
            for word, positions in hits.items():
                category = words[word]
                level, _ = CATEGORY_ADVICE.get(category, ("中", ""))
                grouped.setdefault(level, []).append((word, category, positions))

            for level in sorted(grouped, key=lambda item: order.get(item, 9)):
                lines.append("")
                lines.append(f"—— {level}风险 ——")
                for word, category, positions in grouped[level]:
                    _, advice = CATEGORY_ADVICE.get(category, ("中", "建议人工复核。"))
                    # 取第一次出现的上下文，方便定位
                    first = positions[0]
                    left = max(0, first - 12)
                    right = min(len(content), first + len(word) + 12)
                    context = content[left:right].replace("\n", " ")
                    lines.append(
                        f"· 「{word}」（{category}，出现 {len(positions)} 次）"
                        f" 上下文：…{context}…"
                    )
                    lines.append(f"  建议：{advice}")

            lines.append("")
            lines.append("结论：请按上述建议修改后重新提交审核。")
            return "\n".join(lines)
        except Exception as exc:  # 工具层永不抛异常
            return safe_error("【审核异常】", exc)


# ---------------------------------------------------------------------------
# 工具二：记忆读写
# ---------------------------------------------------------------------------


class MemoryTool(Tool):
    """把记忆系统包装成 Agent 可以主动调用的工具。"""

    def __init__(self, store: MemoryStore | None = None) -> None:
        super().__init__(
            name="memory",
            description=(
                "读写创作者记忆。action=add 写入一条记录；"
                "action=search 检索历史；action=recent 查看最近 5 条；"
                "action=topics 列出历史选题；action=profile 查看人设；"
                "action=update_profile 更新人设。"
            ),
        )
        self.store = store or MemoryStore()

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作类型：add / search / recent / topics / profile / update_profile",
                required=True,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="add 时作为摘要内容，search 时作为查询词",
                required=False,
                default="",
            ),
            ToolParameter(
                name="type",
                type="string",
                description="记录类型：topic / title / copy / review / ops / video / note",
                required=False,
                default="note",
            ),
            ToolParameter(
                name="tags",
                type="string",
                description="标签，多个用逗号分隔",
                required=False,
                default="",
            ),
        ]

    def run(self, parameters: dict[str, Any]) -> str:
        try:
            action = pick(parameters, ("action", "op", "command"), "recent").lower()
            content = pick(parameters, ("content", "text", "query", "summary", "keyword"))
            type_ = pick(parameters, ("type", "kind", "category"), "note")
            tags = [
                tag.strip()
                for tag in pick(parameters, ("tags", "tag", "labels"))
                .replace("，", ",")
                .split(",")
                if tag.strip()
            ]

            if action == "add":
                if not content:
                    return "【记忆失败】action=add 需要提供 content。"
                record = self.store.add(type_, content, tags=tags)
                return f"【记忆已写入】{record['id']} [{type_}] {content}"

            if action == "search":
                if not content:
                    return "【记忆检索失败】action=search 需要提供 content 作为查询词。"
                results = self.store.search(content, limit=5)
                if not results:
                    return f"【记忆检索】没有找到与「{content}」相关的历史记录。"
                lines = [f"【记忆检索】「{content}」相关历史 {len(results)} 条："]
                for record in results:
                    lines.append(
                        f"- [{record.get('type')}] {record.get('summary')}"
                        f"（{record.get('created_at')}）"
                    )
                return "\n".join(lines)

            if action == "topics":
                topics = self.store.list_topics(limit=30)
                if not topics:
                    return "【历史选题】暂无记录。"
                return "【历史选题】\n" + "\n".join(f"- {topic}" for topic in topics)

            if action == "profile":
                return "【人设档案】" + json.dumps(
                    self.store.get_profile(), ensure_ascii=False, indent=2
                )

            if action == "update_profile":
                # 允许用 content 传 "domain=教育;style=专业干货" 这种键值对
                fields: dict[str, Any] = {}
                for item in re.split(r"[;\n]", content):
                    if "=" in item:
                        key, _, value = item.partition("=")
                        fields[key.strip()] = value.strip()
                if not fields:
                    return (
                        "【人设更新失败】请用 content 传键值对，"
                        "例如 domain=教育;style=专业干货"
                    )
                updated = self.store.update_profile(**fields)
                return "【人设已更新】" + json.dumps(updated, ensure_ascii=False)

            if action == "recent":
                records = self.store.recent()
                if not records:
                    return "【最近记忆】暂无记录。"
                lines = ["【最近 5 条记忆】"]
                for record in records:
                    lines.append(
                        f"- [{record.get('type')}] {record.get('summary')}"
                        f"（{record.get('created_at')}）"
                    )
                return "\n".join(lines)

            return f"【记忆工具】不支持的 action：{action}"
        except Exception as exc:
            return safe_error("【记忆异常】", exc)


# ---------------------------------------------------------------------------
# 工具三：MoneyPrinterTurbo 视频成片
# ---------------------------------------------------------------------------

# 官方 Skill 文件（首次使用时自动安装到 MPT_SKILL_DIR）
SKILL_FILES = ("SKILL.md", "mpt_agent.py")
SKILL_MIRRORS = (
    "https://raw.githubusercontent.com/harry0703/MoneyPrinterTurbo/main/docs/skill/{name}",
    "https://cdn.jsdelivr.net/gh/harry0703/MoneyPrinterTurbo@main/docs/skill/{name}",
    "https://fastly.jsdelivr.net/gh/harry0703/MoneyPrinterTurbo@main/docs/skill/{name}",
)

# 退出码语义（来自官方 SKILL.md）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_INPUT = 10

# 需要实时透传给用户的进度行特征（其余行只进缓冲，不刷屏）
PROGRESS_MARKERS = (
    "[MoneyPrinterTurbo]",
    "MPT_RESULT",
    "MPT_ERROR",
    "MPT_NEEDS_INPUT",
    "MISSING=",
    "ERROR",
    "error:",
    "Traceback",
)


def _is_progress_line(line: str) -> bool:
    """判断这一行是否值得实时打印给用户。"""
    text = (line or "").strip()
    if not text:
        return False
    if any(marker in text for marker in PROGRESS_MARKERS):
        return True
    # 形如 "35%|███" 的进度条 / 下载进度
    return "%|" in text or text.endswith("%")


def _kill_process_tree(pid: int) -> None:
    """杀掉整棵进程树。

    只 kill 直接子进程是不够的：uv 会再派生 python 子进程，
    这些孙进程一旦变成孤儿，会继续握着 uv 的缓存锁，
    导致后续所有视频任务全部卡死（实测踩过）。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except OSError:
            pass
    # 非 Windows 或 taskkill 不可用时的兜底
    try:
        os.kill(pid, 9)
    except OSError:
        pass


class MoneyPrinterTurboVideoTool(Tool):
    """通过官方 Agent Skill 生成成片视频。"""

    def __init__(self) -> None:
        super().__init__(
            name="generate_video",
            description=(
                "根据选题或脚本，调用 MoneyPrinterTurbo 官方 Agent Skill 自动生成竖屏短视频，"
                "包含文案生成、素材匹配、TTS 配音、字幕与背景音乐。"
                "耗时较长（通常 5-20 分钟），成功后返回 MP4 文件路径。"
            ),
        )

    def get_parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="subject",
                type="string",
                description="视频主题或选题（必填）",
                required=True,
            ),
            ToolParameter(
                name="extra_args",
                type="string",
                description="额外传给 MoneyPrinterTurbo CLI 的参数，空格分隔，可选",
                required=False,
                default="",
            ),
            ToolParameter(
                name="video_source",
                type="string",
                description="素材来源，默认沿用配置（coverr）",
                required=False,
                default="",
            ),
            ToolParameter(
                name="aspect",
                type="string",
                description="画面比例：9:16 / 3:4 / 1:1 / 4:3 / 16:9，默认 9:16",
                required=False,
                default="",
            ),
        ]

    # ------------------------------------------------------------------
    # 安装与配置
    # ------------------------------------------------------------------
    def _download(self, url: str) -> str:
        """下载文本资源，返回内容；失败抛异常。"""
        request = urllib.request.Request(
            url, headers={"User-Agent": "MoneyPrinterTurbo-Agent-Skill"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")

    def ensure_skill_installed(self) -> str:
        """确保 SKILL.md 与 mpt_agent.py 就位，缺失时从官方仓库拉取。"""
        skill_dir = config.MPT_SKILL_DIR
        missing = [name for name in SKILL_FILES if not (skill_dir / name).is_file()]
        if not missing:
            return ""

        skill_dir.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for name in missing:
            saved = False
            for template in SKILL_MIRRORS:
                url = template.format(name=name)
                try:
                    content = self._download(url)
                    if not content.strip():
                        raise ValueError("下载内容为空")
                    (skill_dir / name).write_text(content, encoding="utf-8")
                    saved = True
                    break
                except Exception as exc:
                    errors.append(f"{url} -> {exc}")
            if not saved:
                return (
                    f"【视频生成失败】无法下载官方 Skill 文件 {name}。\n"
                    f"请手动从 https://github.com/harry0703/MoneyPrinterTurbo "
                    f"的 docs/skill 目录下载后放到 {skill_dir}。\n"
                    f"错误摘要：{config.redact('; '.join(errors[-3:]))[:300]}"
                )
        return ""

    def _patch_config_text(self, text: str, key: str, value_literal: str) -> str:
        """替换 config.toml 中某个键的值；键不存在时在文件末尾追加。"""
        pattern = re.compile(rf"(?m)^{re.escape(key)}\s*=.*$")
        if pattern.search(text):
            return pattern.sub(f"{key} = {value_literal}", text, count=1)
        return text + f"\n# 由自媒体智能运营助手自动补齐\n{key} = {value_literal}\n"

    def prepare_config(self) -> str:
        """准备 config.toml：复制模板 + 写入 Coverr Key。

        整个过程中绝不打印 config.toml 内容，也不打印 Key 原文。
        项目尚未下载时返回空字符串，交给 helper 自己完成首次安装。
        """
        root = config.MPT_ROOT
        config_path = root / "config.toml"
        example_path = root / "config.example.toml"

        # 项目还没下载：helper 会自己下载并创建 config.toml
        if not config_path.is_file():
            if example_path.is_file():
                try:
                    config_path.write_text(
                        example_path.read_text(encoding="utf-8"), encoding="utf-8"
                    )
                except OSError as exc:
                    return f"【视频生成失败】无法创建 config.toml：{exc}"
            else:
                return ""

        # 写入 Coverr Key（值只在内存里流转，不落日志）
        if config.MPT_COVERR_API_KEY:
            try:
                text = config_path.read_text(encoding="utf-8")
                literal = json.dumps([config.MPT_COVERR_API_KEY], ensure_ascii=False)
                patched = self._patch_config_text(text, "coverr_api_keys", literal)
                if patched != text:
                    config_path.write_text(patched, encoding="utf-8")
            except OSError as exc:
                return f"【视频生成失败】无法写入 config.toml：{exc}"
        return ""

    def _build_env(self) -> dict[str, str]:
        """构造子进程环境变量：把本项目的大模型配置透传给视频引擎。"""
        env = os.environ.copy()
        # 让 uv 走国内镜像，否则首次安装依赖会慢到不可用
        if config.MPT_PYPI_MIRROR:
            env.setdefault("UV_DEFAULT_INDEX", config.MPT_PYPI_MIRROR)
        try:
            llm = config.resolve_llm()
        except RuntimeError:
            return env
        # 官方 helper 支持 MPT_LLM_* 系列环境变量，会自动写入 config.toml
        env["MPT_LLM_PROVIDER"] = llm["provider"]
        env["MPT_LLM_API_KEY"] = llm["api_key"]
        env["MPT_LLM_BASE_URL"] = llm["base_url"]
        env["MPT_LLM_MODEL_NAME"] = llm["model"]
        return env

    # ------------------------------------------------------------------
    # 执行与解析
    # ------------------------------------------------------------------
    def _build_command(
        self,
        subject: str,
        video_source: str,
        extra_args: str,
        render_ratio: str = "",
    ) -> list[str]:
        """拼装官方要求的命令：相对脚本名 + workdir=SKILL_DIR。"""
        command = [
            "uv",
            "run",
            "--no-project",
            "--python",
            "3.11",
            "python",
            config.MPT_HELPER_NAME,
            "--subject",
            subject,
            # 安装目录固定到 D 盘（对应 helper 的 --root 参数）
            "--root",
            str(config.MPT_ROOT),
            # 之后的所有参数原样转发给 MoneyPrinterTurbo CLI
            "--",
            "--video-source",
            video_source,
            "--video-aspect",
            render_ratio or config.MPT_VIDEO_ASPECT,
            # 注意：项目 CLI 用的是 --video-language（不是 --language），
            # 参数名写错会让 cli.py 直接以退出码 2 拒绝执行。
            "--video-language",
            config.MPT_VIDEO_LANGUAGE,
            "--voice-name",
            config.MPT_VOICE_NAME,
        ]
        if extra_args:
            command.extend(extra_args.split())
        # 素材关键词：Coverr/Pexels/Pixabay 对长短语几乎搜不到结果（实测多词
        # 短语返回 0 条），因此默认补一组短关键词；在 extra_args 里显式传了
        # --video-terms 就尊重用户的设置。
        if (
            config.MPT_VIDEO_TERMS
            and video_source in {"coverr", "pexels", "pixabay"}
            and "--video-terms" not in command
        ):
            command.extend(["--video-terms", config.MPT_VIDEO_TERMS])
        return command

    # ------------------------------------------------------------------
    # 画面比例后处理
    # ------------------------------------------------------------------
    def _find_ffmpeg(self) -> str:
        """定位可用的 ffmpeg。

        优先顺序：环境变量指定 → 系统 PATH → MoneyPrinterTurbo 环境自带的
        imageio-ffmpeg（官方就是用它，所以本地一定有一份）。
        """
        configured = os.environ.get("MPT_FFMPEG_PATH", "").strip()
        if configured and Path(configured).is_file():
            return configured
        found = shutil.which("ffmpeg")
        if found:
            return found
        binaries = (
            config.MPT_ROOT / ".venv" / "Lib" / "site-packages"
            / "imageio_ffmpeg" / "binaries"
        )
        if binaries.is_dir():
            for exe in sorted(binaries.glob("ffmpeg*")):
                if exe.is_file():
                    return str(exe)
        return ""

    def _convert_aspect(self, video_file: str, target_aspect: str) -> tuple[str, str] | None:
        """把视频转成目标比例：保持画面不变，用模糊背景补边。

        只处理引擎不原生支持的比例（3:4、4:3），转换失败时保留原视频，
        绝不让后处理把已经成功的渲染结果弄丢。
        """
        native = config.render_aspect(target_aspect)
        if config.normalize_aspect(target_aspect) == native:
            return None  # 原生支持，无需转换

        source = Path(video_file)
        if not source.is_file():
            return None

        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            return (
                video_file,
                f"\n注意：未找到 ffmpeg，无法转换成 {target_aspect}，"
                f"已按 {native} 交付。",
            )

        ratio = config.ASPECT_RATIO[config.normalize_aspect(target_aspect)]
        # 保持高度不变，按目标比例算出宽度（取偶数，避免编码报错）
        size = f"w='trunc(ih*{ratio:.6f}/2)*2':h=ih"
        filters = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={size},boxblur=20:2[bg2];"
            f"[fg]scale={size}:force_original_aspect_ratio=decrease[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
        )
        output = source.with_name(f"{source.stem}-{target_aspect.replace(':', 'x')}.mp4")
        command = [
            ffmpeg, "-y", "-i", str(source),
            "-filter_complex", filters,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "copy", "-movflags", "+faststart",
            str(output),
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1800,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return video_file, f"\n注意：比例转换失败（{exc}），已按 {native} 交付。"

        if completed.returncode == 0 and output.is_file() and output.stat().st_size > 0:
            return str(output), ""

        tail = "\n".join((completed.stdout or "").strip().splitlines()[-5:])
        return (
            video_file,
            f"\n注意：比例转换失败，已按 {native} 交付。{config.redact(tail)[:200]}",
        )

    def _parse_result(self, output: str) -> dict[str, str]:
        """解析 helper 输出的 MPT_RESULT 块。"""
        result: dict[str, str] = {}
        if "MPT_RESULT" not in output:
            return result
        for line in output.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                if key in {"VIDEO_FILE", "TASK_DIR", "LOG_FILE", "RESULT_FILE"}:
                    result[key] = value.strip()
        return result

    def _parse_needs_input(self, output: str) -> dict[str, Any]:
        """解析 MPT_NEEDS_INPUT 块，拿到缺失字段清单。"""
        missing = [
            line.split("=", 1)[1].strip()
            for line in output.splitlines()
            if line.startswith("MISSING=")
        ]
        invalid = [
            line.split("=", 1)[1].strip()
            for line in output.splitlines()
            if line.startswith("INVALID=")
        ]
        provider = ""
        for line in output.splitlines():
            if line.startswith("LLM_PROVIDER="):
                provider = line.split("=", 1)[1].strip()
        return {"missing": missing, "invalid": invalid, "provider": provider}

    def _read_manifest(self) -> dict[str, Any]:
        """终端输出被截断时，读取一次结果清单文件。"""
        path = config.MPT_RESULT_MANIFEST
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _run_helper(self, command: list[str], env: dict[str, str]) -> tuple[int, str]:
        """同步执行 helper，返回 (退出码, 完整输出)。

        与直接用 subprocess.run 的区别：
        - 边跑边把关键进度行打到终端，避免几十分钟没有任何反馈；
        - 完整输出仍然缓冲下来，供解析 MPT_RESULT / MPT_ERROR；
        - 用独立线程读输出、用 process.wait(timeout) 计时，
          这样即使子进程（如 uv 安装依赖时）完全不输出，超时依然可靠；
        - 超时或异常时杀整棵进程树，避免留下握着 uv 缓存锁的孤儿进程。
        """
        try:
            process = subprocess.Popen(
                command,
                cwd=str(config.MPT_SKILL_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            return EXIT_ERROR, "MPT_ERROR=未找到 uv 命令，请先安装 uv 并重新打开终端"
        except OSError as exc:
            return EXIT_ERROR, f"MPT_ERROR={exc}"

        buffer: list[str] = []

        def _pump() -> None:
            """独立线程持续读取输出，避免管道写满导致子进程卡死。"""
            stream = process.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    buffer.append(line)
                    # 只透传关键进度行，避免把 uv 的几百行日志刷满屏幕
                    if _is_progress_line(line):
                        print(line.rstrip(), flush=True)
            except (OSError, ValueError):
                # 进程被强杀时读取会中断，属于预期情况
                return

        reader = threading.Thread(target=_pump, daemon=True)
        reader.start()

        try:
            process.wait(timeout=config.MPT_TASK_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process.pid)
            reader.join(timeout=10)
            return (
                EXIT_ERROR,
                f"MPT_ERROR=任务超时（超过 {config.MPT_TASK_TIMEOUT} 秒）\n"
                + "".join(buffer),
            )
        except Exception as exc:  # 其他异常同样清理整棵进程树
            _kill_process_tree(process.pid)
            reader.join(timeout=10)
            return EXIT_ERROR, f"MPT_ERROR={exc}\n" + "".join(buffer)

        reader.join(timeout=10)
        return process.returncode or 0, "".join(buffer)

    def _tail(self, output: str, lines: int = 15) -> str:
        """只取输出尾部若干行，避免把整份日志灌进模型上下文。"""
        tail = output.strip().splitlines()[-lines:]
        return config.redact("\n".join(tail))

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def run(self, parameters: dict[str, Any]) -> str:
        try:
            subject = pick(
                parameters,
                ("subject", "topic", "title", "idea", "prompt", "content", "script"),
            )
            if not subject:
                return "【视频生成失败】缺少 subject（视频主题）。"

            video_source = (
                pick(parameters, ("video_source", "source", "material_source"))
                or config.MPT_VIDEO_SOURCE
            ).lower()
            extra_args = pick(parameters, ("extra_args", "extra", "options", "cli_args"))
            # 画面比例：用户没指定就用配置默认值
            target_aspect = config.normalize_aspect(
                pick(parameters, ("aspect", "ratio", "video_aspect", "size"))
                or config.MPT_VIDEO_ASPECT
            )
            render_ratio = config.render_aspect(target_aspect)

            # 1) 安装 Skill 文件
            error = self.ensure_skill_installed()
            if error:
                return error

            # 2) 准备 config.toml（写入 Coverr Key；项目未下载时交给 helper）
            error = self.prepare_config()
            if error:
                return error

            # 3) 执行（默认 20 分钟超时；需补凭证时自动修复后重试一次）
            env = self._build_env()
            command = self._build_command(
                subject, video_source, extra_args, render_ratio
            )
            code, output = self._run_helper(command, env)
            output = config.redact(output)

            if code == EXIT_NEEDS_INPUT:
                info = self._parse_needs_input(output)
                auto_fixable = {
                    "coverr_api_keys",
                    "pexels_api_keys",
                    "pixabay_api_keys",
                } & set(info["missing"])
                if auto_fixable and not self.prepare_config():
                    code, output = self._run_helper(command, env)
                    output = config.redact(output)
                else:
                    fields = "、".join(info["missing"]) or "未知字段"
                    return (
                        f"【需要补充凭证】视频引擎缺少：{fields}\n"
                        f"当前大模型供应商：{info.get('provider') or '未识别'}\n"
                        "请在 .env 补齐后重试（本项目会一次性告知全部缺口，不会反复询问）。"
                    )

            if code == EXIT_OK:
                result = self._parse_result(output)
                if result.get("VIDEO_FILE"):
                    video_file = result["VIDEO_FILE"]
                    note = ""
                    # 非原生比例：渲染完成后用 ffmpeg 补背景边转换
                    converted = self._convert_aspect(video_file, target_aspect)
                    if converted:
                        video_file, note = converted
                    return (
                        f"【视频生成成功】\n"
                        f"主题：{subject}\n"
                        f"视频文件：{video_file}\n"
                        f"任务目录：{result.get('TASK_DIR', '-')}\n"
                        f"画面比例：{target_aspect}（引擎渲染 {render_ratio}）\n"
                        f"说明：中文视频，含配音、字幕与背景音乐。{note}"
                    )
                # 输出被截断：只读一次结果清单
                manifest = self._read_manifest()
                if manifest.get("status") == "completed" and manifest.get("video_files"):
                    videos = "\n".join(str(item) for item in manifest["video_files"])
                    return f"【视频生成成功】\n主题：{subject}\n视频文件：\n{videos}"
                return (
                    "【视频生成失败】退出码为 0 但未拿到 MP4 路径。\n"
                    f"结果清单：{config.MPT_RESULT_MANIFEST}\n"
                    f"输出尾部：\n{self._tail(output, 8)}"
                )

            # 退出码 1：失败，只回报短错误与日志尾部
            return (
                f"【视频生成失败】主题：{subject}\n"
                f"退出码：{code}\n"
                f"错误与日志尾部：\n{self._tail(output)}"
            )
        except Exception as exc:
            return safe_error("【视频生成异常】", exc)


# ---------------------------------------------------------------------------
# 工厂函数：给 Orchestrator 统一取用
# ---------------------------------------------------------------------------


def build_tools() -> dict[str, Tool]:
    """构建本项目全部自定义工具。"""
    return {
        "compliance": ComplianceCheckTool(),
        "memory": MemoryTool(),
        "video": MoneyPrinterTurboVideoTool(),
    }


if __name__ == "__main__":
    # 自检：只测不需要联网的合规与记忆工具
    tools = build_tools()
    sample = "这套方法绝对能让孩子7天速成，提分保证，想学的加微信我发资料。"
    print(tools["compliance"].run({"content": sample, "platform": "小红书"}))
    print()
    print(tools["memory"].run({"action": "profile"}))
