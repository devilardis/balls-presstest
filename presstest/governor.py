#!/usr/bin/env python3
"""governor.py 鈥?鎷熶汉鍖栬姹傝妭濂忓櫒 + 棰勭畻闂?+ 閫€閬挎(鍘嬫祴鎺㈤拡涓庢湭鏉ュ悇婧愬埛鏂扮瓥鐣ュ叡鐢?

璁捐鐩爣(2026-10-11 澶氭簮鍘嬫祴瀹氭鐨勫墠缃欢):
  路 姣忎釜鏁版嵁婧?鍗冲棬鍥涘煙 / 599 / 鏈潵鏂板绔?鍚勬寔涓€涓嫭绔?Profile;
  路 鍒锋柊闂撮殧鍋氬鏁版鎬佹姈鍔?浜虹被娴忚鐨勯棿闅斿垎甯冨彸鍋?涓綅鏁伴檮杩戝瘑闆嗐€佸伓鍙戦暱鍋滈】),
    澶栧姞銆岄槄璇诲仠椤裤€嶁€斺€斾綆姒傜巼鎻掑叆 3~15 绉掔殑闀块棿姝?妯℃嫙鐪熶汉鐪嬮〉闈?
  路 淇濆畧浣欓噺:鐢熶骇鍩哄噯鐜?= 瀹炴祴杈圭晫 脳 瀹夊叏绯绘暟(榛樿 0.4,瑙?README);
  路 閫€閬挎:403/429 鎸夎繛缁鏁板崌绾ф殏鍋?2鈫?0鈫?0鈫?0 鍒嗛挓灏侀《),200 杩炵画杈炬爣闄嶇骇;
  路 棰勭畻闂?鍗曟杩愯/鍗曟棩鐨勮姹傛暟涓庡閽熶笂闄?鍙屼繚闄?鍒伴檺鍗冲仠銆?
鏈ā鍧楀彧渚濊禆鏍囧噯搴?鍘嬫祴鎺㈤拡(probe.py)涓庢湭鏉?engine/collector 鐨勯€愭簮鍒锋柊
鍏辩敤鍚屼竴濂楄妭濂忚涔?淇濊瘉銆屾祴鍑烘潵鐨勮竟鐣屻€嶄笌銆岀敓浜ц窇鐨勭瓥鐣ャ€嶆槸鍚屼竴鎶婂昂瀛愩€?"""
from __future__ import annotations

import math
import random
import time


# ---------------------------------------------------------------- 鑺傚鍣?class Pacer:
    """鎸夌洰鏍囧钩鍧囬棿闅斾骇鐢熸姈鍔ㄥ欢杩熴€?
    mode="uniform": 卤pct 鍧囧寑灏忔姈鍔ㄢ€斺€旂敤浜?ramp 闃舵绮剧‘鎺х巼鎵捐竟鐣?
    mode="human":  瀵规暟姝ｆ€佹姈鍔?+ 浣庢鐜囬槄璇诲仠椤库€斺€旂敤浜?soak 涓庣敓浜у埛鏂般€?    delay = base * exp(-蟽虏/2) * exp(蟽路z)  淇濊瘉鏈熸湜鍊兼伆涓?base(蟽 涓哄鏁版爣鍑嗗樊)銆?    """

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
        # floor 涓嶅緱楂樹簬 base,鍚﹀垯鐩爣閫熺巼姘歌繙杈句笉鍒?鎵捐竟鐣屾椂浼氳閽充綇)
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


