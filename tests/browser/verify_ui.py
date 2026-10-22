"""Standalone synthetic browser acceptance: python tests/browser/verify_ui.py.

No models, user database, network services or real LLM requests are used.
Actual static files are served through Playwright routing; XHR progress/cancel
events are controlled for deterministic race coverage. HTTP admission itself is
covered by test_upload_sessions.py and test_workflow_e2e.py.
"""
import argparse
import base64
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "asr/web/static"
SEGMENTS = [{"start": i * 2, "end": i * 2 + 1, "text": f"第{i + 1:03d}段：课堂教学内容。"} for i in range(200)]
TASK = {"id": "browser01", "name": "课堂记录：探索与讨论" * 3, "filename": "lesson.1.wav",
        "status": "done", "stage": "done", "progress": 1, "message": "完成", "error": "",
        "created_at": "2026-10-22T10:00:00", "started_at": "2026-10-22T10:00:00",
        "ended_at": "2026-10-22T10:01:00", "transcript": json.dumps(SEGMENTS, ensure_ascii=False),
        "execution_active": False, "can_delete": True, "transcription_status": "done", "ai_status": "failed",
        "available_formats": ["srt", "txt", "json", "feedback"],
        "meta": {"duration_s": 450, "segments_count": 200, "quality": "balanced", "formats": ["srt", "txt", "json"],
                 "ai_files": {"feedback": "last.feedback.md"}, "ai_errors": {"feedback": "本次生成失败"},
                 "ai_attempts": {"feedback": {"status": "failed"}}}}


class Fixture:
    def __init__(self):
        self.task = json.loads(json.dumps(TASK))
        self.projects = [self.task, {**self.task, "id": "browser02", "name": "正在处理的课堂",
                                    "status": "running", "stage": "asr", "transcription_status": "running",
                                    "execution_active": True, "can_delete": False, "ai_status": "idle"}]
        self.cancel_mode = "cancelled"
        self.save_payloads = []
        self.ai_calls = 0

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        path = url.path
        def reply(value, status=200):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(value, ensure_ascii=False))
        if path.startswith("/static/"):
            file = STATIC / path.removeprefix("/static/")
            assert file.resolve().is_relative_to(STATIC.resolve())
            route.fulfill(path=str(file))
        elif path.startswith("/status/"):
            reply(self.task)
        elif path == "/api/llm/status":
            reply({"enabled": True, "model": "fake-model"})
        elif path == "/api/llm/settings":
            if request.method == "POST":
                self.save_payloads.append(request.post_data_json)
                reply({"status": "saved", "enabled": True})
            else:
                reply({"base_url": "http://fake.invalid/v1", "api_key": None, "api_key_set": True,
                       "api_key_masked": "********", "model": "fake-model", "timeout": 30, "max_tokens": 100})
        elif path == "/api/llm/test":
            reply({"ok": True, "reply": "ok"})
        elif path == "/api/uploads":
            reply({"upload_id": "ticket01", "status": "open", "task_id": None}, 201)
        elif path.endswith("/cancel"):
            if self.cancel_mode == "unknown":
                route.abort()
            elif self.cancel_mode == "accepted":
                reply({"detail": {"code": "upload_already_accepted", "task_id": "browser01"}}, 409)
            else:
                reply({"upload_id": "ticket01", "status": "cancelled", "task_id": None})
        elif path == "/api/projects/batch-delete":
            reply({"deleted": [], "failed": ["browser01"], "failure_details": [{"id": "browser01", "code": "task_busy"}]})
        elif path == "/api/projects":
            query = parse_qs(url.query)
            items = [] if query.get("q") else self.projects
            reply({"items": items, "total": len(items)})
        elif path.startswith("/api/projects/"):
            reply(self.task)
        elif path.startswith("/api/tasks/"):
            if request.method == "POST":
                self.ai_calls += 1
                reply({"status": "started"}, 202)
            else:
                body = "<h1>" + "长标题课堂点评" * 8 + "</h1><p>" + "可阅读的完整内容。" * 100 + "</p>"
                body += "<table><tr><th>项目</th><th>说明</th></tr><tr><td>" + "长表格内容" * 40 + "</td><td>检查换行与滚动</td></tr></table>"
                reply({"html": body, "title": "教学点评", "format": "feedback"})
        elif path.startswith("/download/"):
            route.fulfill(body="synthetic output", content_type="text/plain")
        else:
            file = "task.html" if path.startswith("/task/") else "settings.html" if path == "/settings" else "index.html"
            route.fulfill(path=str(STATIC / file))


