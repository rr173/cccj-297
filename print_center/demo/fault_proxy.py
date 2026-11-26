"""演示用故障注入代理（独立进程，不参与任何业务逻辑）。

它把请求转发给真实财务服务，并支持一次性故障注入：

* 演示脚本通过 ``POST /__control/arm?path=<request_id>`` 武装故障后，
  代理对下一个匹配该取件请求号的 **首次 hold**（财务返回 201）在收到
  应答后直接断开与印制服务的连接（应答丢弃），但财务侧处理已经生效
  ——用来精确复现"操作已生效但答复尚未收到"的断连场景；
* 重送产生的幂等 hold（财务返回 409）不会触发断连，故障只生效一次，
  随后自动恢复，重送请求将正常到达财务并得到幂等应答；
* 上游（财务）无响应时，代理对调用方表现为真正的连接中断，而不是
  带 JSON 的 502，以模拟"财务无响应"。

仅用于本地演示；生产部署没有这个组件，印制服务直连财务服务。
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

UPSTREAM = os.environ.get(
    "FINPROXY_UPSTREAM", "http://127.0.0.1:8078").rstrip("/")
PORT = int(os.environ.get("FINPROXY_PORT", "8079"))

_state_lock = threading.Lock()
# 待注入故障的目标路径（精确匹配 self.path），空串表示不注入
_fail_target = {"path": ""}
_stats = {"forwarded": 0, "injected": 0}


def arm_failure(path: str = "") -> None:
    with _state_lock:
        _fail_target["path"] = path


def stats() -> dict:
    with _state_lock:
        return dict(_stats, fail_target=_fail_target["path"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静默常规访问日志
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _proxy(self, method: str):
        body = self._read_body()
        url = UPSTREAM + self.path
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length",
                                        "connection")}
        # 故障目标：按请求体中的 request_id 精确匹配（用于 /holds），
        # 这样只断指定取件请求，重送与其他请求不受影响
        target_id = ""
        if body:
            try:
                target_id = json.loads(body.decode()).get("request_id", "")
            except (ValueError, UnicodeDecodeError):
                target_id = ""
        try:
            with httpx.Client(timeout=8) as client:
                resp = client.request(method, url, content=body,
                                      headers=headers)
        except httpx.HTTPError as exc:
            # 上游无响应时，对调用方表现为真正的连接中断（而不是一个
            # 带 JSON 的 502 业务响应），以精确模拟"财务无响应"
            print(f"[finproxy] 上游 {url} 无响应，断开客户端连接："
                  f"{type(exc).__name__}", flush=True)
            self.close_connection = True
            try:
                self.connection.shutdown(1)
            except OSError:
                pass
            self.connection.close()
            return
        with _state_lock:
            wanted = _fail_target["path"]
            # 只对武装目标的"首次 hold"（财务侧尚无该请求、返回 201）
            # 注入断连；重送产生的幂等 409 正常透传，不会消耗武装。
            is_first_hold = resp.status_code == 201
            should_fail = bool(wanted) and is_first_hold and (
                wanted == self.path or wanted == target_id)
            if should_fail:
                _fail_target["path"] = ""
            _stats["forwarded"] += 1
            if should_fail:
                _stats["injected"] += 1
        if should_fail:
            # 财务已处理并应答，但代理直接断开：印制服务只看到连接重置
            self.close_connection = True
            try:
                self.connection.shutdown(1)  # 半关闭写端，丢弃应答
            except OSError:
                pass
            self.connection.close()
            print(f"[finproxy] 注入断连：{method} {self.path} "
                  f"（财务已返回 {resp.status_code}，应答已丢弃）",
                  flush=True)
            return
        self.send_response(resp.status_code)
        ctype = resp.headers.get("content-type", "application/json")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(resp.content)))
        self.end_headers()
        self.wfile.write(resp.content)

    def do_GET(self):
        if self.path.startswith("/__control"):
            self._control()
            return
        self._proxy("GET")

    def do_POST(self):
        if self.path.startswith("/__control"):
            self._control()
            return
        self._proxy("POST")

    def _control(self):
        # 演示控制面：/arm?path=/holds/xxx 武装指定路径的下一次断连；
        # 不带 path 则对下一次任意请求注入。/stats 查看统计
        if self.path.startswith("/__control/arm"):
            target = ""
            if "path=" in self.path:
                from urllib.parse import parse_qs, urlparse
                target = parse_qs(urlparse(self.path).query).get(
                    "path", [""])[0]
            arm_failure(target)
            payload = json.dumps({"armed": True, "path": target}).encode()
        else:
            payload = json.dumps(stats()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[finproxy] 故障代理监听 127.0.0.1:{PORT} -> {UPSTREAM}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
