"""
AI动态天气查询 Flask 项目
入口: python test.py  →  http://localhost:5000

技术栈:
- Flask (Web框架)
- 通义千问 (DashScope) Function Calling → 解析自然语言天气查询
- 和风天气 API → 获取实时天气数据
- asyncio + aiohttp → 异步并发调用，消除串行等待
- 内存缓存 → 相同城市10分钟内重复查询直接返回缓存
"""

import os
import sys
import time
import asyncio
import logging
import threading
from datetime import datetime
from pathlib import Path

import aiohttp
from flask import Flask, request, jsonify, render_template

# ---------------------------------------------------------------------------
# 自动加载 .env 文件（项目根目录）
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    """简易 .env 解析器，无需额外依赖。"""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

_load_dotenv()

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weather-ai")

# ---------------------------------------------------------------------------
# Flask 初始化
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# 配置 – 优先环境变量，.env 文件已自动加载
# ---------------------------------------------------------------------------
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
HEFENG_API_KEY = os.getenv("HEFENG_API_KEY", "")

# 启动校验
if not DASHSCOPE_API_KEY:
    log.warning("DASHSCOPE_API_KEY 未配置，Qwen AI 将不可用")
if not HEFENG_API_KEY:
    log.warning("HEFENG_API_KEY 未配置，和风天气 API 将不可用")

# ---------------------------------------------------------------------------
# 内存缓存  (city_name → {ts: float, data: dict})  TTL = 10 分钟
# ---------------------------------------------------------------------------
_cache: dict[str, dict] = {}
CACHE_TTL_SECONDS = 600  # 10 分钟

# ---------------------------------------------------------------------------
# 超时与重试
# ---------------------------------------------------------------------------
API_TIMEOUT = aiohttp.ClientTimeout(total=15)  # 单个 API 15 秒超时

# ---------------------------------------------------------------------------
# HTTP Session – 线程本地存储（Flask threaded=True 下每个线程独立 session）
# ---------------------------------------------------------------------------
_thread_local = threading.local()


def _get_session() -> aiohttp.ClientSession:
    """获取当前线程的 aiohttp session，线程安全，自动复用系统代理。"""
    if not hasattr(_thread_local, "session") or _thread_local.session is None or _thread_local.session.closed:
        # 自动检测系统代理（兼容国内网络环境访问外部 API）
        trust_env = True  # aiohttp 自动读取 HTTP_PROXY/HTTPS_PROXY 环境变量
        _thread_local.session = aiohttp.ClientSession(
            timeout=API_TIMEOUT,
            trust_env=trust_env,
        )
    return _thread_local.session


# ========================= 和风天气 API =====================================


async def _hefeng_city_lookup(city: str) -> dict | None:
    """
    和风天气城市查询 → 获取 LocationID。
    GET https://geoapi.qweather.com/v2/city/lookup
    """
    url = "https://geoapi.qweather.com/v2/city/lookup"
    params = {"location": city, "key": HEFENG_API_KEY}
    session = _get_session()
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                log.warning("和风城市查询 HTTP %s: %s", resp.status, city)
                return None
            data = await resp.json()
    except Exception as exc:
        log.error("和风城市查询异常: %s", exc)
        return None

    if data.get("code") != "200" or not data.get("location"):
        log.warning("和风城市查询失败: %s → %s", city, data)
        return None
    return data["location"][0]  # 取第一个匹配结果


async def _hefeng_weather_now(location_id: str) -> dict | None:
    """
    和风天气实时天气。
    GET https://devapi.qweather.com/v7/weather/now
    """
    url = "https://devapi.qweather.com/v7/weather/now"
    params = {"location": location_id, "key": HEFENG_API_KEY}
    session = _get_session()
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                log.warning("和风天气 HTTP %s: %s", resp.status, location_id)
                return None
            data = await resp.json()
    except Exception as exc:
        log.error("和风天气查询异常: %s", exc)
        return None

    if data.get("code") != "200" or not data.get("now"):
        log.warning("和风天气查询失败: %s → %s", location_id, data)
        return None
    return data["now"]


async def _hefeng_weather_7d(location_id: str) -> list[dict] | None:
    """
    和风天气 7 天预报。
    GET https://devapi.qweather.com/v7/weather/7d
    """
    url = "https://devapi.qweather.com/v7/weather/7d"
    params = {"location": location_id, "key": HEFENG_API_KEY}
    session = _get_session()
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                log.warning("和风7天预报 HTTP %s: %s", resp.status, location_id)
                return None
            data = await resp.json()
    except Exception as exc:
        log.error("和风7天预报异常: %s", exc)
        return None

    if data.get("code") != "200" or not data.get("daily"):
        log.warning("和风7天预报失败: %s → %s", location_id, data)
        return None
    return data["daily"]


