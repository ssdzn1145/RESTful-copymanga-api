# 拷贝漫画 API - EdgeOne Pages

基于 Python (Flask) 的拷贝漫画 RESTful API，部署在腾讯云 EdgeOne Pages。

## 特性

- 中文关键词搜索 / 漫画详情 / 章节图片列表（多分组合并去重，按 1..N 编号）
- 图片处理：webp 原图缩放、JPEG / PNG 质量、LVGL 预解码
- `x-auth-signature` HMAC-SHA256 签名自动生成，设备标识逐请求轮换
- API 域名自动发现（`system/network2`），支持环境变量强制指定
- Cookie 透传 token，支持付费内容

## 项目结构

```
├── cloud-functions/
│   ├── index.py          # Flask 入口（路由 /）
│   └── [[default]].py    # Flask 入口（路由 /*），与 index.py 内容一致
├── requirements.txt
└── edgeone.json
```

> 注意：两个入口文件内容一致，修改代码时需保持同步。

## 本地开发

```bash
pip install -r requirements.txt
python cloud-functions/index.py        # http://localhost:8000
```

## EdgeOne Pages 部署

1. Fork 或克隆此仓库
2. 在 EdgeOne Makers 控制台导入 Git 仓库
3. 公网地址经 `Eo-Pages-Host` 请求头自动识别；若有异常可配置环境变量 `PUBLIC_URL=https://your-domain` 强制指定

可选环境变量：

| 变量 | 说明 |
| --- | --- |
| `PUBLIC_URL` | 强制指定对外基础地址 |
| `COPY_API_BASE` | 强制指定拷贝 API 域名（默认自动发现，失败回退 `api.copy2000.online`） |
| `COPY_IMAGE_QUALITY` | 上游拉图清晰度，默认 `800`（即 `c800x.webp`） |

## API

基础 URL：`https://your-project.edgeone.app`

| 端点 | 说明 |
| --- | --- |
| `GET /config` | 漫画源配置 |
| `GET /search/<keyword>/<page>` | 搜索漫画 |
| `GET /comic/<path_word>` | 漫画详情 |
| `GET /comic/<path_word>/cover` | 漫画封面 |
| `GET /photo/<path_word>/chapter/<chapter>` | 章节图片列表，`chapter` 从 1 开始 |
| `GET /photo/<path_word>/chapter/<chapter>/<page>.jpg` | 单页图片，`page` 从 1 开始 |

图片参数（封面 / 单页通用）：`width`（目标宽度，仅缩小）、`quality`（1-100，默认 50）、`ifPNG=1`（返回 PNG）、`ifLVGL=1`（返回 LVGL 预解码二进制，仅正文，优先级高于 ifPNG）。

错误返回：`{ "code": 404, "message": "Comic not found" }`

## Token（付费内容）

免费内容无需登录。付费 / 需登录章节：

1. 从拷贝漫画 App 抓包获取请求头 `authorization: Token xxxx` 中的 token 值
2. 请求方以 `Cookie: <token>` 头携带（支持裸 token 或 `token=xxxx` 形式），服务端自动以 `authorization: Token <值>` 转发上游

带 token 的请求不读写缓存。

## 风控说明

拷贝漫画对 API 有反滥用风控（返回"破解版本"提示的 210 响应），按时间窗累计触发，与瞬时频率关系不大。本服务的缓解措施：

- 设备标识（device / pseudoid / deviceinfo）逐请求随机轮换
- 缓存：详情 30 分钟、章节列表 60 分钟、章节图址 2 小时、封面 60 分钟
- 命中风控时返回 `429`，请几分钟后重试；携带个人 token 可降低限制

## 免责声明

本项目仅用于学习和研究目的，请勿用于商业用途。漫画内容版权归拷贝漫画及原作者所有，请支持正版。

## 许可证

MIT License
# 三平台部署更新

章节 UUID 图片、共享缓存、限流与三平台入口见 [部署说明](docs/DEPLOYMENT.md)。
