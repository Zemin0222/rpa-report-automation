from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import time
from typing import Iterator
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from core.live_observer import LiveRunTracker
from core.logger import get_logger

logger = get_logger("rpa.browser")


class BrowserStepError(RuntimeError):
    """Raised when a browser step fails, carrying the captured screenshot path."""

    def __init__(self, message: str, screenshot_path: Path | None = None):
        super().__init__(message)
        self.screenshot_path = screenshot_path


class BrowserManager:
    """Manages Playwright Chromium lifecycle, storage_state reuse, visual step tracking, and error screenshots."""

    def __init__(
        self,
        auth_dir: Path,
        screenshot_dir: Path,
        download_dir: Path,
        headless: bool = True,
        timeout_ms: int = 15000,
        slow_mo_ms: int = 0,
        observer: LiveRunTracker | None = None,
        session_id: str | None = None,
        visual_delay_ms: int = 0,
        fresh_login: bool = False,
    ) -> None:
        self.auth_dir = auth_dir
        self.screenshot_dir = screenshot_dir
        self.download_dir = download_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.slow_mo_ms = slow_mo_ms
        self.observer = observer
        self.session_id = session_id
        self.visual_delay_ms = visual_delay_ms
        self.fresh_login = fresh_login
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    def __enter__(self) -> "BrowserManager":
        self._pw = sync_playwright().start()
        launch_args = [
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
            args=launch_args,
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    def state_file(self, site_key: str) -> Path:
        return self.auth_dir / f"{site_key}.json"

    @contextmanager
    def open_context(self, site_key: str) -> Iterator[tuple[BrowserContext, Page]]:
        if self._browser is None:
            raise RuntimeError("BrowserManager must be used as a context manager.")

        state_path = self.state_file(site_key)
        ctx_kwargs: dict = {
            "accept_downloads": True,
            "viewport": {"width": 1280, "height": 760},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
        }
        if not self.fresh_login and state_path.exists():
            ctx_kwargs["storage_state"] = str(state_path)
            logger.info("Loaded auth state for %s from %s", site_key, state_path.name)

        context = self._browser.new_context(**ctx_kwargs)
        context.set_default_timeout(self.timeout_ms)
        page = context.new_page()
        try:
            yield context, page
        finally:
            context.close()

    def save_state(self, context: BrowserContext, site_key: str) -> Path:
        self.auth_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_file(site_key)
        context.storage_state(path=str(path))
        logger.info("Saved auth state for %s -> %s", site_key, path.name)
        return path

    @property
    def is_visual(self) -> bool:
        return self.observer is not None and self.session_id is not None

    def emit(
        self,
        phase: str,
        action: str,
        target: str,
        detail: str,
        page: Page | None = None,
    ) -> None:
        if not self.is_visual:
            return
        assert self.observer is not None and self.session_id is not None
        frame_bytes: bytes | None = None
        curr_url = ""
        if page is not None:
            try:
                curr_url = page.url
                frame_bytes = page.screenshot(type="jpeg", quality=78, full_page=False)
            except Exception:
                frame_bytes = None
        self.observer.record_event(
            session_id=self.session_id,
            phase=phase,
            action=action,
            target=target,
            detail=detail,
            current_url=curr_url,
            frame_bytes=frame_bytes,
        )

    def _with_highlight(
        self,
        page: Page,
        selector: str,
        badge_text: str,
        phase: str,
        action: str,
        detail: str,
    ) -> None:
        if not self.is_visual and self.visual_delay_ms <= 0:
            return
        try:
            page.evaluate(
                """([sel, label]) => {
                    const el = document.querySelector(sel);
                    if (!el) return;
                    el.scrollIntoView({block: "center", behavior: "instant"});
                    el.dataset._rpaOrigOutline = el.style.outline || "";
                    el.dataset._rpaOrigShadow = el.style.boxShadow || "";
                    el.style.outline = "2.5px solid #0F52BA";
                    el.style.boxShadow = "0 0 0 5px rgba(15, 82, 186, 0.22)";
                    const rect = el.getBoundingClientRect();
                    const tag = document.createElement("div");
                    tag.id = "_rpa_live_badge";
                    tag.textContent = label;
                    Object.assign(tag.style, {
                        position: "fixed",
                        left: Math.max(8, rect.left) + "px",
                        top: Math.max(6, rect.top - 28) + "px",
                        zIndex: "999999",
                        background: "#0F52BA",
                        color: "#FDFCFA",
                        padding: "3px 9px",
                        borderRadius: "4px",
                        fontSize: "12px",
                        fontWeight: "600",
                        fontFamily: "sans-serif",
                        boxShadow: "0 4px 12px rgba(15,82,186,0.3)",
                        pointerEvents: "none",
                    });
                    document.body.appendChild(tag);
                }""",
                [selector, badge_text],
            )
            self.emit(phase, action, selector, detail, page=page)
            if self.visual_delay_ms > 0:
                time.sleep(self.visual_delay_ms / 1000.0)
        except Exception:
            pass
        finally:
            try:
                page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (el) {
                            el.style.outline = el.dataset._rpaOrigOutline || "";
                            el.style.boxShadow = el.dataset._rpaOrigShadow || "";
                        }
                        const tag = document.getElementById("_rpa_live_badge");
                        if (tag) tag.remove();
                    }""",
                    selector,
                )
            except Exception:
                pass

    def step_goto(self, page: Page, url: str, phase: str, detail: str) -> None:
        page.goto(url, wait_until="domcontentloaded")
        self.emit(phase, "GOTO", url, detail, page=page)
        if self.visual_delay_ms > 0:
            time.sleep(self.visual_delay_ms / 1000.0)

    def step_fill(
        self,
        page: Page,
        selector: str,
        value: str,
        phase: str,
        detail: str,
        mask: bool = False,
    ) -> None:
        page.wait_for_selector(selector)
        page.fill(selector, value)
        shown = "••••••" if mask else value
        self._with_highlight(page, selector, f"RPA 填入: {shown[:22]}", phase, "FILL", detail)

    def step_select(self, page: Page, selector: str, value: str, phase: str, detail: str) -> None:
        page.wait_for_selector(selector)
        page.select_option(selector, value)
        self._with_highlight(page, selector, f"RPA 选择: {value}", phase, "SELECT", detail)

    def step_click(self, page: Page, selector: str, phase: str, detail: str) -> None:
        page.wait_for_selector(selector)
        self._with_highlight(page, selector, f"RPA 点击", phase, "CLICK", detail)
        page.click(selector)

    def step_upload(
        self,
        page: Page,
        selector: str,
        file_path: Path,
        phase: str,
        detail: str,
    ) -> None:
        page.wait_for_selector(selector)
        page.set_input_files(selector, str(file_path))
        self._with_highlight(page, selector, f"RPA 上传: {file_path.name}", phase, "UPLOAD", detail)

    def step_snap(
        self,
        page: Page,
        phase: str,
        action: str,
        target: str,
        detail: str,
    ) -> None:
        self.emit(phase, action, target, detail, page=page)
        if self.visual_delay_ms > 0:
            time.sleep((self.visual_delay_ms * 0.6) / 1000.0)

    def capture_screenshot(self, page: Page, prefix: str) -> Path | None:
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            shot_path = self.screenshot_dir / f"{prefix}_{ts}.png"
            page.screenshot(path=str(shot_path), full_page=True)
            logger.warning("Captured failure screenshot: %s", shot_path)
            return shot_path
        except Exception as exc:
            logger.error("Failed to capture screenshot: %s", exc)
            return None

    @contextmanager
    def capture_on_error(self, page: Page, step_prefix: str) -> Iterator[None]:
        try:
            yield
        except Exception as exc:
            shot = self.capture_screenshot(page, step_prefix)
            self.emit("异常处理", "ERROR", step_prefix, str(exc), page=page)
            raise BrowserStepError(f"{step_prefix} failed: {exc}", screenshot_path=shot) from exc

    def interactive_login(self, site_key: str, login_url: str) -> Path:
        """Open a visible browser so a human can complete QR/slider/MFA login, then save state."""
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = context.new_page()
            page.goto(login_url, wait_until="domcontentloaded")
            print(f"\n已在浏览器中打开登录页: {login_url}")
            print("请在弹出的浏览器窗口中完成登录（扫码/滑块/验证码），完成后回到终端按 Enter 保存登录态...")
            input()
            saved = self.save_state(context, site_key)
            context.close()
            browser.close()
            return saved
