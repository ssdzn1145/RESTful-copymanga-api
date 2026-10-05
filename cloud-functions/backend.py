# 拷贝漫画 RESTful API - EdgeOne Pages Cloud Functions (Python/Flask)
#
# 路由：/config、/comic/<id>、/comic/<id>/cover、/photo/<id>/chapter/<n>[/<page>.jpg]、/search/<text>/<page>
# 注意：cloud-functions/index.py 与 [[default]].py 内容需保持一致（EdgeOne 双入口）
import base64
import hashlib
import hmac
import logging
import os
import random
import re
import string
import struct
import time
from datetime import datetime
from io import BytesIO
from threading import Lock
from urllib.parse import urlparse

from runtime import http as requests
from runtime import create_app, public_url, memoize, serve, cache, scope, ApiError as ServiceError
from flask import Flask, Response, jsonify, request
from PIL import Image
from werkzeug.exceptions import HTTPException

app = create_app(__name__)
app.debug = False
app.json.ensure_ascii = False

Image.MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS","32000000"))


class WSGIPathFixMiddleware:
    """修复部分 Serverless 运行时（如 EdgeOne）将 URL 解码为 Unicode 后
    直接放入 PATH_INFO/QUERY_STRING 的问题。

    PEP 3333 要求 PATH_INFO 为 latin-1 范围内的 str（表示原始字节），
    Werkzeug 会 encode('latin1') 还原字节。若运行时放入的是真正的
    Unicode 字符串（如 /search/航海王/1），需要重新按 UTF-8 编码
    再按 latin-1 解码，恢复 WSGI 标准格式。
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        for key in ("PATH_INFO", "QUERY_STRING", "SCRIPT_NAME"):
            value = environ.get(key)
            if not value:
                continue
            try:
                value.encode("latin1")
            except UnicodeEncodeError:
                environ[key] = value.encode("utf-8").decode("latin1")
        return self.wsgi_app(environ, start_response)


app.wsgi_app = WSGIPathFixMiddleware(app.wsgi_app)


class ApiError(Exception):
    """业务错误，status 为返回给客户端的 HTTP 状态码"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@app.errorhandler(ApiError)
def handle_api_error(e):
    if e.status >= 500:
        logging.warning(f"上游错误 {e.status}: {e.message}")
    return jsonify({"code": e.status, "message": e.message}), e.status


@app.errorhandler(HTTPException)
def handle_http_error(e):
    """Flask/Werkzeug 自身的 HTTP 错误（如路由未命中 404、方法不允许 405），
    统一按约定 JSON 格式返回"""
    return jsonify({"code": e.code, "message": e.name}), e.code


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    logging.exception("未处理异常")
    return jsonify({"code": 500, "message": "服务器内部错误"}), 500


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def get_api_url():
    """获取对外可访问的 API 基础地址。

    EdgeOne Pages 会将 Host 改写为内部域名，原始访问域名通过 Eo-Pages-Host
    请求头透传（平台强制 HTTPS）。优先级：PUBLIC_URL 环境变量 > Eo-Pages-Host
    > X-Forwarded-* > Host。
    """
    public = os.environ.get("PUBLIC_URL")
    if public:
        return public.rstrip("/")

    eo_host = request.headers.get("Eo-Pages-Host", "").strip()
    if eo_host:
        return f"https://{eo_host}"

    host = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    if host and proto:
        return f"{proto}://{host}"
    if not host:
        host = request.host
    if not proto:
        proto = request.scheme
    return f"{proto}://{host}"


def clamp_int(raw, fallback, min_v, max_v, name):
    if raw is None or raw == "":
        return fallback
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ApiError(400, f"参数 {name} 非法")
    return max(min_v, min(max_v, n))


def is_truthy_arg(name):
    return request.args.get(name, "0").lower() in ("1", "true", "yes", "on")


