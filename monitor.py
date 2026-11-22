#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
只做三件事
  ① 每天 15:05 发一封「四个标的的档位表」
  ② 盘中任何一个标的掉到新档位 → 立刻提醒
  ③ 创业板 / 沪深300 当日跌幅超过 2% → 立刻提醒（每天一次）

  python monitor.py          正常跑
  python monitor.py --once   只查一次
  python monitor.py --close  立刻发一次档位表（测试）
"""
import datetime
import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import core                                    # noqa: E402  估值/档位计算

CFG = os.path.join(BASE, "config.json")
SEC = os.path.join(BASE, "secrets.json")
STATE = os.path.join(BASE, "state.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      "Referer": "https://quote.eastmoney.com/"}
TZ = datetime.timezone(datetime.timedelta(hours=8))


def now():
    return datetime.datetime.now(TZ)


def today():
    return now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
def _get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def quotes(secids):
    out = {}
    try:
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=%s"
               "&fields=f1,f12,f13,f14,f2,f3,f18" % ",".join(secids))
        data = json.loads(_get(url).decode("utf-8", "replace"))
        for row in (data.get("data") or {}).get("diff") or []:
            sid = "%s.%s" % (row.get("f13"), row.get("f12"))
            scale = 10 ** int(row.get("f1", 2) or 2)
            px = row.get("f2")
            out[sid] = {"name": row.get("f14") or "",
                        "price": (px / scale) if isinstance(px, (int, float)) else None,
                        "pct": (row.get("f3") / 100.0) if isinstance(row.get("f3"), (int, float)) else 0.0}
    except Exception:                          # noqa: BLE001
        pass

    missing = [s for s in secids if not (out.get(s) or {}).get("price")]
    if missing:
        try:
            sym_map = {}
            for sid in missing:
                mkt, code = sid.split(".")
                pre = "sh" if mkt == "1" else "sz"
                is_idx = code[:3] in ("000", "399")
                sym_map[("s_" + pre + code) if is_idx else (pre + code)] = (sid, is_idx)
            url = "https://hq.sinajs.cn/list=" + ",".join(sym_map.keys())
            txt = _get(url, headers={"User-Agent": UA["User-Agent"],
                                     "Referer": "https://finance.sina.com.cn"}).decode("gbk", "replace")
            for line in [l for l in txt.split(";") if "hq_str" in l]:
                sym = line.split("hq_str_")[1].split("=")[0]
                if sym not in sym_map:
                    continue
                sid, is_idx = sym_map[sym]
                f = line.split('="', 1)[1].rstrip('"\n').split(",")
                if is_idx:
                    px, pct = float(f[1]), float(f[3])
                else:
                    px = float(f[3])
                    prev = float(f[2]) or px
                    pct = (px / prev - 1) * 100 if prev else 0.0
                out[sid] = {"name": f[0], "price": px, "pct": pct}
        except Exception:                      # noqa: BLE001
            pass
    return out


# --------------------------------------------------------------------------- #
def build_levels(cfg):
    out = {}
    for t in cfg["targets"]:
        mode = t.get("mode", "price")
        q = quotes([t["quote_secid"], t["etf_secid"]])
        qp = (q.get(t["quote_secid"]) or {}).get("price")
        ep = (q.get(t["etf_secid"]) or {}).get("price")
        ratio = (ep / qp) if (qp and ep) else 1.0

        if mode == "valuation":
            m = core.build_model({"index_code": t["index_code"], "index_secid": t["quote_secid"]})
            rows = core.grid_rows(m, float(t.get("budget", 0)), cfg["bands"])
            levels = [{"label": r["label"], "line": r["entry_index_price"],
                       "shares": None, "amt": r["target_value"]} for r in rows]
            comp = m["comp"]
        else:
            step = float(t["step"])
            anchor = float(t["anchor_point"])
            per = float(t["shares_per_step"])
            base_sh = float(t["anchor_shares"])
            cap = float(t.get("max_shares", 10 ** 9))
            is_yuan = t.get("unit") == "元"
            levels = []
            for k in range(-8, 9):
                line = anchor + step * k
                sh = max(0.0, min(cap, base_sh - k * per))
                levels.append({"label": ("%.2f" % line) if is_yuan else ("%.0f" % line),
                               "line": line, "shares": sh, "amt": sh * line * ratio})
            levels.sort(key=lambda r: -r["line"])
            comp = None

        out[t["id"]] = {"rows": levels, "ratio": ratio, "comp": comp, "mode": mode}
    return out


def level_of(info, price):
    """算术档（每 N 点/元）取最近格点；估值档按区间归属"""
    rows = info["rows"]
    if not rows:
        return None
    if info.get("mode") in ("pe", "price"):
        return min(rows, key=lambda r: abs(r["line"] - price))
    if price >= rows[0]["line"]:
        return rows[0]
    for i, r in enumerate(rows):
        lower = rows[i + 1]["line"] if i + 1 < len(rows) else float("-inf")
        if lower <= price < r["line"]:
            return r
    return rows[-1]


def fmt(x):
    return format(int(round(x)), ",")


# --------------------------------------------------------------------------- #
def email_channel():
    """优先 secrets.json，其次环境变量（GitHub Secrets）"""
    try:
        with open(SEC, "r", encoding="utf-8") as f:
            ch = (json.load(f).get("channels") or [{}])[0]
        if ch.get("user"):
            return ch
    except Exception:                          # noqa: BLE001
        pass
    user = os.environ.get("EMAIL_USER")
    if not user:
        return None
    return {"type": "email", "user": user,
            "password": os.environ.get("EMAIL_PASS", ""),
            "to": os.environ.get("EMAIL_TO") or user,
            "host": os.environ.get("EMAIL_HOST") or "smtp.qq.com",
            "port": os.environ.get("EMAIL_PORT") or "465"}


def send_email(cfg, title, text):
    if not cfg.get("notify", {}).get("email", True):
        return
    ch = email_channel()
    if not ch:
        print("[warn] 没有邮件配置（secrets.json 或环境变量）")
        return
    html = ("<div style='font-family:Consolas,Microsoft YaHei;font-size:15px;"
            "line-height:1.7;white-space:pre-wrap'>%s</div>"
            % text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((str(Header("档位监控", "utf-8")), ch["user"]))
    msg["To"] = ch.get("to") or ch["user"]
    port = int(ch.get("port") or 465)
    ctx = ssl.create_default_context()
    try:
        srv = (smtplib.SMTP_SSL(ch.get("host", "smtp.qq.com"), port, timeout=20, context=ctx)
               if port == 465 else smtplib.SMTP(ch.get("host", "smtp.qq.com"), port, timeout=20))
        srv.login(ch["user"], ch["password"])
        srv.sendmail(ch["user"], [ch.get("to") or ch["user"]], msg.as_string())
        srv.quit()
        print("      [邮件已发] %s" % title)
    except Exception as e:                     # noqa: BLE001
        print("      [邮件失败] %r" % (e,))


def beep(cfg):
    if cfg.get("notify", {}).get("sound", True):
        for _ in range(3):
            print("\a", end="", flush=True)
            time.sleep(0.2)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                          # noqa: BLE001
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def in_session(cfg):
    n = now()
    if n.weekday() >= 5:
        return False
    th = cfg.get("trade_hours", {})

    def m(s):
        a, b = s.split(":")
        return int(a) * 60 + int(b)

    hm = n.hour * 60 + n.minute
    return (m(th.get("start", "09:30")) <= hm <= m(th.get("end", "15:00"))) and not \
           (m(th.get("lunch_start", "11:30")) < hm < m(th.get("lunch_end", "13:00")))


# --------------------------------------------------------------------------- #
def close_report(cfg, levels, q, state, force=False):
    if not force and state.get("closed") == today():
        print("  [收盘表] 今天已发过，跳过")
        return
    lines = ["## 今日档位表 %s" % today(), ""]
    for t in cfg["targets"]:
        info = levels.get(t["id"])
        qq = q.get(t["quote_secid"]) or {}
        qe = q.get(t["etf_secid"]) or {}
        px = qq.get("price")
        if not px or not info:
            continue
        cur = level_of(info, px)
        etf_px = qe.get("price") or 0
        hold_amt = float(t.get("shares", 0)) * etf_px
        diff = cur["amt"] - hold_amt
        act = ("**买 %s 元**" % fmt(diff)) if diff > 100 else \
              (("**卖 %s 元**" % fmt(-diff)) if diff < -100 else "不动")
        lines.append("**%s**（%s）　%.2f（%+.2f%%）" % (t["name"], t["etf_code"], px, qq.get("pct") or 0))
        lines.append("　档位 %s ｜ 目标 %s 元 ｜ 持仓 %s 元 ｜ %s"
                     % (cur["label"], fmt(cur["amt"]), fmt(hold_amt), act))
        lines.append("")
    send_email(cfg, "今日档位表 %s" % today(), "\n".join(lines))
    state["closed"] = today()
    save_json(STATE, state)
    print("  [收盘表] 已发送")


def check(cfg, levels, state, quiet=False):
    secids = []
    for t in cfg["targets"]:
        secids += [t["quote_secid"], t["etf_secid"]]
    q = quotes(secids)

    for t in cfg["targets"]:
        info = levels.get(t["id"])
        qq = q.get(t["quote_secid"]) or {}
        qe = q.get(t["etf_secid"]) or {}
        px = qq.get("price")
        if not px or not info:
            continue
        pct = qq.get("pct") or 0
        cur = level_of(info, px)
        etf_px = qe.get("price") or 0
        hold_amt = float(t.get("shares", 0)) * etf_px
        diff = cur["amt"] - hold_amt

        if not quiet:
            print("[%s] %-6s %9.2f (%+5.2f%%) 档位 %-9s 目标 %9s 持仓 %9s %s"
                  % (now().strftime("%H:%M:%S"), t["name"], px, pct, cur["label"],
                     fmt(cur["amt"]), fmt(hold_amt),
                     ("买 " + fmt(diff)) if diff > 100 else (("卖 " + fmt(-diff)) if diff < -100 else "对齐")))

        # ① 档位变化
        last = state.setdefault("level", {}).get(t["id"])
        if last != cur["label"]:
            state["level"][t["id"]] = cur["label"]
            if last is not None:
                if abs(diff) <= 100:
                    act_txt = "不需要操作"
                else:
                    act_txt = "%s %s 元" % ("买入" if diff > 0 else "卖出", fmt(abs(diff)))
                text = ("%s 掉档\n\n%s 现价 %.2f（%+.2f%%）\n"
                        "档位：%s → **%s**\n\n"
                        "该档目标：%s 元\n当前持仓：%s 元（%s 股）\n"
                        "**%s**"
                        % (t["name"], t["name"], px, pct, last, cur["label"],
                           fmt(cur["amt"]), fmt(hold_amt), fmt(t.get("shares", 0)), act_txt))
                print("\n" + "=" * 56 + "\n" + text + "\n" + "=" * 56)
                beep(cfg)
                send_email(cfg, "%s 掉到 %s" % (t["name"], cur["label"]), text)

        # ② 暴跌提醒
        da = cfg.get("drop_alert") or {}
        if t["id"] in (da.get("targets") or []) and pct <= float(da.get("pct", -2.0)):
            key = "drop_" + t["id"]
            if state.get(key) != today():
                state[key] = today()
                text = ("%s 当日暴跌\n\n现价 %.2f，跌幅 **%+.2f%%**\n"
                        "当前档位：%s\n该档目标：%s 元 ｜ 持仓：%s 元"
                        % (t["name"], px, pct, cur["label"], fmt(cur["amt"]), fmt(hold_amt)))
                print("\n" + "=" * 56 + "\n" + text + "\n" + "=" * 56)
                beep(cfg)
                send_email(cfg, "%s 暴跌 %+.1f%%" % (t["name"], pct), text)

    save_json(STATE, state)
    return q


def run_session(cfg, levels, state):
    """长驻模式：启动后一直跑到 15:10（GitHub Actions 用）"""
    import os as _os
    end_hm = _os.environ.get("END_AT") or "15:10"
    poll = int(cfg.get("trade_hours", {}).get("poll_seconds", 15))
    close_at = cfg.get("trade_hours", {}).get("close_report_at", "15:05")

    def hm(s):
        a, b = s.split(":")
        return int(a) * 60 + int(b)

    end_m = hm(end_hm)
    close_m = hm(close_at)
    print("长驻模式：跑到 %s，每 %d 秒检查一次" % (end_hm, poll))

    while True:
        n = now()
        cur_m = n.hour * 60 + n.minute
        if cur_m >= end_m:
            print("到达 %s，退出" % end_hm)
            break
        try:
            if in_session(cfg):
                q = check(cfg, levels, state)
                if cur_m >= close_m:
                    close_report(cfg, levels, q, state)
            else:
                print("[%s] 等待开盘/午休" % n.strftime("%H:%M:%S"))
                time.sleep(60)
                continue
        except Exception as e:                 # noqa: BLE001
            print("[%s] 出错 %r" % (n.strftime("%H:%M:%S"), e))
        time.sleep(poll)
    return 0


def apply_patch(cfg, patch):
    """把持仓/预算覆盖到 targets 上（来源：本机 config.local.json 或 GitHub Secret HOLDINGS）"""
    if not patch:
        return 0
    n = 0
    for t in cfg.get("targets") or []:
        ov = patch.get(t.get("id"))
        if not ov:
            continue
        for k, v in ov.items():
            t[k] = v
            n += 1
    return n


def load_cfg():
    cfg = load_json(CFG, None)
    if not cfg:
        return None
    apply_patch(cfg, load_json(os.path.join(BASE, "config.local.json"), None))
    raw = os.environ.get("HOLDINGS")
    if raw:
        try:
            apply_patch(cfg, json.loads(raw))
        except Exception as e:                 # noqa: BLE001
            print("[warn] HOLDINGS 解析失败: %r" % (e,))
    if not any(float(t.get("shares") or 0) for t in cfg.get("targets") or []):
        print("!! 未配置持仓：请设置 GitHub Secret HOLDINGS，或放一份 config.local.json")
    return cfg


def main():
    cfg = load_cfg()
    if not cfg:
        print("找不到 config.json")
        return 1

    print("=" * 70)
    print(" 档位监控　收盘表 / 掉档提醒 / 暴跌2%提醒")
    print("=" * 70)
    print("正在计算档位...")
    levels = build_levels(cfg)
    for t in cfg["targets"]:
        info = levels[t["id"]]
        print("  %-6s %2d 档　分位 %s　区间 %.2f ~ %.2f"
              % (t["name"], len(info["rows"]),
                 ("%.1f%%" % (info["comp"] * 100)) if info["comp"] is not None else "—",
                 info["rows"][-1]["line"], info["rows"][0]["line"]))
    print("=" * 70)

    state = load_json(STATE, {})
    if state.get("date") != today():
        state = {"date": today(), "level": {}, "closed": None}

    if "--close" in sys.argv:
        q = quotes([s for t in cfg["targets"] for s in (t["quote_secid"], t["etf_secid"])])
        close_report(cfg, levels, q, state)
        return 0

    if "--once" in sys.argv:
        check(cfg, levels, state)
        return 0

    if "--session" in sys.argv:
        return run_session(cfg, levels, state)

    poll = int(cfg.get("trade_hours", {}).get("poll_seconds", 15))
    close_at = cfg.get("trade_hours", {}).get("close_report_at", "15:05")

    while True:
        try:
            if state.get("date") != today():
                state = {"date": today(), "level": {}, "closed": None}
                print("\n新的一天，重建档位...")
                levels = build_levels(cfg)

            if in_session(cfg):
                q = check(cfg, levels, state)
                ch, cm = [int(x) for x in close_at.split(":")]
                n = now()
                if state.get("closed") != today() and (n.hour, n.minute) >= (ch, cm):
                    close_report(cfg, levels, q, state)
            else:
                print("[%s] 非交易时段" % now().strftime("%H:%M:%S"))
                time.sleep(max(poll * 4, 60))
                continue
        except KeyboardInterrupt:
            print("\n已退出")
            return 0
        except Exception as e:                 # noqa: BLE001
            print("[%s] 出错 %r" % (now().strftime("%H:%M:%S"), e))
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
