#!/usr/bin/env python3
"""诊断 EbS 拍摄计划为什么一直返回 0 条(只读,打印结果,不写任何东西)。"""
import json
import re
import sys
import urllib.parse

import requests

sys.path.insert(0, ".")
from sync import EBS_API, EUAV_API, HEADERS  # noqa: E402

H = {**HEADERS, "Accept": "application/json"}


def show(label, resp, n=1500):
    print(f"\n=== {label}\n{resp.request.method} {resp.url}\nHTTP {resp.status_code}  "
          f"{resp.headers.get('content-type')}")
    print(resp.text[:n])


def try_get(label, url, n=1500, **kw):
    try:
        show(label, requests.get(url, headers=H, timeout=30, **kw), n)
    except requests.RequestException as e:
        print(f"\n=== {label}\nERROR {e}")


# 1. 现在脚本的原样请求
try_get("EbS as sync.py calls it", EBS_API,
        params={"mediaType": "EVENTPLANNING", "pageSize": 100})
# 2. 同一网关下其它可能的路径(旧 /search 已 404)
base = EBS_API.rsplit("/", 1)[0]
for path in ("", "/events", "/event", "/planning", "/eventplanning", "/search/events",
             "/v2/search", "/api/search", "/media/search"):
    try_get(f"EbS path {path or '/'}", base + path, n=400,
            params={"mediaType": "EVENTPLANNING", "pageSize": 3})

# 4. 影像库 Solr API(这个是好的)里有没有「计划」类 type
for t in ("EVENT", "EVENTPLANNING", "PLANNING", "ADVISORY", "LIVE"):
    try_get(f"EU AV type={t}", EUAV_API,
            params={"wt": "json", "index": 1, "pagesize": 3, "type": t}, n=600)

# 5. 前端 SPA:找它现在调哪些接口
try:
    home = requests.get("https://audiovisual.ec.europa.eu/en", headers=HEADERS, timeout=30)
    print(f"\n=== SPA home HTTP {home.status_code}, {len(home.text)} bytes")
    print(home.text[:3000])
    scripts = re.findall(r'src="([^"]+\.js[^"]*)"', home.text)
    print("scripts:", scripts)
    found = set()
    for s in scripts:
        url = urllib.parse.urljoin(home.url, s)
        js = requests.get(url, headers=HEADERS, timeout=30).text
        print(f"  bundle {s}: {len(js)} bytes")
        for m in re.findall(r'https://[a-zA-Z0-9.-]+(?:amazonaws\.com|europa\.eu)[^"\'`\s]*', js):
            found.add(m)
        for m in re.findall(r'.{60}(?:mediaType|parrotfish).{160}', js)[:10]:
            print(f"[{s}] ...{m}...")
        for m in re.findall(r'.{80}(?:EVENTPLANNING|eventplanning|planning).{120}', js, re.I)[:8]:
            print(f"[{s}] ...{m}...")
    print("execute-api endpoints in bundles:", json.dumps(sorted(found), indent=1))
except requests.RequestException as e:
    print("SPA error", e)
