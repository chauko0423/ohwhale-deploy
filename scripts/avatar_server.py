"""她形象的网页版：OBS 用「浏览器源」指过来，再由 OBS 输出到虚拟摄像头

为什么不直接推帧：
    pyvirtualcam 0.15 写的是老版共享内存协议，OBS 32 的虚拟摄像头滤镜读不到
    （推流不报错，但 QQ 收到的是黑屏）。改成 OBS 自己渲染浏览器源，就没有兼容问题。

口型：
    call_loop 通过 UDP 9999 发当前音量；本服务缓存最新的值，
    网页每 50ms 轮询 /level，用这个值缩放嘴的开口。

用法：
    python avatar_server.py           # 监听 http://127.0.0.1:8899
"""

from __future__ import annotations

from ohwhale.config import get as setting, path as config_path, project_path, state_path, log_path, secret, llm, require

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(setting("services.avatar_web_port"))
UDP_PORT = int(setting("services.avatar_udp_port"))
PORTRAIT = config_path("avatar_image")

level = 0.0
lock = threading.Lock()


def udp_listener():
    global level
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", UDP_PORT))
    s.settimeout(0.2)
    import time
    last = time.time()
    while True:
        try:
            data, _ = s.recvfrom(64)
            with lock:
                level = max(0.0, min(1.0, float(data.decode("ascii", "ignore").strip() or 0)))
            last = time.time()
        except socket.timeout:
            pass
        except Exception:  # noqa: BLE001
            continue
        if time.time() - last > 0.8:
            with lock:
                level *= 0.8


PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>鲸鲸</title>
<style>
  html,body{margin:0;padding:0;width:100%;height:100%;background:#0b1020;overflow:hidden}
  #wrap{position:relative;width:100vw;height:100vh;display:flex;align-items:center;justify-content:center}
  #bg{position:absolute;inset:0;background:radial-gradient(circle at 50% 35%,#1d2a4a 0%,#0b1020 70%)}
  #pic{position:relative;max-height:100%;max-width:100%;object-fit:contain}
  #ph{position:relative;color:#8fb6ff;font:600 64px/1.6 "Microsoft YaHei",sans-serif;text-align:center}
  #ph small{display:block;font-size:24px;color:#5a739f;margin-top:12px}
  #mouth{position:absolute;left:50%;top:62%;width:7%;height:0;transform:translate(-50%,-50%);
         background:#7a2536;border-radius:50%;box-shadow:0 0 8px rgba(0,0,0,.5);
         transition:height .04s linear;opacity:0}
  #dot{position:absolute;left:12px;bottom:10px;color:#4a5f8a;font:14px "Microsoft YaHei"}
</style></head>
<body>
<div id="wrap">
  <div id="bg"></div>
  __MEDIA__
  <div id="mouth"></div>
  <div id="dot">鲸鲸</div>
</div>
<script>
  const mouth = document.getElementById('mouth');
  async function tick(){
    try{
      const r = await fetch('/level', {cache:'no-store'});
      const lv = parseFloat(await r.text()) || 0;
      if (lv > 0.04){
        mouth.style.opacity = 1;
        mouth.style.height = (lv * 5.5) + '%';
      } else {
        mouth.style.opacity = 0;
        mouth.style.height = '0';
      }
    }catch(e){}
    setTimeout(tick, 50);
  }
  tick();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):     # 安静点，别每帧刷
        pass

    def do_GET(self):
        if self.path.startswith("/level"):
            with lock:
                v = level
            body = f"{v:.3f}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith("/portrait.png") and os.path.exists(PORTRAIT):
            with open(PORTRAIT, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        media = ('<img id="pic" src="/portrait.png">' if os.path.exists(PORTRAIT)
                 else '<div id="ph">鲸鲸<small>立绘占位（待替换）</small></div>')
        body = PAGE.replace("__MEDIA__", media).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    threading.Thread(target=udp_listener, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"她形象的网页: http://127.0.0.1:{PORT}  （有立绘={'是' if os.path.exists(PORTRAIT) else '否，用占位'}）")
    srv.serve_forever()


if __name__ == "__main__":
    main()
