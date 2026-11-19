#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩篮球真实数据抓取（GitHub Actions 自动运行）
数据源：500彩票网竞彩篮球 trade.500.com/jclq/
玩法：胜负(sf) / 让分胜负(rfsf) / 大小分(dxf) / 胜分差(sfc)
产物：basketball.json（明文）+ basketball.enc（AES-256-GCM 加密）

数据来源说明：
- 500彩票网竞彩篮球页面按玩法分页，不同 playid 对应不同玩法
- playid=291 胜负 / playid=292 让分胜负 / playid=293 大小分 / playid=294 胜分差
- 本脚本分别抓取各页面后合并为完整数据
"""
import os, re, json, base64, hashlib, sys, time
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime('%Y-%m-%d')
HERE = os.path.dirname(os.path.abspath(__file__))

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Referer': 'https://trade.500.com/jclq/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

# 四种玩法的页面 URL
PLAY_URLS = {
    'sf':   'https://trade.500.com/jclq/?date={date}&g=2&playid=291',  # 胜负
    'rfsf': 'https://trade.500.com/jclq/?date={date}&g=2&playid=292',  # 让分胜负
    'dxf':  'https://trade.500.com/jclq/?date={date}&g=2&playid=293',  # 大小分
    'sfc':  'https://trade.500.com/jclq/?date={date}&g=2&playid=294',  # 胜分差
}
SOURCE = '500\u5f69\u7968\u7f51\u7ade\u5f69\u7bee\u7403'

SFC_MAP = {
    '01': '\u5ba2\u80dc1-5', '02': '\u5ba2\u80dc6-10', '03': '\u5ba2\u80dc11-15',
    '04': '\u5ba2\u80dc16-20', '05': '\u5ba2\u80dc21-25', '06': '\u5ba2\u80dc26+',
    '11': '\u4e3b\u80dc1-5', '12': '\u4e3b\u80dc6-10', '13': '\u4e3b\u80dc11-15',
    '14': '\u4e3b\u80dc16-20', '15': '\u4e3b\u80dc21-25', '16': '\u4e3b\u80dc26+',
}


def log(msg):
    print(msg, flush=True)


def fetch(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
            for enc in ('gb18030', 'gbk', 'utf-8'):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode('utf-8', errors='ignore')
        except Exception as e:
            last = e
            log(f'  [retry {i+1}] {url.split("playid=")[1][:3]} -> {str(e)[:80]}')
            time.sleep(3)
    log(f'  [FAIL] {url.split("playid=")[1][:3]} -> {str(last)[:80]}')
    return ''


def parse_base(html):
    """解析基本信息：编号、联赛、队名、比分、状态"""
    blocks = re.split(r'\s+(?=<tr\b)', html)
    rows = []
    for b in blocks:
        if 'data-matchnum' not in b:
            continue
        num = re.search(r'data-matchnum="([^"]+)"', b)
        league = re.search(r'data-simpleleague="([^"]+)"', b)
        away = re.search(r'class="team-l"[^>]*title="([^"]+)"', b)
        home = re.search(r'class="team-r"[^>]*title="([^"]+)"', b)
        score = re.search(r'class="score[^"]*"[^>]*>([^<]+)</a>', b)
        end = re.search(r'data-buyendtime="([^"]+)"', b)
        isend = re.search(r'data-isend="(\d)"', b)
        total = re.search(r'<p class="betmsg"><span class="gray">([\d.]+)</span>', b)
        hcp = re.search(r'<p class="betmsg"><span class="red"[^>]*>([+-]?[\d.]+)</span>', b)
        rows.append({
            'num': num.group(1) if num else '',
            'league': league.group(1) if league else '',
            'away': away.group(1) if away else '',
            'home': home.group(1) if home else '',
            'score': score.group(1).strip() if score else '',
            'buyend': end.group(1) if end else '',
            'isend': isend.group(1) if isend else '',
            'total': float(total.group(1)) if total else None,
            'hcp': float(hcp.group(1)) if hcp else None,
        })
    return rows


def parse_odds(html, play_type):
    """解析特定玩法的赔率"""
    blocks = re.split(r'\s+(?=<tr\b)', html)
    result = {}
    for b in blocks:
        if 'data-matchnum' not in b:
            continue
        num = re.search(r'data-matchnum="([^"]+)"', b)
        if not num:
            continue
        n = num.group(1)
        if play_type == 'sfc':
            odds = re.findall(r'data-type="sfc"\s+data-value="(\d+)"\s+data-sp="([\d.]+)"', b)
            if odds:
                result[n] = {SFC_MAP.get(v, v): float(s) for v, s in odds}
        else:
            odds = re.findall(r'data-type="%s"\s+data-value="(\d+)"\s+data-sp="([\d.]+)"' % play_type, b)
            if odds:
                result[n] = {v: float(s) for v, s in odds}
    return result


def weekday_cn(d):
    return '\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u65e5'[d.weekday()]


def compute_result(m):
    s = re.match(r'\s*(\d+)\s*[:\uff1a]\s*(\d+)\s*$', m.get('score', ''))
    if not s:
        return m
    a, h = int(s.group(1)), int(s.group(2))
    tot = a + h
    res = dict(m)
    res['away_score'] = a
    res['home_score'] = h
    res['total_score'] = tot
    res['sf_result'] = 'away_win' if a > h else 'home_win'
    if m.get('hcp') is not None:
        ha = a + m['hcp']
        res['rfsf_result'] = 'away_win' if ha > h else ('home_win' if ha < h else 'draw')
    if m.get('total') is not None:
        res['dxf_result'] = 'over' if tot > m['total'] else ('under' if tot < m['total'] else 'draw')
    diff = abs(a - h)
    if a > h:
        if diff <= 5: res['sfc_result'] = '\u5ba2\u80dc1-5'
        elif diff <= 10: res['sfc_result'] = '\u5ba2\u80dc6-10'
        elif diff <= 15: res['sfc_result'] = '\u5ba2\u80dc11-15'
        elif diff <= 20: res['sfc_result'] = '\u5ba2\u80dc16-20'
        elif diff <= 25: res['sfc_result'] = '\u5ba2\u80dc21-25'
        else: res['sfc_result'] = '\u5ba2\u80dc26+'
    else:
        if diff <= 5: res['sfc_result'] = '\u4e3b\u80dc1-5'
        elif diff <= 10: res['sfc_result'] = '\u4e3b\u80dc6-10'
        elif diff <= 15: res['sfc_result'] = '\u4e3b\u80dc11-15'
        elif diff <= 20: res['sfc_result'] = '\u4e3b\u80dc16-20'
        elif diff <= 25: res['sfc_result'] = '\u4e3b\u80dc21-25'
        else: res['sfc_result'] = '\u4e3b\u80dc26+'
    res['diff'] = diff
    res['settled'] = True
    return res


def fetch_day(date_str):
    log(f'\u6293\u53d6 {date_str} ...')
    
    # 1. 先抓胜负页面获取基本信息
    base_html = fetch(PLAY_URLS['sf'].format(date=date_str))
    if not base_html:
        log(f'  {date_str} \u65e0\u6570\u636e')
        return []
    
    rows = parse_base(base_html)
    if not rows:
        log(f'  {date_str} \u65e0\u6bd4\u8d5b')
        return []
    
    # 用编号作为 key 建立字典
    matches = {r['num']: r for r in rows}
    
    # 2. 抓让分胜负
    rfsf_html = fetch(PLAY_URLS['rfsf'].format(date=date_str))
    if rfsf_html:
        rfsf_odds = parse_odds(rfsf_html, 'rfsf')
        for n, odds in rfsf_odds.items():
            if n in matches:
                matches[n]['rfsf'] = odds
    
    # 3. 抓大小分
    dxf_html = fetch(PLAY_URLS['dxf'].format(date=date_str))
    if dxf_html:
        dxf_odds = parse_odds(dxf_html, 'dxf')
        for n, odds in dxf_odds.items():
            if n in matches:
                matches[n]['dxf'] = odds
    
    # 4. 抓胜分差
    sfc_html = fetch(PLAY_URLS['sfc'].format(date=date_str))
    if sfc_html:
        sfc_odds = parse_odds(sfc_html, 'sfc')
        for n, odds in sfc_odds.items():
            if n in matches:
                matches[n]['sfc'] = odds
    
    # 5. 确保 sf 赔率存在
    sf_odds = parse_odds(base_html, 'sf')
    for n, odds in sf_odds.items():
        if n in matches:
            matches[n]['sf'] = odds
    
    # 6. 补充默认值
    for n, m in matches.items():
        m.setdefault('sf', {})
        m.setdefault('rfsf', {})
        m.setdefault('dxf', {})
        m.setdefault('sfc', {})
    
    result_list = [compute_result(m) for m in matches.values()]
    log(f'  {date_str} \u6293\u5230 {len(result_list)} \u573a')
    return result_list


def main():
    log(f'=== \u7ade\u5f69\u7bee\u7403\u6293\u53d6 {TODAY} ===')
    days = {}
    today_d = datetime.now(CST)
    for i in range(7):
        d = today_d - timedelta(days=i)
        ds = d.strftime('%Y-%m-%d')
        rows = fetch_day(ds)
        if rows:
            days[ds] = {
                'date': ds,
                'weekday': weekday_cn(d),
                'count': len(rows),
                'matches': rows,
            }
    out = {
        'real': True,
        'source': SOURCE,
        'updated_at': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
        'days': days,
        'days_order': sorted(days.keys(), reverse=True),
    }
    json_path = os.path.join(HERE, 'basketball.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    total = sum(d['count'] for d in days.values())
    settled = sum(1 for d in days.values() for m in d['matches'] if m.get('settled'))
    log(f'\u5b8c\u6210\uff1a{len(days)} \u4e2a\u9500\u552e\u65e5\uff0c{total} \u573a\uff0c\u5df2\u5f00\u5956 {settled} \u573a')
    
    if total == 0:
        log('WARNING: \u6293\u5230 0 \u573a\uff0c\u4e0d\u8986\u76d6\u5df2\u6709 enc')
        return
    
    print(json.dumps({'days': len(days), 'matches': total, 'settled': settled}, ensure_ascii=False))


if __name__ == '__main__':
    main()