# ---------------------------------------------------------------- 棰勭畻闂?class Budget:
    """璇锋眰鏁?+ 澧欓挓鍙屼笂闄愩€俿pend() 杩斿洖 False 琛ㄧず棰勭畻鑰楀敖,璋冪敤鏂瑰繀椤诲仠銆?""

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


# ---------------------------------------------------------------- 閫€閬挎
class Backoff:
    """403/429 杩炵画鍗囩骇鏆傚仠;200 杩炵画杈炬爣闄嶄竴绾с€?
    姊害(绉?: 120 鈫?600 鈫?1800 鈫?3600 灏侀《鈥斺€旀瘮 engine 鐜拌鐨?90s 璧锋鏇翠繚瀹?
    鍘嬫祴閲岃Е鍙戦椄闂ㄥ悗鏈氨璇ラ暱姝?鐢熶骇鍚敤鏃朵互瀹炴祴鎭㈠鏃堕棿涓哄噯閲嶆牎銆?    """

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
        """璁板綍涓€娆?403/429;杈惧埌 trigger_n 鍗冲崌绾ф殏鍋?杩斿洖鏆傚仠鎴鏃跺埢銆?""
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
        """璁板綍涓€娆?200;杩炵画 recover_n 娆″垯闄嶄竴绾у苟娓呴浂,杩斿洖鏄惁闄嶇骇銆?""
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


# ---------------------------------------------------------------- 鏄煎鍖呯粶
# 鐢熶骇寤鸿(鍘嬫祴榛樿涓嶇敤):鍖椾含鏃堕棿娣卞浣庢椿璺冦€佸倣鏅氶珮宄扳€斺€斾箻鏁拌秺灏忎簨瓒婂皯銆?DEFAULT_ENVELOPE = {h: (0.45 if 3 <= h < 8 else (1.0 if 8 <= h < 19 else 1.25))
                    for h in range(24)}


def envelope_multiplier(mult_table: dict[int, float] | None = None,
                        now: float | None = None) -> float:
    """鎸夊寳浜椂闂村皬鏃惰繑鍥炲埛鏂扮巼涔樻暟;mult_table 缂虹渷鐢?DEFAULT_ENVELOPE銆?""
    table = mult_table if mult_table is not None else DEFAULT_ENVELOPE
    from datetime import datetime, timezone, timedelta
    tz8 = timezone(timedelta(hours=8))
    h = datetime.now(tz8).hour if now is None else datetime.fromtimestamp(now, tz8).hour
    return float(table.get(h, 1.0))


# ---------------------------------------------------------------- 婧愮瓥鐣ユ。
class SourceProfile:
    """涓€涓暟鎹簮鐨勫畬鏁村埛鏂扮瓥鐣?鐢熶骇褰㈡€?鍘嬫祴鎺㈤拡鍙敤鍏朵腑 Pacer/Backoff)銆?
    safe_factor: 鐢熶骇鍩哄噯鐜?= measured_boundary(瀹炴祴棣?03閫熺巼) 脳 safe_factor銆?    0.4 琛ㄧず鐣?2.5 鍊嶄綑閲?杈圭晫鏁版嵁缂虹渷鏃?rate 鐩存帴缁欎繚瀹堢粷瀵瑰€笺€?    """

    def __init__(self, name: str, *, rate: float | None = None,
                 boundary: float | None = None, safe_factor: float = 0.4,
                 sigma: float = 0.35, break_prob: float = 0.03,
                 floor: float = 1.2, use_envelope: bool = True,
                 daily_budget: int = 60000):
        self.name = name
        if rate is None:
            if boundary is None:
                raise ValueError(f"{name}: rate 涓?boundary 鑷冲皯缁欎竴涓?)
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


# ---------------------------------------------------------------- 瀹炴祴鍥炲～妗?# 2026-10-11 浜旂珯 ramp 瀹炴祴鍥炲～(GHA runner 鍗曡疆鍗?IP,鎺㈤拡纭《 3.0 req/s):
#   jihai_dc      5077 璇锋眰 闆?403(run 38113189305)
#   f599_rest     5405 璇锋眰 闆?403(run 38113194699)
#   jihai_www     涓よ疆鍏?7268 璇锋眰 闆?403;502 鍗?~3% 涓斾笌閫熺巼鏃犲叧
#                 (鍒楄〃椤佃儗鏅姈鍔?闈炲皝閿?(run 38113212693 + 38113323142)
#   jihai_apinode 4820 璇锋眰 闆?403;204 鍗?~8.3% 鎭掑畾,涓洪檲鏃ф牱鏈満娆℃棤鏁版嵁,
#                 闈炲皝閿?run 38113323142)
#   jihai_apilive 3796 璇锋眰 鍏?200(run 38113323142)
# 鏁呬簲绔欒竟鐣屽潎璁颁笅鐣?3.0(鐪熷疄杈圭晫 鈮?.0,鏈Е椤?,鐢熶骇鍩哄噯鐜?= 3.0 脳 0.4
# = 1.2 req/s;鍏ㄧ▼闆?403 鏃犳仮澶嶆椂闂存暟鎹?Backoff 姊害娌跨敤榛樿銆?PROFILES: dict[str, SourceProfile] = {
    "jihai_dc":      SourceProfile("jihai_dc",      boundary=3.0),
    "f599_rest":     SourceProfile("f599_rest",     boundary=3.0),
    "jihai_www":     SourceProfile("jihai_www",     boundary=3.0),
    "jihai_apinode": SourceProfile("jihai_apinode", boundary=3.0),
    "jihai_apilive": SourceProfile("jihai_apilive", boundary=3.0),
}


if __name__ == "__main__":
    # 鑷祴:涓嶅彂缃戠粶璇锋眰
    p = Pacer(2.0, mode="human", seed=42)
    ds = [p.next_delay() for _ in range(20000)]
    mean = sum(ds) / len(ds)
    # human 鍧囧€?= base + 闃呰鍋滈】鏈熸湜(棰濆鍔?鏂瑰悜淇濆畧:瀹為檯閫熺巼鐣ヤ綆浜庡悕涔?
    expect = 2.0 + p.break_prob * sum(p.break_range) / 2
    assert abs(mean - expect) < 0.15, f"human 鍧囧€煎亸绂绘湡鏈? {mean} vs {expect}"
    assert min(ds) >= p.floor
    p2 = Pacer.from_rate(1.0, mode="uniform", seed=1)
    d2 = [p2.next_delay() for _ in range(5000)]
    assert abs(1.0 / (sum(d2) / len(d2)) - 1.0) < 0.02
    # floor 涓嶅緱閽充綇楂橀€熺巼:3 req/s(base=0.333)鏃?floor 蹇呴』缂╁埌 base
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
    print(f"governor 鑷祴閫氳繃: human鍧囧€?{mean:.3f}(鏈熸湜={expect:.3f}), "
          f"uniform鐜?{1.0/(sum(d2)/len(d2)):.4f}, boundary2.0鈫掑熀鍑嗙巼{prof.rate}")