class TTLCache:
    """TTL 内存缓存：serverless 暖实例间复用，冷启动后自动重建。
    注意：带用户 Cookie 的请求一律不读不写，防止付费内容泄露。"""

    def __init__(self, ttl_seconds, max_size=500):
        self.ttl = ttl_seconds
        self.max_size = max_size
        self._data = {}
        self._lock = Lock()

    def get(self, key):
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            if entry[0] <= now:
                del self._data[key]
                return None
            return entry[1]

    def set(self, key, value):
        now = time.time()
        with self._lock:
            if len(self._data) >= self.max_size:
                for k in [k for k, v in self._data.items() if v[0] <= now]:
                    del self._data[k]
                if len(self._data) >= self.max_size:
                    self._data.clear()
            self._data[key] = (now + self.ttl, value)

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)


# ---------------------------------------------------------------------------
# 拷贝漫画上游客户端
# ---------------------------------------------------------------------------

SECRET_B64 = "M2FmMDg1OTAzMTEwMzJlZmUwNjYwNTUwYTA1NjNhNTM="
DEFAULT_API_BASE = "api.copy2000.online"
DISCOVERY_URL = "https://api.copy-manga.com/api/v3/system/network2?platform=3"
# 上游图片清晰度：地址后缀替换为 c<值>x.webp，手表端 800 已足够且省带宽
UPSTREAM_IMAGE_QUALITY = os.environ.get("COPY_IMAGE_QUALITY", "800")


def _generate_device_info():
    return f"{random.randint(1000000, 9999999)}V-{random.randint(1000, 9999)}"


def _generate_device():
    def rand_char():
        return chr(65 + random.randint(0, 25))

    def rand_digit():
        return chr(48 + random.randint(0, 9))

    return (
        rand_char() + rand_char() + rand_digit() + rand_char() + "."
        + "".join(rand_digit() for _ in range(6)) + "."
        + "".join(rand_digit() for _ in range(3))
    )


def _generate_pseudoid():
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))


def extract_token(cookie):
    """支持粘贴裸 token 或包含 token=xxx 的 Cookie 字符串"""
    cookie = (cookie or "").strip()
    if not cookie:
        return None
    m = re.search(r"(?:^|;\s*)token=([^;]+)", cookie)
    return m.group(1) if m else cookie


def make_headers(cookie=None):
    """生成拷贝漫画 API 签名请求头。region 固定海外线路（0），无需 request_id。
    若请求方携带 Cookie，其值作为用户 token 放入 authorization 转发上游。

    设备标识每次请求随机生成：拷贝的风控按 device/pseudoid 计数封禁
    （"破解版本"提示，封 1 小时），服务端共享固定身份会快速触顶，
    每次轮换可规避；实测同 IP 下新身份立即可用。
    """
    ts = str(int(time.time()))
    sig = hmac.new(base64.b64decode(SECRET_B64), ts.encode(), hashlib.sha256).hexdigest()
    token = extract_token(cookie)
    return {
        "User-Agent": "COPY/3.0.6",
        "source": "copyApp",
        "deviceinfo": _generate_device_info(),
        "dt": datetime.now().strftime("%Y.%m.%d"),
        "platform": "3",
        "referer": "com.copymanga.app-3.0.6",
        "version": "3.0.6",
        "device": _generate_device(),
        "pseudoid": _generate_pseudoid(),
        "Accept": "application/json",
        "region": "0",
        "authorization": f"Token {token}" if token else "Token",
        "umstring": "b4c89ca4104ea9a97750314d791520ac",
        "x-auth-timestamp": ts,
        "x-auth-signature": sig,
    }


_api_base = {"value": None}
_api_base_lock = Lock()


def get_api_base():
    """API 域名：COPY_API_BASE 环境变量 > 动态发现（内存缓存）> 默认域名"""
    env = os.environ.get("COPY_API_BASE", "").strip()
    if env:
        return re.sub(r"^https?://", "", env).strip("/")
    with _api_base_lock:
        cached = _api_base["value"]
    if cached:
        return cached
    try:
        resp = requests.get(DISCOVERY_URL, headers=make_headers(), timeout=10)
        if resp.status_code == 200:
            api_list = (resp.json().get("results") or {}).get("api") or []
            if api_list and api_list[0] and api_list[0][0]:
                with _api_base_lock:
                    _api_base["value"] = api_list[0][0]
                return api_list[0][0]
    except Exception:
        pass
    return DEFAULT_API_BASE


