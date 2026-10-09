# -*- coding: utf-8 -*-
"""app.py —— 网页界面（面向完全不懂代码的使用者）

启动：
    streamlit run app.py

设计目标：
1. 不用懂代码：所有选项都是下拉框、勾选框，没有需要手输的参数；
2. 可以反复使用：设置自动保存，每次生成的结果永久留存，随时回看；
3. 结果能直接用：每份文案都能一键复制、一键下载；
4. 出问题看得懂：不暴露堆栈，只给能照着做的人话提示。
"""

from __future__ import annotations

import io
import os
import shutil
import traceback
import zipfile
from datetime import datetime

import streamlit as st

import config
from agents import framework_version
from main import Orchestrator
from memory import MemoryStore

st.set_page_config(page_title="自媒体智能运营助手", page_icon="🎬", layout="wide")
config.ensure_dirs()

ALL_PLATFORMS = ["小红书", "抖音", "公众号", "B站", "视频号"]
STYLES = ["专业干货", "轻松幽默", "情感共鸣", "犀利观点"]
EXAMPLES = [
    "孩子写作业总拖拉，家长怎么引导",
    "初中生手机管理，怎么定规矩才不吵架",
    "高三最后三个月，怎么安排复习节奏",
]

# 结果顺序：与生成顺序一致
RESULT_ORDER = ["01-选题", "02-标题", "03-文案", "04-合规审核", "05-运营方案", "06-视频"]
RESULT_TITLES = {
    "01-选题": "① 选题",
    "02-标题": "② 标题",
    "03-文案": "③ 文案",
    "04-合规审核": "④ 合规审核",
    "05-运营方案": "⑤ 运营方案",
    "06-视频": "⑥ 视频成片",
}


