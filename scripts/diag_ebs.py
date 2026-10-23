#!/usr/bin/env python3
"""验证新版 scrape_from_ebs 对真实接口能跑通(只读)。"""
import sys

sys.path.insert(0, ".")
import sync  # noqa: E402

evs = sync.scrape_from_ebs()
for e in evs:
    print(e)
# 换个人名验证过滤逻辑本身能命中(当前计划表里没有他时也能看出接口和解析是好的)
sync.EUAV_PERSON = "7413"   # Dombrovskis
orig = sync.mentions_sejourne
sync.mentions_sejourne = lambda t: "dombrovskis" in t.lower()
for e in sync.scrape_from_ebs():
    print("  [Dombrovskis check]", e)
sync.mentions_sejourne = orig