async def fetch_weather_from_hefeng(city: str) -> dict:
    """
    并发查询和风天气：城市查 LocationID → 实时天气 + 7 天预报（并行）。
    返回结构化天气数据，失败时返回错误描述。
    """
    # 1. 城市 → LocationID
    loc = await _hefeng_city_lookup(city)
    if not loc:
        return {"error": f"未找到城市「{city}」，请检查城市名称是否正确"}

    location_id = loc["id"]
    city_display = f"{loc.get('country', '')} {loc.get('adm1', '')} {loc.get('name', city)}".strip()

    # 2. 实时天气 + 7 天预报 并发获取
    results = await asyncio.gather(
        _hefeng_weather_now(location_id),
        _hefeng_weather_7d(location_id),
        return_exceptions=True,
    )

    now_data, daily_data = results[0], results[1]

    if isinstance(now_data, Exception):
        log.error("实时天气异常: %s", now_data)
        now_data = None
    if isinstance(daily_data, Exception):
        log.error("7天预报异常: %s", daily_data)
        daily_data = None

    if now_data is None:
        return {"error": f"获取「{city_display}」实时天气失败，请稍后重试"}

    return {
        "city": city_display,
        "location_id": location_id,
        "now": {
            "temp": now_data.get("temp"),
            "feelsLike": now_data.get("feelsLike"),
            "text": now_data.get("text"),
            "windDir": now_data.get("windDir"),
            "windScale": now_data.get("windScale"),
            "humidity": now_data.get("humidity"),
            "precip": now_data.get("precip"),
            "vis": now_data.get("vis"),
            "cloud": now_data.get("cloud"),
        },
        "daily": [
            {
                "fxDate": d.get("fxDate"),
                "tempMax": d.get("tempMax"),
                "tempMin": d.get("tempMin"),
                "textDay": d.get("textDay"),
                "textNight": d.get("textNight"),
                "windDirDay": d.get("windDirDay"),
                "windScaleDay": d.get("windScaleDay"),
                "humidity": d.get("humidity"),
                "precip": d.get("precip"),
            }
            for d in (daily_data or [])
        ],
        "updateTime": datetime.now().isoformat(),
    }


# ========================= 通义千问 Function Calling =========================


QWEN_FC_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气，支持查询今天、明天、后天及未来几天的天气状况",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，例如：北京、上海、深圳、杭州、成都",
                    },
                },
                "required": ["city"],
            },
        },
    }
]

QWEN_SYSTEM_PROMPT = (
    "你是一个专业的天气查询助手。用户会用自然语言询问天气，"
    "你需要调用 get_weather 函数获取数据，然后用友好的语气回复用户。"
    "回复要简洁、准确，包含温度、天气状况、风力、湿度等关键信息。"
    "如果用户没有指明具体城市，默认查询北京。"
)


async def _call_qwen_extract_city(user_query: str) -> str | None:
    """
    调用通义千问 Function Calling，从自然语言中提取城市名。
    返回城市名称，失败返回 None。
    """
    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "qwen-plus",
        "messages": [
            {"role": "system", "content": QWEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_query},
        ],
        "tools": QWEN_FC_TOOLS,
        "tool_choice": "auto",
        "temperature": 0.1,
    }
    session = _get_session()
    try:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("DashScope HTTP %s: %s", resp.status, body[:300])
                return None
            data = await resp.json()
    except Exception as exc:
        log.error("DashScope 请求异常: %s", exc)
        return None

    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        log.warning("DashScope 响应格式异常: %s", str(data)[:300])
        return None

    # 如果有 tool_calls，提取 city 参数
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            if fn.get("name") == "get_weather":
                try:
                    import json
                    args = json.loads(fn.get("arguments", "{}"))
                    city = args.get("city", "").strip()
                    if city:
                        return city
                except (json.JSONDecodeError, TypeError):
                    pass

    # 没有 tool_calls 时尝试从 content 中提取
    content = msg.get("content", "")
    if content:
        # 简单兜底：直接返回用户输入中可能包含的城市名
        return None

    return None