def invalidate_api_base():
    with _api_base_lock:
        _api_base["value"] = None


class RateLimited(Exception):
    """上游 210 限流（Expected available in X seconds）"""

    def __init__(self, wait_seconds, message=""):
        super().__init__(message or f"rate limited, wait {wait_seconds}s")
        self.wait_seconds = wait_seconds


@app.errorhandler(RateLimited)
def handle_rate_limited(e):
    return jsonify({"code": 429, "message": f"上游限流，请 {e.wait_seconds} 秒后重试"}), 429


class RiskControlled(Exception):
    """上游"破解版本"风控封禁。封禁按时间窗生效（实测延迟约 1 分钟触发、
    持续数分钟），立即重试无意义，返回 429 让客户端稍后重试。"""


@app.errorhandler(RiskControlled)
def handle_risk_controlled(e):
    return jsonify({"code": 429, "message": "上游风控限制，请几分钟后重试（同步器上传 token 可降低限制）"}), 429


def api_get(path, params=None, cookie=None, timeout=15):
    """请求上游 JSON API。

    重试策略（最多 2 次）：
    - 网络级失败：刷新域名后重试；
    - 210 且为"破解版本"风控：设备身份已每次轮换，直接重试一次；
    - 210 且为 "Expected available in X seconds"：抛 RateLimited；
    - 其余 210（下架/合并/隐藏等）：按 404 透传上游原因。
    """
    url = f"https://{get_api_base()}{path}"
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=make_headers(cookie), params=params, timeout=timeout)
        except requests.RequestException as e:
            if attempt == 0:
                invalidate_api_base()
                url = f"https://{get_api_base()}{path}"
                continue
            raise ApiError(502, f"上游请求失败: {e}")

        if resp.status_code == 210:
            message = ""
            try:
                message = resp.json().get("message") or ""
            except ValueError:
                pass
            if "Expected available in" in message:
                wait = 40
                m = re.search(r"(\d+)\s*seconds", message)
                if m:
                    wait = int(m.group(1))
                raise RateLimited(wait, message)
            if "破解" in message or "正版" in message:
                if attempt == 0:
                    continue  # 换一次设备身份重试
                raise RiskControlled()
            raise ApiError(404, message or "上游业务错误(210)")

        if resp.status_code != 200:
            raise ApiError(502, f"上游请求失败，状态码: {resp.status_code}")

        try:
            data = resp.json()
        except ValueError:
            raise ApiError(502, "上游返回非 JSON 数据")

        code = data.get("code")
        if code is not None and code != 200:
            if code == 404:
                raise ApiError(404, data.get("message") or "Not found")
            raise ApiError(502, f"上游业务错误: {data.get('message') or code}")
        return data


info_cache = TTLCache(30 * 60)  # 漫画基本信息（含分组）30 分钟
chapters_cache = TTLCache(60 * 60)  # 合并后的章节列表 60 分钟
chapter_data_cache = TTLCache(2 * 60 * 60)  # 章节图片数据 2 小时（图床地址无签名过期）
cover_cache = TTLCache(60 * 60)  # 搜索结果回填的封面地址 60 分钟


def get_comic_info(path_word, cookie=None):
    """漫画基本信息（含章节分组）。带 Cookie 的请求不读不写缓存。"""
    if not cookie:
        cached = info_cache.get(path_word)
        if cached:
            return cached
    data = api_get(
        f"/api/v3/comic2/{path_word}",
        params={"in_mainland": "true", "request_id": "", "platform": "3"},
        cookie=cookie,
    )
    results = data.get("results") or {}
    comic = results.get("comic") or {}
    if not comic.get("name"):
        raise ApiError(404, "Comic not found")

    groups = []
    for g in (results.get("groups") or {}).values():
        if isinstance(g, dict) and g.get("path_word"):
            groups.append({"name": g.get("name") or "", "path_word": g["path_word"]})

    status = comic.get("status")
    info = {
        "name": comic.get("name"),
        "cover": comic.get("cover"),
        "authors": [
            a.get("name") for a in comic.get("author") or []
            if isinstance(a, dict) and a.get("name")
        ],
        "themes": [
            t.get("name") for t in comic.get("theme") or []
            if isinstance(t, dict) and t.get("name")
        ],
        "status": status.get("display") if isinstance(status, dict) else None,
        "popular": comic.get("popular"),
        "groups": groups,
    }
    if not cookie:
        info_cache.set(path_word, info)
        if info["cover"]:
            cover_cache.set(path_word, info["cover"])
    return info


