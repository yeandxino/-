# -*- coding: utf-8 -*-
"""config.py —— 全局配置中心

职责：
1. 统一从环境变量 / .env 读取所有凭证与可调参数，代码里没有任何硬编码密钥；
2. 统一管理数据目录、输出目录、记忆文件等路径；
3. 提供「缺什么凭证」的一次性检查，避免 Agent 反复追问用户；
4. 提供 redact()，保证任何日志、报错、工具返回值都不会泄露密钥原文。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 一、路径常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent          # 项目根目录
DATA_DIR = BASE_DIR / "data"                        # 数据目录
OUTPUT_DIR = DATA_DIR / "outputs"                   # 各环节产物落盘目录
MEMORY_FILE = DATA_DIR / "memory.json"              # 记忆文件
BANNED_WORDS_FILE = DATA_DIR / "banned_words.txt"   # 违禁词词库
SETTINGS_FILE = DATA_DIR / "settings.json"          # 界面设置（持久化，重启不丢）


def _load_dotenv() -> None:
    """加载项目根目录下的 .env。

    这里做静默降级：即使 python-dotenv 没装，也不影响程序启动，
    此时会直接使用系统环境变量。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_file = BASE_DIR / ".env"
    if env_file.is_file():
        # override=False：系统环境变量优先级高于 .env，方便临时覆盖
        load_dotenv(env_file, override=False)


_load_dotenv()


def _load_streamlit_secrets() -> None:
    """在 Streamlit 环境里读取 Secrets（云端部署的正确做法）。

    Streamlit Community Cloud 上不应该上传 .env（也已被 .gitignore 排除），
    官方做法是把密钥填进 App 的 Secrets。这里把它们注入环境变量，
    使代码在本地（读 .env）和云端（读 Secrets）行为完全一致。
    """
    # 非 Streamlit 环境直接跳过，避免给命令行启动增加开销
    if "streamlit" not in sys.modules:
        return
    try:
        import streamlit as st

        secrets = st.secrets
        items = dict(secrets)  # 无 secrets 时会抛异常，被下面兜住
    except Exception:
        return

    for key, value in items.items():
        # 只接受标量，跳过嵌套配置块
        if isinstance(value, (str, int, float)) and not os.getenv(str(key)):
            os.environ[str(key)] = str(value)


_load_streamlit_secrets()


# ---------------------------------------------------------------------------
# 二、读取工具（全部做了去空格与异常兜底）
# ---------------------------------------------------------------------------
def _get(name: str, default: str = "") -> str:
    """读取字符串型环境变量，去掉首尾空格。"""
    return (os.getenv(name) or default).strip()


