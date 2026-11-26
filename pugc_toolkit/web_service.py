"""Local transport for the headless renderer."""
from collections import OrderedDict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from threading import RLock, Thread
from urllib.parse import urlsplit

STATIC = Path(__file__).with_name("web")


class SceneStore:
    def __init__(self):
        self.lock = RLock()
        self.scenes = OrderedDict()

    def publish(self, scene, name):
        with self.lock:
            self.scenes[name] = scene
            self.scenes.move_to_end(name)
            while len(self.scenes) > 4:
                self.scenes.popitem(last=False)
        return f"/scenes/{name}.json"

    def asset(self, path):
        with self.lock:
            if path.startswith("/scenes/"):
                key = path.removeprefix("/scenes/").removesuffix(".json")
                scene = self.scenes.get(key)
                return json.dumps(scene.manifest, ensure_ascii=False, allow_nan=False).encode() if scene else None
            for scene in reversed(self.scenes.values()):
                if path in scene.blobs:
                    return scene.blobs[path]
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_content(self, body, mime, *, cached=False, status=200):
        size = body.stat().st_size if isinstance(body, Path) else len(body)
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable" if cached else "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if isinstance(body, Path):
            with body.open("rb") as stream:
                while chunk := stream.read(1024 * 256):
                    self.wfile.write(chunk)
        else:
            self.wfile.write(body)

    def valid_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.valid_host():
            self.send_error(403)
            return
        url = urlsplit(self.path)
        result = self.server.store.asset(url.path)
        if result is None and url.path in {"/render.html", "/render.js"}:
            path = STATIC / url.path[1:]
            if path.is_file():
                result = path
        if result is None:
            self.send_error(404)
            return
        mime = mimetypes.guess_type(url.path)[0] or "application/octet-stream"
        self.send_content(result, mime, cached=url.path.startswith("/blobs/"))


def server(store, port=0):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.store = store
    return httpd


@contextmanager
def background_server(store):
    with server(store) as httpd:
        thread = Thread(target=httpd.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_port}"
        finally:
            httpd.shutdown()
            thread.join()
