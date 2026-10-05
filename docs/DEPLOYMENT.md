# 三平台部署

业务核心 cloud-functions/backend.py；EdgeOne根/catch-all薄入口和Vercel/Gunicorn
根index.py共用同一核心。edgeone.json已修正为python.maxDuration。
图片列表改为章节UUID路径，保留旧序号路径；多分组去重、words图片排序及token隔离保留。
限流不长时间睡眠，返回Retry-After；签名/图床地址失效在同键任务中刷新一次。

VPS：Python3.14 venv安装requirements，加载 `.env.example` 对应 `.env`，
`gunicorn -c gunicorn.conf.py index:app`。Vercel使用index.py和Python3.12；EdgeOne
使用Python云函数，核心辅助文件都位于cloud-functions中。PUBLIC_URL支持路径前缀。

CACHE_DB在VPS持久目录内且各worker共用，Serverless留空。SQLite原子缓存、TTL、
容量淘汰、90秒所有者租约；可选REDIS_URL跨实例共享，协调故障503，Redis用noeviction。
默认2worker×2thread，冷图片单worker1/全局2，内存64MiB/worker，磁盘内容512MiB。
实际磁盘另含WAL/空闲页；同机八源磁盘初始总预算约8.5GiB（MangaDex5GiB）。

width/w、quality/q、LVGL>PNG>JPEG统一，透明白底，LVGL宽高≤2047；完整二进制长度。
下载20MiB、解码3200万像素、响应4MiB。公共图24h，鉴权private,no-store，错误no-store。
Nginx转发127.0.0.1:3005，传Host和代理Host/Proto，读超时25秒；CDN保留全部参数，
鉴权请求跳过缓存读写。诊断 `/health/runtime`、X-Cache、尺寸头。真实平台需另行验收。
