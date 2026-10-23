"""Monitor this installation; never kill QQ processes by executable name."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import psutil
from ohwhale.config import get, path, project_path, log_path


def log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with open(log_path("watchdog.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def same_file(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def matching_processes(executable=None, script=None):
    result = []
    for p in psutil.process_iter(["exe", "cmdline"]):
        try:
            if executable and not same_file(p.info["exe"] or "", executable):
                continue
            if script and not any(same_file(arg, script) for arg in (p.info["cmdline"] or [])[1:]):
                continue
            result.append(p)
        except (psutil.Error, OSError):
            continue
    return result


def ws_connected():
    port = int(get("services.onebot_port"))
    return any(c.status == psutil.CONN_ESTABLISHED and
               ((c.laddr and c.laddr.port == port) or (c.raddr and c.raddr.port == port))
               for c in psutil.net_connections(kind="tcp"))


def start_loop():
    script = project_path("scripts/call_loop.py")
    if matching_processes(path("python"), script):
        return
    with open(log_path("loop.log"), "a", encoding="utf-8") as out:
        subprocess.Popen([path("python"), "-u", script], cwd=project_path("."),
                         stdout=out, stderr=subprocess.STDOUT,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    log("已启动本安装的 call_loop")


def restart_stack():
    if not get("watchdog.restart_qq", False):
        log("QQ 未连接；自动重启未启用，请检查客户端登录状态。")
        return
    executable = path("llbot_exe")
    if not Path(executable).is_file():
        log("请先安装客户端并填写 paths.llbot_exe")
        return
    if not Path(executable).resolve().is_relative_to(Path(project_path("runtime"))):
        log("拒绝自动重启安装目录之外的客户端")
        return
    for parent in matching_processes(executable):
        try:
            children = parent.children(recursive=True)
            parent.terminate()
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            alive = psutil.wait_procs([parent, *children], timeout=5)[1]
            if alive:
                log("客户端仍未退出；停止重启，防止重复启动")
                return
        except psutil.Error as e:
            log(f"客户端停止失败：{e}")
            return
    subprocess.Popen([executable], cwd=str(Path(executable).parent))
    log("已启动本安装的 LuckyLillia，等待登录")


def main():
    failures = 0
    while True:
        try:
            if get("features.phone", True):
                start_loop()
            failures = 0 if ws_connected() else failures + 1
            if failures >= 4:
                restart_stack()
                failures = 0
        except (OSError, psutil.Error, ValueError) as e:
            log(str(e))
        time.sleep(20)


if __name__ == "__main__":
    main()
