#!/usr/bin/env python3
"""governor.py — 拟人化请求节奏器 + 预算闸 + 退避梯(压测探针与未来各源刷新策略共用)

设计目标(2026-10-11 多源压测定案的前置件):
  · 每个数据源(即嗨四域 / 599 / 未来新增站)各持一个独立 Profile;
  · 刷新间隔做对数正态抖动(人类浏览的间隔分布右偏,中位数附近密集、偶发长停顿),
    外加「阅读停顿」——低概率插入 3~15 秒的长间歇,模拟真人看页面;
  · 保守余量:生产基准率 = 实测边界 × 安全系数(默认 0.4,见 README);
  · 退避梯:403/429 按连续次数升级暂停(2→10→30→60 分钟封顶),200 连续达标降级;
  · 预算闸:单次运行/单日的请求数与墙钟上限,双保险,到限即停。

本模块只依赖标准库;压测探针(probe.py)与未来 engine/collector 的逐源刷新
共用同一套节奏语义,保证「测出来的边界」与「生产跑的策略」是同一把尺子。
"""
from __future__ import annotations

import math
import random
import time


# ---------------------------------------------------------------- 节奏器
class Pacer:
    """按目标平均间隔产生抖动延迟。

    mode="uniform": ±pct 均匀小抖动——用于 ramp 阶段精确控率找边界;
    mode="human":  对数正态抖动 + 低概率阅读停顿——用于 soak 与生产刷新。
    delay = base * exp(-σ²/2) * exp(σ·z)  保证期望值恰为 base(σ 为对数标准差)。
    """

    def __init__(self, base_interval: float, *, mode: str = "human",
                 sigma: float = 0.35, pct: float = 0.10,
                 break_prob: float = 0.03, break_range: tuple = (3.0, 15.0),
                 floor: float = 0.8, seed: int | None = None):
        if base_interval <= 0:
            raise ValueError("base_interval must be > 0")
        self.base = float(base_interval)
        self.mode = mode
        self.sigma = float(sigma)
        self.pct = float(pct)
        self.break_prob = float(break_prob)
        self.break_range = break_range
        # floor 不得高于 base,否则目标速率永远达不到(找边界时会被钳住)
        self.floor = min(max(0.0, float(floor)), self.base)
        self.rng = random.Random(seed)

    def next_delay(self) -> float:
        if self.mode == "uniform":
            d = self.base * (1.0 + self.rng.uniform(-self.pct, self.pct))
        else:
            z = self.rng.gauss(0.0, 1.0)
            d = self.base * math.exp(-self.sigma ** 2 / 2) * math.exp(self.sigma * z)
            if self.rng.random() < self.break_prob:
                d += self.rng.uniform(*self.break_range)
        return max(self.floor, d)

    @classmethod
    def from_rate(cls, rate_per_sec: float, **kw) -> "Pacer":
        return cls(1.0 / rate_per_sec, **kw)


# ---------------------------------------------------------------- 预算闸
class Budget:
    """请求数 + 墙钟双上限。spend() 返回 False 表示预算耗尽,调用方必须停。"""

    def __init__(self, max_requests: int = 30000, max_seconds: float = 6 * 3600):
        self.max_requests = int(max_requests)
        self.max_seconds = float(max_seconds)
        self.n = 0
        self.t0 = time.time()

    def spend(self) -> bool:
        if self.n >= self.max_requests:
            return False
        if time.time() - self.t0 >= self.max_seconds:
            return False
        self.n += 1
        return True

    @property
    def elapsed(self) -> float:
        return time.time() - self.t0


# ---------------------------------------------------------------- 退避梯
class Backoff:
    """403/429 连续升级暂停;200 连续达标降一级。

    梯度(秒): 120 → 600 → 1800 → 3600 封顶——比 engine 现行的 90s 起步更保守,
    压测里触发闸门后本就该长歇;生产启用时以实测恢复时间为准重校。
    """

    LADDER = (120, 600, 1800, 3600)

    def __init__(self, trigger_n: int = 3, recover_n: int = 5):
        self.trigger_n = int(trigger_n)
        self.recover_n = int(recover_n)
        self.consec_block = 0
        self.consec_ok = 0
        self.level = 0
        self.pause_until = 0.0
        self.total_pauses = 0

    def on_block(self, now: float | None = None) -> float:
        """记录一次 403/429;达到 trigger_n 即升级暂停,返回暂停截止时刻。"""
        now = now if now is not None else time.time()
        self.consec_block += 1
        self.consec_ok = 0
        if self.consec_block >= self.trigger_n:
            self.level = min(self.level + 1, len(self.LADDER))
            self.pause_until = now + self.LADDER[self.level - 1]
            self.consec_block = 0
            self.total_pauses += 1
        return self.pause_until

    def on_ok(self) -> bool:
        """记录一次 200;连续 recover_n 次则降一级并清零,返回是否降级。"""
        self.consec_ok += 1
        self.consec_block = 0
        if self.consec_ok >= self.recover_n and self.level > 0:
            self.level -= 1
            self.consec_ok = 0
            self.pause_until = 0.0
            return True
        return False

    def is_blocked(self, now: float | None = None) -> bool:
        return time.time() < self.pause_until