def ready(page):
    page.wait_for_load_state("networkidle")


def no_overflow(page, label):
    dimensions = page.evaluate("({width:innerWidth, scroll:document.documentElement.scrollWidth})")
    assert dimensions["scroll"] <= dimensions["width"] + 1, (label, dimensions)


def task_flow(page, fixture, output):
    page.goto("http://classroom.test/task/browser01")
    ready(page)
    expect(page.locator("#transcript-page")).to_contain_text("1 / 3")
    expect(page.locator("#transcript")).to_contain_text("第001段")
    expect(page.locator("#transcript .line")).to_have_count(80)
    page.locator("#transcript-next").click()
    expect(page.locator("#transcript")).to_contain_text("第081段")
    page.locator("#transcript-next").click()
    expect(page.locator("#transcript")).to_contain_text("第161段")
    page.locator("#transcript-search").fill("第081段")
    expect(page.locator("#transcript")).to_contain_text("第081段")
    expect(page.locator("#transcript")).not_to_contain_text("第001段")
    page.evaluate("navigator.clipboard.writeText = async text => { window.copiedText = text; }")
    page.locator("#copy-transcript").click()
    assert "第001段" in page.evaluate("window.copiedText") and "第200段" in page.evaluate("window.copiedText")
    expect(page.locator('#downloads [data-fmt="srt"]')).to_be_visible()
    expect(page.locator('#downloads [data-fmt="txt"]')).to_be_visible()
    expect(page.locator('#downloads [data-fmt="vtt"]')).to_be_hidden()
    expect(page.locator("#sum-duration")).to_contain_text("7m30s")
    expect(page.locator("#ai-error")).to_contain_text("上次版本")
    button = page.locator('[data-view="feedback"]')
    button.click()
    expect(page.locator("#ai-modal")).to_be_visible()
    assert page.locator("main").evaluate("el => el.inert")
    assert page.evaluate("document.querySelector('#ai-modal').contains(document.activeElement)")
    for _ in range(12):
        page.keyboard.press("Tab")
        assert page.evaluate("document.querySelector('#ai-modal').contains(document.activeElement)")
    page.locator("#ai-modal-expand").click()
    expect(page.locator("#ai-modal-expand")).to_have_attribute("aria-pressed", "true")
    no_overflow(page, "expanded reader")
    page.screenshot(path=str(output / "reader-desktop.png"), full_page=True)
    page.keyboard.press("Escape")
    expect(page.locator("#ai-modal")).to_be_hidden()
    expect(button).to_be_focused()
    assert not page.locator("main").evaluate("el => el.inert")
    # A running task retains only the latest 80 lines, a completed one keeps all data for paging.
    fixture.task.update(status="running", stage="asr", transcription_status="running", ai_status="idle", execution_active=True)
    page.reload()
    ready(page)
    expect(page.locator("#transcript")).to_contain_text("第200段")
    expect(page.locator("#transcript")).not_to_contain_text("第001段")
    fixture.task = json.loads(json.dumps(TASK))


def settings_flow(page, fixture):
    page.goto("http://classroom.test/settings?from_task=browser01")
    ready(page)
    expect(page.locator("#api-key")).to_have_value("")
    page.locator("#save-btn").click()
    expect(page.locator("#return-task")).to_be_visible()
    assert fixture.save_payloads[-1]["api_key"] is None
    page.locator("#api-key").fill("replacement")
    page.locator("#save-btn").click()
    expect(page.locator("#api-key")).to_have_value("")
    assert fixture.save_payloads[-1]["api_key"] == "replacement"
    page.locator("#clear-api-key").check()
    page.locator("#save-btn").click()
    expect(page.locator("#clear-api-key")).not_to_be_checked()
    assert fixture.save_payloads[-1]["api_key"] == ""
    assert fixture.ai_calls == 0


