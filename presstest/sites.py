#!/usr/bin/env python3
"""sites.py — 压测站点/接口注册表(即嗨四域 + 599 两接口)

每个 Site = 一个独立频控域的假设。注意:压测按「域」分别起跑,但同一台
GHA runner 的出口 IP 是同一个——即嗨四域共享同一 IP 预算,跨域结论要
分开跑、并考虑上一次运行可能留下的惩罚态(README「解读」一节)。

  · jihai_www      www.jihai8.com/zq/ NUXT 列表(家宽实测 5req/s 触发 403,约150s解封)
  · jihai_apinode  api-node.jihai8.com(header/leaguerank/event_analysis,经中继生产)
  · jihai_apilive  api-live.jihai8.com(oddsList 全量 + eventTechList 逐场)
  · jihai_dc       dc.jihai8.com(getZQMatchInfo/getTeamInfo 等,生产直连,边界未知)
  · f599_rest      fb-p.599.com(instant 全量 + statistical 逐场;TLS 指纹门禁,curl_cffi)

per_match=True 的接口用 bootstrap 端点拿到的 id 列表轮转;ids 也可用
probe.py --ids 覆盖。样本 id 是 2026-10-11 从生产库抽的近期场次,站方
对老 id 普遍仍回数据(599 统计回溯实测无界),id 失效不影响测「闸门」。
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field

# 2026-10-11 生产库近 3 天有盘口场次(压测样本)
SAMPLE_MATCH_IDS = ["2908831", "2956836", "2997434", "3000106", "3001214",
                    "3071119", "3071120", "3086990", "3001220", "3071118",
                    "2908832", "2910917"]
SAMPLE_TEAM_IDS = ["10116", "10359", "12908", "12948", "14144", "16213",
                   "16640", "16662", "16994", "16995", "1986", "20022",
                   "2023", "21164", "2228", "22847", "24025", "24453",
                   "24501", "27303"]

JIHAI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
JIHAI_HEADERS = {"User-Agent": JIHAI_UA, "Referer": "https://www.jihai8.com/zq/"}


@dataclass
class Site:
    key: str
    description: str
    endpoints: list                      # [{name, url, per_match, weight}]
    headers: dict = field(default_factory=dict)
    session: str = "requests"            # requests | curl_cffi
    bootstrap: str | None = None         # 从该端点响应提取 per_match id 列表
    id_field: str = "matchId"            # 拼进 per_match url 的查询参数名
    notes: str = ""


# ---- id 提取器(bootstrap 响应 → id 列表) --------------------------------
def _ids_from_oddslist(resp) -> list[str]:
    d = (resp.json() or {}).get("data") or {}
    return [str(k) for k in d.keys()][:200]


def _ids_from_instant(resp) -> list[str]:
    """599 instant: AES→b64→gzip→JSON 的密文快照 → thirdId 列表。

    解密链复用 research/599 的 legacy 结论(密钥硬编码于前端 js)。
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    import base64
    key, iv = b"777db0c19edfaace", b"9876543210599311"
    data = (resp.json() or {}).get("data")
    if not isinstance(data, str) or len(data) < 40:
        return []
    ct = base64.b64decode(data)
    d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = d.update(ct) + d.finalize()
    raw = gzip.decompress(base64.b64decode(padded[:-padded[-1]].decode("utf-8")))
    rows = (json.loads(raw.decode("utf-8")) or {}).get("immediateMatch") or []
    return [str(r[1]) for r in rows if len(r) > 1][:200]


SITES: dict[str, Site] = {
    "jihai_www": Site(
        key="jihai_www",
        description="www.jihai8.com 列表页 NUXT(≈250KB HTML,家宽独立闸门)",
        headers=JIHAI_HEADERS,
        endpoints=[{"name": "zq_list", "url": "https://www.jihai8.com/zq/",
                    "per_match": False}],
        notes="已知家宽 5req/s 触发 403 约 150s 解封;测干净机房 IP 边界",
    ),
    "jihai_apinode": Site(
        key="jihai_apinode",
        description="api-node:逐场 header / leaguerank 轮转(生产经云中继)",
        headers=JIHAI_HEADERS,
        bootstrap="header",
        id_field="matchId",
        endpoints=[
            {"name": "header",
             "url": "https://api-node.jihai8.com/api/zq/header?matchId={id}",
             "per_match": True},
            {"name": "event_analysis",
             "url": "https://api-node.jihai8.com/api/zq/event_analysis?matchId={id}",
             "per_match": True},
        ],
        notes="闸门=每 IP 聚合频率+时间衰减(实验定案);ramp 期望找到首 403 速率",
    ),
    "jihai_apilive": Site(
        key="jihai_apilive",
        description="api-live:oddsList 全量 + eventTechList 逐场",
        headers=JIHAI_HEADERS,
        bootstrap="oddslist",
        id_field="matchId",
        endpoints=[
            {"name": "oddslist",
             "url": "https://api-live.jihai8.com/newapi/football/oddsList?companyId=3",
             "per_match": False},
            {"name": "event_tech",
             "url": "https://api-live.jihai8.com/newapi/football/eventTechList?matchId={id}",
             "per_match": True},
        ],
        notes="按场次接口已知干净 IP 下 0.5s 间隔偶发 403(≥1.2s 稳);全量接口极高效",
    ),
    "jihai_dc": Site(
        key="jihai_dc",
        description="dc.jihai8.com:getZQMatchInfo/getTeamInfo(海选静态面主域,边界未知)",
        headers=JIHAI_HEADERS,
        endpoints=[
            {"name": "zq_match_info",
             "url": "https://dc.jihai8.com/pc/getZQMatchInfo?match_id={id}",
             "per_match": True, "ids": SAMPLE_MATCH_IDS},
            {"name": "team_info",
             "url": "https://dc.jihai8.com/jhpc/getTeamInfo?team_id={id}",
             "per_match": True, "ids": SAMPLE_TEAM_IDS},
        ],
        notes="海选预采集(问题一)的预算关键;测前先确认 dc 与 api-node 是否同闸",
    ),
    "f599_rest": Site(
        key="f599_rest",
        description="599 fb-p.599.com:instant 全量 + statistical 逐场(TLS 指纹门禁)",
        headers={"Referer": "https://www.599.com/"},
        session="curl_cffi",
        bootstrap="instant",
        id_field="thirdId",
        endpoints=[
            {"name": "instant",
             "url": ("https://fb-p.599.com/footballapi/core/matchlist/pupuu/instant"
                     "?comId=8&lang=zh&timeZone=8&version=666&versionCode=666"
                     "&channelNumber=GF1001&platform=pc&appType=3"),
             "per_match": False},
            {"name": "statistical",
             "url": ("https://fb-p.599.com/footballapi/core/details/statistical"
                     "?comId=8&lang=zh&timeZone=8&version=666&versionCode=666"
                     "&channelNumber=GF1001&platform=pc&appType=3&thirdId={id}"),
             "per_match": True},
        ],
        notes="门禁=TLS 指纹与 IP 无关;statistical 无需签名(实测);id 提取走 AES 解密链",
    ),
}


def get_site(key: str) -> Site:
    if key not in SITES:
        raise SystemExit(f"未知站点 {key};可用: {', '.join(SITES)}")
    return SITES[key]
