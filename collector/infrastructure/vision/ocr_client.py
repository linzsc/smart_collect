# -*- coding: utf-8 -*-
"""滴滴内部 OCR 客户端（自包含，仅依赖 requests）
============================================================================
来源：mars-data-old/docs/OCR_INTEGRATION.md（第二节，直接复制适配）

- URL 模式：传图片 URL，客户端自行下载
- 本地文件模式：传本地路径，读取二进制后 base64 提交（本项目主用）
- 并发：线程池，默认 30
- 重试/超时：可配置（默认 retry=3, timeout=(2,10)，timeout=-1 表示不设超时）
- 输出：cache_ret = {url 或 本地路径: (ocrData, ocrLocations)}
"""
import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import requests

# ============ 1. 配置 ============
PROD_OCR_URL = "http://10.66.96.24:16019/predict/ocr_system"   # 生产
DEV_OCR_URL = "http://10.196.104.7:8068/predict/ocr_system"    # 开发/测试


def get_ocr_url():
    """通过环境变量 OCR_PROFILE=DEV 切换环境"""
    return DEV_OCR_URL if os.getenv("OCR_PROFILE") == "DEV" else PROD_OCR_URL


# ============ 2. OCR 识别纠错字典（按业务持续补充） ============
CONV_DICT = {
    "消点宝经济型": "捎点宝经济型",
    "消点宝出行": "捎点宝出行",
    "背操出行": "曹操出行",
    "费操出行": "曹操出行",
}


# ============ 3. 底层工具函数 ============
def cv2_to_base64(images):
    """二进制图片列表 -> base64 字符串列表"""
    return [base64.b64encode(img).decode("utf-8") for img in images]


def read_file(file_path):
    with open(file_path, "rb") as f:
        return f.read()


def download_image(url, timeouts=(5, 10, 15)):
    """下载图片，逐级加大超时重试；全部失败抛异常"""
    for t in timeouts:
        try:
            r = requests.get(url, timeout=t)
            r.raise_for_status()
            return r.content
        except Exception as e:
            logging.warning("[ocr] 下载图片失败 url=%s timeout=%s err=%s", url, t, e)
            time.sleep(3)
    raise RuntimeError("[ocr] 图片下载失败: %s" % url)


def convert_point(didi_point):
    """
    滴滴返回的单字块 -> 统一字块格式 {x, y, w, h, text, c}
    注意：与老仓库保持一致，w/h 表示右/下边界（不是宽度/高度），
    后续位置匹配时 top=y, bottom=h。
    """
    xs = sorted(p[0] for p in didi_point["text_region"])
    ys = sorted(p[1] for p in didi_point["text_region"])
    w = 0.5 * (xs[-1] + xs[-2] - xs[0] - xs[1])
    h = 0.5 * (ys[-1] + ys[-2] - ys[0] - ys[1])
    x = 0.5 * (xs[0] + xs[1])
    y = 0.5 * (ys[0] + ys[1])
    text = CONV_DICT.get(didi_point["text"], didi_point["text"])
    c = round(didi_point["confidence"], 3) if "confidence" in didi_point else None
    return {"x": x, "y": y, "w": w + x, "h": h + y, "text": text, "c": c}


def convert_output(key_list, lines):
    """(url/路径列表, 字块行列表) -> {key: (ocrData, ocrLocations)}"""
    ocr_data = [",".join(p["text"] for p in line) for line in lines]
    return {key_list[i]: (ocr_data[i], lines[i]) for i in range(len(key_list))}


def _request_with_retry(url, headers, data, max_retries, timeout):
    """带重试的 HTTP POST；timeout=-1 表示不设超时"""
    for attempt in range(max_retries):
        try:
            kwargs = {} if timeout == -1 else {"timeout": timeout}
            return requests.post(url, headers=headers, data=json.dumps(data), **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            logging.warning("[ocr] 请求重试 %s/%s err=%s", attempt + 1, max_retries, e)
            time.sleep(1)
    raise RuntimeError("[ocr] 请求失败")


def ocr_base_api(images, max_retries=3, timeout=(2, 10)):
    """POST base64 图片到 OCR 服务，返回解析后的 json"""
    url = get_ocr_url()
    headers = {"Content-type": "application/json"}
    data = {"images": cv2_to_base64(images)}
    r = _request_with_retry(url, headers, data, max_retries, timeout)
    return r.json()


# ============ 4. 客户端 ============
class DidiOcrCli:
    """
    用法：
        client = DidiOcrCli()
        client.scan_concurrency([url1, url2])            # URL 模式
        client.scan_concurrency_local_files([path1])     # 本地文件模式
        # 结果
        for key, (ocr_data, ocr_locations) in client.cache_ret.items():
            ...
    同一实例内已识别的 key（URL 或路径）不会重复请求。
    """

    def __init__(self, max_workers=30):
        self.cache_ret = {}                       # key: url 或 本地路径
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._lock = threading.Lock()

    # ---------- URL 模式 ----------
    def single_scan(self, urls, max_retries=3, timeout=(2, 10)):
        images = [download_image(url) for url in urls if url is not None]
        didi_ret = ocr_base_api(images, max_retries, timeout)
        lines = [[convert_point(p) for p in line] for line in didi_ret.get("results", [])]
        if len(lines) != len(urls):
            logging.warning("[ocr] 返回结果数量不符 urls=%s results=%s", len(urls), len(lines))
            return
        with self._lock:
            self.cache_ret.update(convert_output(urls, lines))

    def scan_concurrency(self, urls, retry=3, timeout=(2, 10), wait_exception=False):
        futures = []
        for url in urls:
            if url not in self.cache_ret:
                futures.append(self._executor.submit(self.single_scan, [url], retry, timeout))
        wait(futures)
        if wait_exception:
            for f in futures:
                f.result()          # 有异常会抛出，便于上层感知

    # ---------- 本地文件模式 ----------
    def single_scan_local_file(self, file_paths, max_retries=3, timeout=(2, 10)):
        images = [read_file(p) for p in file_paths if p is not None]
        didi_ret = ocr_base_api(images, max_retries, timeout)
        lines = [[convert_point(p) for p in line] for line in didi_ret.get("results", [])]
        if len(lines) != len(file_paths):
            logging.warning("[ocr] 返回结果数量不符 paths=%s results=%s", len(file_paths), len(lines))
            return
        with self._lock:
            self.cache_ret.update(convert_output(file_paths, lines))

    def scan_concurrency_local_files(self, file_paths, retry=3, timeout=(2, 10)):
        futures = []
        for path in file_paths:
            if path not in self.cache_ret:
                futures.append(self._executor.submit(self.single_scan_local_file, [path], retry, timeout))
        wait(futures)


if __name__ == "__main__":
    # 自测：本地单张图片
    import sys
    client = DidiOcrCli()
    client.scan_concurrency_local_files(sys.argv[1:])
    for k, v in client.cache_ret.items():
        print(k, "=>", v[0][:100], "...")
