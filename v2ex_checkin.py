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
        title = "🎯 V2EX 每日签到"
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{title}\n\n{text}", "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if resp.status_code != 200:
            log("TG", f"通知发送失败 HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log("TG", f"通知发送异常: {e}")


def send_wecom(plain_text):
    """以纯文本内容排版后，作为企业微信图文卡片发送。"""
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
    if not apps:
        return

    now_bj = datetime.now(timezone(timedelta(hours=8)))
    title = f"V2EX 每日签到 - {now_bj:%Y-%m-%d}"
    content = "<br/>".join(html.escape(line) for line in plain_text.splitlines())
    digest = "\n".join(line.strip() for line in plain_text.splitlines() if line.strip())[:300]

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
                params={"corpid": corp_id, "corpsecret": corp_secret}, timeout=15)
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
                "mpnews": {"articles": [{
                    "title": title, "thumb_media_id": thumb_media_id,
                    "content": content, "digest": digest,
                }]},
                "safe": 0,
            }
            resp = requests.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": access_token}, json=payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
            if result.get("errcode") != 0:
                log("企业微信", f"应用 {index} 通知发送失败: {result.get('errmsg', '未知错误')}")
        except (requests.RequestException, ValueError) as e:
            log("企业微信", f"应用 {index} 通知发送异常: {e}")


def send_notification(text):
    """统一通知入口：依次调用已配置的通知渠道，未配置的渠道自动跳过。"""
    send_telegram(text)
    send_wecom(text)


def load_config():
    """加载账号配置，返回 [(name, cookie), ...]"""
    accounts = []
    env_accounts = os.getenv("V2EX_ACCOUNTS", "").strip()
    if env_accounts:
        try:
            for a in json.loads(env_accounts):
                accounts.append((a.get("name", "account"), a["cookie"]))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            sys.exit(f"V2EX_ACCOUNTS 格式错误（应为 JSON 数组）: {e}")
    env_cookie = os.getenv("V2EX_COOKIE", "").strip()
    if env_cookie:
        accounts.append(("env", env_cookie))
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


def _plain_text(markup):
    """将 HTML 转为可搜索文本，保留单元格/标签之间的分隔。"""
    markup = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", markup or "")
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def _extract_amount(text):
    """从文本片段提取币种金额，兼容“20 铜币”和“铜币 20”等格式。"""
    patterns = [
        r"([+-]?\s*\d+)\s*(铜币|银币|金币)",
        r"(铜币|银币|金币)\s*([+-]?\s*\d+)",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text)
        if match:
            amount, unit = ((match.group(1), match.group(2)) if index == 0
                            else (match.group(2), match.group(1)))
            return f"获得 {amount.replace(' ', '').lstrip('+')} {unit}"
    return ""


def _extract_reward(page):
    """从签到页 HTML 提取奖励信息，返回 '获得 X 铜币' 或 ''"""
    text = _plain_text(page)
    for keyword in ("每日登录奖励", "成功领取", "获得"):
        for match in re.finditer(re.escape(keyword), text):
            snippet = text[max(0, match.start() - 50):match.end() + 100]
            reward = _extract_amount(snippet)
            if reward:
                return reward
    return ""


def _reward_from_ledger_row(row, date_patterns):
    """从同一条账单记录中取奖励，避免误把其他日期或余额当作今日奖励。"""
    text = _plain_text(row)
    if "每日登录奖励" not in text:
        return ""
    if any(re.search(pattern, text) for pattern in date_patterns):
        return _extract_amount(text)
    return ""


def _fetch_today_reward(session):
    """已签过的账号从 /balance 今日账单提取奖励，失败时返��奖励描述或空字符串。"""
    try:
        response = session.get(f"{BASE_URL}/balance", timeout=TIMEOUT, allow_redirects=True)
        if response.status_code != 200:
            return ""
        today = datetime.now(timezone(timedelta(hours=8)))
        date_patterns = [
            re.escape(today.strftime("%Y%m%d")),
            re.escape(today.strftime("%Y-%m-%d")),
            re.escape(today.strftime("%Y/%m/%d")),
            rf"{today.year}\s*年\s*0?{today.month}\s*月\s*0?{today.day}\s*日?",
            rf"0?{today.month}\s*月\s*0?{today.day}\s*日",
        ]
        rows = re.findall(r"(?is)<tr\b[^>]*>.*?</tr>", response.text)
        for row in rows:
            reward = _reward_from_ledger_row(row, date_patterns)
            if reward:
                return reward

        text = _plain_text(response.text)
        for match in re.finditer("每日登录奖励", text):
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 120)
            snippet = text[start:end]
            if any(re.search(pattern, snippet) for pattern in date_patterns):
                reward = _extract_amount(snippet)
                if reward:
                    return reward
        for match in re.finditer("每日登录奖励", text):
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 120)
            reward = _extract_amount(text[start:end])
            if reward:
                return reward
        return ""
    except requests.RequestException:
        return ""


