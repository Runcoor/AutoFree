#!/bin/sh
# 起虚拟显示器(Cloudflare turnstile 抓 headless,Chromium 必须非 headless)
# 然后 exec uvicorn 让它成为 PID 1,stdio 直通 docker logs
set -e

Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >/dev/null 2>&1 &

export DISPLAY=:99

# 等 X 服务器就绪
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if [ -e /tmp/.X11-unix/X99 ]; then break; fi
  sleep 0.2
done

exec uv run uvicorn autofree.main:app --host 0.0.0.0 --port 8000
