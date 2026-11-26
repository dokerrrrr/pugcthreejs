"""Render a scene in Chromium for automated previews and visualization."""
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import cli_protocol as cli
from .web_service import STATIC, SceneStore, background_server


@contextmanager
def browser_page(*, chromium=None, timeout=30_000):
    if not (STATIC / "render.html").is_file():
        raise cli.CliError(cli.RESOURCE, "web_assets_missing", "Compiled Three.js assets are missing.",
                           "Build the web bundle or install a built wheel.")
    try:
        from playwright.sync_api import Error, sync_playwright
    except ModuleNotFoundError as exc:
        if exc.name != "playwright":
            raise
        raise cli.CliError(cli.RESOURCE, "playwright_missing", "Playwright is not installed.",
                           "Install the toolkit with the [render] extra.") from exc
    executable = str(Path(chromium).expanduser().absolute()) if chromium else None
    with ExitStack() as stack:
        try:
            playwright = stack.enter_context(sync_playwright())
            browser = stack.enter_context(playwright.chromium.launch(
                executable_path=executable, timeout=timeout, args=["--enable-unsafe-swiftshader"]))
            page = browser.new_page()
        except Error as exc:
            raise cli.CliError(cli.RESOURCE, "chromium_unavailable", str(exc),
                               "Run python -m playwright install chromium or pass a working --chromium executable.",
                               path=executable, field="--chromium") from exc
        yield page


def check_chromium(chromium=None):
    with browser_page(chromium=chromium, timeout=15_000) as page:
        from playwright.sync_api import Error

        try:
            available = page.evaluate("Boolean(document.createElement('canvas').getContext('webgl2'))")
        except Error as exc:
            raise cli.CliError(cli.RESOURCE, "chromium_unavailable", str(exc),
                               "Check the Chromium installation and operating system dependencies.",
                               path=chromium, field="--chromium") from exc
        if not available:
            raise cli.CliError(cli.RESOURCE, "webgl_unavailable", "Chromium started, but WebGL 2 is unavailable.",
                               "Check browser dependencies or try a different --chromium executable.",
                               path=chromium, field="--chromium")
        return {"version": page.context.browser.version, "webgl2": True,
                "executable": str(Path(chromium).expanduser().absolute()) if chromium else None}


class Renderer:
    def __init__(self, page, endpoint):
        self.page = page
        self.page.goto(f"{endpoint}/render.html", wait_until="load")
        self.page.wait_for_function("typeof window.pugcRender === 'function'", timeout=15000)

    def frame(self, url, output, *, resolution=(1280, 720), camera=None, preset="iso", ids=None, island=False):
        from playwright.sync_api import Error

        width, height = resolution
        viewport = {"width": width, "height": height}
        if self.page.viewport_size != viewport:
            self.page.set_viewport_size(viewport)
        try:
            stats = self.page.evaluate("request => window.pugcRender(request)", {
                "url": url, "camera": camera, "preset": preset, "width": width, "height": height,
                "ids": ids, "island": island,
            })
            self.page.locator("canvas").screenshot(path=output, animations="disabled", timeout=15000)
        except Error as exc:
            raise cli.CliError(cli.RESOURCE, "render_failed", str(exc),
                               "Check Chromium WebGL and the scene resources.", path=output) from exc
        return stats


@contextmanager
def renderer(*scenes, chromium=None):
    store = SceneStore()
    urls = [store.publish(scene, str(i)) for i, scene in enumerate(scenes)]
    with background_server(store) as endpoint, browser_page(chromium=chromium) as page:
        yield Renderer(page, endpoint), urls