def get_chapter_list(path_word, cookie=None):
    """合并所有分组的章节列表（保持官方顺序，按 uuid 去重）"""
    if not cookie:
        cached = chapters_cache.get(path_word)
        if cached is not None:
            return cached
    info = get_comic_info(path_word, cookie)
    chapters = []
    seen = set()
    for group in info["groups"]:
        offset = 0
        while True:
            data = api_get(
                f"/api/v3/comic/{path_word}/group/{group['path_word']}/chapters",
                params={"limit": 100, "offset": offset, "in_mainland": "true", "request_id": ""},
                cookie=cookie,
            )
            results = data.get("results") or {}
            items = results.get("list") or []
            for e in items:
                if not isinstance(e, dict):
                    continue
                uuid, name = e.get("uuid"), e.get("name")
                if uuid and uuid not in seen:
                    seen.add(uuid)
                    chapters.append({"uuid": uuid, "title": name or f"第{len(chapters) + 1}话"})
            total = results.get("total") or 0
            offset += 100
            if not items or offset >= total:
                break
    if not cookie:
        chapters_cache.set(path_word, chapters)
    return chapters


def get_chapter_data(path_word, uuid, cookie=None):
    """章节图片地址列表（按 words 重排）。210 限流时短等待后重试一次。"""
    if not cookie:
        cached = chapter_data_cache.get(uuid)
        if cached:
            return cached
    data = None
    for attempt in range(2):
        try:
            data = api_get(
                f"/api/v3/comic/{path_word}/chapter2/{uuid}",
                params={"in_mainland": "true", "request_id": ""},
                cookie=cookie,
            )
            break
        except RateLimited as e:
            if attempt == 0 and e.wait_seconds <= 1:
                time.sleep(e.wait_seconds)
                continue
            raise

    chapter = (data.get("results") or {}).get("chapter") or {}
    contents = chapter.get("contents") or []
    words = chapter.get("words") or []
    if not contents:
        raise ApiError(403, "章节不可用（付费内容请通过同步器上传 token）")

    urls = []
    for c in contents:
        if isinstance(c, dict) and c.get("url"):
            # 替换为指定清晰度，减小拉图体积
            urls.append(re.sub(
                r"([./])c\d+x\.[a-zA-Z]+$",
                rf"\g<1>c{UPSTREAM_IMAGE_QUALITY}x.webp",
                c["url"],
            ))
    if not urls:
        raise ApiError(502, "未获取到章节图片")

    # contents 为乱序，words 给出每个地址的正确页码位置
    if len(words) == len(urls):
        ordered = [None] * len(urls)
        ok = True
        for i, u in enumerate(urls):
            pos = words[i]
            if isinstance(pos, int) and 0 <= pos < len(urls):
                ordered[pos] = u
            else:
                ok = False
                break
        if ok and all(ordered):
            urls = ordered

    data_out = {"title": chapter.get("name") or "", "images": urls}
    if not cookie:
        chapter_data_cache.set(uuid, data_out)
    return data_out


# ---------------------------------------------------------------------------
# 图片处理管线
# ---------------------------------------------------------------------------

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20MB 下载上限
LVGL_MAX_DIM = 2047  # LVGL 头部宽高各占 11 bit

IMAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}


