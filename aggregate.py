#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
视频自动抓取 Worker（跑在 GitHub Actions 上）

流程：
  1. 从 Firebase 队列 /videoRequests 读取「pending」的链接
  2. 用 yt-dlp 下载成 mp4
  3. GET  {API}/api/presign?name=标题   -> 拿到阿里云 OSS 签名上传地址
  4. PUT  把 mp4 传到该地址
  5. POST {API}/api/complete            -> 写入视频清单，网站上立刻可见
  6. 删除队列项（失败则标记 error 并写原因）

不需要任何密钥：presign / complete 都是公开接口。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

FB = "https://project-3699877372426450142-default-rtdb.firebaseio.com"
API = "https://video-api-tcqaokccgf.cn-hangzhou.fcapp.run"
QUEUE = "/videoRequests"
MAX_PER_RUN = 3          # 每轮最多处理多少条
ALLOW_BILI = ("bilibili.com", "b23.tv")
ALLOW_DOUYIN = ("douyin.com", "iesdouyin.com")


def http(method, url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def fb_get():
    _, b = http("GET", FB + QUEUE + ".json",
                headers={"Cache-Control": "no-store"})
    return json.loads(b or b"null") or {}


def fb_patch(key, obj):
    http("PUT", FB + QUEUE + "/" + urllib.parse.quote(key, safe="") + ".json",
         json.dumps(obj, ensure_ascii=False).encode("utf-8"),
         {"Content-Type": "application/json"})


def fb_del(key):
    http("DELETE", FB + QUEUE + "/" + urllib.parse.quote(key, safe="") + ".json")


def is_supported(url):
    u = (url or "").lower()
    return any(h in u for h in ALLOW_BILI + ALLOW_DOUYIN)


def download(url, workdir):
    """返回 (文件路径, 标题)。"""
    out = os.path.join(workdir, "video.%(ext)s")
    meta_raw = subprocess.check_output(
        ["yt-dlp", "-J", "--no-warnings", "--no-playlist", url],
        stderr=subprocess.STDOUT, timeout=300)
    meta = json.loads(meta_raw)
    title = (meta.get("title") or "未命名视频").strip()

    # B站是「视频流 + 音频流」分离，必须由 ffmpeg 合并（GitHub runner 自带）
    # 720p 封顶：兼顾清晰度与文件体积；1080p 高码率需要 B站会员，普通链接拿不到
    subprocess.run(
        ["yt-dlp", "--no-playlist", "--no-warnings",
         "-f", "bv*[height<=720]+ba/b[height<=720]/b",
         "--merge-output-format", "mp4",
         "-o", out, url],
        check=True, timeout=1800)

    for f in os.listdir(workdir):
        if f.startswith("video.") and f.endswith(".mp4"):
            p = os.path.join(workdir, f)
            if os.path.getsize(p) > 0:
                return p, title
    raise RuntimeError("yt-dlp 没有产出 mp4 文件")


def upload(local_path, title):
    """presign -> PUT -> complete，返回 key。"""
    name = title[:60] if title else "video"
    _, b = http("GET", API + "/api/presign?name=" + urllib.parse.quote(name))
    sign = json.loads(b)
    if not sign.get("ok") or not sign.get("uploadUrl"):
        raise RuntimeError("获取上传签名失败: %s" % b[:200])

    size = os.path.getsize(local_path)
    with open(local_path, "rb") as fh:
        req = urllib.request.Request(
            sign["uploadUrl"], method="PUT", data=fh,
            headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
        with urllib.request.urlopen(req, timeout=1800) as r:
            if r.status >= 300:
                raise RuntimeError("上传 OSS 失败 HTTP %s" % r.status)

    payload = json.dumps(
        {"key": sign["key"], "title": title, "desc": "", "source": "web"},
        ensure_ascii=False).encode("utf-8")
    _, b2 = http("POST", API + "/api/complete", payload,
                 {"Content-Type": "application/json"})
    res = json.loads(b2 or b"{}")
    if res.get("ok") is False:
        raise RuntimeError("写入清单失败: %s" % b2[:200])
    return sign["key"]


def main():
    try:
        queue = fb_get()
    except Exception as e:
        print("读取队列失败:", e)
        return

    pending = [(k, v) for k, v in queue.items()
               if isinstance(v, dict) and v.get("status") in (None, "pending")]

    print("队列中共 %d 条，待处理 %d 条" % (len(queue), len(pending)))
    if not pending:
        return

    pending.sort(key=lambda kv: kv[1].get("ts") or 0)
    done = fail = 0

    for key, item in pending[:MAX_PER_RUN]:
        url = item.get("url") or ""
        print("\n=== 处理 %s : %s" % (key, url))

        if not is_supported(url):
            fb_patch(key, dict(item, status="error", msg="只支持 B站 / 抖音 链接"))
            fail += 1
            continue

        fb_patch(key, dict(item, status="processing", msg="正在下载…"))
        workdir = tempfile.mkdtemp()
        try:
            path, title = download(url, workdir)
            print("已下载:", title, os.path.getsize(path), "字节")
            k = upload(path, title)
            print("已上传并写入清单:", k)
            fb_del(key)
            done += 1
        except subprocess.CalledProcessError as e:
            fb_patch(key, dict(item, status="error",
                               msg="下载失败（该平台可能需要登录凭证）"))
            print("下载失败:", e)
            fail += 1
        except Exception as e:
            fb_patch(key, dict(item, status="error", msg=str(e)[:200]))
            print("失败:", e)
            fail += 1
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    print("\n本轮完成：成功 %d，失败 %d" % (done, fail))


if __name__ == "__main__":
    main()