def _get_int(name: str, default: int) -> int:
    """读取整数型环境变量，值非法时回退到默认值而不是崩溃。"""
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    """读取浮点型环境变量，值非法时回退到默认值。"""
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 三、大模型配置（OpenAI 兼容接口，支持多供应商自动回退）
# ---------------------------------------------------------------------------
# 每个供应商只暴露「Key / Base URL / Model」三项，全部来自环境变量。
# 想接入新厂商，在这里加一段即可，其余代码无需改动。
LLM_PROVIDERS: dict[str, dict[str, str]] = {
    "modelscope": {
        "label": "ModelScope 魔搭",
        "api_key": _get("MODELSCOPE_API_KEY"),
        "base_url": _get(
            "MODELSCOPE_BASE_URL", "https://api-inference.modelscope.cn/v1"
        ),
        "model": _get("MODELSCOPE_MODEL", "deepseek-ai/DeepSeek-V4.1-Flash"),
    },
    "dashscope": {
        "label": "阿里云百炼 DashScope",
        "api_key": _get("DASHSCOPE_API_KEY"),
        "base_url": _get(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "model": _get("DASHSCOPE_MODEL", "qwen-plus"),
    },
}

# 优先级顺序：靠前的不可用时自动回退到下一个（含未知名字时用默认顺序兜底）
LLM_PROVIDER_ORDER = [
    name.strip()
    for name in _get("LLM_PROVIDER_ORDER", "modelscope,dashscope").split(",")
    if name.strip() in LLM_PROVIDERS
] or list(LLM_PROVIDERS)

LLM_TEMPERATURE = _get_float("LLM_TEMPERATURE", 0.7)
LLM_TIMEOUT = _get_int("LLM_TIMEOUT", 120)
# 输出上限：DeepSeek-V4.1-Flash 属于推理模型，reasoning_content 同样计入
# completion_tokens。实测 2048 会被思考全部吃掉、正文只剩 229 字即被截断，
# 因此默认给到 8192（RobustLLM 在正文为空时还会自动翻倍重试）。
LLM_MAX_TOKENS = _get_int("LLM_MAX_TOKENS", 8192)


def resolve_llm() -> dict[str, str]:
    """按优先级挑出一个可用的大模型配置。

    返回 {"provider", "label", "api_key", "base_url", "model"}。
    全部不可用时抛出带中文说明的异常，由上层决定是提示用户还是降级。
    """
    for name in LLM_PROVIDER_ORDER:
        conf = LLM_PROVIDERS[name]
        if conf["api_key"]:
            return {"provider": name, **conf}
    # 兜底再扫一遍，确保没有漏网之鱼
    for name, conf in LLM_PROVIDERS.items():
        if conf["api_key"]:
            return {"provider": name, **conf}
    names = " 或 ".join(f"{name.upper()}_API_KEY" for name in LLM_PROVIDERS)
    raise RuntimeError(f"没有可用的大模型 API Key，请在 .env 中配置：{names}")


# ---------------------------------------------------------------------------
# 四、MoneyPrinterTurbo 视频引擎配置
# ---------------------------------------------------------------------------
# Skill 目录：存放官方 SKILL.md 与 mpt_agent.py，同时作为执行命令的工作目录
# 说明：D 盘根目录 ACL 只给「读取和执行」，无法新建文件夹，
#       因此落在 D 盘可写的 workspace 子目录下。
MPT_SKILL_DIR = Path(_get("MPT_SKILL_DIR", "D:/workspace/mpt-skill"))
# 项目安装根目录（首次运行由 mpt_agent.py 自动下载官方仓库到这里）
# 注意：官方 helper 要求该目录「不存在」或「是空目录」或「已是有效项目」，
#       所以 Skill 目录必须放在它外面，否则首次安装会被直接拒绝。
MPT_ROOT = Path(_get("MPT_ROOT", "D:/workspace/MoneyPrinterTurbo"))
# 素材来源：coverr（本项目默认）/ pexels / pixabay / local
MPT_VIDEO_SOURCE = _get("MPT_VIDEO_SOURCE", "coverr")
# Coverr 素材 Key：官方只支持写进 config.toml，没有环境变量入口
MPT_COVERR_API_KEY = _get("MPT_COVERR_API_KEY")
# 单次视频任务超时（秒）：官方要求至少 20 分钟。
# 首次运行还要下载项目本体与依赖，实测 1200 秒会被用光，因此默认给 3600。
MPT_TASK_TIMEOUT = _get_int("MPT_TASK_TIMEOUT", 3600)
# 画面比例与配音语言
MPT_VIDEO_ASPECT = _get("MPT_VIDEO_ASPECT", "9:16")
MPT_VIDEO_LANGUAGE = _get("MPT_VIDEO_LANGUAGE", "zh-CN")
# 素材检索关键词（英文、逗号分隔）。
# 实测 Coverr 是精确/短语匹配：education、homework 这类短词能返回素材，
# 而模型自动生成的 third grade homework、homemade routine 这类长短语一律 0 条。
# 因此这里固定给一组短关键词，覆盖教育类内容的常见画面。
MPT_VIDEO_TERMS = _get(
    "MPT_VIDEO_TERMS",
    "education,study,homework,student,learning,writing",
)
# 中文女声 Edge TTS 音色（与官方 Skill 默认值保持一致）
MPT_VOICE_NAME = _get("MPT_VOICE_NAME", "zh-CN-XiaoxiaoNeural-Female")
# 官方 Skill helper 脚本名（必须用相对文件名调用，见官方 SKILL.md）
MPT_HELPER_NAME = "mpt_agent.py"
# PyPI 镜像：国内直连 pypi.org 极慢（实测 7 分钟只下到 1.3 MB），
# 通过 UV_DEFAULT_INDEX 让 uv 走国内镜像。置空则不干预。
MPT_PYPI_MIRROR = _get("MPT_PYPI_MIRROR", "https://pypi.tuna.tsinghua.edu.cn/simple")
# 结果清单文件（终端输出被截断时的兜底读取位置）
MPT_RESULT_MANIFEST = (
    MPT_ROOT / ".agent-logs" / "moneyprinterturbo-video" / "latest-result.json"
)


# ---------------------------------------------------------------------------
# 五、业务默认人设（用户已确认：教育领域 / 小红书+抖音+公众号 / 专业干货）
# ---------------------------------------------------------------------------
DEFAULT_PROFILE = {
    "domain": "教育",
    "platforms": ["小红书", "抖音", "公众号"],
    "primary_platform": "小红书",
    "style": "专业干货",
}


# ---------------------------------------------------------------------------
# 六、凭证检查与脱敏
# ---------------------------------------------------------------------------
# 凭证清单：(环境变量名, 中文说明, 是否必需)
CREDENTIAL_SPECS = [
    ("MODELSCOPE_API_KEY", "ModelScope 大模型 API Key（首选）", False),
    ("DASHSCOPE_API_KEY", "阿里云百炼 DashScope API Key（备用）", False),
    ("MPT_COVERR_API_KEY", "Coverr 素材 API Key（视频成片依赖）", True),
]


def missing_credentials() -> list[str]:
    """返回缺失的必需凭证说明列表。

    设计要点：把缺口一次性收集齐，让上层可以「一次问完」，
    而不是每调用一个 Agent 就追问用户一次。
    """
    missing: list[str] = []
    for env_name, desc, required in CREDENTIAL_SPECS:
        if required and not _get(env_name):
            missing.append(f"{env_name} —— {desc}")
    # 大模型属于「多选一」：只有全部缺失才算缺口
    if not any(conf["api_key"] for conf in LLM_PROVIDERS.values()):
        names = " 或 ".join(f"{name.upper()}_API_KEY" for name in LLM_PROVIDERS)
        missing.append(f"{names} —— 至少配一个大模型 Key（选题/标题/文案/审核/运营依赖）")
    return missing


def credentials_status() -> dict[str, str]:
    """返回各凭证明细的脱敏状态，用于启动自检打印。"""
    status = {
        f"llm:{name}": f"{conf['label']} / {conf['model']} / {mask(conf['api_key'])}"
        for name, conf in LLM_PROVIDERS.items()
    }
    status["video:coverr"] = mask(MPT_COVERR_API_KEY)
    return status


def mask(value: str) -> str:
    """把密钥脱敏成 sk-ws-****s8ww 形式，任何日志只允许打印脱敏结果。"""
    if not value:
        return "<未配置>"
    text = str(value)
    if len(text) <= 10:
        return text[:2] + "****"
    return f"{text[:6]}****{text[-4:]}"


def redact(text: str) -> str:
    """把文本中出现的任何真实密钥替换成脱敏形式。

    所有 subprocess 输出、异常信息、工具返回值在返回给模型或打印前，
    都应该先过一遍这个函数。
    """
    if not text:
        return ""
    result = str(text)

    # 收集当前所有真实密钥（含各供应商），统一打码
    secrets = [conf["api_key"] for conf in LLM_PROVIDERS.values()]
    secrets.append(MPT_COVERR_API_KEY)
    for secret in secrets:
        if secret and len(secret) >= 8:
            result = result.replace(secret, mask(secret))
    return result


def ensure_dirs() -> None:
    """确保运行所需的目录都存在。"""
    for path in (DATA_DIR, OUTPUT_DIR):
        path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 七、视频比例
# ---------------------------------------------------------------------------
# MoneyPrinterTurbo 原生只接受 9:16 / 16:9 / 1:1，
# 其余比例先用最接近的原生比例渲染，再用 ffmpeg 补背景边转成目标比例。
ASPECT_RATIO = {
    "9:16": 9 / 16,     # 竖屏，抖音 / 小红书视频
    "3:4": 3 / 4,       # 小红书图文常见的偏竖比例
    "1:1": 1 / 1,       # 正方形
    "4:3": 4 / 3,       # 传统横屏
    "16:9": 16 / 9,     # 宽屏，公众号 / B站
}

# 视频引擎原生支持的比例
NATIVE_ASPECTS = {"9:16", "16:9", "1:1"}

# 非原生比例 → 先用哪个原生比例渲染（渲染完再补边转换）
ASPECT_FALLBACK = {
    "3:4": "9:16",   # 9:16 更窄，左右补边 → 3:4
    "4:3": "16:9",   # 16:9 更宽，上下补边 → 4:3
}

# 供界面展示的比例说明
ASPECT_LABELS = {
    "9:16": "9:16 竖屏（抖音、小红书视频，最常用）",
    "3:4": "3:4 偏竖（小红书图文封面感）",
    "1:1": "1:1 正方形（朋友圈、信息流）",
    "4:3": "4:3 横屏（课程视频、老式比例）",
    "16:9": "16:9 宽屏（公众号、B站、视频号）",
}


def normalize_aspect(value: str) -> str:
    """把用户输入的比例规范化；非法值回退到 9:16。"""
    text = (value or "").strip().replace("：", ":").replace("／", "/")
    if text in ASPECT_RATIO:
        return text
    # 兼容 9x16 / 9-16 之类的写法
    alt = text.replace("x", ":").replace("X", ":").replace("-", ":")
    return alt if alt in ASPECT_RATIO else "9:16"


def render_aspect(target: str) -> str:
    """返回实际传给视频引擎的比例（非原生比例用最接近的原生比例渲染）。"""
    aspect = normalize_aspect(target)
    return ASPECT_FALLBACK.get(aspect, aspect)


# ---------------------------------------------------------------------------
# 八、界面设置持久化
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS = {
    "domain": DEFAULT_PROFILE["domain"],
    "platforms": list(DEFAULT_PROFILE["platforms"]),
    "primary_platform": DEFAULT_PROFILE["primary_platform"],
    "style": DEFAULT_PROFILE["style"],
    "aspect": MPT_VIDEO_ASPECT,
    "enable_video": True,
    "video_args": "",
}


def load_settings() -> dict:
    """读取界面设置；文件不存在或损坏时返回默认值。"""
    settings = dict(DEFAULT_SETTINGS)
    if not SETTINGS_FILE.is_file():
        return settings
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            for key, value in saved.items():
                if value not in (None, "", [], {}):
                    settings[key] = value
    except (OSError, ValueError):
        # 设置文件损坏不应该影响使用，静默回退到默认值
        return dict(DEFAULT_SETTINGS)
    return settings


def save_settings(settings: dict) -> None:
    """原子写入界面设置，保证重启后配置还在。"""
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(SETTINGS_FILE)
    except OSError as exc:
        print(f"[config] 保存设置失败：{exc}")
