import os
import io
import re
import json
import time
import traceback
import zipfile
from datetime import datetime
from urllib.parse import urlparse, parse_qs

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
        pass


# ──────────────────────── CCMZ 解析 & MIDI 转换 ────────────────────────


class CCMZInfo:
    """解析后的 ccmz 数据容器"""
    def __init__(self):
        self.ver = None      # 1 或 2
        self.score = None    # 乐谱数据（XML 或 JSON 字符串）
        self.midi = None     # MIDI 数据（v1: bytes, v2: JSON 字符串）


def _parse_midi_event(event_bytes):
    """解析单个 MIDI 事件字节序列，返回事件字典（用于 v2 格式）"""
    if not event_bytes or len(event_bytes) == 0:
        return None

    first_byte = event_bytes[0]

    # Channel 事件 (0x80-0xEF)
    if (first_byte & 0xF0) != 0xF0:
        event_type = first_byte >> 4
        channel = first_byte & 0x0F
        if len(event_bytes) < 2:
            return None
        event = {'type': 'channel', 'channel': channel}
        if event_type == 0x8:
            event['subtype'] = 'noteOff'
            event['noteNumber'] = event_bytes[1]
            if len(event_bytes) > 2:
                event['velocity'] = event_bytes[2]
        elif event_type == 0x9:
            event['noteNumber'] = event_bytes[1]
            if len(event_bytes) > 2:
                event['velocity'] = event_bytes[2]
            event['subtype'] = 'noteOff' if event.get('velocity', 0) == 0 else 'noteOn'
        elif event_type == 0xA:
            event['subtype'] = 'noteAftertouch'
            event['noteNumber'] = event_bytes[1]
            if len(event_bytes) > 2:
                event['amount'] = event_bytes[2]
        elif event_type == 0xB:
            event['subtype'] = 'controller'
            event['controllerType'] = event_bytes[1]
            if len(event_bytes) > 2:
                event['value'] = event_bytes[2]
        elif event_type == 0xC:
            event['subtype'] = 'programChange'
            event['programNumber'] = event_bytes[1]
        elif event_type == 0xD:
            event['subtype'] = 'channelAftertouch'
            event['amount'] = event_bytes[1]
        elif event_type == 0xE:
            event['subtype'] = 'pitchBend'
            event['value'] = event_bytes[1] + ((event_bytes[2] << 7) if len(event_bytes) > 2 else 0)
        else:
            event['subtype'] = 'unknown'
        return event

    # Meta 事件 (0xFF)
    elif first_byte == 0xFF:
        if len(event_bytes) < 2:
            return None
        event = {'type': 'meta'}
        meta_type = event_bytes[1]
        length = 0
        pos = 2
        if pos < len(event_bytes):
            byte = event_bytes[pos]
            pos += 1
            while byte & 0x80 and pos < len(event_bytes):
                length = (length << 7) + (byte & 0x7F)
                byte = event_bytes[pos]
                pos += 1
            length = (length << 7) + (byte & 0x7F)
        if meta_type == 0x51 and length == 3:
            event['subtype'] = 'setTempo'
            if pos + 2 < len(event_bytes):
                event['microsecondsPerBeat'] = (event_bytes[pos] << 16) + (event_bytes[pos + 1] << 8) + event_bytes[pos + 2]
        elif meta_type == 0x58 and length == 4:
            event['subtype'] = 'timeSignature'
            if pos + 3 < len(event_bytes):
                event['numerator'] = event_bytes[pos]
                event['denominator'] = 2 ** event_bytes[pos + 1]
        elif meta_type == 0x03:
            event['subtype'] = 'trackName'
            if pos + length <= len(event_bytes):
                event['text'] = event_bytes[pos:pos + length].decode('utf-8', errors='ignore')
        return event

    return None