def empty_final_flow(page, fixture):
    for final_value in ("[]", ""):
        fixture.task = {**TASK, "status": "running", "stage": "asr", "transcription_status": "running",
                        "execution_active": True, "ai_status": "idle"}
        page.goto("http://classroom.test/task/browser01")
        ready(page)
        expect(page.locator("#transcript .line")).to_have_count(80)
        fixture.task = {**TASK, "transcript": final_value, "meta": {"duration_s": 450, "segments_count": 0}}
        page.evaluate("poll()")
        expect(page.locator("#transcript .line")).to_have_count(0)
        expect(page.locator("#transcript-count")).to_contain_text("0 段")
    fixture.task = json.loads(json.dumps(TASK))


def list_flow(page):
    page.goto("http://classroom.test/")
    ready(page)
    page.locator('#projects-body input[data-id="browser01"]').check()
    expect(page.locator("#selected-count")).to_have_text("1")
    page.locator("#search-input").fill("不存在")
    expect(page.locator("#selected-count")).to_have_text("0")
    expect(page.locator("#empty-message")).to_have_text("没有符合条件的项目")
    page.locator("#empty-clear-btn").click()
    expect(page.locator("#projects-body tr")).to_have_count(2)
    page.locator("#select-all").check()
    expect(page.locator("#batch-busy-note")).to_contain_text("跳过")
    dialogs = []
    def dialog_handler(dialog):
        dialogs.append(dialog.message)
        dialog.accept()
    page.on("dialog", dialog_handler)
    page.locator("#batch-delete-btn").click()
    expect(page.locator("#selected-count")).to_have_text("0")
    assert any(TASK["name"] in message and "跳过" in message for message in dialogs)
    page.remove_listener("dialog", dialog_handler)


XHR_STUB = """class ControlledXHR {
 constructor(){this.upload={};window.uploadXHR=this;}
 open(){} setRequestHeader(name,value){this.ticket=value;}
 send(){window.uploadSent=true;}
 abort(){if(this.onabort)this.onabort();}
} window.XMLHttpRequest=ControlledXHR;"""


def upload_flow(context, fixture):
    page = context.new_page()
    page.add_init_script(XHR_STUB)
    for mode in ("cancelled", "accepted", "unknown"):
        fixture.cancel_mode = mode
        page.goto("http://classroom.test/")
        ready(page)
        page.locator("#new-project-btn").click()
        page.locator("#file-input").set_input_files({"name": "lesson.wav", "mimeType": "audio/wav", "buffer": b"synthetic"})
        page.locator("#submit-btn").click()
        page.wait_for_function("window.uploadSent === true")
        assert page.evaluate("uploadXHR.ticket") == "ticket01"
        page.evaluate("uploadXHR.upload.onprogress({lengthComputable:true,loaded:50,total:100})")
        expect(page.locator("#upload-progress")).to_have_attribute("value", "50")
        page.evaluate("uploadXHR.upload.onload()")
        expect(page.locator("#upload-message")).to_have_text("正在确认接收…")
        page.locator("#cancel-upload-btn").click()
        if mode == "cancelled":
            expect(page.locator("#upload-message")).to_contain_text("已取消上传")
        elif mode == "accepted":
            expect(page.locator("#upload-message")).to_contain_text("任务已接收")
            expect(page.locator("#upload-task-link")).to_be_visible()
            assert urlsplit(page.url).path == "/"
        else:
            expect(page.locator("#upload-message")).to_contain_text("取消结果暂未确认")
            fixture.cancel_mode = "accepted"
            page.locator("#upload-check-btn").click()
            expect(page.locator("#upload-task-link")).to_be_visible()
    page.close()


def layout_matrix(page, output):
    measured = []
    for width, height in ((320, 844), (390, 844), (700, 900), (701, 900), (768, 1024), (900, 900), (901, 900), (1440, 900)):
        page.set_viewport_size({"width": width, "height": height})
        for theme in ("light", "dark"):
            for motion in ("no-preference", "reduce"):
                page.emulate_media(color_scheme=theme, reduced_motion=motion)
                for path in ("/", "/task/browser01", "/settings"):
                    page.goto("http://classroom.test" + path)
                    ready(page)
                    no_overflow(page, (width, theme, motion, path))
                    if path == "/":
                        expect(page.locator(".project-link").first).to_be_visible()
                    measured.append([width, theme, motion, path])
                if width in (390, 1440) and motion == "no-preference":
                    page.goto("http://classroom.test/task/browser01")
                    ready(page)
                    page.screenshot(path=str(output / f"task-{width}-{theme}.png"), full_page=True)
    # Chromium page-scale is not browser zoom. 200% CSS zoom with half-width
    # viewport supplies a deterministic reflow check, recorded separately.
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("http://classroom.test/task/browser01")
    ready(page)
    page.evaluate("document.body.style.zoom='200%'")
    no_overflow(page, "CSS zoom 200%")
    page.screenshot(path=str(output / "task-zoom200.png"), full_page=True)
    return measured


