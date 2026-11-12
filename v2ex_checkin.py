#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V2EX 每日自动签到脚本
=====================

原理：带 Cookie 访问 /mission/daily → 解析 once token → 请求 /mission/daily/redeem?once=xxx 完成签到。
签到后同时统计：今日获得奖励（已签过则从 /balance 账单读取）、连续签到天数、账户余额
（金/银/铜，1金=100银=10000铜），在日志与通知中展示。

【获取 Cookie】
  未开 2FA 的账号只需 A2 一个 Cookie；开了 2FA 的账号需完整 Cookie 串（含 A2O 等）
  1. 浏览器登录 https://www.v2ex.com/
  2. F12 → Application(应用) → Cookies → https://www.v2ex.com
  3. 复制所需 cookie，拼成 "A2=值" 或完整串 "A2=...; A2O=...; ..."

【配置方式】(可叠加，优先级从高到低)
  A. 环境变量多账号（推荐 GitHub Actions 用）：
       V2EX_ACCOUNTS='[{"name":"主号","cookie":"A2=...; A2O=..."},
                       {"name":"小号","cookie":"A2=...; A2O=..."}]'
  B. 环境变量单账号：
       V2EX_COOKIE="A2=你的A2值"
  C. config.json（格式同 V2EX_ACCOUNTS，文件方式）：
       {"accounts": [{"name":"主号","cookie":"A2=主号值"},
                     {"name":"小号","cookie":"A2=小号值"}]}

【运行】
  python v2ex_checkin.py

【定时】
  A. GitHub Actions（推荐，免服务器）：
     - 把脚本和 .github/workflows/v2ex-checkin.yml 提交到 GitHub 仓库
     - 仓库 Settings → Secrets → Actions → New secret
       单账号：名称填 V2EX_COOKIE，值填 cookie 串
       多账号：名称填 V2EX_ACCOUNTS，值填 JSON 数组
     - 自定义签到时间：修改 yml 中 cron 表达式（UTC 时间，北京 = UTC + 8）
     - 也可在 Actions 页手动 Run workflow 测试
  B. Windows 任务计划程序 / Linux crontab：
     5 9 * * * /path/to/python /path/to/v2ex_checkin.py >> v2ex.log 2>&1

【Telegram 通知】（可选）
  签到完成后将结果推送到 Telegram，需配置两个环境变量：
    TG_BOT_TOKEN — 通过 @BotFather 创建 Bot 获取
    TG_CHAT_ID   — 你的 Chat ID（通过 @userinfobot 获取）
  未配置则自动跳过，不影响签到。
  GitHub Actions 用户：把这两个值加到仓库 Secrets 里即可。

【企业微信应用卡片通知】（可选）
  配置 WECOM_APPS 环境变量，值为 JSON 数组，允许配置一个或多个应用：
    WECOM_APPS='[{"corp_id":"wwxxxx","corp_secret":"xxxx","agent_id":1000002,"thumb_media_id":"MEDIA_ID","to_user":"@all"}]'
  说明：
    - thumb_media_id 为已上传到企业微信的图片素材 ID（用于图文卡片封面）
    - to_user 可选，默认 @all
    - 未配置或数组为空时自动跳过
