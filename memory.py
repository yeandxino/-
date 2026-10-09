# -*- coding: utf-8 -*-
"""memory.py —— 基于 JSON 文件的记忆系统

设计要点：
1. 短期记忆：最近 5 条，直接拼进 Agent 的 prompt，保证上下文连贯；
2. 长期记忆：全部历史记录，用轻量关键词检索，避免选题重复、维持人设一致；
3. 人设档案：记录领域 / 平台 / 风格偏好；
4. 落地为单个 JSON 文件，写入用「临时文件 + 原子替换」，避免中途崩溃把文件写坏；
5. 文件损坏时自动备份并重建，绝不让记忆系统拖垮主流程。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from config import DEFAULT_PROFILE, MEMORY_FILE

# 短期记忆容量：最近 5 条
SHORT_TERM_LIMIT = 5
# 当前记忆结构版本号，便于日后平滑升级
SCHEMA_VERSION = 1

# 中文检索用的停用词（极简版，够用即可，避免检索被高频虚词带偏）
_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "我", "你", "他", "她", "它",
    "这", "那", "一个", "如何", "怎么", "什么", "为什么", "可以", "要", "会",
    "the", "a", "an", "of", "to", "and", "for", "is", "are", "how", "what",
}

# 中文连续串 / 英文单词 / 数字
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_]+")


def _now() -> str:
    """统一时间戳格式，便于人工排查。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def tokenize(text: str) -> list[str]:
    """轻量分词：中文按「单字 + 二元组」切，英文数字按单词切。

    不引入 jieba 之类的重依赖，保证项目开箱即用；
    对「选题去重 + 历史检索」这类场景，二元组精度已经足够。
    """
    if not text:
        return []
    tokens: list[str] = []
    for run in _CJK_RUN.findall(text):
        tokens.extend(ch for ch in run)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    tokens.extend(word.lower() for word in _LATIN.findall(text))
    return [token for token in tokens if token and token not in _STOPWORDS]


