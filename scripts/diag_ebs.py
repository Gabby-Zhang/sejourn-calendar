#!/usr/bin/env python3
"""诊断 EbS 拍摄计划(只读)。旧 parrotfish /search 已 404;影像库 Solr API 的
type=EVENTPLANNING 有数据,这里把字段全打出来,供改写 scrape_from_ebs。"""
import json
import sys

import requests

sys.path.insert(0, ".")
from sync import EUAV_API, HEADERS  # noqa: E402

resp = requests.get(EUAV_API, headers={**HEADERS, "Accept": "application/json"},
                    timeout=30, params={"wt": "json", "index": 1, "pagesize": 100,
                                        "type": "EVENTPLANNING"})
docs = resp.json()["response"]["docs"]
print(f"numFound={len(docs)}")
print("fields:", sorted({k for d in docs for k in d}))
print(json.dumps(docs[0], ensure_ascii=False, indent=1)[:6000])
for d in docs:
    titles = d.get("titles_json") or {}
    t = titles.get("EN") if isinstance(titles, dict) else titles
    print("\n---", d.get("ref"), d.get("productiondate"), d.get("shootstartdate"),
          "|", json.dumps(t, ensure_ascii=False)[:200])
    print("   pers:", json.dumps(d.get("pers_json"), ensure_ascii=False)[:300])
    print("   loc:", json.dumps({k: v for k, v in d.items() if "loc" in k or "place" in k},
                                 ensure_ascii=False)[:300])