def _write_midi_from_json(midi_data, output_path):
    """将 v2 格式的 midi.json 数据写成标准 .mid 文件"""
    from midiutil.MidiFile import MIDIFile

    ticks_per_beat = 480
    tempos = midi_data.get('tempos', [])
    tracks = midi_data.get('tracks', [])
    events = midi_data.get('events', [])

    if not tracks and not events:
        raise ValueError("No track or event data in midi.json")

    track_count = len(tracks) if tracks else 1
    midi = MIDIFile(track_count)

    initial_tempo = 500000
    if tempos and tempos[0].get('tempo'):
        initial_tempo = tempos[0]['tempo']

    for idx in range(track_count):
        if tracks and idx < len(tracks):
            track_name = tracks[idx].get('name', f'Track{idx}')
            midi.addTrackName(idx, 0, track_name)
        bpm = round(60000000 / initial_tempo)
        midi.addTempo(idx, 0, bpm)
        if tracks and idx < len(tracks):
            program = tracks[idx].get('program', 0)
            midi.addProgramChange(idx, 0, 0, program)

    note_on_map = {}
    for event in events:
        track_id = event.get('track', 0)
        if track_id >= track_count:
            track_id = 0
        event_bytes = event.get('event', [])
        parsed = _parse_midi_event(event_bytes)
        if not parsed:
            continue
        parsed['tick'] = event['tick']
        parsed['track'] = track_id

        if parsed['type'] == 'channel':
            if parsed['subtype'] == 'noteOn':
                key = (parsed['track'], parsed.get('channel', 0), parsed.get('noteNumber', 0))
                note_on_map[key] = parsed
            elif parsed['subtype'] == 'noteOff':
                key = (parsed['track'], parsed.get('channel', 0), parsed.get('noteNumber', 0))
                if key in note_on_map:
                    note_on = note_on_map[key]
                    start_tick = note_on['tick']
                    end_tick = parsed['tick']
                    duration_ticks = max(10, end_tick - start_tick)
                    start_time = start_tick / ticks_per_beat
                    duration_sec = duration_ticks / ticks_per_beat
                    velocity = note_on.get('velocity', 90)
                    tidx = parsed['track']
                    if tidx >= track_count:
                        tidx = track_count - 1
                    midi.addNote(tidx, 0, note_on['noteNumber'],
                                 start_time, duration_sec, velocity)
                    del note_on_map[key]

    with open(output_path, 'wb') as f:
        midi.writeFile(f)


def parse_ccmz_and_save_midi(ccmz_url, output_dir, file_base_name):
    """
    下载 ccmz 文件，解析其中的 MIDI 数据，保存为 .mid 文件。
    返回保存的文件路径，失败返回 None。
    """
    try:
        # 下载 ccmz 文件（绕过代理）
        session = requests.Session()
        session.trust_env = False
        resp = session.get(ccmz_url, timeout=30)
        resp.raise_for_status()
        buffer = resp.content
        session.close()

        if not buffer or len(buffer) < 2:
            print("[WARN] ccmz 文件为空或过小")
            return None

        version = buffer[0]
        data = buffer[1:]
        info = CCMZInfo()
        info.ver = version

        if version == 1:
            # v1: 直接是 zip，内含 data.mid
            zf = zipfile.ZipFile(io.BytesIO(data))
            info.midi = zf.read("data.mid")
            mid_path = os.path.join(output_dir, f"{file_base_name}.mid")
            with open(mid_path, 'wb') as f:
                f.write(info.midi)
            print(f"[INFO] MIDI 已保存 (v1): {mid_path}")
            return mid_path

        elif version == 2:
            # v2: 简单异或混淆后是 zip，内含 midi.json
            decoded = bytes([v + 1 if v % 2 == 0 else v - 1 for v in data])
            zf = zipfile.ZipFile(io.BytesIO(decoded))
            midi_json_str = zf.read("midi.json").decode('utf-8')
            midi_data = json.loads(midi_json_str)
            mid_path = os.path.join(output_dir, f"{file_base_name}.mid")
            _write_midi_from_json(midi_data, mid_path)
            print(f"[INFO] MIDI 已保存 (v2): {mid_path}")
            return mid_path

        else:
            print(f"[WARN] 未知的 ccmz 版本号: {version}")
            return None

    except Exception:
        err = f"解析 ccmz / 生成 MIDI 失败: {ccmz_url}\n{traceback.format_exc()}"
        print(err)
        log_error(err)
        return None


# ──────────────────────── 网页抓取 ────────────────────────


