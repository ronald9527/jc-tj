#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩篮球真实数据抓取（GitHub Actions 自动运行）
数据源：500彩票网竞彩篮球 trade.500.com/jclq/
正确 playid：
  playid=313  胜负（sf）
  playid=275  让分胜负（rfsf）
  playid=281  大小分（dxf）
  playid=291  胜分差（sfc）
产物：basketball.json（明文）+ basketball.enc（AES-256-GCM 加密）
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

# 正确 playid（实测确认）
PLAY_URLS = {
    'sf':   'https://trade.500.com/jclq/?date={date}&g=2&playid=313',
    'rfsf': 'https://trade.500.com/jclq/?date={date}&g=2&playid=275',
    'dxf':  'https://trade.500.com/jclq/?date={date}&g=2&playid=281',
    'sfc':  'https://trade.500.com/jclq/?date={date}&g=2&playid=291',
}
SOURCE = '500彩票网竞彩篮球（国内数据）'

SFC_KEYS = [
    ('11', '客胜1-5'), ('12', '客胜6-10'), ('13', '客胜11-15'),
    ('14', '客胜16-20'), ('15', '客胜21-25'), ('16', '客胜26+'),
    ('01', '主胜1-5'), ('02', '主胜6-10'), ('03', '主胜11-15'),
    ('04', '主胜16-20'), ('05', '主胜21-25'), ('06', '主胜26+'),
]
SFC_DICT = dict(SFC_KEYS)


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
                    return raw.decode(enc, errors='replace')
                except Exception:
                    continue
            return raw.decode('utf-8', errors='replace')
        except Exception as e:
            last = e
            log(f'  [retry {i+1}] {str(e)[:100]}')
            time.sleep(2)
    log(f'  [FAIL] {str(last)[:100]}')
    return ''


def get_main_trs(html):
    """获取所有主tr（含data-matchnum）"""
    return re.findall(r'<tr\b[^>]*data-matchnum="([^"]+)"[^>]*>(.*?)</tr>', html, re.DOTALL)


def get_more_wraps(html):
    """获取所有bet-more-wrap的(tr位置, 内容)"""
    return [(m.start(), m.group(0)) for m in
            re.finditer(r'<tr\b[^>]*bet-more-wrap[^>]*>(.*?)</tr>', html, re.DOTALL)]


def get_all_tr_positions(html):
    """获取所有主tr的(matchnum, 位置)"""
    return [(re.search(r'data-matchnum="([^"]+)"', tr).group(1), m.start())
            for tr, m in [(mt.group(0), mt) for mt in
                          re.finditer(r'<tr\b[^>]*data-matchnum="([^"]+)"[^>]*>.*?</tr>', html, re.DOTALL)]]


def parse_basic(tr_html, matchnum):
    """从主tr提取基本信息"""
    m = {'matchnum': matchnum}

    league = re.search(r'data-simpleleague="([^"]*)"', tr_html)
    m['league'] = league.group(1) if league else ''

    away = re.search(r'class="team-l"[^>]*title="([^"]+)"', tr_html)
    home = re.search(r'class="team-r"[^>]*title="([^"]+)"', tr_html)
    m['away'] = away.group(1) if away else ''
    m['home'] = home.group(1) if home else ''

    score = re.search(r'class="score[^"]*"[^>]*>([^<]+)</a>', tr_html)
    m['score'] = score.group(1).strip() if score else ''

    kickoff = re.search(r'data-matchtime="([^"]+)"', tr_html)
    if not kickoff:
        kickoff = re.search(r'class="td-endtime"[^>]*title="([^"]+)开始"', tr_html)
    m['kickoff'] = kickoff.group(1) if kickoff else ''

    ranks = re.findall(r'title="排名([^"]+)"', tr_html)
    m['ranks'] = ranks

    return m


def parse_sf(tr_html):
    odds = re.findall(r'data-type="sf"\s+data-value="(\d+)"\s+data-sp="([\d.]+)"', tr_html)
    return {v: float(s) for v, s in odds}


def parse_rfsf(tr_html):
    odds = re.findall(r'data-type="rfsf"\s+data-value="(\d+)"\s+data-sp="([\d.]+)"', tr_html)
    hcp = re.search(r'class="betmsg"><span[^>]*>([+-]?[\d.]+)</span>', tr_html)
    hcp_val = None
    if hcp:
        try:
            hcp_val = float(hcp.group(1))
        except ValueError:
            pass
    return {v: float(s) for v, s in odds}, hcp_val