"""

import os
import re
import sys
import json
import time
import html
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    sys.exit("缺少依赖 requests，请先安装：pip install requests")

BASE_URL = "https://www.v2ex.com"
TIMEOUT = 20
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# V2EX 官方换算：1 金币 = 100 银币 = 10000 铜币（https://www.v2ex.com/help/currency）
COPPER_RATE = {"金币": 10000, "银币": 100, "铜币": 1}

def log(name, msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{name}] {msg}", flush=True)

def send_telegram(text):
    """通过 Telegram Bot 推送通知，需配置 TG_BOT_TOKEN 和 TG_CHAT_ID 环境变量"""
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code != 200:
            log("TG", f"通知发送失败 HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log("TG", f"通知发送异常: {e}")

def _wecom_card_body(text):
    """把 Telegram 风格的文本改成企业微信图文卡片 HTML 内容。"""
    plain_text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]

    # 标题必须显示在卡片顶部，避免内容为空
    article_parts = [
        "<div><strong>V2EX 每日签到</strong></div>"
    ]

    for line in lines:
        if line.startswith(("⏰", "📊", "💎", "🏦")):
            article_parts.append(f"<div>{html.escape(line)}</div>")
        elif line.startswith(("✅", "❌")):
            article_parts.append(f"<div>{html.escape(line)}</div>")
        elif line.startswith("   └") or line.startswith("└"):
            cleaned = re.sub(r"^\\s*└\\s*", "", line)
            article_parts.append(f"<div>　{html.escape(cleaned)}</div>")
        elif line.startswith("🔥"):
            article_parts.append(f"<div>{html.escape(line)}</div>")
        else:
            article_parts.append(f"<div>{html.escape(line)}</div>")

    return "<br/>".join(article_parts)

def send_wecom(text):
    """通过企业微信自建应用发送图文卡片消息，使用 WECOM_APPS 数组配置。"""
    raw_apps = os.getenv("WECOM_APPS", "").strip()
    if not raw_apps:
        return

    try:
        apps = json.loads(raw_apps)
        if not isinstance(apps, list):
            raise ValueError("WECOM_APPS 必须是 JSON 数组")
    except (json.JSONDecodeError, ValueError) as e:
        log("企业微信", f"WECOM_APPS 格式错误（应为 JSON 数组）: {e}")
        return

    # 生成企业微信图文卡片
    article_content = _wecom_card_body(text)
    plain_text = html.unescape(re.sub(r"<[^>]+>", "", text))
    digest = plain_text.replace("\\n", " ").strip()[:100]

    now_bj = datetime.now(timezone(timedelta(hours=8)))
    title = f"V2EX 每日签到 - {now_bj:%Y-%m-%d}"

    for index, app in enumerate(apps, start=1):
        if not isinstance(app, dict):
            log("企业微信", f"WECOM_APPS 第 {index} 项格式错误，应为对象")
            continue

        corp_id = str(app.get("corp_id", "")).strip()
        corp_secret = str(app.get("corp_secret", "")).strip()
        thumb_media_id = str(app.get("thumb_media_id", "")).strip()
        agent_id = app.get("agent_id")

        if (not corp_id or not corp_secret or not thumb_media_id
                or agent_id is None or str(agent_id).strip() == ""):
            log("企业微信", f"WECOM_APPS 第 {index} 项缺少 corp_id、corp_secret、agent_id 或 thumb_media_id")
            continue

        try:
            agent_id = int(agent_id)
        except (TypeError, ValueError):
            log("企业微信", f"WECOM_APPS 第 {index} 项 agent_id 必须是整数")
            continue

        try:
            token_resp = requests.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": corp_id, "corpsecret": corp_secret},
                timeout=15,
            )
            token_resp.raise_for_status()
            token_data = token_resp.json()
            access_token = token_data.get("access_token")
            if token_data.get("errcode") != 0 or not access_token:
                log("企业微信", f"应用 {index} 获取 access_token 失败: {token_data.get('errmsg', '未知错误')}")
                continue

            payload = {
                "touser": str(app.get("to_user", "@all")).strip() or "@all",
                "msgtype": "mpnews",
                "agentid": agent_id,
                "mpnews": {
                    "articles": [{
                        "title": title,
                        "thumb_media_id": thumb_media_id,
                        "content": article_content,
                        "digest": digest,
                    }]
                },
                "safe": 0,
            }

            resp = requests.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": access_token},
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode") != 0:
                log("企业微信", f"应用 {index} 通知发送失败: {result.get('errmsg', '未知错误')}")
        except (requests.RequestException, ValueError) as e:
            log("企业微信", f"应用 {index} 通知发送异常: {e}")

def load_config():
    """加载账号配置，返回 [(name, cookie), ...]
    优先级（高→低，可叠加）：
      1. V2EX_ACCOUNTS 环境变量（JSON 数组，多账号）
      2. V2EX_COOKIE 环境变量（单账号）
      3. config.json 文件
    """
    accounts = []

    # 1. V2EX_ACCOUNTS：JSON 数组，支持多账号
    env_accounts = os.getenv("V2EX_ACCOUNTS", "").strip()
    if env_accounts:
        try:
            for a in json.loads(env_accounts):
                accounts.append((a.get("name", "account"), a["cookie"]))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            sys.exit(f"V2EX_ACCOUNTS 格式错误（应为 JSON 数组）: {e}")

    # 2. V2EX_COOKIE：单账号
    env_cookie = os.getenv("V2EX_COOKIE", "").strip()
    if env_cookie:
        accounts.append(("env", env_cookie))

    # 3. config.json
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if "accounts" in cfg:
            for a in cfg["accounts"]:
                accounts.append((a.get("name", "account"), a["cookie"]))
        elif "cookie" in cfg:
            accounts.append((cfg.get("name", "account"), cfg["cookie"]))
    return accounts

def _extract_reward(html_text):
    """从签到页 HTML 提取奖励信息，返回 '获得 X 铜币' 或 ''"""
    patterns = [
        r'已成功领取每日登录奖励\s*(\d+)\s*(铜币|银币|金币)',
        r'每日登录奖励\s*(\d+)\s*(铜币|银币|金币)',
        r'领取[^<]*?(\d+)\s*(铜币|银币|金币)',
        r'(\d+)\s*个?\s*(铜币|银币|金币)',
    ]
    for p in patterns:
        m = re.search(p, html_text)
        if m:
            return f"获得 {m.group(1)} {m.group(2)}"
    return ""

def _fetch_today_reward(session):
    """已签过的账号从 /balance 账单提取今日奖励，返回 '获得 X 铜币' 或 ''"""
    try:
        r = session.get(f"{BASE_URL}/balance", timeout=TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            return ""
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        m = re.search(today + r'\\s*的每日登录奖励[^<]*?(\\d+)\\s*(铜币|银币|金币)', r.text)
        return f"获得 {m.group(1)} {m.group(2)}" if m else ""
    except requests.RequestException:
        return ""

def _extract_days(html_text):
    """提取连续登录天数，返回 '已连续登录 X 天' 或 ''"""
    m = re.search(r'已连续登录\\s*(\\d+)\\s*天', html_text)
    return f"已连续登录 {m.group(1)} 天" if m else ""

def _extract_balance(html_text):
    """从页面右上角余额区提取账户余额，返回 {'金币': x, '银币': y, '铜币': z} 或 None"""
    m = re.search(r'class=\"balance_area\"[^>]*>(.*?)</(?:a|div)>', html_text, re.S)
    if not m:
        return None
    area = m.group(1)
    units = (("gold", "金币"), ("silver", "银币"), ("bronze", "铜币"))

    res = {}
    for icon, unit in units:
        mm = re.search(r'(\\d+)\\s*<img[^>]*' + icon, area)
        if mm:
            res[unit] = int(mm.group(1))
    if len(res) == 3:
        return res

    nums = re.findall(r'\\d+', re.sub(r'<[^>]+>', ' ', area))
    if len(nums) >= 3:
        return {unit: int(n) for (_, unit), n in zip(units, nums[:3])}
    return None

def _fmt_balance(balance):
    """紧凑格式，如 '1金61银56铜'（金/银为 0 时省略）"""
    return "".join(f"{balance[u]}{u[0]}" for u in ("金币", "银币", "铜币")
                   if u in balance and (balance[u] or u == "铜币"))

def _to_copper(balance):
    """余额折合铜币总数"""
    return sum(balance.get(u, 0) * r for u, r in COPPER_RATE.items())

def _norm_copper(c):
    """铜币总数 → 规范金/银/铜表示（用于多账号汇总进位）"""
    return {"金币": c // 10000, "银币": c % 10000 // 100, "铜币": c % 100}

def _bal_text(balance):
    """'16156铜（1金61银56铜）'，纯铜号简化为 '512铜'"""
    c = _to_copper(balance)
    if balance.get("金币") or balance.get("银币"):
        return f"{c}铜（{_fmt_balance(balance)}）"
    return f"{c}铜"

def _day_suffix(days):
    return f"，{days}" if days else ""

def _bal_suffix(balance):
    return f"，余额 {_bal_text(balance)}" if balance else ""

def checkin(name, cookie):
    """签到，返回 (success: bool, reward: str, balance: dict|None, days: str)"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": f"{BASE_URL}/mission/daily",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })

    r = s.get(f"{BASE_URL}/mission/daily", timeout=TIMEOUT, allow_redirects=True)
    if "/signin" in r.url or "You need to sign in" in r.text or "请先登录" in r.text:
        log(name, "X 未登录或 Cookie 已失效")
        return False, "未登录或 Cookie 已失效", None, ""

    balance = _extract_balance(r.text)
    days = _extract_days(r.text)

    if "每日登录奖励已领取" in r.text:
        reward = _extract_reward(r.text) or _fetch_today_reward(s)
        msg = f"今日已签过（{reward}）" if reward else "今日已签过"
        log(name, f"OK {msg}" + _day_suffix(days) + _bal_suffix(balance))
        return True, msg, balance, days

    m = re.search(r'/mission/daily/redeem\\?once=(\\d+)', r.text)
    if not m:
        m = re.search(r'once=(\\d+)', r.text)
    if not m:
        log(name, "X 未找到签到 token，页面结构可能已变化")
        return False, "未找到签到 token", None, ""

    once = m.group(1)
    r2 = s.get(f"{BASE_URL}/mission/daily/redeem?once={once}", timeout=TIMEOUT, allow_redirects=True)
    if r2.status_code != 200:
        log(name, f"X 签到失败 HTTP {r2.status_code}")
        return False, f"签到失败 HTTP {r2.status_code}", None, ""

    time.sleep(1)
    r3 = s.get(f"{BASE_URL}/mission/daily", timeout=TIMEOUT, allow_redirects=True)
    pages = [r3.text if r3.status_code == 200 else "", r2.text]

    balance = (_extract_balance(pages[0]) or _extract_balance(r2.text) or balance)
    days = _extract_days(pages[0]) or _extract_days(r2.text) or days

    for page in pages:
        reward = _extract_reward(page)
        if reward:
            log(name, f"OK 签到成功（{reward}）" + _day_suffix(days) + _bal_suffix(balance))
            return True, reward, balance, days

    reward = _fetch_today_reward(s)
    if reward:
        log(name, f"OK 签到成功（{reward}）" + _day_suffix(days) + _bal_suffix(balance))
        return True, reward, balance, days

    for page in pages:
        for kw in ["铜币", "银币", "金币", "领取", "奖励"]:
            idx = page.find(kw)
            if idx >= 0:
                snippet = page[max(0, idx - 40):idx + 60].strip()
                log(name, f"诊断 [{kw}] 片段: ...{snippet}...")
                break
        else:
            continue
        break

    log(name, "OK 签到成功" + _day_suffix(days) + _bal_suffix(balance))
    return True, "签到成功（奖励信息未识别）", balance, days