class MemoryStore:
    """JSON 记忆系统。

    文件结构：
    {
      "version": 1,
      "profile":  {"domain": ..., "platforms": [...], "style": ...},
      "short_term": [最近 5 条记录],
      "long_term":  [全部历史记录]
    }

    单条记录结构：
    {
      "id": "...", "type": "topic|title|copy|review|ops|video|note",
      "created_at": "...", "summary": "一句话摘要",
      "payload": {...}, "tags": [...]
    }
    """

    def __init__(self, path: Path | str = MEMORY_FILE) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def _default_data(self) -> dict[str, Any]:
        """首次运行时使用的初始结构，已带上用户确认好的人设。"""
        return {
            "version": SCHEMA_VERSION,
            "profile": {**DEFAULT_PROFILE, "updated_at": _now()},
            "short_term": [],
            "long_term": [],
        }

    def _load(self) -> dict[str, Any]:
        """读取记忆文件；文件不存在 / 损坏时自动降级处理。"""
        if not self.path.is_file():
            return self._default_data()
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("记忆文件根节点不是对象")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # 备份损坏文件而不是直接覆盖，方便事后人工找回
            backup = self.path.with_suffix(
                f".corrupt-{datetime.now():%Y%m%d%H%M%S}.bak"
            )
            try:
                self.path.replace(backup)
                print(f"[memory] 记忆文件损坏（{exc}），已备份为 {backup.name}，将重建。")
            except OSError:
                print(f"[memory] 记忆文件损坏（{exc}），且备份失败，将重建。")
            return self._default_data()

        # 字段补全，兼容老版本文件
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("short_term", [])
        data.setdefault("long_term", [])
        profile = data.setdefault("profile", {})
        for key, value in DEFAULT_PROFILE.items():
            profile.setdefault(key, value)
        return data

    def _save(self) -> None:
        """原子写入：先写临时文件再替换，避免进程中断导致文件损坏。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            # 记忆写失败不应该中断主流程，仅提示
            print(f"[memory] 写入记忆文件失败：{exc}")

    def reload(self) -> None:
        """外部改动了文件时，重新加载。"""
        self._data = self._load()

    # ------------------------------------------------------------------
    # 人设档案
    # ------------------------------------------------------------------
    def get_profile(self) -> dict[str, Any]:
        """读取人设档案（领域 / 平台 / 风格）。"""
        return dict(self._data.get("profile", {}))

    def update_profile(self, **fields: Any) -> dict[str, Any]:
        """更新人设档案，只接受非空值。"""
        profile = self._data.setdefault("profile", {})
        for key, value in fields.items():
            if value not in (None, "", [], {}):
                profile[key] = value
        profile["updated_at"] = _now()
        self._save()
        return self.get_profile()

    # ------------------------------------------------------------------
    # 记录读写
    # ------------------------------------------------------------------
    def add(
        self,
        type_: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        tags: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """写入一条记忆：同时进入长期记忆和短期记忆。"""
        record = {
            "id": f"{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:4]}",
            "type": type_,
            "created_at": _now(),
            "summary": (summary or "").strip(),
            "payload": payload or {},
            "tags": [str(tag) for tag in (tags or []) if str(tag).strip()],
        }
        self._data.setdefault("long_term", []).append(record)

        short_term = self._data.setdefault("short_term", [])
        short_term.append(record)
        # 只保留最近 SHORT_TERM_LIMIT 条
        del short_term[:-SHORT_TERM_LIMIT]

        self._save()
        return record

    def recent(self, n: int = SHORT_TERM_LIMIT) -> list[dict[str, Any]]:
        """读取最近 n 条记忆。"""
        return list(self._data.get("short_term", []))[-max(0, n):]

    def all_records(self) -> list[dict[str, Any]]:
        """读取全部长期记忆。"""
        return list(self._data.get("long_term", []))

    def by_type(self, type_: str, limit: int = 50) -> list[dict[str, Any]]:
        """按类型筛选长期记忆，取最近的若干条。"""
        items = [record for record in self.all_records() if record.get("type") == type_]
        return items[-limit:]

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        limit: int = 5,
        type_: str | None = None,
    ) -> list[dict[str, Any]]:
        """关键词检索长期记忆。

        打分规则：命中的查询词越多分越高；命中标签额外加权。
        对中文而言二元组已经能覆盖大部分同义近似场景。
        """
        query_tokens = set(tokenize(query))
        if not query_tokens:
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for record in self.all_records():
            if type_ and record.get("type") != type_:
                continue
            haystack = " ".join(
                [
                    str(record.get("summary", "")),
                    " ".join(record.get("tags", []) or []),
                    json.dumps(record.get("payload", {}), ensure_ascii=False),
                ]
            )
            doc_tokens = set(tokenize(haystack))
            if not doc_tokens:
                continue
            overlap = query_tokens & doc_tokens
            if not overlap:
                continue
            # 覆盖率优先，同时给标签命中一点加成
            score = len(overlap) / len(query_tokens)
            if set(record.get("tags", []) or []) & query_tokens:
                score += 0.2
            scored.append((score, record))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:limit]]

    def list_topics(self, limit: int = 30) -> list[str]:
        """列出历史选题标题，供选题官去重。"""
        topics: list[str] = []
        for record in self.by_type("topic", limit=limit):
            title = record.get("payload", {}).get("title") or record.get("summary", "")
            if title:
                topics.append(str(title))
        return topics

    def is_duplicate_topic(self, title: str, threshold: float = 0.72) -> bool:
        """判断选题是否与历史选题高度相似。

        用序列相似度做兜底，能拦住「换词不换题」的伪新选题。
        """
        candidate = (title or "").strip()
        if not candidate:
            return False
        for old in self.list_topics(limit=200):
            if SequenceMatcher(None, candidate, old).ratio() >= threshold:
                return True
        return False

    # ------------------------------------------------------------------
    # 组装给 Agent 的上下文
    # ------------------------------------------------------------------
    def build_context(self, query: str = "", long_term_limit: int = 4) -> str:
        """把记忆压缩成一段可直接拼进 prompt 的文本。

        包含三部分：人设档案、最近动态、与当前需求相关的历史。
        """
        profile = self.get_profile()
        lines: list[str] = [
            "【创作者人设】",
            f"- 领域：{profile.get('domain', '未设置')}",
            f"- 主发平台：{profile.get('primary_platform', '未设置')}"
            f"（全平台：{'、'.join(profile.get('platforms', [])) or '未设置'}）",
            f"- 内容风格：{profile.get('style', '未设置')}",
        ]

        recent = self.recent()
        lines.append("")
        lines.append("【最近 5 条动态】")
        if recent:
            for record in recent:
                lines.append(f"- [{record.get('type')}] {record.get('summary')}")
        else:
            lines.append("- 暂无历史记录，这是第一次创作。")

        related = self.search(query, limit=long_term_limit) if query else []
        # 去掉与「最近动态」重复的条目
        recent_ids = {record.get("id") for record in recent}
        related = [r for r in related if r.get("id") not in recent_ids]
        lines.append("")
        lines.append("【相关历史（必须避免重复）】")
        if related:
            for record in related:
                lines.append(f"- [{record.get('type')}] {record.get('summary')}")
        else:
            lines.append("- 无相关历史。")

        return "\n".join(lines)

    def stats(self) -> dict[str, int]:
        """统计信息，供界面与自检展示。"""
        return {
            "short_term": len(self._data.get("short_term", [])),
            "long_term": len(self._data.get("long_term", [])),
        }


if __name__ == "__main__":
    # 自检：写入一条记忆并检索一次，确认读写与检索链路正常
    store = MemoryStore()
    print("当前人设：", store.get_profile())
    store.add("note", "自检：记忆系统初始化完成", tags=["自检"])
    print("检索「教育」：", [r["summary"] for r in store.search("教育")])
    print("统计：", store.stats())
