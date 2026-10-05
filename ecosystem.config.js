module.exports = {
  apps: [{
    name: 'copymanga-api',
    script: 'gunicorn',
    args: '-c gunicorn.conf.py index:app',
    interpreter: 'none',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '512M',
    env: {
      PORT: 3005,
      CACHE_DB: './data/cache.sqlite3',
      CACHE_NAMESPACE: 'copymanga',
      IMAGE_CONCURRENCY: '1',
      IMAGE_GLOBAL_CONCURRENCY: '2'
    },
    log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
    merge_logs: true
  }]
};