def main():
    accounts = load_config()
    if not accounts:
        sys.exit("未找到配置！请设置环境变量 V2EX_ACCOUNTS / V2EX_COOKIE 或创建 config.json（参考脚本头部说明）")

    log("系统", f"开始签到，共 {len(accounts)} 个账号")
    ok = 0
    results = []
    coins = {}
    total_bal = {}
    bal_count = 0

    for name, cookie in accounts:
        try:
            success, reward, balance, days = checkin(name, cookie)
            if success:
                ok += 1
                line = f"✅ <b>{name}</b>\n   └ {reward}"
                if days:
                    line += f"\n   └ 🔥 <b>{days}</b>"
                if balance:
                    line += f"\n   └ 💰 <b>{_bal_text(balance)}</b>"
                    for u, v in balance.items():
                        total_bal[u] = total_bal.get(u, 0) + v
                    bal_count += 1
                results.append(line)
                for amt, unit in re.findall(r'(\\d+)\\s*(铜币|银币|金币)', reward):
                    coins[unit] = coins.get(unit, 0) + int(amt)
            else:
                results.append(f"❌ <b>{name}</b>\n   └ {reward}")
        except requests.RequestException as e:
            log(name, f"X 网络错误: {e}")
            results.append(f"❌ <b>{name}</b>\n   └ 网络错误")
        time.sleep(2)

    log("系统", f"完成：{ok}/{len(accounts)} 成功")

    now_bj = datetime.now(timezone(timedelta(hours=8)))
    sep = "━━━━━━━━━━━━━━━━━━"
    icon = "🎯" if ok == len(accounts) else "⚠️"

    msg = f"<b>{icon} V2EX 每日签到</b>\n"
    msg += f"{sep}\n"
    msg += f"⏰ {now_bj:%Y-%m-%d %H:%M}\n"
    msg += f"📊 成功 <b>{ok}</b>/<b>{len(accounts)}</b>\n"
    msg += f"{sep}\n"
    msg += "\n".join(results)
    msg += f"\n{sep}\n"
    if coins:
        coin_str = "，".join(f"{v} {k}" for k, v in coins.items())
        msg += f"💎 今日收获 · <b>{coin_str}</b>"
    else:
        msg += "💎 今日收获 · 无"
    if bal_count and len(accounts) > 1:
        total = _norm_copper(_to_copper(total_bal))
        msg += f"\n🏦 合计 <b>{_bal_text(total)}</b>"

    send_telegram(msg)
    send_wecom(msg)

    sys.exit(0 if ok == len(accounts) else 1)

if __name__ == "__main__":
    main()