def native_zoom_check(playwright, fixture, output):
    """Use Chromium's tabs.setZoom, then verify the actual zoom and CSS viewport.

    The extension and fresh browser profile exist only in a temporary directory.
    No user browser profile or existing tabs are accessed.
    """
    with tempfile.TemporaryDirectory(prefix="asr-browser-zoom-") as directory:
        root = Path(directory)
        extension = root / "extension"
        extension.mkdir()
        (extension / "manifest.json").write_text(json.dumps({
            "manifest_version": 3, "name": "Local zoom acceptance", "version": "1.0",
            "permissions": ["tabs"], "background": {"service_worker": "background.js"}}), encoding="utf-8")
        (extension / "background.js").write_text("console.log('zoom acceptance ready');", encoding="utf-8")
        context = playwright.chromium.launch_persistent_context(
            str(root / "profile"), channel="chromium", headless=True, no_viewport=True,
            ignore_default_args=["--disable-extensions"],
            args=["--window-size=1440,900", f"--disable-extensions-except={extension}", f"--load-extension={extension}"])
        try:
            worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
            context.route("http://classroom.test/**", fixture.route)
            page = context.pages[0]
            page.goto("http://classroom.test/task/browser01")
            ready(page)
            zoom = worker.evaluate("""async () => {
              const tabs = await chrome.tabs.query({});
              const tab = tabs.find(tab => tab.url.startsWith('http://classroom.test'));
              await chrome.tabs.setZoom(tab.id, 2);
              return await chrome.tabs.getZoom(tab.id);
            }""")
            assert zoom == 2
            page.wait_for_function("devicePixelRatio === 2 && innerWidth < 750")
            for path in ("/task/browser01", "/", "/settings"):
                page.goto("http://classroom.test" + path)
                ready(page)
                no_overflow(page, ("native zoom 200%", path))
                if path == "/task/browser01":
                    expect(page.locator("#transcript-next")).to_be_visible()
                    page.locator("#transcript-next").click()
                    expect(page.locator("#transcript")).to_contain_text("第081段")
                    # At browser zoom, Playwright's full-page clip mixes CSS
                    # and physical coordinates. CDP contentSize captures the
                    # complete physical canvas rather than a cropped image.
                    session = context.new_cdp_session(page)
                    size = session.send("Page.getLayoutMetrics")["contentSize"]
                    capture = session.send("Page.captureScreenshot", {"captureBeyondViewport": True,
                        "clip": {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1}})
                    (output / "task-native-zoom200.png").write_bytes(base64.b64decode(capture["data"]))
                    session.detach()
            return {"factor": zoom, **page.evaluate("({css_width:innerWidth,outer_width:outerWidth,device_pixel_ratio:devicePixelRatio})")}
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "review/ui-screenshots")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = Fixture()
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.route("**/*", fixture.route)
        context.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copiedText=text;}}})")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        task_flow(page, fixture, args.output)
        empty_final_flow(page, fixture)
        settings_flow(page, fixture)
        list_flow(page)
        upload_flow(context, fixture)
        matrix = layout_matrix(page, args.output)
        assert not errors, errors
        context.close()
        browser.close()
        native_zoom = native_zoom_check(playwright, fixture, args.output)
    result = {"status": "passed", "layout_checks": len(matrix), "matrix": matrix,
              "flows": ["200 segments / paging / full search / full copy", "modal focus / inert / escape / expand",
                        "settings key states / return without AI", "selection clearing / busy skip / partial delete",
                        "controlled XHR progress / cancel-first / accept-first / unconfirmed recovery"],
              "zoom": {"css_reflow": "200%", "native_browser": native_zoom},
              "contrast": "not measured; no WCAG contrast claim", "page_errors": errors}
    (args.output / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "matrix"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
