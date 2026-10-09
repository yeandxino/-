# -*- coding: utf-8 -*-
"""check_llm.py —— 大模型与素材接口连通性自检

用途：开工前先跑一次，确认「哪个供应商通、模型名对不对」，
避免把接口问题误判成 Agent 提示词问题。

用法：
    python check_llm.py            # 逐个测试所有已配置的供应商
    python check_llm.py --models   # 额外列出各家可用模型 ID（排查模型名写错）

安全：只打印脱敏后的 Key 和精简后的响应，绝不输出完整凭证。
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

import config

# 单次请求超时（秒）
REQUEST_TIMEOUT = 60


def _post_json(url: str, api_key: str, payload: dict, timeout: int = REQUEST_TIMEOUT):
    """发送一个 JSON POST 请求，返回 (状态码, 解析后的响应体)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # 部分网关会拦截默认的 Python UA
            "User-Agent": "self-media-assistant/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _safe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _safe_json(raw)


def _get_json(url: str, api_key: str, timeout: int = REQUEST_TIMEOUT):
    """发送一个 GET 请求，返回 (状态码, 解析后的响应体)。"""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "self-media-assistant/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, _safe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, _safe_json(raw)


def _safe_json(raw: str):
    """尽量解析 JSON；解析不了就原样返回字符串。"""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _short_error(body) -> str:
    """把错误响应压成一行短信息，避免刷屏。"""
    if isinstance(body, dict):
        error = body.get("error") or body
        if isinstance(error, dict):
            message = error.get("message") or json.dumps(error, ensure_ascii=False)
            # 某些网关把真正的错误藏在 metadata 里
            meta = error.get("metadata") or {}
            if isinstance(meta, dict) and meta.get("raw"):
                message = f"{message} | {str(meta['raw'])[:200]}"
            return str(message)[:300]
        return str(error)[:300]
    return str(body)[:300]


def check_chat(conf: dict, raw: bool = False) -> bool:
    """测试某个供应商的 chat/completions 是否可用。"""
    label = conf["label"]
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": conf["model"],
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 64,
    }
    print(f"\n[测试] {label}  模型={conf['model']}")
    print(f"       地址={url}")
    print(f"       Key ={config.mask(conf['api_key'])}")

    try:
        status, body = _post_json(url, conf["api_key"], payload)
    except Exception as exc:  # 网络层异常统一归为失败
        print(f"       结果=✗ 网络异常：{config.redact(str(exc))[:200]}")
        return False

    if status == 200 and isinstance(body, dict) and body.get("choices"):
        choice = body["choices"][0]
        message = choice.get("message", {}) or {}
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        print(f"       结果=✓ HTTP 200，finish_reason={choice.get('finish_reason')}")
        print(f"       content 长度={len(str(content))}，reasoning_content 长度={len(str(reasoning))}")
        if str(content).strip():
            print(f"       正文：{str(content).strip()[:80]}")
        if raw:
            # 打印响应结构（键名 + 截断后的值），用于排查字段差异
            print("       原始响应结构：")
            print("       " + json.dumps(body, ensure_ascii=False)[:1500])
        return True

    print(f"       结果=✗ HTTP {status}：{_short_error(body)}")
    return False


def list_models(conf: dict) -> None:
    """列出供应商可用模型 ID，用于排查模型名写错。"""
    url = conf["base_url"].rstrip("/") + "/models"
    print(f"\n[模型列表] {conf['label']} —— {url}")
    try:
        status, body = _get_json(url, conf["api_key"])
    except Exception as exc:
        print(f"       ✗ 网络异常：{config.redact(str(exc))[:200]}")
        return
    if status != 200:
        print(f"       ✗ HTTP {status}：{_short_error(body)}")
        return
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        ids = [str(item.get("id")) for item in body["data"] if isinstance(item, dict)]
        print(f"       ✓ 共 {len(ids)} 个模型")
        for model_id in ids[:40]:
            mark = "  ← 当前配置" if model_id == conf["model"] else ""
            print(f"         - {model_id}{mark}")
    else:
        print(f"       ✓ 返回：{str(body)[:300]}")


def check_coverr() -> bool:
    """测试 Coverr 素材接口是否可用。"""
    if not config.MPT_COVERR_API_KEY:
        print("\n[测试] Coverr 素材 —— 未配置 Key，跳过")
        return False
    url = "https://api.coverr.co/videos?query=education&page_size=1"
    print("\n[测试] Coverr 素材")
    print(f"       地址={url}")
    print(f"       Key ={config.mask(config.MPT_COVERR_API_KEY)}")
    try:
        status, body = _get_json(url, config.MPT_COVERR_API_KEY)
    except Exception as exc:
        print(f"       结果=✗ 网络异常：{config.redact(str(exc))[:200]}")
        return False
    if status == 200:
        count = len(body.get("hits", [])) if isinstance(body, dict) else 0
        print(f"       结果=✓ 可用（本次返回 {count} 条素材）")
        return True
    print(f"       结果=✗ HTTP {status}：{_short_error(body)}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="大模型与素材接口连通性自检")
    parser.add_argument("--models", action="store_true", help="额外列出各家可用模型 ID")
    parser.add_argument("--raw", action="store_true", help="打印原始响应结构，排查字段差异")
    args = parser.parse_args()

    print("=" * 60)
    print("接口连通性自检")
    print("=" * 60)
    print("凭证状态（已脱敏）：")
    for name, value in config.credentials_status().items():
        print(f"  - {name}: {value}")

    results: list[tuple[str, bool]] = []
    for name, conf in config.LLM_PROVIDERS.items():
        if not conf["api_key"]:
            print(f"\n[跳过] {conf['label']} —— 未配置 Key")
            continue
        results.append((conf["label"], check_chat(conf, raw=args.raw)))
        if args.models:
            list_models(conf)

    results.append(("Coverr 素材", check_coverr()))

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    for label, ok in results:
        print(f"  {'✓' if ok else '✗'} {label}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
