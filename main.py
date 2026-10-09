# -*- coding: utf-8 -*-
"""main.py —— 多 Agent 主流程（Orchestrator-Workers）

总调度按顺序编排六个 Worker：
    热点选题官 → 标题创作官 → 文案撰写官 → 合规审核官 → 运营顾问 → 视频成片生成官

运行示例：
    python main.py "孩子写作业总拖拉，怎么引导"
    python main.py "初中生手机管理" --pick 2          # 直接选用第 2 个选题往下走
    python main.py "高中生时间管理" --no-video        # 跳过视频渲染
    python main.py "家庭教育方法" --video-args "--video-count 1"
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

import config
from agents import build_agents
from memory import MemoryStore
from tools import build_tools


def setup_stdout() -> None:
    """强制 UTF-8 输出。

    Windows 默认代码页是 GBK，中文会显示成乱码，
    这里在入口统一修正，避免用户以为程序出错了。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


class Orchestrator:
    """总调度 Agent：接收需求，依次调用各 Worker 并汇总结果。"""

    def __init__(self, enable_video: bool = True, aspect: str = "") -> None:
        config.ensure_dirs()
        self.memory = MemoryStore()
        self.tools = build_tools()
        self.agents = build_agents(self.memory, self.tools)
        self.enable_video = enable_video
        # 画面比例：界面/命令行传进来，缺省用配置文件里的值
        self.aspect = config.normalize_aspect(aspect or config.MPT_VIDEO_ASPECT)

        # 本轮所有产物落盘目录
        self.out_dir = config.OUTPUT_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")

        # 各环节产出
        self.outputs: dict[str, str] = {}
        self.video_path = ""

    # ------------------------------------------------------------------
    # 基础工具方法
    # ------------------------------------------------------------------
    def _say(self, message: str, level: str = "info") -> None:
        """统一进度输出，异常信息一律先脱敏。"""
        prefix = {"info": "▸", "ok": "✓", "warn": "!", "error": "✗"}.get(level, "▸")
        print(f"{prefix} {config.redact(message)}", flush=True)

    def _banner(self, text: str) -> None:
        print("\n" + "=" * 64)
        print(f"  {text}")
        print("=" * 64, flush=True)

    def _save(self, name: str, content: str) -> Path:
        """把单个环节的产出写入文件。"""
        path = self.out_dir / f"{name}.md"
        try:
            # 惰性创建目录：只实例化 Orchestrator 不应该产生空文件夹
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            self._say(f"结果落盘失败：{exc}", "warn")
        return path

    def _remember(self, type_: str, summary: str, payload: dict | None = None,
                  tags: list[str] | None = None) -> None:
        """写入记忆，失败不影响主流程。"""
        try:
            self.memory.add(type_, summary, payload=payload, tags=tags)
        except Exception as exc:
            self._say(f"写入记忆失败：{exc}", "warn")

    @staticmethod
    def _extract_topics(text: str) -> list[str]:
        """从选题官输出里提取选题名，用于选中与去重。

        模型的实际输出格式并不稳定，实测中出现过：
            ## 选题一：xxx
            1. xxx
            - **1. xxx**
        因此这里优先解析标准清单，解析不到再宽容匹配全文。
        """
        # 优先取「【选题清单】」这一段（prompt 里强制要求输出）
        marker = "【选题清单】"
        if marker in text:
            block = text.split(marker, 1)[1]
            # 截断到下一个 Markdown 标题，避免把后续内容算进来
            block = re.split(r"\n#{1,6}\s", block)[0]
            topics = Orchestrator._parse_numbered_lines(block)
            if len(topics) >= 3:
                return topics[:5]
        return Orchestrator._parse_numbered_lines(text)

    # 匹配 "## 选题一：xxx" / "1. xxx" / "- **1. xxx**" 等多种写法
    _NUMBERED_LINE = re.compile(
        r"^\s*(?:[#>*\-•·]+\s*)*\*{0,2}\s*(?:选题|话题|方案)?\s*"
        r"([0-9]+|[一二三四五六七八九十]+)\s*[.、)：:．\-]\s*(.+?)\s*\*{0,2}\s*$"
    )

    @staticmethod
    def _parse_numbered_lines(text: str) -> list[str]:
        """解析「编号 + 标题」形式的行，返回去重后的标题列表。"""
        topics: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            match = Orchestrator._NUMBERED_LINE.match(line)
            if not match:
                continue
            title = match.group(2).strip().strip("*#「」").strip()
            # 去掉「选题名：」这类前缀
            title = re.sub(r"^(选题名?|标题|话题)[：:]\s*", "", title)
            title = title.replace("**", "").strip()
            # 过滤掉明显不是选题名的短片段
            if len(title) >= 4 and title not in topics:
                topics.append(title)
        return topics

    # ------------------------------------------------------------------
    # 第一步：热点选题
    # ------------------------------------------------------------------
    def step_topics(self, request: str) -> str:
        self._banner("第一步 · 热点选题官")
        context = self.memory.build_context(request)
        task = (
            f"{context}\n\n"
            f"【本次创作需求】\n{request}\n\n"
            "请按你的职责产出 5 个可执行选题。"
        )
        result = self.agents["topic"].run(task)
        self.outputs["01-选题"] = result
        self._save("01-选题", result)
        print(result)
        self._remember(
            "topic",
            f"需求「{request}」产出选题：{self._extract_topics(result)[:1] or '见文件'}",
            payload={"request": request, "topics": self._extract_topics(result)},
            tags=[self.memory.get_profile().get("domain", "")],
        )
        return result

    # ------------------------------------------------------------------
    # 第二步：标题创作
    # ------------------------------------------------------------------
    def step_titles(self, topic: str) -> str:
        self._banner("第二步 · 标题创作官")
        task = (
            f"本轮已确定的选题：\n{topic}\n\n"
            "请按 5 种指定风格各产出一个标题，并说明每个标题为什么有效。"
        )
        result = self.agents["title"].run(task)
        self.outputs["02-标题"] = result
        self._save("02-标题", result)
        print(result)
        self._remember("title", f"选题「{topic[:30]}」产出标题方案", tags=["标题"])
        return result

    # ------------------------------------------------------------------
    # 第三步：文案与脚本
    # ------------------------------------------------------------------
    def step_copy(
        self, topic: str, titles: str, platforms: list[str] | None = None
    ) -> str:
        self._banner("第三步 · 文案/脚本撰写官")
        targets = platforms or self.memory.get_profile().get("platforms") or ["小红书"]
        target_text = "、".join(targets)
        task = (
            f"选定选题：\n{topic}\n\n"
            f"已生成的标题方案：\n{titles}\n\n"
            f"本轮要产出的平台：{target_text}\n\n"
            f"请只产出以上 {len(targets)} 个平台的成品，"
            "其他平台一个都不要写。每个平台用二级标题隔开。"
        )
        result = self.agents["copy"].run(task)
        self.outputs["03-文案"] = result
        self._save("03-文案", result)
        print(result)
        self._remember("copy", f"完成「{topic[:30]}」的{target_text}文案", tags=["文案"])
        return result

    # ------------------------------------------------------------------
    # 第四步：合规审核
    # ------------------------------------------------------------------
    def step_review(self, copy_text: str) -> str:
        self._banner("第四步 · 合规审核官")
        platform = self.memory.get_profile().get("primary_platform", "小红书")
        # 机器检查是确定性的；模型负责语境判断与逐句改写
        machine, report = self.agents["compliance"].check(copy_text, platform)
        result = f"【机器词表检查】\n{machine}\n\n【审核官报告】\n{report}"
        self.outputs["04-合规审核"] = result
        self._save("04-合规审核", result)
        print(result)

        # 抽样统计，方便一眼看出风险规模
        hit = re.search(r"命中风险词 (\d+) 个", machine)
        count = hit.group(1) if hit else "未知"
        self._remember(
            "review",
            f"合规审核完成，机器命中风险词 {count} 个",
            payload={"hit_count": count},
            tags=["合规"],
        )
        return result

    # ------------------------------------------------------------------
    # 第五步：运营建议
    # ------------------------------------------------------------------
    def step_ops(self, topic: str, review: str) -> str:
        self._banner("第五步 · 运营顾问")
        platforms = "、".join(self.memory.get_profile().get("platforms", []))
        task = (
            f"选题：{topic}\n\n"
            f"目标平台：{platforms}\n\n"
            f"合规审核结论摘要：\n{review[-800:]}\n\n"
            "请给出完整的发布运营方案。"
        )
        result = self.agents["ops"].run(task)
        self.outputs["05-运营方案"] = result
        self._save("05-运营方案", result)
        print(result)
        self._remember("ops", f"完成「{topic[:30]}」的运营方案", tags=["运营"])
        return result

    # ------------------------------------------------------------------
    # 第六步：视频成片
    # ------------------------------------------------------------------
    def step_video(
        self, topic: str, copy_text: str, extra_args: str = "", aspect: str = ""
    ) -> str:
        self._banner("第六步 · 视频成片生成官")
        if not self.enable_video:
            self._say("已按 --no-video 跳过视频渲染。", "warn")
            self.outputs["06-视频"] = "（本次跳过视频渲染）"
            self._save("06-视频", self.outputs["06-视频"])
            return self.outputs["06-视频"]

        target_aspect = config.normalize_aspect(aspect or self.aspect)
        subject = self.agents["video"].polish_subject(topic, copy_text)
        self._say(f"视频主题：{subject}　画面比例：{target_aspect}")
        self._say(
            f"开始调用 MoneyPrinterTurbo，素材来源 {config.MPT_VIDEO_SOURCE}，"
            f"超时上限 {config.MPT_TASK_TIMEOUT // 60} 分钟。"
        )
        self._say("首次运行会先下载视频引擎本体（几百 MB），请耐心等待，不要中断。")

        result = self.agents["video"].generate_with_aspect(
            subject, target_aspect, extra_args
        )
        self.outputs["06-视频"] = result
        self._save("06-视频", result)
        print(result)

        # 只有工具确认给出了文件路径，才算成功
        if "视频生成成功" in result:
            path_match = re.search(r"([A-Za-z]:\\[^\n]+\.mp4)", result)
            self.video_path = path_match.group(1) if path_match else ""
            self._say("视频成片已完成。", "ok")
            self._remember(
                "video",
                f"「{subject}」视频生成成功",
                payload={"video_file": self.video_path},
                tags=["视频"],
            )
        else:
            self._say("视频环节未成功，详见上方错误信息。", "error")
            self._remember("video", f"「{subject}」视频生成失败", tags=["视频"])
        return result

    # ------------------------------------------------------------------
    # 主编排流程
    # ------------------------------------------------------------------
    def run(
        self,
        request: str,
        pick_index: int = 0,
        extra_args: str = "",
        platforms: list[str] | None = None,
    ) -> dict[str, str]:
        started = datetime.now()

        # 凭证一次性检查，缺什么一次说清，不反复打断
        missing = config.missing_credentials()
        if missing:
            self._banner("启动检查未通过")
            print("缺少以下凭证，请在 .env 中补齐后重试：")
            for item in missing:
                print(f"  - {item}")
            return self.outputs

        self._banner("自媒体智能运营助手 · 开始工作")
        profile = self.memory.get_profile()
        print(f"领域：{profile.get('domain')}　主发平台：{profile.get('primary_platform')}"
              f"　风格：{profile.get('style')}")
        print(f"需求：{request}")
        print(f"产物目录：{self.out_dir}")

        # ① 选题
        topics_text = self.step_topics(request)

        # 选出本次要做的选题：用户指定优先，否则取第一个
        topics = self._extract_topics(topics_text)
        if not topics:
            self._say("未能从选题结果中解析出选题，请人工指定后重跑。", "error")
            return self.outputs
        index = pick_index if 1 <= pick_index <= len(topics) else 1
        topic = topics[index - 1]
        self._banner(f"本轮选定选题（第 {index} 个）")
        print(topic)

        # ② 标题 → ③ 文案 → ④ 审核 → ⑤ 运营 → ⑥ 视频
        titles = self.step_titles(topic)
        copy_text = self.step_copy(topic, titles, platforms)
        review = self.step_review(copy_text)
        self.step_ops(topic, review)
        self.step_video(topic, copy_text, extra_args, self.aspect)

        # 汇总
        elapsed = (datetime.now() - started).seconds
        self._banner("全部完成")
        print(f"总耗时：{elapsed // 60} 分 {elapsed % 60} 秒")
        print(f"产物目录：{self.out_dir}")
        for name in sorted(self.outputs):
            print(f"  - {name}.md")
        if self.video_path:
            print(f"\n视频文件：{self.video_path}")
        return self.outputs


def main() -> int:
    setup_stdout()
    parser = argparse.ArgumentParser(description="自媒体智能运营助手（多 Agent 协作）")
    parser.add_argument("request", help="本次创作需求，例如：孩子写作业总拖拉怎么引导")
    parser.add_argument("--pick", type=int, default=0,
                        help="选用第几个选题往下走，默认第 1 个")
    parser.add_argument("--no-video", action="store_true",
                        help="跳过视频渲染（其余环节照常执行）")
    parser.add_argument("--video-args", default="",
                        help="额外传给 MoneyPrinterTurbo CLI 的参数")
    parser.add_argument("--aspect", default=config.MPT_VIDEO_ASPECT,
                        help="画面比例：9:16 / 3:4 / 1:1 / 4:3 / 16:9")
    parser.add_argument("--platforms", default="",
                        help="目标平台，逗号分隔，例如：小红书,抖音")
    args = parser.parse_args()

    platforms = [p.strip() for p in args.platforms.replace("，", ",").split(",") if p.strip()]
    orchestrator = Orchestrator(enable_video=not args.no_video, aspect=args.aspect)
    orchestrator.run(
        args.request,
        pick_index=args.pick,
        extra_args=args.video_args,
        platforms=platforms or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