def _extract_days(page):
    match = re.search(r"已连续登录\s*(\d+)\s*天", page)
    return f"已连续登录 {match.group(1)} 天" if match else ""


def _extract_balance(page):
    match = re.search(r'class="balance_area"[^>]*>(.*?)</(?:a|div)>', page, re.S)
    if not match:
        return None
    area = match.group(1)
    units = (("gold", "金币"), ("silver", "银币"), ("bronze", "铜币"))
    result = {}
    for icon, unit in units:
        found = re.search(r"(\d+)\s*<img[^>]*" + icon, area)
        if found:
            result[unit] = int(found.group(1))
    if len(result) == 3:
        return result
    numbers = re.findall(r"\d+", re.sub(r"<[^>]+>", " ", area))
    if len(numbers) >= 3:
        return {unit: int(number) for (_, unit), number in zip(units, numbers[:3])}
    return None


def _fmt_balance(balance):
    return "".join(f"{balance[unit]}{unit[0]}" for unit in ("金币", "银币", "铜币")
                   if unit in balance and (balance[unit] or unit == "铜币"))


def _to_copper(balance):
    return sum(balance.get(unit, 0) * rate for unit, rate in COPPER_RATE.items())


def _norm_copper(copper):
    return {"金币": copper // 10000, "银币": copper % 10000 // 100, "铜币": copper % 100}


def _bal_text(balance):
    copper = _to_copper(balance)
    if balance.get("金币") or balance.get("银币"):
        return f"{copper}铜（{_fmt_balance(balance)}）"
    return f"{copper}铜"


def _reward_text(reward):
    match = re.search(r"(\d+)\s*(铜币|银币|金币)", reward or "")
    return f"+{match.group(1)} {match.group(2)}" if match else ""


def _is_login_page(response):
    """识别签到请求被重定向到登录页或返回登录提示的情况。"""
    return ("/signin" in response.url or "You need to sign in" in response.text
            or "请先登录" in _plain_text(response.text))


def checkin(name, cookie):
    """签到，返回 (success, reward, balance, days)"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA, "Cookie": cookie,
        "Referer": f"{BASE_URL}/mission/daily",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    response = session.get(f"{BASE_URL}/mission/daily", timeout=TIMEOUT, allow_redirects=True)
    if _is_login_page(response):
        log(name, "X 未登录或 Cookie 已失效")
        return False, "未登录或 Cookie 已失效", None, ""

    balance = _extract_balance(response.text)
    days = _extract_days(response.text)
    if "每日登录奖励已领取" in _plain_text(response.text):
        reward = _extract_reward(response.text) or _fetch_today_reward(session)
        msg = f"今日已签过（{reward}）" if reward else "今日已签过"
        log(name, f"OK {msg}" + (f"，{days}" if days else "") + (f"，余额 {_bal_text(balance)}" if balance else ""))
        return True, msg, balance, days

    # 2. 提取 once token（签到流程沿用上游 BYGD 版本）
    checkin_html = html.unescape(response.text)
    match = re.search(r"/mission/daily/redeem\?once=(\d+)", checkin_html)
    if not match:
        match = re.search(r"[?&]once=(\d+)", checkin_html)
    if not match:
        log(name, "X 未找到签到 token，页面结构可能已变化")
        return False, "未找到签到 token", None, ""

    # 3. 领取奖励
    redeem_response = session.get(
        f"{BASE_URL}/mission/daily/redeem?once={match.group(1)}",
        timeout=TIMEOUT, allow_redirects=True)
    if redeem_response.status_code != 200:
        log(name, f"X 签到失败 HTTP {redeem_response.status_code}")
        return False, f"签到失败 HTTP {redeem_response.status_code}", None, ""

    # 4. 领取后从再次请求的签到页和 redeem 响应里提取奖励。
    #    上游逻辑不因签到后的回查页跳转到登录页而判失败；兑换请求 HTTP 200 后继续确认结果。
    time.sleep(1)
    page_response = session.get(f"{BASE_URL}/mission/daily", timeout=TIMEOUT, allow_redirects=True)
    pages = [page_response.text if page_response.status_code == 200 else "", redeem_response.text]
    balance = (_extract_balance(pages[0]) or _extract_balance(redeem_response.text) or balance)
    days = _extract_days(pages[0]) or _extract_days(redeem_response.text) or days

    # 5. 多重提取奖励（两个页面都试）
    for page in pages:
        reward = _extract_reward(page)
        if reward:
            log(name, f"OK 签到成功（{reward}）" + (f"，{days}" if days else "") + (f"，余额 {_bal_text(balance)}" if balance else ""))
            return True, reward, balance, days

    if "每日登录奖励已领取" in _plain_text(page_response.text):
        reward = _fetch_today_reward(session)
        msg = f"今日已签过（{reward}）" if reward else "今日已签过"
        log(name, f"OK {msg}" + (f"，{days}" if days else "") + (f"，余额 {_bal_text(balance)}" if balance else ""))
        return True, msg, balance, days

    reward = _fetch_today_reward(session)
    if reward:
        log(name, f"OK 签到成功（{reward}）" + (f"，{days}" if days else "") + (f"，余额 {_bal_text(balance)}" if balance else ""))
        return True, reward, balance, days

    # 诊断：打印疑似片段，方便下次精确定位
    for page in pages:
        for keyword in ["铜币", "银币", "金币", "领取", "奖励"]:
            index = page.find(keyword)
            if index >= 0:
                snippet = page[max(0, index - 40):index + 60].strip()
                log(name, f"诊断 [{keyword}] 片段: ...{snippet}...")
                break
        else:
            continue
        break

    log(name, "OK 签到成功" + (f"，{days}" if days else "") + (f"，余额 {_bal_text(balance)}" if balance else ""))
    return True, "签到成功（奖励信息未识别）", balance, days


def main():
    accounts = load_config()
    if not accounts:
        sys.exit("未找到配置！请设置环境变量 V2EX_ACCOUNTS / V2EX_COOKIE 或创建 config.json（参考脚本头部说明）")

    log("系统", f"开始签到，共 {len(accounts)} 个账号")
    ok = 0
    account_results = []
    coins = {}
    total_bal = {}
    bal_count = 0

    for name, cookie in accounts:
        try:
            success, reward, balance, days = checkin(name, cookie)
            if success:
                ok += 1
                status = "✅ 今日已签过" if "今日已签过" in reward else "✅ 签到成功"
                account_results.append({"name": name, "success": True, "status": status,
                                        "reward": reward, "balance": balance, "days": days})
                for amount, unit in re.findall(r"(\d+)\s*(铜币|银币|金币)", reward):
                    coins[unit] = coins.get(unit, 0) + int(amount)
                if balance:
                    for unit, value in balance.items():
                        total_bal[unit] = total_bal.get(unit, 0) + value
                    bal_count += 1
            else:
                account_results.append({"name": name, "success": False, "status": "❌ 签到失败",
                                        "reward": reward, "balance": None, "days": ""})
        except requests.RequestException as e:
            log(name, f"X 网络错误: {e}")
            account_results.append({"name": name, "success": False, "status": "❌ 网络错误",
                                    "reward": "网络错误", "balance": None, "days": ""})
        time.sleep(2)

    log("系统", f"完成：{ok}/{len(accounts)} 成功")
    now_bj = datetime.now(timezone(timedelta(hours=8)))
    lines = []
    for index, result in enumerate(account_results, start=1):
        lines.append(f"【账号 {index}】")
        lines.append(f"👤 账号信息: {result['name']}")
        reward = _reward_text(result["reward"]) if result["success"] else ""
        if result["success"]:
            reward_suffix = f"，获得 {reward[1:]}" if reward else ""
            count_suffix = "" if "今日已签过" in result["status"] else f" ({ok}/{len(accounts)})"
            lines.append(f"📊 签到状态: {result['status']}{count_suffix}{reward_suffix}")
        else:
            lines.append(f"📊 签到状态: {result['status']} ({ok}/{len(accounts)})，{result['reward']}")
        if result["balance"]:
            balance = result["balance"]
            copper = _to_copper(balance)
            asset_detail = " ".join(
                f"{balance.get(unit, 0)}{short}"
                for unit, short in (("金币", "金"), ("银币", "银"), ("铜币", "铜")))
            lines.append("📦 账户资产：")
            if reward:
                lines.append(f"   铜币：[{reward}] 总计 {copper} ({asset_detail})")
            else:
                lines.append(f"   总计 {copper} 铜币 ({asset_detail})")
        if result["days"]:
            lines.append(f"   连续：{result['days']}")
        lines.append(f"🕐 完成时间: {now_bj:%Y-%m-%d %H:%M:%S}")
        if index != len(account_results):
            lines.append("")
    text = "\n".join(lines)

    send_notification(text)
    sys.exit(0 if ok == len(accounts) else 1)


if __name__ == "__main__":
    main()