def parse_dxf(tr_html):
    odds = re.findall(r'data-type="dxf"\s+data-value="(\d+)"\s+data-sp="([\d.]+)"', tr_html)
    total = re.search(r'<p class="betmsg"><span class="gray">([\d.]+)</span>', tr_html)
    total_val = None
    if total:
        try:
            total_val = float(total.group(1))
        except ValueError:
            pass
    return {v: float(s) for v, s in odds}, total_val


def parse_sfc_from_more(more_html):
    """从bet-more-wrap中提取胜分差12项赔率"""
    odds = re.findall(r'data-type="sfc"\s+data-value="(\d+)"\s+data-sp="([\d.]+)"', more_html)
    return {SFC_DICT.get(v, v): float(s) for v, s in odds}


def find_more_wrap(matchnum, main_positions, more_positions):
    """找到某个matchnum对应的bet-more-wrap"""
    if matchnum not in main_positions:
        return []
    pos = main_positions[matchnum]
    for mp, mc in more_positions:
        if mp > pos:
            return parse_sfc_from_more(mc)
    return []


def compute_result(m):
    """计算赛果"""
    res = dict(m)
    res['settled'] = False

    sm = re.match(r'\s*(\d+)\s*[:\uff1a]\s*(\d+)\s*$', m.get('score', ''))
    if not sm:
        return res

    away_s = int(sm.group(1))
    home_s = int(sm.group(2))
    total = away_s + home_s
    diff = abs(away_s - home_s)

    res['away_score'] = away_s
    res['home_score'] = home_s
    res['total_score'] = total
    res['diff'] = diff
    res['settled'] = True
    res['sf_result'] = 'away_win' if away_s > home_s else 'home_win'

    if m.get('hcp') is not None:
        adj = away_s + m['hcp']
        if adj > home_s:
            res['rfsf_result'] = 'away_win'
        elif adj < home_s:
            res['rfsf_result'] = 'home_win'
        else:
            res['rfsf_result'] = 'draw'

    if m.get('total_line') is not None:
        if total > m['total_line']:
            res['dxf_result'] = 'over'
        elif total < m['total_line']:
            res['dxf_result'] = 'under'
        else:
            res['dxf_result'] = 'draw'

    if away_s > home_s:
        if diff <= 5: res['sfc_result'] = '客胜1-5'
        elif diff <= 10: res['sfc_result'] = '客胜6-10'
        elif diff <= 15: res['sfc_result'] = '客胜11-15'
        elif diff <= 20: res['sfc_result'] = '客胜16-20'
        elif diff <= 25: res['sfc_result'] = '客胜21-25'
        else: res['sfc_result'] = '客胜26+'
    else:
        if diff <= 5: res['sfc_result'] = '主胜1-5'
        elif diff <= 10: res['sfc_result'] = '主胜6-10'
        elif diff <= 15: res['sfc_result'] = '主胜11-15'
        elif diff <= 20: res['sfc_result'] = '主胜16-20'
        elif diff <= 25: res['sfc_result'] = '主胜21-25'
        else: res['sfc_result'] = '主胜26+'

    return res


def fetch_day(date_str):
    log(f'抓取 {date_str} ...')

    # 胜负页面（含基本信息 + 胜负赔率）
    sf_html = fetch(PLAY_URLS['sf'].format(date=date_str))
    if not sf_html:
        log(f'  {date_str} 无数据')
        return []

    main_trs = get_main_trs(sf_html)
    if not main_trs:
        log(f'  {date_str} 无比赛')
        return []

    # 建立基本数据
    matches = {}
    for num, tr in main_trs:
        matches[num] = parse_basic(tr, num)
        sf_odds = parse_sf(tr)
        if sf_odds:
            matches[num]['sf_odds'] = sf_odds

    log(f'  {date_str} 基础: {len(matches)} 场')

    # 让分胜负
    rfsf_html = fetch(PLAY_URLS['rfsf'].format(date=date_str))
    if rfsf_html:
        for num, tr in get_main_trs(rfsf_html):
            if num in matches:
                odds, hcp = parse_rfsf(tr)
                if odds:
                    matches[num]['rfsf_odds'] = odds
                if hcp is not None:
                    matches[num]['hcp'] = hcp

    # 大小分
    dxf_html = fetch(PLAY_URLS['dxf'].format(date=date_str))
    if dxf_html:
        for num, tr in get_main_trs(dxf_html):
            if num in matches:
                odds, total = parse_dxf(tr)
                if odds:
                    matches[num]['dxf_odds'] = odds
                if total is not None:
                    matches[num]['total_line'] = total

    # 胜分差（页面含主tr + more_wrap）
    sfc_html = fetch(PLAY_URLS['sfc'].format(date=date_str))
    if sfc_html:
        more_positions = get_more_wraps(sfc_html)
        main_positions = dict(get_all_tr_positions(sfc_html))
        for num, tr in get_main_trs(sfc_html):
            if num in matches:
                sfc_odds = find_more_wrap(num, main_positions, more_positions)
                if sfc_odds:
                    matches[num]['sfc_odds'] = sfc_odds

    # 补充默认值
    for n, m in matches.items():
        m.setdefault('sf_odds', {})
        m.setdefault('rfsf_odds', {})
        m.setdefault('dxf_odds', {})
        m.setdefault('sfc_odds', {})
        m.setdefault('hcp', None)
        m.setdefault('total_line', None)

    results = [compute_result(m) for m in matches.values()]
    log(f'  {date_str} 完成: {len(results)} 场')
    return results