def read_text_safely(path) -> str:
    """读取产物文件，失败时返回可读提示而不是抛异常。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"（读取失败：{exc}）"


def list_history() -> list:
    """列出历史生成记录，最新的排最前面。"""
    if not config.OUTPUT_DIR.is_dir():
        return []
    runs = [d for d in config.OUTPUT_DIR.iterdir() if d.is_dir()]
    # 目录名是时间戳，直接按名字倒序即按时间倒序
    return sorted(runs, key=lambda p: p.name, reverse=True)


def format_run_name(path) -> str:
    """把 20261008-135545 这样的目录名变成好读的时间。"""
    try:
        return datetime.strptime(path.name, "%Y%m%d-%H%M%S").strftime(
            "%Y年%m月%d日 %H:%M"
        )
    except ValueError:
        return path.name


def video_engine_available() -> tuple[bool, str]:
    """判断当前环境能不能跑视频成片。

    视频成片依赖本机的 uv、ffmpeg 和可写的安装目录，并且要跑一条
    几十分钟的前台命令。Streamlit Community Cloud 是 Linux 容器，
    这些条件都不满足，所以必须提前判断并给出明确提示，
    而不是让用户点下去之后收到一条看不懂的报错。
    """
    if os.name != "nt":
        return (
            False,
            "这是云端环境，视频成片用不了：它需要本机的 uv、ffmpeg 和写盘权限。"
            "文字部分（选题/标题/文案/审核/运营）在云端可以正常使用；"
            "要出片请在本机双击「启动.bat」运行。",
        )
    if not shutil.which("uv"):
        return (
            False,
            "本机没有检测到 uv，视频成片需要它来驱动 MoneyPrinterTurbo。"
            "安装后重新打开即可（安装命令：powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\"）。",
        )
    return True, ""


# ---------------------------------------------------------------------------
# 读取上次保存的设置
# ---------------------------------------------------------------------------
settings = config.load_settings()
aspect_keys = list(config.ASPECT_LABELS.keys())


# ---------------------------------------------------------------------------
# 左侧设置栏
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ 设置")
    st.caption("改完自动保存，下次打开还在，不用重复填。")

    domain = st.text_input(
        "你的领域",
        value=settings["domain"],
        help="例如：教育、职场、情感。会影响选题和文案的措辞。",
    )

    saved_platforms = [p for p in settings["platforms"] if p in ALL_PLATFORMS]
    platforms = st.multiselect(
        "要发到哪些平台",
        ALL_PLATFORMS,
        default=saved_platforms or ["小红书"],
        help="选几个，就只会产出这几个平台的文案。选多了每份内容仍然会单独适配。",
    )

    if platforms:
        primary_default = (
            settings["primary_platform"]
            if settings["primary_platform"] in platforms
            else platforms[0]
        )
        primary_platform = st.selectbox(
            "主发平台",
            platforms,
            index=platforms.index(primary_default),
            help="用于合规审核侧重和运营建议的默认平台。",
        )
    else:
        primary_platform = "小红书"
        st.warning("请至少选择一个平台。")

    style = st.selectbox(
        "内容风格",
        STYLES,
        index=STYLES.index(settings["style"]) if settings["style"] in STYLES else 0,
    )

    st.divider()

    aspect = st.selectbox(
        "视频画面比例",
        aspect_keys,
        index=(
            aspect_keys.index(settings["aspect"])
            if settings.get("aspect") in aspect_keys
            else 0
        ),
        format_func=lambda key: config.ASPECT_LABELS[key],
        help="抖音/小红书竖屏选 9:16；想要小红书图文那种偏竖的选 3:4。",
    )
    if config.normalize_aspect(aspect) in config.ASPECT_FALLBACK:
        st.caption(
            f"ℹ️ 引擎不原生支持 {aspect}：会先用 "
            f"{config.ASPECT_FALLBACK[aspect]} 渲染，再自动补背景边转成 {aspect}。"
        )

    video_ok, video_reason = video_engine_available()
    if video_ok:
        enable_video = st.toggle(
            "生成视频成片",
            value=bool(settings["enable_video"]),
            help="打开后会调用 MoneyPrinterTurbo 渲染 MP4（含配音、字幕、背景音乐）。",
        )
    else:
        enable_video = False
        st.toggle("生成视频成片", value=False, disabled=True)
        st.warning(video_reason)

    with st.expander("高级设置（一般不用改）"):
        video_args = st.text_input(
            "视频附加参数",
            value=settings["video_args"],
            help="直接透传给 MoneyPrinterTurbo，例如：--video-count 1",
        )
        if st.button("🔌 检查接口是否正常", use_container_width=True):
            with st.spinner("正在逐个测试，大约需要 1-2 分钟…"):
                try:
                    from check_llm import check_chat, check_coverr

                    for _, conf in config.LLM_PROVIDERS.items():
                        if conf["api_key"]:
                            ok = check_chat(conf)
                            st.write(("✅ " if ok else "❌ ") + conf["label"])
                    st.write(
                        ("✅ " if check_coverr() else "❌ ") + "Coverr 素材库"
                    )
                except Exception as exc:
                    st.error(f"检查失败：{config.redact(str(exc))}")

    # 每次界面刷新都把当前设置写回磁盘，实现"长久可用"
    config.save_settings(
        {
            "domain": domain,
            "platforms": platforms,
            "primary_platform": primary_platform,
            "style": style,
            "aspect": aspect,
            "enable_video": enable_video,
            "video_args": video_args,
        }
    )

    st.divider()
    st.caption(f"框架版本：hello-agents {framework_version()}")
    st.caption("凭证状态（已脱敏，不会显示完整密钥）")
    for name, value in config.credentials_status().items():
        st.caption(f"· {name}：{value}")
    missing = config.missing_credentials()
    if missing:
        st.error("还缺这些凭证：\n\n" + "\n\n".join(f"- {m}" for m in missing))


# ---------------------------------------------------------------------------
# 主区域：输入
# ---------------------------------------------------------------------------
st.title("🎬 自媒体智能运营助手")
st.caption("写清楚这次想做什么，点一下按钮，剩下的交给六个助手依次完成。")

st.subheader("① 这次想做什么？")
request = st.text_area(
    "本次需求",
    placeholder=f"例如：{EXAMPLES[0]}",
    height=100,
    label_visibility="collapsed",
)

st.caption("不知道怎么写？点一个例子直接套用：")
example_cols = st.columns(len(EXAMPLES))
for col, example in zip(example_cols, EXAMPLES):
    with col:
        if st.button(example, use_container_width=True, key=f"ex-{example}"):
            st.session_state["request_text"] = example
            st.rerun()

# 例子被点击后回填到输入框
if st.session_state.get("request_text") and not request:
    request = st.session_state["request_text"]

st.subheader("② 确认这次要出的东西")
platform_text = "、".join(platforms) if platforms else "（未选择）"
if enable_video:
    video_text = f"生成视频，画面比例 {aspect}"
else:
    video_text = "不生成视频（只要文案）"
st.info(f"发布平台：{platform_text}　｜　主发：{primary_platform}　｜　{video_text}")

start = st.button("🚀 开始生成", type="primary", use_container_width=True)


# ---------------------------------------------------------------------------
# 执行流程
# ---------------------------------------------------------------------------
if start:
    if not request.strip():
        st.warning("请先在「① 这次想做什么？」里写明你的需求。")
    elif not platforms:
        st.warning("请先在左侧至少选择一个发布平台。")
    elif missing:
        st.error("凭证不全，请先补齐 .env 里的配置。")
    else:
        st.session_state["results"] = {}
        st.session_state["video_file"] = ""
        st.session_state["output_dir"] = ""

        try:
            orchestrator = Orchestrator(enable_video=enable_video, aspect=aspect)
            # 把界面设置同步到记忆系统的人设档案
            orchestrator.memory.update_profile(
                domain=domain,
                platforms=platforms,
                primary_platform=primary_platform,
                style=style,
            )
            st.session_state["output_dir"] = str(orchestrator.out_dir)

            with st.status("① 正在想选题…", expanded=True) as status:
                topics_text = orchestrator.step_topics(request)
                st.markdown(topics_text)
                status.update(label="① 选题完成", state="complete")
            st.session_state["results"]["01-选题"] = topics_text

            topic_list = orchestrator._extract_topics(topics_text)
            if not topic_list:
                st.error("没能从结果里识别出选题，请再点一次「开始生成」。")
                st.stop()

            chosen = st.radio(
                "👉 这次要推进哪一个选题？",
                options=topic_list,
                index=0,
                key="chosen_topic",
            )

            with st.status("② 正在写标题…", expanded=False) as status:
                titles = orchestrator.step_titles(chosen)
                st.markdown(titles)
                status.update(label="② 标题完成", state="complete")
            st.session_state["results"]["02-标题"] = titles

            with st.status("③ 正在写文案和脚本…", expanded=False) as status:
                copy_text = orchestrator.step_copy(chosen, titles, platforms)
                st.markdown(copy_text)
                status.update(label="③ 文案完成", state="complete")
            st.session_state["results"]["03-文案"] = copy_text

            with st.status("④ 正在做合规检查…", expanded=False) as status:
                review = orchestrator.step_review(copy_text)
                st.markdown(review)
                status.update(label="④ 合规检查完成", state="complete")
            st.session_state["results"]["04-合规审核"] = review

            with st.status("⑤ 正在出运营方案…", expanded=False) as status:
                ops = orchestrator.step_ops(chosen, review)
                st.markdown(ops)
                status.update(label="⑤ 运营方案完成", state="complete")
            st.session_state["results"]["05-运营方案"] = ops

            if enable_video:
                with st.status(
                    f"⑥ 正在生成视频（{aspect}，可能需要几分钟到几十分钟）…",
                    expanded=True,
                ) as status:
                    video_result = orchestrator.step_video(
                        chosen, copy_text, video_args, aspect
                    )
                    st.markdown(video_result)
                    ok = "视频生成成功" in video_result
                    status.update(
                        label="⑥ 视频完成" if ok else "⑥ 视频没成功（文案部分不受影响）",
                        state="complete" if ok else "error",
                    )
                st.session_state["results"]["06-视频"] = video_result
                st.session_state["video_file"] = orchestrator.video_path
            else:
                st.session_state["results"]["06-视频"] = "（本次没有生成视频）"

            st.success("全部完成！下面的内容可以直接复制或下载。")
        except Exception as exc:
            st.error(f"中途出错了：{config.redact(str(exc))}")
            st.caption("已经生成的部分仍然保存在下面的「结果」和「历史记录」里。")
            # 把完整堆栈收在折叠区里：平时不干扰普通用户，
            # 出问题时能一次性看清是哪一行、哪个库报的错
            with st.expander("🔍 查看详细错误（排查问题时复制这里的内容）"):
                st.code(config.redact(traceback.format_exc()), language="text")


# ---------------------------------------------------------------------------
# 结果展示
# ---------------------------------------------------------------------------
results: dict = st.session_state.get("results") or {}

if results:
    st.divider()
    st.subheader("📄 这次的结果")
    st.caption("每一份都可以单独复制或下载；点右上角的图标可以复制全文。")

    names = [n for n in RESULT_ORDER if n in results]
    tabs = st.tabs([RESULT_TITLES.get(n, n) for n in names])
    for tab, name in zip(tabs, names):
        content = results[name]
        with tab:
            col_dl, col_tip = st.columns([1, 3])
            with col_dl:
                st.download_button(
                    "⬇️ 下载这一份",
                    data=content.encode("utf-8"),
                    file_name=f"{name}.md",
                    mime="text/markdown",
                    key=f"dl-{name}",
                    use_container_width=True,
                )
            with col_tip:
                st.caption("下载后可以用记事本或 Word 打开。")

            st.markdown(content)

            with st.expander("📋 需要纯文本？点这里复制全文"):
                st.code(content, language=None)

    # 视频预览与下载
    video_file = st.session_state.get("video_file") or ""
    if video_file:
        st.divider()
        st.subheader("🎥 视频成片")
        st.write(f"文件位置：`{video_file}`")
        try:
            with open(video_file, "rb") as handle:
                st.download_button(
                    "⬇️ 下载视频",
                    data=handle.read(),
                    file_name="video.mp4",
                    mime="video/mp4",
                    use_container_width=False,
                )
        except OSError as exc:
            st.caption(f"读取视频失败：{exc}")
        try:
            st.video(video_file)
        except Exception as exc:
            st.caption(f"网页预览失败，请直接打开上面的文件位置：{config.redact(str(exc))}")

    # 打包下载全部文案
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in names:
            if name != "06-视频":
                bundle.writestr(f"{name}.md", results[name])
    st.download_button(
        "📦 打包下载全部文案（ZIP）",
        data=buffer.getvalue(),
        file_name="文案打包.zip",
        mime="application/zip",
    )


# ---------------------------------------------------------------------------
# 历史记录（这才是"长久工具"的关键：每次生成都会留存）
# ---------------------------------------------------------------------------
st.divider()
with st.expander("📁 历史记录 —— 以前生成的内容都在这里，随时回看", expanded=not results):
    runs = list_history()
    if not runs:
        st.caption("还没有历史记录。生成一次之后，这里就会留下存档。")
    else:
        st.caption(f"共 {len(runs)} 次记录，最新的排在最前面。")
        chosen_run = st.selectbox(
            "选择一次记录",
            runs,
            format_func=format_run_name,
            key="history-run",
        )
        if chosen_run:
            files = [f for f in sorted(chosen_run.glob("*.md")) if f.stat().st_size > 0]
            if not files:
                st.caption("这次没有留下内容。")
            else:
                chosen_file = st.selectbox(
                    "查看哪一份",
                    files,
                    format_func=lambda p: RESULT_TITLES.get(p.stem, p.stem),
                    key="history-file",
                )
                text = read_text_safely(chosen_file)
                st.download_button(
                    "⬇️ 下载这一份",
                    data=text.encode("utf-8"),
                    file_name=chosen_file.name,
                    mime="text/markdown",
                    key="history-dl",
                )
                st.markdown(text)


# ---------------------------------------------------------------------------
# 记忆（让工具越用越懂你）
# ---------------------------------------------------------------------------
with st.expander("🧠 它记住的东西（避免选题重复、保持人设一致）"):
    try:
        store = MemoryStore()
        stats = store.stats()
        left, right = st.columns(2)
        left.metric("最近动态", stats["short_term"])
        right.metric("历史累计", stats["long_term"])

        st.write("你的档案")
        st.json(store.get_profile())

        recent = store.recent()
        if recent:
            st.write("最近做过什么")
            for record in recent:
                st.write(f"- [{record.get('type')}] {record.get('summary')}")

        keyword = st.text_input("搜索历史（比如：作业、青春期）", value="")
        if keyword:
            hits = store.search(keyword, limit=8)
            if hits:
                for record in hits:
                    st.write(f"- [{record.get('type')}] {record.get('summary')}")
            else:
                st.caption("没有找到相关记录。")
    except Exception as exc:
        st.error(f"读取记忆失败：{config.redact(str(exc))}")