def fetch_image(image_url, retries=1):
    """下载上游图片。地址均来自上游接口数据（非用户输入），仅做 scheme 校验。"""
    try:
        if urlparse(image_url).scheme not in ("http", "https"):
            raise ValueError
    except Exception:
        raise ApiError(502, "上游图片地址非法")
    last_exc = None
    for _ in range(retries + 1):
        try:
            resp = requests.get(image_url, headers=IMAGE_HEADERS, timeout=20)
        except requests.RequestException as e:
            last_exc = e
            continue
        if resp.status_code != 200:
            raise ServiceError(resp.status_code if resp.status_code in (403,404,410,429) else 502,"图片下载失败")
        declared = int(resp.headers.get("content-length") or 0)
        if declared > MAX_DOWNLOAD_BYTES:
            raise ApiError(413, "图片体积过大")
        data = resp.content
        if len(data) > MAX_DOWNLOAD_BYTES:
            raise ApiError(413, "图片体积过大")
        try:
            image = Image.open(BytesIO(data))
            if image.width*image.height>int(os.environ.get("MAX_IMAGE_PIXELS","32000000")):
                image.close()
                raise ServiceError(413,"原图超过像素预算")
            image.load()
        except ServiceError:
            raise
        except Exception:
            raise ApiError(502, "图片解码失败")
        return image
    raise ApiError(502, f"图片下载失败: {last_exc}")


def normalize_rgb(image):
    """透明通道铺白底转 RGB"""
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        if image.mode in ("RGBA", "LA"):
            background.paste(image, mask=image.split()[-1])
        else:
            background.paste(image)
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def convert_to_lvgl8(image):
    """转换为 LVGL indexed-8 二进制：4 字节头 + 256 项 BGRA 调色板 + 每像素 1 字节索引"""
    image = normalize_rgb(image).quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    w, h = image.size
    if w > LVGL_MAX_DIM or h > LVGL_MAX_DIM:
        raise ApiError(413, "图片尺寸超出 LVGL 编码上限")

    raw_palette = image.getpalette()
    out = BytesIO()
    out.write(struct.pack("<I", 10 | (w << 10) | (h << 21)))
    for i in range(256):
        idx = i * 3
        if raw_palette and idx + 2 < len(raw_palette):
            r, g, b = raw_palette[idx], raw_palette[idx + 1], raw_palette[idx + 2]
        else:
            r = g = b = 0
        out.write(bytes([b, g, r, 0xFF]))
    out.write(image.tobytes())
    return out.getvalue()


def process_image(image, width, quality, if_png, if_lvgl):
    """等比缩放（仅缩小）后输出 LVGL / PNG / JPEG，返回 (bytes, content_type)"""
    if if_lvgl:
        w = min(width, image.width)
        h = round(image.height * w / image.width)
        if h > LVGL_MAX_DIM:  # LVGL 头部宽高仅 11 bit，保证缩放后高度也不超限
            h = LVGL_MAX_DIM
            w = max(1, int(image.width * h / image.height))
        image = normalize_rgb(image).resize((w, h), Image.Resampling.LANCZOS)
        return convert_to_lvgl8(image), "application/octet-stream"

    if image.width > width:
        image = image.resize(
            (width, round(image.height * width / image.width)), Image.Resampling.LANCZOS
        )

    out = BytesIO()
    if if_png:
        colors = max(16, min(256, int(16 + quality * 2.4)))
        image = normalize_rgb(image).quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
        image.save(out, "PNG", optimize=True, compress_level=9)
        return out.getvalue(), "image/png"

    normalize_rgb(image).save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue(), "image/jpeg"


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def image_response(data, content_type):
    return Response(
        data,
        mimetype=content_type,
        headers={"Cache-Control": "public, max-age=86400", "Content-Type": content_type},
    )


def page_image_params(default_width):
    """解析图片参数：width/quality/ifPNG/ifLVGL"""
    width = clamp_int(request.args.get("width") or request.args.get("w"), default_width, 1, 2047, "width")
    quality = clamp_int(request.args.get("quality") or request.args.get("q"), 50, 1, 100, "quality")
    return width, quality, is_truthy_arg("ifPNG"), is_truthy_arg("ifLVGL")


def resolve_chapter(path_word, chapter_number, cookie):
    """解析第 chapter_number 章（1 起）的图片数据，返回 (章节信息, 章节数据)"""
    chapters = get_chapter_list(path_word, cookie)
    if chapter_number < 1 or chapter_number > len(chapters):
        raise ApiError(404, f"未找到第 {chapter_number} 章")
    target = chapters[chapter_number - 1]
    data = get_chapter_data(path_word, target["uuid"], cookie)
    return target, data


