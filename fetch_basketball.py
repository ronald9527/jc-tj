#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩篮球数据抓取脚本
数据源：500 彩票网竞彩篮球页（国内，与足球页同源，GitHub Actions 出口实测可直连）。
  https://trade.500.com/jclq/?playid=PID&g=2&date=YYYY-MM-DD
  playid: 313 胜负  314 让分胜负  315 大小分  316 胜分差
产出（密文）：
  basketball.enc  指定销售日的竞彩篮球赛程 + 四种玩法赔率 + 开奖赛果
加密规格与 fetch_schedule.py 完全一致（同一 SITE_PASS 可解密）。
"""
import json
import os
import re
import base64
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'basketball.enc')

SITE_USER = 'admin'
KDF_SALT = b'jc-tj-2026-static-salt-v1'
KDF_ITER = 210000
CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime('%Y-%m-%d')

SOURCE = '500彩票网竞彩篮球（国内数据）'
DAYS = 4  # 抓取最近 4 个销售日：当天 + 昨日 + 前日 + 明日预售

PLAYID = {'sf': 313, 'rf': 314, 'dx': 315, 'sf_diff': 316}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*; q=0.01',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://trade.500.com/jclq/',
}

# 胜分差 12 个区间的显示标签（主胜 1-5 / 主胜 6-10 / ... / 主胜 26+ / 客胜 1-5 / ... / 客胜 26+）
SFDIFF_KEYS = ['w1', 'w2', 'w3', 'w4', 'w5', 'w6', 'l1', 'l2', 'l3', 'l4', 'l5', 'l6']
SFDIFF_LABEL = {
    'w1': '胜1-5', 'w2': '胜6-10', 'w3': '胜11-15', 'w4': '胜16-20', 'w5': '胜21-25', 'w6': '胜26+',
    'l1': '负1-5', 'l2': '负6-10', 'l3': '负11-15', 'l4': '负16-20', 'l5': '负21-25', 'l6': '负26+',
}


def fetch_text(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        for enc in ('utf-8', 'gbk', 'gb2312'):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode('utf-8', errors='ignore')
    except Exception as e:
        print('  [fetch-fail] %s -> %s' % (url, e))
        return ''


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


def site_key(passphrase=None):
    pw = passphrase if passphrase is not None else os.environ.get('SITE_PASS', '')
    if not pw:
        return None
    material = (SITE_USER + '\x00' + pw).encode('utf-8')
    return hashlib.pbkdf2_hmac('sha256', material, KDF_SALT, KDF_ITER, 32)


def encrypt_obj(obj, key):
    aes = _aesgcm()(key)
    nonce = os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ct = aes.encrypt(nonce, plain, None)
    return {
        'v': 1, 'alg': 'AES-256-GCM', 'kdf': 'PBKDF2-HMAC-SHA256',
        'iter': KDF_ITER, 'salt': base64.b64encode(KDF_SALT).decode(),
        'iv': base64.b64encode(nonce).decode(), 'ct': base64.b64encode(ct).decode(),
        'bytes': len(plain),
    }


def parse_table(html, playid):
    """
    解析 500 篮球赛表：编号 / 主客队 / 开赛时间 / 玩法赔率。
    返回 {match_id: {...}}。
    """
    rows = {}
    # 每条比赛记录对应一个 <tr data-id="...">，内部含编号、队名、赔率
    trs = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    if not trs:
        # 兼容 <div class="bet_table"> 或 tbody 之外的结构
        trs = re.findall(r'id="tr_(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in trs:
        rec = rows.setdefault(mid, {'match_id': mid})
        # 编号（周一301 类）
        m = re.search(r'>([周][一二三四五六日]\d{2,3})<', body)
        if m:
            rec['num'] = m.group(1).strip()
        # 队名
        teams = re.findall(r'<span[^>]*>([^<]+)</span>', body)
        if 'home' not in rec and len(teams) >= 2:
            rec['home'] = teams[0].strip()
            rec['away'] = teams[1].strip()
        # 开赛时间
        m = re.search(r'(\d{2}:\d{2})', body)
        if m:
            rec['kickoff'] = m.group(1)
        # 赔率：data-sp="x.xx" data-k="..."  或  class="betOdds" 内的数字
        sps = re.findall(r'data-sp="([\d.]+)"[^>]*data-k="(\d+)"', body)
        if sps:
            rec.setdefault('odds', {})
            for sp, k in sps:
                rec['odds'][k] = float(sp)
        else:
            # 退路：取所有纯数字赔率
            nums = re.findall(r'>([1-9]\d*\.\d{2})<', body)
            if nums:
                rec.setdefault('odds', {})
                for i, n in enumerate(nums):
                    rec['odds']['g%d' % i] = float(n)
        # 让分值（让分胜负页 data-h="..." 或 pk 字段）
        m = re.findall(r'data-h="(-?\d+\.?\d*)"', body)
        if m:
            rec['handicap'] = float(m[0])
        # 预设总分（大小分页）
        m = re.findall(r'data-d="(-?\d+\.?\d*)"', body)
        if m:
            rec['total'] = float(m[0])
    return rows


def parse_result(html):
    """解析开奖赛果：主客得分 / 胜负。"""
    res = {}
    trs = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in trs:
        m = re.search(r'>(\d+)<[^>]*>(\d+)<', body)
        if m:
            res[mid] = {'score': '%s-%s' % (m.group(1), m.group(2))}
    return res


def grab_day(day):
    """抓取某销售日四种玩法的全部比赛并合并。"""
    merged = {}
    # 胜负页作为主结构（含编号/队名/时间）
    html = fetch_text('https://trade.500.com/jclq/?playid=%d&g=2&date=%s' % (PLAYID['sf'], day))
    if html:
        merged = parse_table(html, PLAYID['sf'])
    for name, pid in PLAYID.items():
        if name == 'sf':
            continue
        html = fetch_text('https://trade.500.com/jclq/?playid=%d&g=2&date=%s' % (pid, day))
        if not html:
            continue
        for mid, rec in parse_table(html, pid).items():
            base = merged.setdefault(mid, {'match_id': mid})
            if 'odds' in rec:
                base.setdefault('odds', {}).update(rec['odds'])
            if 'handicap' in rec:
                base['handicap'] = rec['handicap']
            if 'total' in rec:
                base['total'] = rec['total']
            for k in ('num', 'home', 'away', 'kickoff'):
                if k in rec and k not in base:
                    base[k] = rec[k]
    # 赛果回填
    if merged:
        html = fetch_text('https://trade.500.com/jclq/?playid=%d&g=2&date=%s' % (PLAYID['sf'], day))
        for mid, r in parse_result(html).items():
            if mid in merged:
                merged[mid].setdefault('result', {}).update(r)
                merged[mid]['settled'] = True
    return merged


def build():
    days = {}
    order = [TODAY]
    d0 = datetime.now(CST)
    for i in range(1, DAYS):
        order.insert(0, (d0 - timedelta(days=i)).strftime('%Y-%m-%d'))
    order.append(((d0 + timedelta(days=1)).strftime('%Y-%m-%d')))  # 明日预售
    for day in order:
        print('=== 抓取篮球 %s ===' % day)
        merged = grab_day(day)
        days[day] = {
            'date': day,
            'count': len(merged),
            'matches': list(merged.values()),
        }
        print('  场次 %d' % len(merged))
        for m in list(merged.values())[:3]:
            print('    %s %s vs %s %s' % (
                m.get('num', '?'), m.get('home', ''), m.get('away', ''), m.get('kickoff', '')))
    return {
        'v': 1,
        'source': SOURCE,
        'updated_at': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
        'playid': PLAYID,
        'sfdiff': SFDIFF_LABEL,
        'days': days,
    }


def main():
    data = build()
    key = site_key()
    if not key:
        print('警告：未配置 SITE_PASS，跳过本轮加密输出（保留现有密文，不写明文）')
        return
    wrap = encrypt_obj(data, key)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(wrap, f, ensure_ascii=False, separators=(',', ':'))
    print('[OK] 已加密写入 %s （%d 个销售日）' % (OUT, len(data['days'])))


if __name__ == '__main__':
    main()


def decrypt_obj(wrap, key):
    aes = _aesgcm()(key)
    nonce = base64.b64decode(wrap['iv'])
    plain = aes.decrypt(nonce, base64.b64decode(wrap['ct']), None)
    return json.loads(plain.decode('utf-8'))


def safe_load(path):
    key = site_key()
    if key:
        try:
            wrap = json.load(open(path, encoding='utf-8'))
            if wrap.get('ct'):
                return decrypt_obj(wrap, key)
        except Exception:
            pass
    return None