def weekday_cn(d):
    return '一二三四五六日'[d.weekday()]


def build_output(days_data):
    out = {
        'v': 1,
        'real': True,
        'source': SOURCE,
        'updated_at': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
        'days': {},
        'days_order': [],
    }
    for ds in sorted(days_data.keys(), reverse=True):
        matches = days_data[ds]
        d_obj = datetime.strptime(ds, '%Y-%m-%d')
        formatted = []
        for m in matches:
            item = {
                'matchnum': m['matchnum'],
                'away': m['away'],
                'home': m['home'],
                'league': m['league'],
                'kickoff': m['kickoff'],
                'score': m.get('score', ''),
                'ranks': m.get('ranks', []),
                'settled': m.get('settled', False),
                'sf': {
                    'win': m['sf_odds'].get('2'),
                    'lose': m['sf_odds'].get('1'),
                },
                'rfsf': {
                    'line': m.get('hcp'),
                    'away_sp': m['rfsf_odds'].get('2'),
                    'home_sp': m['rfsf_odds'].get('1'),
                },
                'dxf': {
                    'line': m.get('total_line'),
                    'over_sp': m['dxf_odds'].get('1'),
                    'under_sp': m['dxf_odds'].get('2'),
                },
                'sfc': m.get('sfc_odds', {}),
            }
            if m.get('settled'):
                item['away_score'] = m.get('away_score')
                item['home_score'] = m.get('home_score')
                item['total_score'] = m.get('total_score')
                item['diff'] = m.get('diff')
                item['result'] = {
                    'sf': m.get('sf_result'),
                    'rfsf': m.get('rfsf_result'),
                    'dxf': m.get('dxf_result'),
                    'sfc': m.get('sfc_result'),
                }
            formatted.append(item)

        out['days'][ds] = {
            'date': ds,
            'weekday': weekday_cn(d_obj),
            'count': len(formatted),
            'matches': formatted,
        }
        out['days_order'].append(ds)
    return out


def safe_load(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            data = f.read()
        if not data or data[0] != 1:
            return None
        iv = data[1:13]
        ct = data[13:]
        passwd = os.environ.get('SITE_PASS', '')
        if not passwd:
            return None
        salt = b'jc-tj-2026-static-salt-v1'
        key = hashlib.pbkdf2_hmac('sha256', passwd.encode('utf-8'), salt, 210000, dklen=32)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(iv, ct, None)
        return json.loads(plaintext.decode('utf-8'))
    except Exception as e:
        log(f'解密失败: {e}')
        return None


def encrypt_and_save(data, passwd):
    plaintext = json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')
    salt = b'jc-tj-2026-static-salt-v1'
    key = hashlib.pbkdf2_hmac('sha256', passwd.encode('utf-8'), salt, 210000, dklen=32)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    aesgcm = AESGCM(key)
    encrypted = aesgcm.encrypt(iv, plaintext, None)
    out = b'\x01' + iv + encrypted
    with open(os.path.join(HERE, 'basketball.enc'), 'wb') as f:
        f.write(out)
    log(f'加密保存: {len(out)} bytes')


def main():
    log(f'=== 竞彩篮球抓取 {TODAY} ===')

    days_data = {}
    today_d = datetime.now(CST)
    for i in range(7):
        d = today_d - timedelta(days=i)
        ds = d.strftime('%Y-%m-%d')
        rows = fetch_day(ds)
        if rows:
            days_data[ds] = rows

    if not days_data:
        log('WARNING: 未抓到任何数据')
        return

    out = build_output(days_data)

    with open(os.path.join(HERE, 'basketball.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    total = sum(d['count'] for d in out['days'].values())
    settled = sum(1 for d in out['days'].values() for m in d['matches'] if m.get('settled'))
    log(f'完成: {len(out["days"])} 个销售日, {total} 场, 已开奖 {settled} 场')

    passwd = os.environ.get('SITE_PASS', '')
    if passwd:
        encrypt_and_save(out, passwd)
    else:
        log('WARNING: SITE_PASS 未设置')


if __name__ == '__main__':
    main()