def get_html_and_extract(link: str, max_retries: int = 3):
    """
    使用 requests 获取 HTML，并解析 iframe#ai-score 的 src。
    绕过系统代理，带重试机制。
    """
    session = requests.Session()
    session.trust_env = False

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(link, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            iframe = soup.find("iframe", id="ai-score")
            if iframe and iframe.has_attr("src"):
                return iframe["src"]
            else:
                print(f"[WARN] 未找到 id='ai-score' 的 iframe：{link}")
                return None
        except requests.exceptions.ProxyError:
            print(f"[WARN] 代理错误 (尝试 {attempt}/{max_retries}): {link}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            err = f"代理错误，重试 {max_retries} 次后仍失败: {link}\n{traceback.format_exc()}"
            print(err)
            log_error(err)
            return None
        except requests.exceptions.SSLError:
            print(f"[WARN] SSL 错误 (尝试 {attempt}/{max_retries}): {link}")
            if attempt < max_retries:
                time.sleep(2)
                continue
            err = f"SSL 错误，重试 {max_retries} 次后仍失败: {link}\n{traceback.format_exc()}"
            print(err)
            log_error(err)
            return None
        except requests.exceptions.ConnectionError:
            print(f"[WARN] 连接错误 (尝试 {attempt}/{max_retries}): {link}")
            if attempt < max_retries:
                time.sleep(3)
                continue
            err = f"连接错误，重试 {max_retries} 次后仍失败: {link}\n{traceback.format_exc()}"
            print(err)
            log_error(err)
            return None
        except Exception:
            err = f"Error fetching/parsing page: {link}\n{traceback.format_exc()}"
            print(err)
            log_error(err)
            return None
        finally:
            session.close()


def extract_ccmz_url(iframe_src):
    """
    从 iframe src 中提取 ccmz 文件的实际下载地址。
    iframe src 形如: /sheetplayer/web.html?jianpuMode=0&url=https://...xxx.ccmz
    """
    parsed = urlparse(iframe_src)
    params = parse_qs(parsed.query)
    url_list = params.get('url', [])
    return url_list[0] if url_list else None


# ──────────────────────── PDF 生成 ────────────────────────


def sanitize_filename(name: str) -> str:
    invalid = r'\/:*?"<>|'
    for ch in invalid:
        name = name.replace(ch, "_")
    name = name.strip()
    return name or "Unknown"


def save_page_as_pdf(p, url: str, output_suffix: str):
    """
    使用 Playwright 打开 url，等待 SVG <text> 元素，
    用第一个 text 的内容作为标题创建文件夹，并生成 PDF。
    同时返回文件夹名（用于 MIDI 保存）。
    """
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
    folder_name = None

    try:
        print(f"[INFO] 打开页面: {url}")
        page.goto(url, wait_until="networkidle", timeout=60_000)

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

        retries = 4
        title_text = None

        for attempt in range(retries):
            time.sleep(5)
            elements = page.query_selector_all(
                "xpath=//*[name()='text' and @text-anchor='middle']"
            )
            if elements:
                raw = elements[0].text_content()
                title_text = (raw or "").strip()
                print(f"[INFO] 获取到标题: {title_text}")
                break
            else:
                print(f"[WARN] Attempt {attempt + 1} failed, 未找到 SVG text 元素，重试中...")

        if not title_text:
            print("[ERROR] Failed to load the page content completely（未获取到标题）。")
            log_error(f"Failed to load page content for URL: {url}")
            return None

        folder_name = sanitize_filename(title_text)
        os.makedirs(folder_name, exist_ok=True)

        file_name = sanitize_filename(f"{title_text}-{output_suffix}.pdf")
        output_pdf_path = os.path.join(folder_name, file_name)

        page.pdf(
            path=output_pdf_path,
            width="8.27in",
            height="11.69in",
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            print_background=True,
            landscape=False,
        )

        print(f"[INFO] Page saved as PDF: {output_pdf_path}")
        return folder_name

    except PlaywrightTimeoutError:
        err = f"Timeout while loading/waiting page: {url}\n{traceback.format_exc()}"
        print(err)
        log_error(err)
        return folder_name
    except Exception:
        err = f"Error in save_page_as_pdf for URL: {url}\n{traceback.format_exc()}"
        print(err)
        log_error(err)
        return folder_name
    finally:
        context.close()
        browser.close()


# ──────────────────────── 主流程 ────────────────────────


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
            print(f"\n{'='*60}")
            print(f"[INFO] Processing {link}")
            print(f"{'='*60}")
            try:
                src_value = get_html_and_extract(link)
                if not src_value:
                    print(f"[ERROR] Failed to extract iframe src for {link}")
                    log_error(f"Failed to extract iframe src for {link}")
                    continue

                full_url = base_url + src_value

                # 提取 ccmz 下载地址
                ccmz_url = extract_ccmz_url(src_value)
                if ccmz_url:
                    print(f"[INFO] ccmz 地址: {ccmz_url}")
                else:
                    print("[WARN] 未能从 iframe src 中提取 ccmz 地址")

                # 五线谱 PDF
                folder_name = save_page_as_pdf(p, full_url, "五线谱")

                # 简谱 PDF
                if "jianpuMode=0" in full_url:
                    simplified_url = full_url.replace("jianpuMode=0", "jianpuMode=1")
                else:
                    sep = "&" if "?" in full_url else "?"
                    simplified_url = full_url + f"{sep}jianpuMode=1"
                save_page_as_pdf(p, simplified_url, "简谱")

                # 下载 MIDI
                if ccmz_url and folder_name and os.path.isdir(folder_name):
                    parse_ccmz_and_save_midi(ccmz_url, folder_name, folder_name)

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