@app.get("/")
def read_root():
    return "it works!"


@app.get("/config")
@app.get("/config/")
def config():
    api_url = get_api_url()
    return jsonify(
        {
            "CopyManga": {
                "name": "拷贝漫画",
                "apiUrl": api_url,
                "detailPath": "/comic/<id>",
                "photoPath": "/photo/<id>/chapter/<chapter>",
                "searchPath": "/search/<text>/<page>",
                "type": "copymanga",
            }
        }
    )


@app.get("/comic/<path_word>")
@app.get("/comic/<path_word>/")
def comic_info(path_word):
    cookie = request.headers.get("Cookie") or None
    info = get_comic_info(path_word, cookie)
    chapters = get_chapter_list(path_word, cookie)

    tags = info["authors"] + info["themes"]
    if info["status"]:
        tags.append(info["status"])
    return jsonify(
        {
            "item_id": path_word,
            "name": info["name"] or "未知标题",
            "page_count": 0,
            "views": info["popular"] if info["popular"] is not None else "0",
            "cover": f"{get_api_url()}/comic/{path_word}/cover",
            "tags": tags,
            "total_chapters": len(chapters),
        }
    )


@app.get("/comic/<path_word>/cover")
def comic_cover(path_word):
    """漫画封面图片，支持 width/quality/ifPNG 参数"""
    cookie = request.headers.get("Cookie") or None
    cover_url = None if cookie else cover_cache.get(path_word)
    if not cover_url:
        cover_url = get_comic_info(path_word, cookie)["cover"]
    if not cover_url:
        raise ApiError(404, "No cover found")

    width, quality, if_png, _ = page_image_params(200)
    image = fetch_image(cover_url)
    data, content_type = process_image(image, width, quality, if_png=if_png, if_lvgl=False)
    return image_response(data, content_type)


@app.get("/photo/<path_word>/chapter/<int:chapter_number>")
def chapter_images(path_word, chapter_number):
    if chapter_number < 1:
        raise ApiError(400, "章节号必须从 1 开始")

    cookie = request.headers.get("Cookie") or None
    target, data = resolve_chapter(path_word, chapter_number, cookie)

    api_url = get_api_url()
    images = [
        {"url": f"{api_url}/photo/{path_word}/chapter/{chapter_number}/{page}.jpg"}
        for page in range(1, len(data["images"]) + 1)
    ]
    return jsonify({"title": data["title"] or target["title"], "images": images})


@app.get("/photo/<path_word>/chapter/<int:chapter_number>/<path:page>")
def chapter_page_image(path_word, chapter_number, page):
    """章节单页图片，格式 /photo/<id>/chapter/<chapter>/<page>.jpg"""
    if chapter_number < 1:
        raise ApiError(400, "章节号必须从 1 开始")
    m = re.match(r"^(\d+)", page or "")
    if not m:
        raise ApiError(400, "无效的页码")
    page_num = int(m.group(1))

    cookie = request.headers.get("Cookie") or None
    target, data = resolve_chapter(path_word, chapter_number, cookie)

    def pick_image_url(chapter_data):
        if page_num < 1 or page_num > len(chapter_data["images"]):
            raise ApiError(404, "Page not found")
        return chapter_data["images"][page_num - 1]

    width, quality, if_png, if_lvgl = page_image_params(600)
    try:
        image = fetch_image(pick_image_url(data))
    except ApiError as e:
        # 图床地址失效时刷新章节数据重试一次
        if e.status != 502 or cookie:
            raise
        chapter_data_cache.delete(target["uuid"])
        _, data = resolve_chapter(path_word, chapter_number, cookie)
        image = fetch_image(pick_image_url(data))

    img_data, content_type = process_image(image, width, quality, if_png=if_png, if_lvgl=if_lvgl)
    return image_response(img_data, content_type)