# ---------------------------------------------------------------- 昼夜包络
# 生产建议(压测默认不用):北京时间深夜低活跃、傍晚高峰——乘数越小事越少。
DEFAULT_ENVELOPE = {h: (0.45 if 3 <= h < 8 else (1.0 if 8 <= h < 19 else 1.25))
                    for h in range(24)}


def envelope_multiplier(mult_table: dict[int, float] | None = None,
                        now: float | None = None) -> float:
    """按北京时间小时返回刷新率乘数;mult_table 缺省用 DEFAULT_ENVELOPE。"""
    table = mult_table if mult_table is not None else DEFAULT_ENVELOPE
    from datetime import datetime, timezone, timedelta
    tz8 = timezone(timedelta(hours=8))
    h = datetime.now(tz8).hour if now is None else datetime.fromtimestamp(now, tz8).hour
    return float(table.get(h, 1.0))


# ---------------------------------------------------------------- 源策略档
class SourceProfile:
    """一个数据源的完整刷新策略(生产形态;压测探针只用其中 Pacer/Backoff)。

    safe_factor: 生产基准率 = measured_boundary(实测首403速率) × safe_factor。
    0.4 表示留 2.5 倍余量;边界数据缺省时 rate 直接给保守绝对值。
    """

    def __init__(self, name: str, *, rate: float | None = None,
                 boundary: float | None = None, safe_factor: float = 0.4,
                 sigma: float = 0.35, break_prob: float = 0.03,
                 floor: float = 1.2, use_envelope: bool = True,
                 daily_budget: int = 60000):
        self.name = name
        if rate is None:
            if boundary is None:
                raise ValueError(f"{name}: rate 与 boundary 至少给一个")
            rate = boundary * safe_factor
        self.rate = float(rate)
        self.use_envelope = use_envelope
        self.daily_budget = int(daily_budget)
        self.pacer = Pacer.from_rate(self.rate, mode="human", sigma=sigma,
                                     break_prob=break_prob, floor=floor)
        self.backoff = Backoff()

    def effective_rate(self, now: float | None = None) -> float:
        r = self.rate
        if self.use_envelope:
            r *= envelope_multiplier(now=now)
        if self.backoff.is_blocked(now):
            r = 0.0
        return r

    def next_delay(self, now: float | None = None) -> float:
        if self.backoff.is_blocked(now):
            return max(self.backoff.pause_until - (now or time.time()), 1.0)
        r = self.effective_rate(now)
        if r <= 0:
            return 60.0
        return Pacer.from_rate(r, mode="human", sigma=self.pacer.sigma,
                               break_prob=self.pacer.break_prob,
                               floor=self.pacer.floor).next_delay()


if __name__ == "__main__":
    # 自测:不发网络请求
    p = Pacer(2.0, mode="human", seed=42)
    ds = [p.next_delay() for _ in range(20000)]
    mean = sum(ds) / len(ds)
    # human 均值 = base + 阅读停顿期望(额外加,方向保守:实际速率略低于名义)
    expect = 2.0 + p.break_prob * sum(p.break_range) / 2
    assert abs(mean - expect) < 0.15, f"human 均值偏离期望: {mean} vs {expect}"
    assert min(ds) >= p.floor
    p2 = Pacer.from_rate(1.0, mode="uniform", seed=1)
    d2 = [p2.next_delay() for _ in range(5000)]
    assert abs(1.0 / (sum(d2) / len(d2)) - 1.0) < 0.02
    # floor 不得钳住高速率:3 req/s(base=0.333)时 floor 必须缩到 base
    p3 = Pacer.from_rate(3.0, mode="uniform", floor=0.8, seed=2)
    assert p3.floor <= 1.0 / 3.0 + 1e-9
    b = Budget(max_requests=5)
    assert all(b.spend() for _ in range(5)) and not b.spend()
    bo = Backoff(trigger_n=3)
    for _ in range(3):
        bo.on_block()
    assert bo.level == 1 and bo.pause_until > 0
    for _ in range(5):
        bo.on_ok()
    assert bo.level == 0
    prof = SourceProfile("t", boundary=2.0)
    assert abs(prof.rate - 0.8) < 1e-9
    assert prof.next_delay() > 0
    print(f"governor 自测通过: human均值={mean:.3f}(期望={expect:.3f}), "
          f"uniform率={1.0/(sum(d2)/len(d2)):.4f}, boundary2.0→基准率{prof.rate}")
