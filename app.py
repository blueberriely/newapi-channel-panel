"""
NewAPI / One-API 风格中转站的渠道面板后端。

管理多个 API 中转渠道（name / base_url / api_key / status_url），并提供：
- 渠道 CRUD（增删改查 + 标记默认渠道）
- 拉取渠道下的模型列表，按你账号所在的计费分组换算出实际单价（不是官方标价）
- 探测渠道的状态页，兼容三种常见格式：Uptime-Kuma 风格状态页 / NewAPI 内置的
  模型状态组件 / 自定义的 model-status 监控接口

不包含：基于控制台 session cookie 读取账户余额的功能。这类功能需要把登录 cookie
粘贴进配置里，出于安全考虑没有收录进开源版——具体思路见 README。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CHANNEL_PANEL_DATA_DIR", os.path.join(APP_DIR, "data"))
CHANNELS_FILE = os.path.join(DATA_DIR, "channels.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()

app = FastAPI(title="NewAPI Channel Panel")


def _require_auth(request: Request):
    if not AUTH_TOKEN:
        return None
    header = request.headers.get("authorization", "")
    if header == f"Bearer {AUTH_TOKEN}":
        return None
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data
        except Exception:
            pass
    return default


def _save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def _load_channels() -> list:
    data = _load_json(CHANNELS_FILE, [])
    return data if isinstance(data, list) else []


def _save_channels(items: list):
    _save_json(CHANNELS_FILE, items)


def _load_state() -> dict:
    data = _load_json(STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_state(state: dict):
    _save_json(STATE_FILE, state)


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


# ============================================================
# Channel CRUD
# 渠道结构：{ id, name, base_url, api_key, status_url? }
# "active" 只是一个默认指针，方便你的前端高亮当前用哪个渠道，不影响其它接口。
# ============================================================

@app.get("/channels")
async def list_channels(request: Request):
    err = _require_auth(request)
    if err: return err
    items = _load_channels()
    state = _load_state()
    return JSONResponse({
        "channels": items,
        "active_channel_id": state.get("active_channel_id") or None,
    })


@app.post("/channels")
async def create_channel(request: Request):
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
        base_url = (body.get("base_url") or "").strip()
        api_key = (body.get("api_key") or "").strip()
        status_url = (body.get("status_url") or "").strip()
        if not name or not base_url:
            return JSONResponse({"ok": False, "error": "name and base_url required"}, status_code=400)
        new_item = {
            "id": _gen_id("ch"),
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "status_url": status_url,
        }
        items = _load_channels()
        items.append(new_item)
        _save_channels(items)
        return JSONResponse({"ok": True, "channel": new_item})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.patch("/channels/{channel_id}")
async def update_channel(channel_id: str, request: Request):
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
        items = _load_channels()
        for i, ch in enumerate(items):
            if ch.get("id") == channel_id:
                for k in ("name", "base_url", "api_key", "status_url"):
                    if k in body:
                        items[i][k] = (body[k] or "").strip() if k == "status_url" else body[k]
                _save_channels(items)
                return JSONResponse({"ok": True, "channel": items[i]})
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# 注意：/channels/active 必须注册在 /channels/{channel_id} 之前——两者路径形状
# 会重叠（{channel_id} 匹配任意单段路径，包括字面量 "active"），FastAPI/Starlette
# 按注册顺序取第一个匹配的路由，后注册的字面量路径永远轮不到。
@app.delete("/channels/active")
async def clear_active_channel(request: Request):
    err = _require_auth(request)
    if err: return err
    state = _load_state()
    state["active_channel_id"] = ""
    _save_state(state)
    return JSONResponse({"ok": True})


@app.delete("/channels/{channel_id}")
async def delete_channel(channel_id: str, request: Request):
    err = _require_auth(request)
    if err: return err
    items = _load_channels()
    new_items = [ch for ch in items if ch.get("id") != channel_id]
    if len(new_items) == len(items):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    _save_channels(new_items)
    state = _load_state()
    if state.get("active_channel_id") == channel_id:
        state["active_channel_id"] = ""
        _save_state(state)
    return JSONResponse({"ok": True})


@app.post("/channels/{channel_id}/active")
async def set_active_channel(channel_id: str, request: Request):
    err = _require_auth(request)
    if err: return err
    items = _load_channels()
    if not any(ch.get("id") == channel_id for ch in items):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    state = _load_state()
    state["active_channel_id"] = channel_id
    _save_state(state)
    return JSONResponse({"ok": True, "active_channel_id": channel_id})


# ============================================================
# Models + pricing + status aggregation
# ============================================================

@app.get("/channels/{channel_id}/models")
async def fetch_models(channel_id: str, request: Request):
    """拉取渠道下的模型列表：GET {base_url}/models（带渠道的 api_key）。
    附带按账号分组换算过的实际单价，以及状态页探测结果。"""
    err = _require_auth(request)
    if err: return err
    ch = None
    for c in _load_channels():
        if c.get("id") == channel_id:
            ch = c
            break
    if not ch:
        return JSONResponse({"ok": False, "error": "channel not found"}, status_code=404)
    base_url = (ch.get("base_url") or "").rstrip("/")
    api_key = ch.get("api_key") or ""
    if not base_url:
        return JSONResponse({"ok": False, "error": "channel has no base_url"}, status_code=400)
    include_status_param = (request.query_params.get("include_status") or "true").strip().lower()
    include_status = include_status_param not in ("0", "false", "no", "off")

    def _origin_from_base_url(url: str) -> str:
        from urllib.parse import urlparse
        raw = (url or "").strip()
        if "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw.rstrip("/")
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")

    site_origin = _origin_from_base_url(base_url)

    def _string_model_price(value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return value.strip() or None
        return str(value)

    def _format_prompt_price(value):
        text_value = _string_model_price(value)
        if not text_value:
            return None
        try:
            numeric = float(text_value)
        except Exception:
            return text_value
        return f"${numeric * 1_000_000:.2f}/M"

    def _first_present(item, keys):
        for key in keys:
            if key in item and item.get(key) is not None:
                return item.get(key)
        return None

    def _first_pricing_present(pricing, keys):
        if not pricing:
            return None
        for key in keys:
            if key in pricing and pricing.get(key) is not None:
                return pricing.get(key)
        return None

    def _route_and_model(model_name: str):
        text = (model_name or "").strip()
        m = re.match(r"^\[([^\]]+)\](.+)$", text)
        if not m:
            return None, text
        return m.group(1).strip() or None, m.group(2).strip() or text

    def _norm_match(value: str) -> str:
        return re.sub(r"[\s_\-·|/｜:：()\[\]（）]+", "", (value or "").lower())

    def _loose_match(a: str, b: str) -> bool:
        na, nb = _norm_match(a), _norm_match(b)
        return bool(na and nb and (na in nb or nb in na))

    def _channel_label_candidates():
        name = (ch.get("name") or "").strip()
        labels = [name]
        for sep in (" · ", " | ", " / ", "｜"):
            if sep in name:
                labels.extend([part.strip() for part in name.split(sep) if part.strip()])
        return [label for label in labels if label]

    def _format_newapi_price(row: dict, group_ratio=None):
        quota_type = row.get("quota_type")
        model_price = row.get("model_price")
        model_ratio = row.get("model_ratio")
        completion_ratio = row.get("completion_ratio")
        cache_ratio = row.get("cache_ratio")
        create_cache_ratio = row.get("create_cache_ratio")

        from decimal import Decimal, InvalidOperation

        def _decimal_value(value):
            if value in (None, ""):
                return None
            try:
                number = Decimal(str(value))
            except (InvalidOperation, ValueError):
                return None
            return number if number.is_finite() else None

        def _format_decimal(number):
            text = format(number.normalize(), "f")
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return text or "0"

        def _format_price_value(value):
            number = _decimal_value(value)
            if number is None:
                return _string_model_price(value)
            return _format_decimal(number)

        factor = _decimal_value(group_ratio)
        if factor is None:
            factor = Decimal("1")

        # NewAPI/One-API 生态的约定：model_ratio=1 对应每 1M tokens $2（沿用自最早
        # gpt-3.5-turbo 的基准价）。站方前端渲染价格时都会拿 model_ratio 再乘这个 2
        # 才是真实美元价，这里之前漏乘了，导致所有按量计费模型显示的价格是实际的一半。
        # quota_type=1（按次）不受影响，model_price 本身已经是绝对金额。
        BASE_RATE_PER_MILLION = Decimal("2")

        def _with_group(value):
            number = _decimal_value(value)
            if number is None:
                return None
            return number * factor

        def _with_group_rate(value):
            number = _decimal_value(value)
            if number is None:
                return None
            return number * factor * BASE_RATE_PER_MILLION

        def _derived_price(multiplier):
            base = _decimal_value(model_ratio)
            factor_inner = _decimal_value(multiplier)
            if base is None or factor_inner is None:
                return _format_price_value(multiplier)
            return _format_decimal(base * factor_inner * factor * BASE_RATE_PER_MILLION)

        if str(quota_type) == "1" and model_price not in (None, ""):
            adjusted = _with_group(model_price)
            return f"按次 {_format_decimal(adjusted)}" if adjusted is not None else f"按次 {model_price}"

        parts = []
        if model_ratio not in (None, ""):
            adjusted_input = _with_group_rate(model_ratio)
            parts.append(f"输入 {_format_decimal(adjusted_input)}" if adjusted_input is not None else f"输入 {_format_price_value(model_ratio)}")
        if completion_ratio not in (None, ""):
            parts.append(f"输出 {_derived_price(completion_ratio)}")
        if cache_ratio not in (None, ""):
            parts.append(f"缓存 {_derived_price(cache_ratio)}")
        if create_cache_ratio not in (None, ""):
            parts.append(f"写入 {_derived_price(create_cache_ratio)}")
        return " / ".join(parts) if parts else None

    async def _fetch_json(client, url: str, *, auth: bool = False, extra_headers: dict | None = None, timeout: float = 10.0):
        headers = {"Accept": "application/json,text/plain,*/*"}
        if auth and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = await client.get(url, headers=headers, timeout=timeout)
            if resp.status_code in (401, 403):
                return None, {"auth_required": True, "status": resp.status_code}
            if resp.status_code != 200:
                return None, {"status": resp.status_code}
            return resp.json(), None
        except Exception as exc:
            return None, {"error": str(exc)}

    async def _post_json(client, url: str, payload, *, extra_headers: dict | None = None, timeout: float = 20.0):
        headers = {"Accept": "application/json,text/plain,*/*", "Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = await client.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code in (401, 403):
                return None, {"auth_required": True, "status": resp.status_code}
            if resp.status_code != 200:
                return None, {"status": resp.status_code}
            return resp.json(), None
        except Exception as exc:
            return None, {"error": str(exc)}

    def _pricing_group_ratios(data):
        raw = data.get("group_ratio") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        out = {}
        for key, value in raw.items():
            key_text = str(key or "").strip()
            if key_text and value not in (None, ""):
                out[key_text] = value
        return out

    def _channel_group_from_ratios(group_ratios: dict):
        if not group_ratios:
            return None, None
        labels = _channel_label_candidates()
        groups = list(group_ratios.keys())
        for label in labels:
            label_norm = _norm_match(label)
            if not label_norm:
                continue
            for group in groups:
                if label_norm == _norm_match(group):
                    return group, group_ratios[group]
        loose_matches = []
        for label in labels:
            for group in groups:
                if _loose_match(label, group):
                    loose_matches.append(group)
        if loose_matches:
            loose_matches.sort(key=lambda item: len(_norm_match(item)), reverse=True)
            group = loose_matches[0]
            return group, group_ratios[group]
        return None, None

    def _row_group_ratio(groups, channel_group, channel_group_ratio):
        if not channel_group or channel_group_ratio in (None, ""):
            return None, None
        if not groups:
            return channel_group, channel_group_ratio
        for group in groups:
            if _norm_match(group) == _norm_match(channel_group) or _loose_match(group, channel_group):
                return channel_group, channel_group_ratio
        return None, None

    async def _fetch_public_pricing(client):
        pricing_url = f"{site_origin}/api/pricing"
        data, err = await _fetch_json(client, pricing_url)
        if err:
            return [], bool(err.get("auth_required")), pricing_url if err.get("auth_required") else None
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return [], False, pricing_url
        group_ratios = _pricing_group_ratios(data)
        channel_group, channel_group_ratio = _channel_group_from_ratios(group_ratios)
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_name = row.get("model_name") or row.get("model") or row.get("id") or ""
            if not raw_name:
                continue
            route, clean_model = _route_and_model(raw_name)
            groups = row.get("enable_groups") if isinstance(row.get("enable_groups"), list) else []
            groups = [str(g) for g in groups]
            applied_group, applied_ratio = _row_group_ratio(groups, channel_group, channel_group_ratio)
            base_price = _format_newapi_price(row)
            adjusted_price = _format_newapi_price(row, applied_ratio) if applied_ratio not in (None, "") else base_price
            normalized.append({
                "raw_name": raw_name,
                "route": route,
                "model": clean_model,
                "groups": groups,
                "group": applied_group,
                "group_ratio": applied_ratio,
                "base_price": base_price,
                "price": adjusted_price,
                "row": row,
            })
        return normalized, False, pricing_url

    def _walk_strings(value):
        if isinstance(value, str):
            yield value
            stripped = value.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except Exception:
                    parsed = None
                if parsed is not None:
                    yield from _walk_strings(parsed)
        elif isinstance(value, dict):
            for child in value.values():
                yield from _walk_strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from _walk_strings(child)

    def _status_api_urls_from_status_url(status_url: str):
        from urllib.parse import urlparse
        raw = (status_url or "").strip()
        if raw.startswith("//"):
            raw = "https:" + raw
        elif raw and "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        match = re.search(r"/status/([^/?#]+)", parsed.path or "")
        if not parsed.scheme or not parsed.netloc or not match:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}"
        slug = match.group(1)
        return f"{origin}/api/status-page/{slug}", f"{origin}/api/status-page/heartbeat/{slug}", raw

    def _status_url_candidates_from_user_url(status_url: str):
        from urllib.parse import urlparse
        raw = (status_url or "").strip()
        if not raw:
            return []
        exact = _status_api_urls_from_status_url(raw)
        if exact:
            return [exact]
        if raw.startswith("//"):
            raw = "https:" + raw
        elif "://" not in raw:
            raw = "https://" + raw
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return []
        origin = f"{parsed.scheme}://{parsed.netloc}"
        guessed_slugs = []
        path_parts = [p for p in (parsed.path or "").split("/") if p]
        if path_parts and path_parts[-1] not in ("api", "status", "status-page", "heartbeat"):
            guessed_slugs.append(path_parts[-1])
        guessed_slugs.extend(["api", "tree", "status", "main"])
        out = []
        seen = set()
        for slug in guessed_slugs:
            if not slug or slug in seen:
                continue
            seen.add(slug)
            out.append((
                f"{origin}/api/status-page/{slug}",
                f"{origin}/api/status-page/heartbeat/{slug}",
                f"{origin}/status/{slug}",
            ))
        return out

    def _status_origin_candidates():
        from urllib.parse import urlparse
        has_configured_status_url = bool((ch.get("status_url") or "").strip())
        parsed = urlparse(site_origin)
        host = parsed.netloc
        parts = host.split(".")
        hosts = [host]
        if not has_configured_status_url:
            if len(parts) >= 3 and parts[0] in ("api", "www", "newapi"):
                hosts.append("status." + ".".join(parts[1:]))
            elif len(parts) >= 2 and parts[0] != "status":
                hosts.append("status." + ".".join(parts[-2:]))
        out = []
        seen = set()
        for host_value in hosts:
            url = f"{parsed.scheme}://{host_value}"
            if url not in seen:
                seen.add(url)
                out.append(url)
        return out

    def _status_int_from_newapi(value: str):
        text = (value or "").lower()
        if text == "green":
            return 1
        if text == "yellow":
            return 2
        if text == "red":
            return 0
        if text in ("gray", "grey", "empty"):
            return 2
        return None

    def _status_timeout(deadline, max_timeout):
        if deadline is None:
            return max_timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        return max(0.5, min(max_timeout, remaining))

    def _newapi_status_message(row: dict):
        parts = []
        rate = row.get("success_rate")
        if rate not in (None, ""):
            try:
                parts.append(f"{float(rate):.2f}%")
            except Exception:
                parts.append(f"{rate}%")
        total = row.get("total_requests")
        if total not in (None, ""):
            parts.append(f"{total} req")
        window = row.get("time_window")
        if window:
            parts.append(str(window))
        current = row.get("current_status")
        if current:
            parts.append(str(current))
        return " · ".join(parts)

    def _format_duration(seconds):
        try:
            total = int(float(seconds))
        except Exception:
            return str(seconds) if seconds not in (None, "") else ""
        if total % 3600 == 0:
            return f"{total // 3600}h"
        if total % 60 == 0:
            return f"{total // 60}m"
        return f"{total}s"

    def _format_ms(value):
        if value in (None, ""):
            return None
        try:
            numeric = float(value)
        except Exception:
            return str(value)
        if numeric >= 1000:
            return f"{numeric / 1000:.1f}s"
        return f"{int(numeric)}ms"

    def _availability_status(value):
        try:
            numeric = float(value)
        except Exception:
            return None
        if numeric <= 0:
            return 0
        if numeric >= 99:
            return 1
        return 2

    def _custom_status_message(row: dict):
        parts = []
        availability = row.get("availability_percent")
        if availability not in (None, ""):
            try:
                parts.append(f"{float(availability):.1f}%")
            except Exception:
                parts.append(f"{availability}%")
        calls = row.get("calls")
        if calls not in (None, ""):
            parts.append(f"{calls} req")
        response = row.get("response_time_ms") if isinstance(row.get("response_time_ms"), dict) else {}
        response_p50 = _format_ms(response.get("p50"))
        if response_p50:
            parts.append(f"P50 {response_p50}")
        first_token = row.get("first_token_time_ms") if isinstance(row.get("first_token_time_ms"), dict) else {}
        first_token_p50 = _format_ms(first_token.get("p50"))
        if first_token_p50:
            parts.append(f"首字 {first_token_p50}")
        return " · ".join(parts)

    async def _fetch_custom_model_status_summary(client, deadline=None):
        configured_status_url = (ch.get("status_url") or "").strip()
        if not configured_status_url:
            return [], None
        origin = _origin_from_base_url(configured_status_url)
        if not origin:
            return [], None
        browser_headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": configured_status_url,
        }
        models_url = f"{origin}/api/v1/model-status/models"
        timeout = _status_timeout(deadline, 3.0)
        if timeout is None:
            return [], None
        directory, directory_err = await _fetch_json(
            client,
            models_url,
            extra_headers=browser_headers,
            timeout=timeout,
        )
        if directory_err or not isinstance(directory, dict):
            return [], None
        targets = directory.get("models")
        if not isinstance(targets, list) or not targets:
            return [], None
        clean_targets = []
        seen_targets = set()
        for target in targets:
            if not isinstance(target, dict):
                continue
            group_name = str(target.get("group_name") or "")
            model_name = str(target.get("model_name") or "")
            if not group_name or not model_name:
                continue
            key = (group_name, model_name)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            clean_targets.append({"group_name": group_name, "model_name": model_name})
        if not clean_targets:
            return [], None
        status_url = f"{origin}/api/v1/model-status"
        timeout = _status_timeout(deadline, 20.0)
        if timeout is None:
            return [], None
        status_data, status_err = await _post_json(
            client,
            status_url,
            {"targets": clean_targets},
            extra_headers=browser_headers,
            timeout=timeout,
        )
        if status_err or not isinstance(status_data, dict):
            return [], None
        rows = status_data.get("models")
        if not isinstance(rows, list):
            return [], None
        time_label = _format_duration(status_data.get("window_seconds"))
        summary = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            group_name = str(row.get("group_name") or "")
            model_name = str(row.get("model_name") or "")
            if not model_name:
                continue
            summary.append({
                "id": f"{group_name}\u0000{model_name}" if group_name else model_name,
                "name": model_name,
                "group": group_name or "模型监控",
                "status": _availability_status(row.get("availability_percent")),
                "time": time_label,
                "message": _custom_status_message(row),
                "ping": None,
            })
        if summary:
            return summary, configured_status_url
        return [], None

    async def _fetch_newapi_middleware_status_summary(client, deadline=None):
        from urllib.parse import quote
        configured_status_url = (ch.get("status_url") or "").strip()
        origins = []
        if configured_status_url:
            origins.append(_origin_from_base_url(configured_status_url))
        origins.append(site_origin)
        seen_origins = set()
        for origin in origins:
            if not origin or origin in seen_origins:
                continue
            seen_origins.add(origin)
            config_url = f"{origin}/api/model-status/embed/config/selected"
            timeout = _status_timeout(deadline, 3.0)
            if timeout is None:
                return [], None
            config, config_err = await _fetch_json(client, config_url, timeout=timeout)
            if config_err or not isinstance(config, dict) or not config.get("success"):
                continue
            models = config.get("data")
            if not isinstance(models, list) or not models:
                continue
            model_names = [str(model) for model in models if model]
            if not model_names:
                continue
            time_window = str(config.get("time_window") or "24h")
            batch_url = f"{origin}/api/model-status/embed/status/batch?window={quote(time_window)}"
            timeout = _status_timeout(deadline, 12.0)
            if timeout is None:
                return [], None
            batch, batch_err = await _post_json(client, batch_url, model_names, timeout=timeout)
            if batch_err or not isinstance(batch, dict) or not batch.get("success"):
                continue
            rows = batch.get("data")
            if not isinstance(rows, list):
                continue

            group_by_model = {}
            groups = config.get("custom_groups")
            if isinstance(groups, list):
                for group in groups:
                    if not isinstance(group, dict):
                        continue
                    group_name = str(group.get("name") or group.get("id") or "")
                    group_models = group.get("models")
                    if not isinstance(group_models, list):
                        continue
                    for model in group_models:
                        model_text = str(model or "")
                        if model_text and group_name:
                            group_by_model[model_text] = group_name

            summary = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                model_name = str(row.get("model_name") or row.get("display_name") or "")
                if not model_name:
                    continue
                display_name = str(row.get("display_name") or model_name)
                summary.append({
                    "id": model_name,
                    "name": display_name,
                    "group": group_by_model.get(model_name) or str(config.get("site_title") or "模型监控"),
                    "status": _status_int_from_newapi(row.get("current_status")),
                    "time": str(row.get("time_window") or time_window),
                    "message": _newapi_status_message(row),
                    "ping": None,
                })
            if summary:
                if configured_status_url and _origin_from_base_url(configured_status_url) == origin:
                    return summary, configured_status_url
                return summary, origin
        return [], None

    async def _fetch_status_summary(client):
        status_deadline = time.monotonic() + 10.0
        candidates = []
        configured_status_url = (ch.get("status_url") or "").strip()
        if configured_status_url:
            candidates.extend(_status_url_candidates_from_user_url(configured_status_url))
        status_config, _ = await _fetch_json(client, f"{site_origin}/api/status")
        if status_config is not None:
            for text in _walk_strings(status_config):
                if "/status/" in text:
                    urls = _status_api_urls_from_status_url(text)
                    if urls:
                        candidates.append(urls)
        for origin in _status_origin_candidates():
            for slug in ("api", "tree", "status", "main"):
                candidates.append((
                    f"{origin}/api/status-page/{slug}",
                    f"{origin}/api/status-page/heartbeat/{slug}",
                    f"{origin}/status/{slug}",
                ))

        seen = set()
        for status_url, heartbeat_url, source_url in candidates:
            key = (status_url, heartbeat_url)
            if key in seen:
                continue
            seen.add(key)
            timeout = _status_timeout(status_deadline, 1.5)
            if timeout is None:
                return [], None
            status_data, status_err = await _fetch_json(client, status_url, timeout=timeout)
            if status_err or not isinstance(status_data, dict):
                continue
            timeout = _status_timeout(status_deadline, 1.5)
            if timeout is None:
                return [], None
            heartbeat_data, _ = await _fetch_json(client, heartbeat_url, timeout=timeout)
            groups = status_data.get("publicGroupList")
            if not isinstance(groups, list):
                continue
            latest_by_id = {}
            hb_list = heartbeat_data.get("heartbeatList") if isinstance(heartbeat_data, dict) else {}
            if isinstance(hb_list, dict):
                for mid, rows in hb_list.items():
                    if isinstance(rows, list) and rows:
                        latest_by_id[str(mid)] = rows[-1]

            summary = []
            for group in groups:
                group_name = str(group.get("name") or "")
                monitors = group.get("monitorList") or []
                for monitor in monitors:
                    if not isinstance(monitor, dict):
                        continue
                    mid = str(monitor.get("id") or "")
                    latest = latest_by_id.get(mid) or {}
                    summary.append({
                        "id": mid,
                        "name": str(monitor.get("name") or ""),
                        "group": group_name,
                        "status": latest.get("status"),
                        "time": latest.get("time"),
                        "message": latest.get("msg") or "",
                        "ping": latest.get("ping"),
                    })
            return summary, source_url
        summary, source = await _fetch_newapi_middleware_status_summary(client, status_deadline)
        if summary:
            return summary, source
        return await _fetch_custom_model_status_summary(client, status_deadline)

    def _pick_pricing_for_option(option, pricing_rows):
        model_id = option.get("id") or ""
        if not model_id:
            return None
        _, clean_model_id = _route_and_model(model_id)
        matches = []
        for row in pricing_rows:
            if row["raw_name"] == model_id or row["model"] == model_id or row["model"] == clean_model_id:
                matches.append(row)
        if not matches:
            return None
        labels = _channel_label_candidates()
        def score(row):
            value = 0
            if row["raw_name"] == model_id:
                value += 10
            if row["route"] and any(_loose_match(label, row["route"]) for label in labels):
                value += 4
            if any(any(_loose_match(label, group) for label in labels) for group in row["groups"]):
                value += 3
            return value
        return sorted(matches, key=score, reverse=True)[0]

    def _pick_status_for_option(option, pricing_row, status_summary):
        model_id = option.get("id") or ""
        if not model_id:
            return None
        _, clean_model_id = _route_and_model(model_id)

        def _exact(a, b):
            na, nb = _norm_match(a), _norm_match(b)
            return bool(na and nb and na == nb)

        group_labels = _channel_label_candidates()
        if pricing_row:
            if pricing_row.get("group"):
                group_labels.append(pricing_row["group"])
            group_labels.extend(pricing_row.get("groups") or [])

        def _best_by_group(candidates):
            if len(candidates) <= 1:
                return candidates[0] if candidates else None
            def group_score(status):
                group_name = status.get("group") or ""
                return 1 if any(_loose_match(label, group_name) for label in group_labels if label) else 0
            return sorted(candidates, key=group_score, reverse=True)[0]

        # 第一层：带路由前缀的"全名"精确对上号——这是最具体的一层。不少中转站的
        # 价位分档就是直接烧在完整模型名里（比如同一个 claude-opus-4-8 会有
        # [特价MAX-CC]claude-opus-4-8 / [特价次kiro]claude-opus-4-8 这种不同分档的
        # 完整名字），而且同一个底层模型的不同路由实测可用率可能天差地别（实测过
        # 同一个 claude-opus-4-6，两个路由一个 97% 一个 0%），全名对上了就该直接用，
        # 不能再退到更笼统的匹配把它和别的路由混在一起。
        full_candidates = [
            status for status in status_summary
            if _exact(status.get("name") or "", model_id)
        ]
        if full_candidates:
            return _best_by_group(full_candidates)

        # 第二层：全名没有专属监控，退一步找"去掉路由前缀后的裸模型名"精确匹配——
        # 用于监控只按底层模型整体挂、不区分路由的站（好几个不同定价路由背后复用
        # 同一个真实上游模型，站方也就只监控了那一个真实模型）。
        clean_candidates = [
            status for status in status_summary
            if clean_model_id and _exact(status.get("name") or "", clean_model_id)
        ]
        if clean_candidates:
            return _best_by_group(clean_candidates)

        # 两层精确匹配都没有，说明这个站的监控是按"分组/路由"整体挂的，不是按单个
        # 模型挂的（同一条监控背后可能是好几个不同版本、甚至不同厂商的模型）。这种
        # 情况下拿路由/分组这些词去做子串模糊匹配只会瞎绑——之前试过，会把 opus 绑到
        # sonnet、把 Claude 绑到 Gemini 的监控上。宁可这个模型不带 status 字段，也不要绑错。
        return None

    def _model_option(item):
        if isinstance(item, str):
            return {"id": item}
        if not isinstance(item, dict):
            return None
        model_id = item.get("id") or item.get("name") or ""
        if not model_id:
            return None
        name = item.get("name") or item.get("display_name") or item.get("label") or ""
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else None
        direct_price = _first_present(
            item,
            ("price", "prompt_price", "input_price", "price_prompt"),
        )
        prompt_price = _first_pricing_present(pricing, ("prompt", "input", "input_cost"))

        option = {"id": model_id}
        if name and name != model_id:
            option["name"] = name
        formatted_price = _string_model_price(direct_price) if direct_price is not None else _format_prompt_price(prompt_price)
        if formatted_price:
            option["price"] = formatted_price
        if pricing:
            option["pricing"] = pricing
        return option

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            pricing_rows, pricing_requires_auth, pricing_source = await _fetch_public_pricing(client)
            resp = await client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
            if resp.status_code != 200:
                return JSONResponse(
                    {"ok": False, "error": f"upstream {resp.status_code}: {resp.text[:200]}"},
                    status_code=502,
                )
            data = resp.json()
            if include_status:
                status_summary, status_source = await _fetch_status_summary(client)
            else:
                status_summary, status_source = [], None
            # 统一抽出 id 列表（兼容 {data:[{id,...}]} / [{id,...}] / {models:[...]}）
            raw = None
            if isinstance(data, dict):
                raw = data.get("data") or data.get("models") or data.get("results")
            if raw is None and isinstance(data, list):
                raw = data
            ids = []
            options = []
            if isinstance(raw, list):
                for it in raw:
                    option = _model_option(it)
                    if option:
                        pricing_row = _pick_pricing_for_option(option, pricing_rows)
                        if pricing_row:
                            if pricing_row.get("route"):
                                option["route"] = pricing_row["route"]
                            if pricing_row.get("groups"):
                                option["groups"] = pricing_row["groups"]
                            if pricing_row.get("group"):
                                option["group"] = pricing_row["group"]
                            if pricing_row.get("group_ratio") not in (None, ""):
                                option["group_ratio"] = pricing_row["group_ratio"]
                            if pricing_row.get("base_price"):
                                option["base_price"] = pricing_row["base_price"]
                            if pricing_row.get("price"):
                                option["price"] = pricing_row["price"]
                        status = _pick_status_for_option(option, pricing_row, status_summary)
                        if status:
                            option["status"] = status
                        ids.append(option["id"])
                        options.append(option)
            return JSONResponse({
                "ok": True,
                "models": ids,
                "model_options": options,
                "status_summary": status_summary,
                "pricing_requires_auth": pricing_requires_auth,
                "pricing_source": pricing_source,
                "status_source": status_source,
                "raw": data,
            })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8787")))