async def _call_qwen_format_response(user_query: str, weather_data: dict) -> str:
    """
    让通义千问根据天气数据生成友好的自然语言回复。
    """
    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }

    city = weather_data.get("city", "未知城市")
    now = weather_data.get("now", {})
    daily = weather_data.get("daily", [])

    weather_summary = f"""
城市: {city}
当前天气: {now.get('text', '未知')}
当前温度: {now.get('temp', 'N/A')}°C
体感温度: {now.get('feelsLike', 'N/A')}°C
风向: {now.get('windDir', 'N/A')}
风力等级: {now.get('windScale', 'N/A')}
湿度: {now.get('humidity', 'N/A')}%
降水量: {now.get('precip', 'N/A')} mm
能见度: {now.get('vis', 'N/A')} km
"""
    if daily:
        weather_summary += "\n未来天气预报:\n"
        for d in daily[:7]:
            weather_summary += (
                f"  {d.get('fxDate', 'N/A')}: {d.get('textDay', 'N/A')}, "
                f"{d.get('tempMin', 'N/A')}~{d.get('tempMax', 'N/A')}°C\n"
            )

    payload = {
        "model": "qwen-plus",
        "messages": [
            {"role": "system", "content": QWEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_query},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_weather_1",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "' + city + '"}',
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_weather_1", "content": weather_summary},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }
    try:
        session = _get_session()
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                return _format_weather_text(weather_data)
            data = await resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as exc:
        log.error("DashScope 格式化回复异常: %s", exc)
        return _format_weather_text(weather_data)


def _format_weather_text(data: dict) -> str:
    """纯本地格式化天气数据（Qwen 不可用时的兜底）。"""
    city = data.get("city", "未知")
    now = data.get("now", {})
    lines = [
        f"📍 {city} 天气",
        f"🌤 天气: {now.get('text', 'N/A')}",
        f"🌡 温度: {now.get('temp', 'N/A')}°C  (体感 {now.get('feelsLike', 'N/A')}°C)",
        f"💨 风向: {now.get('windDir', 'N/A')}  风力: {now.get('windScale', 'N/A')} 级",
        f"💧 湿度: {now.get('humidity', 'N/A')}%",
        f"🌧 降水量: {now.get('precip', 'N/A')} mm",
        f"👁 能见度: {now.get('vis', 'N/A')} km",
    ]
    daily = data.get("daily", [])
    if daily:
        lines.append("\n📅 未来预报:")
        for d in daily[:7]:
            lines.append(
                f"  {d.get('fxDate', 'N/A')}: {d.get('textDay', 'N/A')} "
                f"{d.get('tempMin', 'N/A')}~{d.get('tempMax', 'N/A')}°C"
            )
    return "\n".join(lines)


# ========================= 缓存工具 =========================================


def _cache_key(city: str) -> str:
    return city.strip().lower()


def _cache_get(city: str) -> dict | None:
    key = _cache_key(city)
    entry = _cache.get(key)
    if entry is None:
        return None
    if time.time() - entry["ts"] > CACHE_TTL_SECONDS:
        del _cache[key]
        return None
    log.info("缓存命中: %s", key)
    return entry["data"]


def _cache_set(city: str, data: dict) -> None:
    key = _cache_key(city)
    _cache[key] = {"ts": time.time(), "data": data}
    log.info("缓存写入: %s  (当前缓存城市数: %d)", key, len(_cache))


# ========================= 核心业务 =========================================


def _close_thread_session() -> None:
    """同步关闭当前线程的 aiohttp session。"""
    if not hasattr(_thread_local, "session") or _thread_local.session is None:
        return
    sess = _thread_local.session
    _thread_local.session = None
    if not sess.closed:
        try:
            # aiohttp session.close() 是协程，但 close() 操作本身很快，
            # 在 loop 还活着时直接安排关闭
            loop = asyncio.get_running_loop()
            loop.create_task(sess.close())
        except RuntimeError:
            # 没有运行中的 event loop（不应发生，但安全兜底）
            pass


async def _query_weather_async(user_query: str) -> dict:
    """
    异步天气查询主流程:
    1. Qwen Function Calling 提取城市（异步）
    2. 和风天气 API 查询（异步）
    3. Qwen 格式化自然语言回复（异步）

    两个 API 调用是串行依赖的（需要先知道城市），但各自内部的
    HTTP 请求使用 aiohttp 异步 I/O，不阻塞 Flask worker。

    增加缓存：同城市 10 分钟内直接返回。
    """
    start = time.time()
    result: dict

    try:
        # 1. 先用 Qwen 提取城市名称
        city = await _call_qwen_extract_city(user_query)
        if not city:
            # 兜底：从用户输入中简单提取常见城市名
            city = _fallback_extract_city(user_query)
        if not city:
            result = {"error": "未能识别您要查询的城市，请明确输入城市名称（如「北京天气」）"}
            return result

        log.info("识别城市: %s  (耗时 %.2fs)", city, time.time() - start)

        # 2. 检查缓存
        cached = _cache_get(city)
        if cached is not None:
            cached["fromCache"] = True
            cached["cacheCity"] = city
            log.info("查询完成(缓存): %s  (总耗时 %.2fs)", city, time.time() - start)
            return cached

        # 3. 调用和风天气 API
        weather_data = await fetch_weather_from_hefeng(city)

        if "error" in weather_data:
            return weather_data

        weather_data["fromCache"] = False
        weather_data["cacheCity"] = city

        # 4. 写入缓存
        _cache_set(city, weather_data)

        # 5. 可选：让 Qwen 生成自然语言回复
        try:
            ai_reply = await _call_qwen_format_response(user_query, weather_data)
            weather_data["aiReply"] = ai_reply
        except Exception as exc:
            log.warning("Qwen 格式化回复失败，使用本地格式化: %s", exc)
            weather_data["aiReply"] = _format_weather_text(weather_data)

        log.info("查询完成: %s  (总耗时 %.2fs)", city, time.time() - start)
        return weather_data

    finally:
        # 请求结束后清理线程级 session，避免 asyncio 报 Unclosed 警告
        _close_thread_session()


def _fallback_extract_city(user_query: str) -> str | None:
    """本地兜底提取城市名（Qwen 不可用时）。"""
    import re

    common_cities = [
        "北京", "上海", "广州", "深圳", "杭州", "成都", "重庆",
        "武汉", "西安", "南京", "天津", "苏州", "长沙", "郑州",
        "东莞", "青岛", "沈阳", "宁波", "昆明", "大连", "厦门",
        "合肥", "佛山", "福州", "哈尔滨", "济南", "温州", "长春",
        "石家庄", "常州", "泉州", "南宁", "贵阳", "南昌", "太原",
        "烟台", "嘉兴", "南通", "金华", "珠海", "惠州", "徐州",
        "海口", "乌鲁木齐", "兰州", "呼和浩特", "银川", "西宁",
        "拉萨", "香港", "澳门", "台北",
    ]
    for c in common_cities:
        if c in user_query:
            return c

    # 尝试匹配"XX天气" / "XX的天气"
    m = re.search(r"([一-龥]{2,4})(?:的?天气|今天|明天|后天|气温|温度)", user_query)
    if m:
        return m.group(1)

    return None


# ========================= Flask Routes =====================================


@app.route("/")
def index():
    """天气查询首页。"""
    return render_template("index.html")


@app.route("/api/weather", methods=["GET"])
def weather_api():
    """
    天气查询 API。
    GET /api/weather?q=北京今天天气怎么样
    返回 JSON:
      - city, now, daily, aiReply, fromCache, updateTime
    """
    user_query = (request.args.get("q") or "").strip()
    if not user_query:
        return jsonify({"error": "请输入查询内容，例如：北京今天天气怎么样"}), 400

    log.info("收到查询: %s", user_query[:80])

    try:
        # Flask 同步 route 中运行异步业务逻辑
        result = asyncio.run(_query_weather_async(user_query))
    except Exception as exc:
        log.exception("查询异常: %s", exc)
        return jsonify({"error": f"服务内部异常，请稍后重试: {str(exc)}"}), 500

    if "error" in result:
        return jsonify(result), 404

    return jsonify(result)


@app.route("/api/health", methods=["GET"])
def health():
    """健康检查接口。"""
    return jsonify({
        "status": "ok",
        "cacheSize": len(_cache),
        "timestamp": datetime.now().isoformat(),
    })


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    """清除缓存（调试用）。"""
    count = len(_cache)
    _cache.clear()
    log.info("缓存已清除，清理了 %d 条记录", count)
    return jsonify({"cleared": count})


# ========================= 启动入口 =========================================

def main():
    # Windows 控制台 UTF-8 支持，避免 emoji 打印崩溃
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 56)
    print("  [W] AI Dynamic Weather Query Service")
    print("  Local: http://localhost:5000")
    print("  Tunnel: use cpolar to map localhost:5000")
    print()
    print("  Start cpolar: cpolar http 5000")
    print()
    print("  Env from .env:")
    print(f"    DASHSCOPE_API_KEY = {'[OK] sk-...' + DASHSCOPE_API_KEY[-8:] if DASHSCOPE_API_KEY else '[MISSING]'}")
    print(f"    HEFENG_API_KEY    = {'[OK] ' + HEFENG_API_KEY[:4] + '...' + HEFENG_API_KEY[-4:] if HEFENG_API_KEY else '[MISSING]'}")
    print("=" * 56)

    # Flask 开发服务器
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,       # 生产/调试均关闭 debug 模式，避免 reloader 导致崩溃
        use_reloader=False,
        threaded=True,     # 多线程处理并发请求
    )


if __name__ == "__main__":
    main()
