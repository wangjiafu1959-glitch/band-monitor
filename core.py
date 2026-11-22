#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
估值 / 网格 / 仓位 核心引擎
数据源：蛋卷(雪球)指数估值 + 东方财富实时行情
纯标准库，无第三方依赖。
"""
import bisect
import datetime
import json
import os
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _get(url, timeout=20, tries=3, headers=None):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(1.0 + 1.5 * i)
    raise RuntimeError(f"请求失败 {url} :: {last}")


def _getj(url, **kw):
    return json.loads(_get(url, **kw))


# --------------------------------------------------------------------------- #
# 本地缓存：网络抖动或云端 IP 被限流时，用最近一次成功的数据继续工作
# --------------------------------------------------------------------------- #
CACHE_PATH = os.path.join(BASE, "cache.json")


def _cache_read():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("data", {})
    except Exception:                             # noqa: BLE001
        return {}


def _cache_write(key, value):
    try:
        data = _cache_read()
        data[key] = value
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                       "data": data}, f, ensure_ascii=False)
    except Exception:                             # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 实时行情（东方财富批量接口，一次拿到所有标的）
# --------------------------------------------------------------------------- #
def fetch_quotes(secids):
    """secids: ['0.399006', ...] -> {secid: {name, price, pct, prev}}
    东财优先，缺谁就补谁（批量接口偶尔漏个别代码，漏了会导致持仓市值算成 0）。"""
    if not secids:
        return {}
    out = {}
    try:
        out = _quotes_em(secids)
    except Exception as e:                       # noqa: BLE001
        print("[warn] 东财行情失败，切新浪: %r" % (e,))

    missing = [s for s in secids if not (out.get(s) or {}).get("price")]
    if missing:
        try:
            out.update(_quotes_sina(missing))
        except Exception as e:                   # noqa: BLE001
            print("[warn] 新浪批量行情失败: %r" % (e,))

    # 还缺的，逐个补
    still = [s for s in secids if not (out.get(s) or {}).get("price")]
    for s in still:
        for fn in (_quotes_em, _quotes_sina):
            try:
                got = fn([s])
                if (got.get(s) or {}).get("price"):
                    out.update(got)
                    break
            except Exception:                    # noqa: BLE001
                continue
        else:
            print("[warn] 行情取不到: %s" % s)
    return out


def _quotes_em(secids):
    out = {}
    for start in range(0, len(secids), 30):
        chunk = secids[start:start + 30]
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=%s"
               "&fields=f1,f12,f13,f14,f2,f3,f18" % ",".join(chunk))
        data = _getj(url).get("data") or {}
        rows = data.get("diff") or []
        if not rows:
            raise RuntimeError("东财返回空")
        for row in rows:
            secid = "%s.%s" % (row.get("f13"), row.get("f12"))
            dec = row.get("f1", 2)
            scale = 10 ** int(dec if dec is not None else 2)
            price = row.get("f2")
            prev = row.get("f18")
            out[secid] = {
                "name": row.get("f14") or "",
                "price": (price / scale) if isinstance(price, (int, float)) else None,
                "prev": (prev / scale) if isinstance(prev, (int, float)) else None,
                "pct": (row.get("f3") / 100.0) if isinstance(row.get("f3"), (int, float)) else None,
            }
    return out


def _is_index(code):
    return code[:3] in ("000", "399", "899", "930", "931", "932", "933", "906", "hq")


def _quotes_sina(secids):
    symbols = []
    for s in secids:
        mkt, code = s.split(".")
        pre = "sh" if mkt == "1" else "sz"
        symbols.append(("s_" + pre + code) if _is_index(code) else (pre + code))
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    req = urllib.request.Request(url, headers={
        "User-Agent": HEADERS["User-Agent"], "Referer": "https://finance.sina.com.cn"})
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode("gbk", "replace")
    out = {}
    for line, secid in zip([l for l in text.split(";") if "hq_str" in l], symbols):
        try:
            payload = line.split('="', 1)[1].rstrip('"\n')
            f = payload.split(",")
            if len(f) < 4:
                continue
            code = (secid[2:] if secid.startswith("s_") else secid)[2:]
            base = secid[2:] if secid.startswith("s_") else secid
            mkt = "1" if base[:2] == "sh" else "0"
            if secid.startswith("s_"):
                price, pct, prev = float(f[1]), float(f[3]), None
            else:
                price, prev = float(f[3]), float(f[2])
                pct = (price / prev - 1) * 100 if prev else 0.0
            out["%s.%s" % (mkt, code)] = {"name": f[0], "price": price,
                                          "prev": prev, "pct": pct}
        except Exception:                        # noqa: BLE001
            continue
    if not out:
        raise RuntimeError("新浪行情也失败")
    return out


# --------------------------------------------------------------------------- #
# 指数估值（PE/PB 及十年分位）
# --------------------------------------------------------------------------- #
_EVA_CACHE = {}


def fetch_eva(index_code):
    if index_code in _EVA_CACHE:
        return _EVA_CACHE[index_code]
    url = "https://danjuanfunds.com/djapi/index_eva/detail/%s" % index_code
    try:
        data = _getj(url, headers={"User-Agent": HEADERS["User-Agent"],
                                   "Referer": "https://danjuanfunds.com/"})
        d = data.get("data")
        if not d:
            raise RuntimeError("估值接口无数据: %s -> %s" % (index_code, data))
        _cache_write("eva:" + index_code, d)
    except Exception as e:                        # noqa: BLE001
        d = _cache_read().get("eva:" + index_code)
        if not d:
            raise
        print("[warn] %s 估值取数失败，用缓存(%s): %r" % (index_code, d.get("ts"), e))
    _EVA_CACHE[index_code] = d
    return d


# --------------------------------------------------------------------------- #
# 指数历史日线（近 N 年收盘价）
# --------------------------------------------------------------------------- #
def fetch_kline(secid, years=10):
    """历史日线：东财优先，失败自动切新浪，都失败用本地缓存"""
    last_err = None
    for source in (_kline_em, _kline_sina):
        try:
            series = source(secid, years)
            if len(series) < 300:
                raise RuntimeError("数据太少: %d 条" % len(series))
            _cache_write("kline:" + secid, [[d, p] for d, p in series])
            return series
        except Exception as e:                     # noqa: BLE001
            last_err = e
    cached = _cache_read().get("kline:" + secid)
    if not cached:
        raise last_err if last_err else RuntimeError("无历史数据: " + secid)
    print("[warn] %s 历史行情取数失败，用缓存(%d 条): %r" % (secid, len(cached), last_err))
    return [(d, float(p)) for d, p in cached]


def _parse_klines(klines):
    series = []
    for s in klines:
        parts = s.split(",")
        try:
            series.append((parts[0], float(parts[1])))
        except (IndexError, ValueError):
            continue
    return series


def _kline_em(secid, years=10):
    beg = (datetime.date.today() - datetime.timedelta(days=int(365.25 * years) + 30)).strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=1&beg=%s&end=20500101"
           % (secid, beg))
    data = _getj(url, timeout=12, tries=2).get("data") or {}
    return _parse_klines(data.get("klines") or [])


def _kline_sina(secid, years=10):
    mkt, code = secid.split(".")
    if mkt == "1":
        sym = "sh" + code
    elif mkt == "0":
        sym = "sz" + code
    else:
        raise RuntimeError("新浪不支持的代码: " + secid)
    n = min(int(250 * years), 4000)
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (sym, n))
    text = _get(url, timeout=20, tries=2,
                headers={"User-Agent": HEADERS["User-Agent"],
                         "Referer": "https://finance.sina.com.cn"})
    arr = json.loads(text)
    return [(x["day"], float(x["close"])) for x in arr if x.get("close")]


# --------------------------------------------------------------------------- #
# 估值分位 <-> 点位 的换算模型
#
# 问题：估值分位是"当前 PE/PB 在十年历史中的排名"，但盘中只有价格。
# 做法：PB = 价格 / 每股净资产，净资产变动缓慢（每季一次），所以
#       PB_t ≈ P_t * (1+g)^(age_t)，g 为净资产年化增速。
# 用二分法校准 g，使"按 P_t 去趋势后的序列"复现接口给的当前分位，
# 标定后即可把任意目标分位换算成点位，也可把盘中点位换算成分位。
# 这是近似模型（真实净资产增速不是常数），误差约 1-3 个百分点分位；
# 每天收盘后用最新估值重新标定一次，档位线因此自动跟随基本面漂移。
# --------------------------------------------------------------------------- #
def detrended(series, g):
    end = datetime.date.fromisoformat(series[-1][0])
    out = []
    for d, p in series:
        age = (end - datetime.date.fromisoformat(d)).days / 365.0
        out.append(p * (1.0 + g) ** age)
    out.sort()
    return out


def _pct(sorted_vals, x):
    return bisect.bisect_right(sorted_vals, x) / float(len(sorted_vals))


def calibrate_g(series, target_pct, lo=-0.05, hi=0.30):
    """求 g 使 _pct(detrended(series, g), P_now) == target_pct（单调，二分）"""
    target_pct = min(max(target_pct, 1e-4), 0.9999)
    x = series[-1][1]
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if _pct(detrended(series, mid), x) > target_pct:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def price_at(sorted_vals, q):
    """分位 q -> 点位（线性插值）"""
    if not sorted_vals:
        return None
    q = min(max(q, 0.0), 1.0)
    pos = q * (len(sorted_vals) - 1)
    i = int(pos)
    if i >= len(sorted_vals) - 1:
        return sorted_vals[-1]
    frac = pos - i
    return sorted_vals[i] * (1 - frac) + sorted_vals[i + 1] * frac


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def load_config(path=None):
    path = path or os.path.join(BASE, "config.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
# 组合层：估值 -> 权重 -> 预算
# --------------------------------------------------------------------------- #
def composite_pct(ev, w_pe=0.6, w_pb=0.4):
    return w_pe * float(ev["pe_percentile"]) + w_pb * float(ev["pb_percentile"])


def allocate(targets, evas, total, invest_ratio=0.90, anchor=70.0, cap=0.55):
    """
    两种模式：
      1) 显式权重：targets 里每个都写了 weight，就直接用（剩余为现金/预备金）
      2) 估值驱动：权重 ∝ max(anchor - 综合分位)，单标的封顶 cap
    """
    if targets and all(t.get("weight") is not None for t in targets):
        w = [float(t["weight"]) for t in targets]
        return w, [total * x for x in w]

    raw = []
    for t in targets:
        comp = composite_pct(evas[t["id"]])
        raw.append(max(anchor - comp * 100.0, 0.5))
    s = sum(raw)
    w = [r / s for r in raw]
    for _ in range(20):
        over = [i for i, x in enumerate(w) if x > cap + 1e-9]
        if not over:
            break
        for i in over:
            w[i] = cap
        free = [i for i in range(len(w)) if i not in over]
        rest = 1.0 - cap * len(over)
        sub = sum(w[i] for i in free)
        if sub <= 1e-9 or not free:
            break
        for i in free:
            w[i] = w[i] / sub * rest
    budgets = [total * invest_ratio * x for x in w]
    return w, budgets


# --------------------------------------------------------------------------- #
# 模型构建 + 网格表
# --------------------------------------------------------------------------- #
def build_model(target, years=10):
    ev = fetch_eva(target["index_code"])
    secid = target.get("kline_secid") or target["index_secid"]
    series = fetch_kline(secid, years=years)
    comp = composite_pct(ev)
    g = calibrate_g(series, comp)
    d = detrended(series, g)
    return {
        "ev": ev,
        "series": series,
        "comp": comp,
        "g": g,
        "detrended": d,
        "price_now_hist": series[-1][1],
        "history_n": len(series),
        "history_from": series[0][0],
    }


def grid_rows(model, budget, bands):
    rows = []
    for b in bands:
        lo, hi = float(b["pct_low"]), float(b["pct_high"])
        entry_q = hi / 100.0
        rows.append({
            "label": b["label"],
            "pct_low": lo,
            "pct_high": hi,
            "ratio": float(b["ratio"]),
            "target_value": budget * float(b["ratio"]),
            "entry_index_price": price_at(model["detrended"], entry_q),
        })
    rows.sort(key=lambda r: -r["entry_index_price"])
    prev = 0.0
    for r in rows:                     # 自上而下，“跨档买入额”= 降到本档时要补多少
        r["delta_in"] = r["target_value"] - prev
        prev = r["target_value"]
    return rows


def band_of(comp, bands):
    p = comp * 100.0
    for b in bands:
        if float(b["pct_low"]) <= p < float(b["pct_high"]):
            return b
    return bands[-1]


def analyze(cfg):
    """一次算完：估值、权重、预算、网格、当前动作。"""
    targets = cfg["targets"]
    bands = cfg["bands"]
    engine = cfg.get("engine", {})
    years = engine.get("history_years", 10)
    w_pe = engine.get("w_pe", 0.6)
    w_pb = engine.get("w_pb", 0.4)
    invest_ratio = engine.get("invest_ratio", 0.90)
    anchor = engine.get("weight_anchor", 70.0)
    cap = engine.get("max_single_weight", 0.55)

    models, evas = {}, {}
    for t in targets:
        m = build_model(t, years=years)
        m["comp"] = w_pe * float(m["ev"]["pe_percentile"]) + w_pb * float(m["ev"]["pb_percentile"])
        models[t["id"]] = m
        evas[t["id"]] = m["ev"]

    total = float(cfg["capital"]["total"])
    weights, budgets = allocate(targets, evas, total, invest_ratio, anchor, cap)

    quotes = fetch_quotes([t["index_secid"] for t in targets if t.get("index_secid")] +
                          [t["etf_secid"] for t in targets if t.get("etf_secid")])

    for i, t in enumerate(targets):
        m = models[t["id"]]
        m["weight"] = weights[i]
        m["budget"] = budgets[i]
        basis_etf = not t.get("index_secid")
        q_idx = quotes.get(t["index_secid"] or t["etf_secid"]) or {}
        q_etf = quotes.get(t["etf_secid"]) or {}
        m["index_quote"] = q_idx
        m["etf_quote"] = q_etf
        m["price_basis_etf"] = basis_etf
        m["unit"] = "ETF价" if basis_etf else "指数点"
        m["etf_ratio"] = 1.0 if basis_etf else (
            ((q_etf.get("price") or 0) / q_idx["price"]) if q_idx.get("price") else None)
        m["bands"] = grid_rows(m, budgets[i], bands)
        m["band_now"] = band_of(m["comp"], bands)
        hold_shares = float(t.get("hold_shares") or 0)
        etf_px = q_etf.get("price") or 0
        m["hold_value"] = hold_shares * etf_px
        m["target_value"] = budgets[i] * float(m["band_now"]["ratio"])
        m["action_value"] = m["target_value"] - m["hold_value"]
        m["min_action"] = max(500.0, budgets[i] * float(engine.get("min_action_pct", 0.05)))
        m["min_sell"] = max(2000.0, budgets[i] * float(engine.get("min_sell_pct", 0.10)))

    reserve = total - sum(budgets)
    cash = total - sum(m["hold_value"] for m in models.values())
    planned = sum(m["action_value"] for m in models.values() if m["action_value"] > 0)

    return {
        "total": total,
        "cash": cash,
        "planned_buy": planned,
        "cash_left": cash - planned,
        "invest_ratio": invest_ratio,
        "reserve": reserve,
        "models": models,
        "targets": targets,
        "quotes": quotes,
    }


def money(x):
    return format(int(round(x)), ",d")


if __name__ == "__main__":
    cfg = load_config()
    res = analyze(cfg)
    for t in res["targets"]:
        m = res["models"][t["id"]]
        print("%s 综合分位 %.1f%%  权重 %.1f%%  预算 %s" %
              (t["name"], m["comp"] * 100, m["weight"] * 100, money(m["budget"])))

