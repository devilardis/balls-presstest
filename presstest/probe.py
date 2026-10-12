#!/usr/bin/env python3
"""probe.py — 数据源频控边界压测探针(ramp 找边界 / soak 浸泡 / decay 测恢复)

⚠️ 用途与纪律:
  只为已接入(或准备接入)的数据源做容量规划,在**一次性云出口 IP**(GHA runner)
  上运行;严禁在 220 中继、家宽、采集点等生产出口上执行。--go 缺省只打印
  计划不发任何请求。安全硬顶:速率 ≤3 req/s、总请求 ≤30000,首个 403 即停 ramp。

三个阶段(默认 auto = ramp→自动接 decay):
  ramp   速率从 --start-rate 起每 --step-mins 升 --step-rate,每档记录
         请求数/403 数;首个 403/429 即认定边界=上一档速率,自动转 decay。
  soak   以 --rate 匀速(拟人抖动)浸泡 --duration 秒,测持续预算。
  decay  已被封禁的前提下,每 --probe-interval 秒轻探一次,连续
         --recover-n 次 200 即认定恢复,输出恢复耗时(退避梯的校准依据)。

输出:--out JSONL(逐请求)+ --report JSON(汇总)。解读见同目录 README。

用法示例:
  python probe.py --list-sites
  python probe.py --site jihai_apinode --phase auto --go
  python probe.py --site jihai_dc --phase soak --rate 1.0 --duration 7200 --go
  python probe.py --site f599_rest --phase ramp --ids 123456,123457 --go
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from governor import Pacer, Budget, Backoff          # noqa: E402
from sites import SITES, get_site, SAMPLE_MATCH_IDS  # noqa: E402

HARD_MAX_RATE = 8.0        # 硬顶:任何配置不得超过(原 3.0;2026-10-12 用户授权逐级试探
                           # 被封速率窗口后放宽;首轮 ramp 已证 3.0 内全零 403)
HARD_MAX_REQUESTS = 30000


# ---------------------------------------------------------------- 传输层
class Transport:
    """requests / curl_cffi 统一封装。curl_cffi 按需导入(TLS 指纹门禁站点)。"""

    def __init__(self, kind: str, headers: dict):
        self.kind = kind
        self.headers = headers
        if kind == "curl_cffi":
            try:
                from curl_cffi import requests as cr
                self.s = cr.Session(impersonate="chrome")
            except ImportError:
                raise SystemExit("f599 站点需要 curl_cffi: pip install curl_cffi")
        else:
            import requests as rq
            self.s = rq.Session()

    def get(self, url: str, timeout: float = 15):
        if self.kind == "curl_cffi":
            return self.s.get(url, timeout=timeout, headers=self.headers,
                              impersonate="chrome")
        return self.s.get(url, timeout=timeout, headers=self.headers)


# ---------------------------------------------------------------- 工具
def classify(status: int) -> str:
    if status == 200:
        return "ok"
    if status in (403, 429):
        return "block"
    if 500 <= status < 600:
        return "server"
    return "other"


def pick_ids(site, ep, cli_ids: list[str], cache: dict) -> list[str]:
    """per_match 端点的 id 池:CLI --ids > bootstrap 提取 > 端点自带 > 内置样本。"""
    if cli_ids:
        return cli_ids
    if site.bootstrap and cache.get(site.bootstrap):
        return cache[site.bootstrap]
    return ep.get("ids") or SAMPLE_MATCH_IDS


def fmt_plan(args, site) -> str:
    eps = " + ".join(e["name"] for e in site.endpoints)
    return (f"站点={site.key}({site.description};session={site.session})\n"
            f"  阶段={args.phase} 速率={args.rate or '-'} 时长={args.duration}s "
            f"ramp起点={args.start_rate} 步长={args.step_rate}/"
            f"{args.step_mins}min 上限={args.max_rate}\n"
            f"  接口: {eps}\n  硬顶: ≤{HARD_MAX_RATE}req/s, ≤{HARD_MAX_REQUESTS}req")


# ---------------------------------------------------------------- 阶段实现
def run_requests(t: Transport, site, ids_cache: dict, cli_ids, pacer: Pacer,
                 budget: Budget, backoff: Backoff, out, label: str,
                 stop_on_block: bool = False, state: dict | None = None):
    """按 pacer 节奏轮转 site 全部端点发请求,逐条写 JSONL,返回本段统计。"""
    stats = Counter()
    first_block = None
    rr = 0
    while True:
        if state is not None and state.get("stop"):
            break
        if not budget.spend():
            break
        if backoff.is_blocked():
            time.sleep(min(30.0, backoff.pause_until - time.time() + 0.5))
            continue
        ep = site.endpoints[rr % len(site.endpoints)]
        rr += 1
        url = ep["url"]
        if ep.get("per_match"):
            ids = pick_ids(site, ep, cli_ids, ids_cache)
            if not ids:
                break
            url = url.replace("{id}", ids[rr % len(ids)])
        t0 = time.time()
        try:
            r = t.get(url)
            status, ms = r.status_code, int((time.time() - t0) * 1000)
        except Exception as e:
            status, ms = -1, int((time.time() - t0) * 1000)
            out.write(json.dumps({"ts": t0, "site": site.key, "ep": ep["name"],
                                  "status": status, "ms": ms,
                                  "err": type(e).__name__, "phase": label}) + "\n")
            stats["net_err"] += 1
            out.flush()
            time.sleep(pacer.next_delay())
            continue
        kind = classify(status)
        stats[kind] += 1
        if kind == "block" and first_block is None:
            first_block = t0
        out.write(json.dumps({"ts": t0, "site": site.key, "ep": ep["name"],
                              "status": status, "ms": ms, "phase": label}) + "\n")
        out.flush()
        if kind == "block":
            if stop_on_block and state is not None:
                state["stop"] = True
            backoff.on_block()
        elif kind == "ok":
            backoff.on_ok()
            # decay 阶段:连续 200 达到恢复判据即收工,不再空探
            if (label == "decay" and state is not None
                    and state.get("recover_n")
                    and backoff.consec_ok >= state["recover_n"]):
                state["stop"] = True
        time.sleep(pacer.next_delay())
    return {"label": label, "ok": stats["ok"], "block": stats["block"],
            "server": stats["server"], "other": stats["other"],
            "net_err": stats["net_err"], "first_block_ts": first_block,
            "elapsed_s": round(budget.elapsed, 1)}


def extract_bootstrap_ids(t: Transport, site, cli_ids) -> dict:
    """跑一次 bootstrap 端点,提取 per_match id 池(失败不致命,退端点自带样本)。"""
    cache: dict = {}
    if not site.bootstrap or cli_ids:
        return cache
    # apinode 没有免登录的全量列表端点,直接用生产库样本,不浪费请求
    if site.key == "jihai_apinode":
        return {site.bootstrap: list(SAMPLE_MATCH_IDS)}
    ep = next((e for e in site.endpoints if e["name"] == site.bootstrap), None)
    if ep is None:
        return cache
    try:
        r = t.get(ep["url"])
        if r.status_code == 200:
            if site.key == "jihai_apilive":
                cache = {site.bootstrap: _ids_from_oddslist(r)}
            elif site.key == "f599_rest":
                cache = {site.bootstrap: _ids_from_instant(r)}
            if cache.get(site.bootstrap):
                print(f"[bootstrap] {site.bootstrap} → {len(cache[site.bootstrap])} ids")
    except Exception as e:
        print(f"[bootstrap] 失败(退回内置样本): {type(e).__name__}: {e}")
    return cache


def phase_auto(t, site, ids_cache, cli_ids, args, out, budget) -> list[dict]:
    """ramp(首403即停)→ decay(测恢复),一个 job 出完整边界报告。"""
    results = []
    rate = args.start_rate
    boundary = None
    while rate <= args.max_rate + 1e-9 and not budget_exceeded(budget):
        print(f"[ramp] 档位 {rate:.2f} req/s …", flush=True)
        pacer = Pacer.from_rate(rate, mode="uniform", floor=0.2)
        backoff = Backoff(trigger_n=2, recover_n=999)   # ramp 内不自动暂停
        state = {"stop": False}
        step_budget = Budget(max_requests=min(HARD_MAX_REQUESTS - budget.n,
                                              max(30, int(rate * args.step_mins * 60))),
                             max_seconds=args.step_mins * 60 + 120)
        r = run_requests(t, site, ids_cache, cli_ids, pacer, step_budget,
                         backoff, out, f"ramp@{rate:.2f}",
                         stop_on_block=True, state=state)
        results.append(r)
        budget.n += step_budget.n
        if r["block"] > 0:
            boundary = rate
            print(f"[ramp] {rate:.2f} req/s 触发 403 ×{r['block']},"
                  f"边界=首触档,前档 {max(args.start_rate, rate - args.step_rate):.2f} 安全", flush=True)
            break
        rate = round(rate + args.step_rate, 2)
    if boundary is None:
        print(f"[ramp] 直到 {args.max_rate} req/s 未触发闸门(预算或上限先到)", flush=True)
        return results
    # ---- decay:轻探恢复(默认上限 2h;--decay-max-mins 可缩,逐级试探场景
    #      边界信息到手即可,不必为「未恢复」耗满 2h job)
    print(f"[decay] 每 {args.probe_interval}s 轻探,连续 {args.recover_n} 次 200 判恢复", flush=True)
    pacer = Pacer(1.0 / max(args.probe_interval, 1.0), mode="uniform", floor=1.0)
    backoff = Backoff(trigger_n=999)
    decay_secs = min(7200, max(60, int(args.decay_max_mins * 60)))
    decay_budget = Budget(max_requests=min(200, HARD_MAX_REQUESTS - budget.n),
                          max_seconds=decay_secs)
    r = run_requests(t, site, ids_cache, cli_ids, pacer, decay_budget, backoff,
                     out, "decay", stop_on_block=False,
                     state={"recover_n": args.recover_n})
    budget.n += decay_budget.n
    r["boundary_rate"] = boundary
    r["recovered"] = r["ok"] >= args.recover_n
    r["safe_rate_suggested"] = round(max(args.start_rate, boundary - args.step_rate)
                                     * args.safe_factor, 3)
    results.append(r)
    return results


def budget_exceeded(budget: Budget) -> bool:
    return budget.n >= budget.max_requests or budget.elapsed >= budget.max_seconds


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="数据源频控边界压测探针(勿在生产出口运行)")
    ap.add_argument("--site", help="站点 key(见 --list-sites)")
    ap.add_argument("--phase", default="auto", choices=["auto", "ramp", "soak", "decay"])
    ap.add_argument("--rate", type=float, default=1.0, help="soak 目标速率 req/s")
    ap.add_argument("--duration", type=int, default=3600, help="soak 时长秒")
    ap.add_argument("--start-rate", type=float, default=0.5)
    ap.add_argument("--step-rate", type=float, default=0.5)
    ap.add_argument("--step-mins", type=float, default=10.0)
    ap.add_argument("--max-rate", type=float, default=3.0)
    ap.add_argument("--probe-interval", type=float, default=180.0, help="decay 轻探间隔秒")
    ap.add_argument("--decay-max-mins", type=float, default=120.0,
                    help="decay 恢复探测上限分钟(超时报未恢复)")
    ap.add_argument("--recover-n", type=int, default=5)
    ap.add_argument("--safe-factor", type=float, default=0.4)
    ap.add_argument("--ids", help="逗号分隔的 per_match id(覆盖 bootstrap/内置样本)")
    ap.add_argument("--max-requests", type=int, default=HARD_MAX_REQUESTS)
    ap.add_argument("--out", default="presstest_result.jsonl")
    ap.add_argument("--report", default="presstest_report.json")
    ap.add_argument("--list-sites", action="store_true")
    ap.add_argument("--go", action="store_true", help="缺省只打印计划不发请求")
    args = ap.parse_args()

    if args.list_sites or not args.site:
        for k, s in SITES.items():
            print(f"{k:16s} {s.description}")
        return
    site = get_site(args.site)
    args.max_rate = min(args.max_rate, HARD_MAX_RATE)
    args.max_requests = min(args.max_requests, HARD_MAX_REQUESTS)
    cli_ids = [x.strip() for x in args.ids.split(",") if x.strip()] if args.ids else []

    print(fmt_plan(args, site))
    print("⚠️ 请确认当前出口是一次性云 IP(GHA runner),不是生产出口!")
    if not args.go:
        print("(dry-run:未发任何请求;确认无误后加 --go 执行)")
        return

    t = Transport(site.session, site.headers)
    budget = Budget(max_requests=args.max_requests, max_seconds=args.duration * 3 + 6 * 3600)
    ids_cache = extract_bootstrap_ids(t, site, cli_ids)
    segments = []
    with open(args.out, "w", encoding="utf-8") as out:
        if args.phase == "auto":
            segments = phase_auto(t, site, ids_cache, cli_ids, args, out, budget)
        elif args.phase == "ramp":
            segments = phase_auto(t, site, ids_cache, cli_ids, args, out, budget)
        elif args.phase == "soak":
            # 绝对时刻调度:相对 pacer 会被 human floor(rate>1.25 时几乎全部钳到 0.8s)
            # +3% 阅读停顿+串行耗时叠加,名义速率从未真正达标(2026-10-12 实证:名义
            # 2.5 实测 1.007,首轮名义 1.5/1.2 实测 0.71/0.69)
            pacer = Pacer.from_rate(args.rate, mode="uniform", pct=0.10,
                                    floor=0.0, due_aligned=True)
            backoff = Backoff(trigger_n=3)
            # soak 必须按 duration 墙钟停止:此前误用共享预算(max_seconds=duration*3+6h),
            # 实际按 max_requests/速率跑满(如 1.5req/s→5.6h),撞 GHA 355min job 上限被掐、
            # artifacts 丢失(2026-10-11 两个 soak 均因此报废)
            soak_budget = Budget(max_requests=args.max_requests,
                                 max_seconds=args.duration)
            seg = run_requests(t, site, ids_cache, cli_ids, pacer, soak_budget,
                               backoff, out, f"soak@{args.rate:.2f}")
            budget.n += soak_budget.n
            seg["target_rate"] = args.rate
            seg["duration_s"] = args.duration
            segments = [seg]
        elif args.phase == "decay":
            pacer = Pacer(1.0 / max(args.probe_interval, 1.0), mode="uniform", floor=1.0)
            backoff = Backoff(trigger_n=999)
            segments = [run_requests(t, site, ids_cache, cli_ids, pacer, budget,
                                     backoff, out, "decay")]

    report = {"site": site.key, "phase": args.phase, "args": vars(args),
              "segments": segments,
              "generated_ts": int(time.time())}
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report["segments"], ensure_ascii=False, indent=2))
    print(f"[done] 明细→{args.out}  汇总→{args.report}")


if __name__ == "__main__":
    main()
