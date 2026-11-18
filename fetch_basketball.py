#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自动抓取「竞彩篮球赛程 + 赔率 + 赛果」，加密输出 basketball.enc。
纯标准库，GitHub Actions 直接跑，0 元、免 key、无需服务器。

数据源：500 彩票网竞彩篮球页（国内）。
  https://trade.500.com/jclq/?playid=313&g=2&date=YYYY-MM-DD   胜负/让分/大小/胜分差
  https://live.500.com/jclq.php?e=YYYY-MM-DD                  开奖/赛果页
页面直接含竞彩编号（周一301 等）、赛事、开赛时间、中文队名、让分、盘口、
预设总分、胜分差 12 项奖金、全场比分。全部照抄，不臆造、不混别的赛事。

产出（密文，仓库里无明文数据）：
  basketball.enc   当天 + 最近销售日 的篮球数据
密钥由「账号 admin + 用户密码」经 PBKDF2-SHA256 派生；密码只存在
GitHub Actions Secret（SITE_PASS），不写进任何公开源码、日志或数据文件。
"""
import json, os, re, html, urllib.request, urllib.error
import base64, hashlib
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'basketball.enc')

SITE_USER = 'admin'
KDF_SALT = b'jc-tj-2026-static-salt-v1'
KDF_ITER = 210000
CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)
TODAY = NOW.strftime('%Y-%m-%d')
SOURCE = '500彩票网竞彩篮球（国内数据）'
PLAYURL = 'https://trade.500.com/jclq/?playid=%s&g=2&date=%s'
RESULTURL = 'https://live.500.com/jclq.php?e=%s'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*; q=0.01',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://trade.500.com/jclq/',
}
PLAYID_NAME = {'313': '胜负', '314': '让分胜负', '315': '大小分', '316': '胜分差'}


def fetch_text(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(3000000)
        return raw.decode('gb18030', errors='replace')
    except Exception as e:
        print('  [抓取失败] %s -> %s' % (url, e))
        return ''


def txt(x):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', x or ''))).strip()


def parse_list(html):
    """从胜负玩法页解析比赛列表：编号 / 主客队 / 开赛时间 / 联赛"""
    matches = {}
    trs = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in trs:
        num_m = re.search(r'>([周][一二三四五六日]\d{3})<', body)
        if not num_m:
            continue
        num = num_m.group(1).strip()
        # 队名
        teams = re.findall(r'<span[^>]*>([^<]+)</span>', body)
        home = teams[0].strip() if teams else ''
        away = teams[1].strip() if len(teams) > 1 else ''
        if not home or not away:
            continue
        kick_m = re.search(r'(\d{2}:\d{2})', body)
        # 联赛名（一般在首列）
        league_m = re.search(r'class="[^"]*league[^"]*"[^>]*>([^<]+)<', body)
        league = league_m.group(1).strip() if league_m else ''
        matches[num] = {
            "match_id": mid, "num": num, "home": home, "away": away,
            "league": league, "kickoff": kick_m.group(1) if kick_m else '',
            "odds": {}, "result": {},
        }
    return matches


def parse_odds(html, matches):
    """从各玩法页解析赔率，按 data-k / data-id 归到对应场次。"""
    # 通用 SP：data-sp="x.xx" data-k="赛事id"
    for sp, k in re.findall(r'data-sp="([\d.]+)"[^>]*data-k="(\d+)"', html):
        for m in matches.values():
            if m["match_id"] == k:
                m["odds"].setdefault("胜负", []).append(round(float(sp), 3))
    # 让分胜负
    for sp, k in re.findall(r'data-sp="([\d.]+)"[^>]*data-hsp="([-\d.]+)"', html):
        for m in matches.values():
            if m["match_id"] == k:
                m["odds"].setdefault("让分胜负", []).append({
                    "sp": round(float(sp), 3), "handicap": float(hsp)
                })
    # 大小分（含预设总分）
    for sp, k, total in re.findall(r'data-sp="([\d.]+)"[^>]*data-k="(\d+)"[^>]*data-d="([\d.]+)"'):
        for m in matches.values():
            if m["match_id"] == k:
                m["odds"].setdefault("大小分", []).append({
                    "sp": round(float(sp), 3), "total": float(total)
                })
    return matches


def parse_shengfencha(html, matches):
    """胜分差 12 项奖金：主胜1-5 / 主胜6-10 ... 主胜26+ / 客胜1-5 ... 客胜26+"""
    blocks = re.findall(r'data-sd="(\d+)"(.*?)</tr>', html, re.S)
    for sd, body in blocks:
        sps = re.findall(r'data-sp="([\d.]+)"', body)
        # 归到所属场次（通过外层 tr 的 data-id）
        mid_m = re.search(r'data-id="(\d+)"', body)
        if not mid_m or not sps:
            continue
        for m in matches.values():
            if m["match_id"] == mid_m.group(1):
                m["odds"]["胜分差"] = [round(float(x), 3) for x in sps[:12]]
    return matches


def parse_result(html):
    """从开奖页解析赛果：主客得分 / 胜负"""
    results = {}
    rows = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in rows:
        # 比分：找形如 112-108
        score_m = re.search(r'>(\d{2,3})-(\d{2,3})<', body)
        score = f"{score_m.group(1)}-{score_m.group(2)}" if score_m else ''
        # 胜负标记
        win = '主胜' if re.search(r'主胜', body) else ('客胜' if re.search(r'客胜', body) else '')
        results[mid] = {"score": score, "win": win}
    return results


def derive_key(passphrase):
    """与足球脚本一致的密钥派生，保证同一个密码能解密所有 enc 文件。"""
    if not passphrase:
        return None
    material = (SITE_USER + ':' + passphrase).encode('utf-8')
    return hashlib.pbkdf2_hmac('sha256', material, KDF_SALT, KDF_ITER, 32)


def encrypt_obj(obj, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import os as _os
    aes = AESGCM(key)
    nonce = _os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    ct = aes.encrypt(nonce, plain, None)
    return {
        'v': 1, 'alg': 'AES-256-GCM', 'kdf': 'PBKDF2-HMAC-SHA256',
        'iter': KDF_ITER, 'salt': base64.b64encode(KDF_SALT).decode(),
        'nonce': base64.b64encode(nonce).decode(),
        'ct': base64.b64encode(ct).decode(),
    }


def write_encrypted(obj, path, key):
    if key is None:
        print('警告：未配置 SITE_PASS，跳过篮球加密输出（保留现有密文，不写明文）')
        return False
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(encrypt_obj(obj, key), f, ensure_ascii=False, separators=(',', ':'))
    return True


def main():
    passphrase = os.environ.get('SITE_PASS', '')
    cipher_key = derive_key(passphrase)
    if cipher_key is None:
        print('警告：未配置 SITE_PASS，跳过篮球加密输出')
        return

    # 抓取窗口：今天 + 前 3 天（滚动，避免漏掉预售/延迟场次）
    dates = []
    for i in range(4):
        d = (datetime.now(CST) - timedelta(days=i)).strftime('%Y-%m-%d')
        dates.append(d)

    days_out = []
    for d in dates:
        print('\n=== 竞彩篮球 %s ===' % d)
        # 1) 胜负页取列表
        html313 = fetch_text(PLAYURL % ('313', d))
        matches = parse_list(html313)
        if not matches:
            print('  [无场次] %s' % d)
            days_out.append({"date": d, "weekday": '', "count": 0, "matches": []})
            continue
        print('  列表 %d 场' % len(matches))

        # 2) 各玩法赔率
        for pid in ('313', '314', '315', '316'):
            h = fetch_text(PLAYURL % (pid, d))
            if pid == '314':
                parse_odds(h, matches)
            elif pid == '316':
                parse_shengfencha(h, matches)
            else:
                parse_odds(h, matches)

        # 3) 赛果回填
        rh = fetch_text(RESULTURL % d)
        results = parse_result(rh)
        for m in matches.values():
            if m["match_id"] in results:
                m["result"] = results[m["match_id"]]
                m["settled"] = True

        # 周几
        wk = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        wd = wk[datetime.strptime(d, '%Y-%m-%d').weekday()]
        day_obj = {
            "date": d, "weekday": wd, "count": len(matches),
            "matches": list(matches.values()),
            "odds_note": '竞彩篮球官方固定奖金（胜负/让分胜负/大小分/胜分差）；销售期内 SP 会随投注量调整，停售后锁定。',
        }
        days_out.append(day_obj)
        print('  => %d 场' % len(matches))
        for m in list(matches.values())[:3]:
            print('     %s %s vs %s %s' % (m['num'], m['home'], m['away'], m['kickoff']))

    out = {
        "status": "ok" if any(x["count"] > 0 for x in days_out) else "pending",
        "kind": "domestic",
        "source": SOURCE,
        "updated_at": NOW.strftime('%Y-%m-%d %H:%M:%S') + ' (北京时间)',
        "date_note": '竞彩篮球编号前缀为比赛日标签（如周一301），凌晨开赛场次仍挂前一日编号，卡片按真实开赛时间显示，不改写。',
        "day_count": len(days_out),
        "days": days_out,
        "note": '竞彩篮球 4 种玩法（胜负/让分胜负/大小分/胜分差）官方固定奖金；含编号、队名、联赛、开赛时间、让分值、预设总分、胜分差 12 项奖金，完赛后回填比分与胜负。',
    }
    ok = write_encrypted(out, OUT, cipher_key)
    if ok:
        print('\n[OK] 已写出加密 %s（%d 个日期，密码未落盘）' % (OUT, len(days_out)))


if __name__ == '__main__':
    main()
