import os
import time
import traceback
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


LOG_FILE = "error_log.txt"


def log_error(message: str):
    """
    将错误信息（带时间戳）写入 error_log.txt（追加）。
    """
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        # 日志如果都写不进去，就只能静默忽略，避免再次抛异常
        pass


def get_html_and_extract(link: str):
    """
    使用 requests 获取 HTML，并解析 iframe#ai-score 的 src。
    """
    try:
        resp = requests.get(link, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        iframe = soup.find("iframe", id="ai-score")
        if iframe and iframe.has_attr("src"):
            return iframe["src"]
        else:
            print(f"[WARN] 未找到 id='ai-score' 的 iframe：{link}")
            return None
    except Exception:
        err = f"Error fetching/parsing page: {link}\n{traceback.format_exc()}"
        print(err)
        log_error(err)
        return None


def sanitize_filename(name: str) -> str:
    """
    简单处理 Windows 不允许的文件名字符。
    """
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    name = name.strip()
    return name or "Unknown"


def save_page_as_pdf(p, url: str, output_suffix: str):
    """
    使用 Playwright 打开 url，等待 SVG <text> 元素，
    用第一个 text 的内容作为标题创建文件夹，并生成 PDF。
    """
    # 你可以改成 channel="msedge" 来强制用 Edge（前提：playwright install msedge）
    # browser = p.chromium.launch(headless=True, channel="msedge")
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/92.0.4515.131 Safari/537.36"
        ),
        locale="en-US",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "",
        },
    )
    page = context.new_page()

    try:
        print(f"[INFO] 打开页面: {url}")
        page.goto(url, wait_until="networkidle", timeout=60_000)

        # 注入你原来 Selenium 里的脚本
        script = """
        (function(){
            'use strict';
            if (!document.referrer) {
                location.href += '';
            }
            var style = document.createElement('style');
            style.innerHTML = '.print{display:none!important}';
            document.head.appendChild(style);
        })();
        """
        page.evaluate(script)

        # 和原逻辑一致：最多重试 4 次，每次 sleep(5)，找 SVG <text>
        retries = 4
        title_text = None

        for attempt in range(retries):
            time.sleep(5)
            elements = page.query_selector_all(
                "xpath=//*[name()='text' and @text-anchor='middle']"
            )
            if elements:
                # ⚠️ Playwright 这里必须用 text_content()，不能用 inner_text()
                raw = elements[0].text_content()
                title_text = (raw or "").strip()
                print(f"[INFO] 获取到标题: {title_text}")
                break
            else:
                print(f"[WARN] Attempt {attempt + 1} failed, 未找到 SVG text 元素，重试中...")

        if not title_text:
            print("[ERROR] Failed to load the page content completely（未获取到标题）。")
            log_error(f"Failed to load page content for URL: {url}")
            return

        folder_name = sanitize_filename(title_text)
        os.makedirs(folder_name, exist_ok=True)

        # 文件名保持和你原先逻辑一致：标题-后缀.pdf
        file_name = sanitize_filename(f"{title_text}-{output_suffix}.pdf")
        output_pdf_path = os.path.join(folder_name, file_name)

        # 使用 Playwright 的 pdf 功能：
        # 相当于 printToPDF，纸张尺寸 8.27 x 11.69 inch（A4），无边距，竖向，带背景
        page.pdf(
            path=output_pdf_path,
            width="8.27in",
            height="11.69in",
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            print_background=True,
            landscape=False,
        )

        print(f"[INFO] Page saved as PDF: {output_pdf_path}")

    except PlaywrightTimeoutError:
        err = f"Timeout while loading/waiting page: {url}\n{traceback.format_exc()}"
        print(err)
        log_error(err)
    except Exception:
        err = f"Error in save_page_as_pdf for URL: {url}\n{traceback.format_exc()}"
        print(err)
        log_error(err)
    finally:
        context.close()
        browser.close()


def main():
    print("请输入要处理的链接，每行一个，输入空行结束：")
    links = []
    try:
        while True:
            line = input().strip()
            if not line:
                break
            links.append(line)
    except EOFError:
        # 支持重定向输入
        pass
    except Exception:
        err = f"Error while reading input links\n{traceback.format_exc()}"
        print(err)
        log_error(err)

    if not links:
        print("[INFO] 未输入任何链接，程序结束。")
        return

    base_url = "https://www.gangqinpu.com"

    with sync_playwright() as p:
        for link in links:
            print(f"\n[INFO] Processing {link}")
            try:
                src_value = get_html_and_extract(link)
                if not src_value:
                    print(f"[ERROR] Failed to extract iframe src for {link}")
                    log_error(f"Failed to extract iframe src for {link}")
                    continue

                full_url = base_url + src_value

                # 五线谱模式（和原逻辑一致）
                save_page_as_pdf(p, full_url, "五线谱")

                # 简谱模式：把 jianpuMode=0 替换为 1
                if "jianpuMode=0" in full_url:
                    simplified_url = full_url.replace("jianpuMode=0", "jianpuMode=1")
                else:
                    # 原逻辑只做替换，这里做个兜底：如果没有参数就自己加
                    sep = "&" if "?" in full_url else "?"
                    simplified_url = full_url + f"{sep}jianpuMode=1"

                save_page_as_pdf(p, simplified_url, "简谱")

            except Exception:
                err = f"Error while processing link: {link}\n{traceback.format_exc()}"
                print(err)
                log_error(err)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = f"Unhandled exception in main\n{traceback.format_exc()}"
        print(err)
        log_error(err)