@app.get("/search/<value>")
@app.get("/search/<value>/")
@app.get("/search/<value>/<page>")
def search(value, page="1"):
    # Flask 已对路径参数做过一次 URL 解码，直接使用
    if not page.isdigit() or int(page) < 1:
        raise ApiError(400, "页码必须大于0")
    page = int(page)
    if not value:
        raise ApiError(400, "缺少搜索关键词")

    cookie = request.headers.get("Cookie") or None
    data = api_get(
        "/api/v3/search/comic",
        params={"limit": 30, "offset": (page - 1) * 30, "q": value, "q_type": ""},
        cookie=cookie,
    )
    results = data.get("results") or {}
    total = results.get("total") or 0

    api_url = get_api_url()
    out = []
    for item in results.get("list") or []:
        if not isinstance(item, dict):
            continue
        comic = item.get("comic") if isinstance(item.get("comic"), dict) else item
        pw, name = comic.get("path_word"), comic.get("name")
        if not pw or not name:
            continue
        cover = comic.get("cover")
        if cover and not cookie:
            cover_cache.set(pw, cover)
        out.append(
            {
                "comic_id": pw,
                "title": name,
                "cover_url": f"{api_url}/comic/{pw}/cover",
                "pages": 0,
            }
        )

    return jsonify({"page": page, "has_more": page * 30 < total, "results": out})


get_api_url = public_url
get_comic_info = memoize(600)(get_comic_info)
get_chapter_list = memoize(600)(get_chapter_list)
get_chapter_data = memoize(1800)(get_chapter_data)
api_get = memoize(60)(api_get)
get_api_base = memoize(300)(get_api_base)


def cached_cover(path_word):
    def load():
        url=get_comic_info(path_word,request.headers.get("Cookie"))["cover"]
        if not url: raise ServiceError(404,"No cover found")
        return fetch_image(url,retries=0)
    return serve(["cover",path_word],load,200,ttl=3600)


@app.get("/image/chapter/<path_word>/<chapter_uuid>/<int:page>")
def stable_page(path_word,chapter_uuid,page):
    def load():
        data=get_chapter_data(path_word,chapter_uuid,request.headers.get("Cookie"))
        if not 1<=page<=len(data["images"]): raise ServiceError(404,"Page not found")
        url=data["images"][page-1]
        try:
            return fetch_image(url,retries=0)
        except ServiceError as e:
            if e.status not in (403,404,410): raise
            cookie=request.headers.get("Cookie")
            def refresh():
                chapter_data_cache.delete(chapter_uuid)
                cache.delete(cache.key(["get_chapter_data",[(path_word,chapter_uuid,cookie),{}],scope()]))
                return get_chapter_data(path_word,chapter_uuid,cookie)
            packed,_=cache.load(cache.key(["refresh-chapter",chapter_uuid,url,scope()]),lambda:json.dumps(refresh()).encode(),5)
            fresh=json.loads(packed)
            if page>len(fresh["images"]): raise ServiceError(404,"Page not found")
            return fetch_image(fresh["images"][page-1],retries=0)
    return serve(["page",path_word,chapter_uuid,page],load)


def stable_list(path_word,chapter_number):
    target,data=resolve_chapter(path_word,chapter_number,request.headers.get("Cookie"))
    return jsonify(title=data["title"] or target["title"],images=[{"url":public_url()+f"/image/chapter/{path_word}/{target['uuid']}/{page}"} for page in range(1,len(data["images"])+1)])


def cached_legacy(path_word,chapter_number,page):
    match=re.fullmatch(r"([1-9]\d*)(?:\.jpg)?",page)
    if not match: raise ServiceError(400,"无效页码")
    def load():
        _,data=resolve_chapter(path_word,chapter_number,request.headers.get("Cookie"))
        number=int(match[1])
        if number>len(data["images"]): raise ServiceError(404,"Page not found")
        return fetch_image(data["images"][number-1],retries=0)
    return serve(["legacy",path_word,chapter_number,page],load,ttl=600)


app.view_functions["comic_cover"]=cached_cover
app.view_functions["chapter_images"]=stable_list
app.view_functions["chapter_page_image"]=cached_legacy


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
