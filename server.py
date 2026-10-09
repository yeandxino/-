# -*- coding: utf-8 -*-
"""server.py —— FastAPI 对外接口

启动：
    python server.py
    # 或
    uvicorn server:app --host 127.0.0.1 --port 8000 --reload

接口一览：
    GET  /health              健康检查
    GET  /api/status          凭证（脱敏）、记忆统计、视频引擎配置
    GET  /api/check           大模型与素材接口连通性自检
    POST /api/run             跑完整多 Agent 流程（可含视频渲染）
    POST /api/topics          只跑选题环节
    POST /api/compliance      只跑合规检查
    POST /api/video           只跑视频成片
    GET  /api/memory          读取记忆
    POST /api/memory          写入/检索记忆

设计说明：
- 全部用同步 def 定义，FastAPI 会自动放到线程池执行，避免阻塞事件循环；
- 视频渲染非常吃资源，官方明确要求「不要并发跑多个视频任务」，
  因此用一把进程内互斥锁串行化整条流水线。
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import config
from main import Orchestrator
from memory import MemoryStore
from tools import build_tools

app = FastAPI(
    title="自媒体智能运营助手",
    description="多 Agent 协作的教育类自媒体全流程助手（选题/标题/文案/审核/运营/视频）",
    version="1.0.0",
)

# 进程内复用：记忆与工具都是无状态的轻量对象
_memory = MemoryStore()
_tools = build_tools()

# 流水线互斥锁：视频渲染耗时且吃资源，禁止并发
_pipeline_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    """完整流程请求。"""

    request: str = Field(..., description="创作需求，例如：孩子写作业总拖拉怎么引导")
    pick: int = Field(0, description="选用第几个选题往下走，0 表示自动取第 1 个")
    enable_video: bool = Field(True, description="是否执行视频渲染")
    video_args: str = Field("", description="额外传给 MoneyPrinterTurbo 的参数")


class ComplianceRequest(BaseModel):
    """合规检查请求。"""

    content: str = Field(..., description="需要审核的文案全文")
    platform: str = Field("小红书", description="目标平台")


class VideoRequest(BaseModel):
    """视频生成请求。"""

    subject: str = Field(..., description="视频主题")
    extra_args: str = Field("", description="额外的 MoneyPrinterTurbo 参数")


class MemoryRequest(BaseModel):
    """记忆读写请求。"""

    action: str = Field("recent", description="add / search / recent / topics / profile")
    content: str = Field("", description="add 的摘要内容，search 的查询词")
    type: str = Field("note", description="记录类型")
    tags: str = Field("", description="标签，逗号分隔")


# ---------------------------------------------------------------------------
# 基础接口
# ---------------------------------------------------------------------------


@app.get("/health", summary="健康检查")
def health() -> dict[str, Any]:
    """最轻量的存活探测，不触发任何外部调用。"""
    return {"status": "ok"}


@app.get("/api/status", summary="查看运行状态")
def status() -> dict[str, Any]:
    """返回脱敏后的凭证状态与记忆统计。绝不返回密钥原文。"""
    return {
        "credentials": config.credentials_status(),
        "missing": config.missing_credentials(),
        "memory": _memory.stats(),
        "profile": _memory.get_profile(),
        "video_engine": {
            "install_root": str(config.MPT_ROOT),
            "skill_dir": str(config.MPT_SKILL_DIR),
            "skill_installed": all(
                (config.MPT_SKILL_DIR / name).is_file()
                for name in ("SKILL.md", "mpt_agent.py")
            ),
            "source": config.MPT_VIDEO_SOURCE,
            "timeout_seconds": config.MPT_TASK_TIMEOUT,
        },
    }


@app.get("/api/check", summary="接口连通性自检")
def check() -> dict[str, Any]:
    """逐个探测大模型供应商与素材接口，方便快速定位故障方。"""
    # 延迟导入：避免服务启动时就依赖网络相关代码
    from check_llm import check_chat, check_coverr

    results: dict[str, bool] = {}
    for name, conf in config.LLM_PROVIDERS.items():
        if not conf["api_key"]:
            continue
        results[conf["label"]] = check_chat(conf)
    results["Coverr 素材"] = check_coverr()
    return {"results": results}


# ---------------------------------------------------------------------------
# 流程接口
# ---------------------------------------------------------------------------


@app.post("/api/run", summary="跑完整多 Agent 流程")
def run_pipeline(req: RunRequest) -> dict[str, Any]:
    """串行执行：选题 → 标题 → 文案 → 审核 → 运营 → 视频。"""
    missing = config.missing_credentials()
    if missing:
        raise HTTPException(status_code=400, detail={"missing_credentials": missing})

    if not _pipeline_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="已有流程正在执行。视频渲染不可并发，请等待当前任务结束。",
        )
    try:
        orchestrator = Orchestrator(enable_video=req.enable_video)
        outputs = orchestrator.run(
            req.request, pick_index=req.pick, extra_args=req.video_args
        )
        return {
            "outputs": outputs,
            "video_file": orchestrator.video_path,
            "output_dir": str(orchestrator.out_dir),
        }
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=config.redact(str(exc))
        ) from exc
    finally:
        _pipeline_lock.release()


@app.post("/api/topics", summary="只跑选题环节")
def run_topics(req: RunRequest) -> dict[str, Any]:
    """单跑选题，返回选题文本。"""
    if config.missing_credentials():
        raise HTTPException(status_code=400, detail="缺少大模型凭证，请检查 .env")
    try:
        orchestrator = Orchestrator(enable_video=False)
        text = orchestrator.step_topics(req.request)
        return {"topics": orchestrator._extract_topics(text), "raw": text}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=config.redact(str(exc))) from exc


@app.post("/api/compliance", summary="合规审核")
def run_compliance(req: ComplianceRequest) -> dict[str, Any]:
    """纯本地词表检查 + 可选的模型审核报告。"""
    try:
        machine = _tools["compliance"].run(
            {"content": req.content, "platform": req.platform}
        )
        return {"machine_result": machine}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=config.redact(str(exc))) from exc


@app.post("/api/video", summary="视频成片")
def run_video(req: VideoRequest) -> dict[str, Any]:
    """单独触发视频渲染。耗时较长，不可并发。"""
    if not _pipeline_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有视频任务在跑，请稍后重试。")
    try:
        result = _tools["video"].run(
            {"subject": req.subject, "extra_args": req.extra_args}
        )
        return {"result": result, "success": "视频生成成功" in result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=config.redact(str(exc))) from exc
    finally:
        _pipeline_lock.release()


# ---------------------------------------------------------------------------
# 记忆接口
# ---------------------------------------------------------------------------


@app.get("/api/memory", summary="读取记忆")
def read_memory(limit: int = 20) -> dict[str, Any]:
    """返回短期记忆与长期记忆（倒序）。"""
    return {
        "profile": _memory.get_profile(),
        "recent": _memory.recent(),
        "long_term": list(reversed(_memory.all_records()))[: max(1, limit)],
        "stats": _memory.stats(),
    }


@app.post("/api/memory", summary="写入或检索记忆")
def write_memory(req: MemoryRequest) -> dict[str, Any]:
    """转发到记忆工具，复用同一套 action 语义。"""
    try:
        return {"result": _tools["memory"].run(req.model_dump())}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=config.redact(str(exc))) from exc


if __name__ == "__main__":
    import uvicorn

    # 默认只监听本机，避免把带凭证的服务暴露到局域网
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=False)
