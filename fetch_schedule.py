#!/usr/bin/env python3
# 每日自动抓取「国内竞彩足球赛程 + 带日期的历史赛果」。
# 纯标准库，GitHub Actions 直接跑，0 元、免 key、无需服务器。
#
# 数据源：500 彩票网竞彩足球页（国内，实测 GitHub Actions 出口可直连）。
#   https://live.500.com/jczq.php?e=YYYY-MM-DD   指定销售日（历史/当天）
# 页面表格直接含竞彩编号、赛事、轮次、开赛时间、中文队名、让球、盘口、
# 全场比分、半场比分、胜平负赛果、状态。全部照抄，不臆造、不混别的赛事。
#
# 产出（均为密文，仓库里不再有明文数据）：
#   data.enc     当天赛程
#   history.enc  最近 8 个销售日，按日期分组，供页面切换查看历史赛果
# 密钥由「账号 admin + 用户密码」经 PBKDF2-SHA256 派生；密码只存在
# GitHub Actions Secret（SITE_PASS），不写进任何公开源码、日志或数据文件。
import json, os, re, html, urllib.request, urllib.error
import math, statistics, base64, hashlib
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'data.enc')
HIST = os.path.join(HERE, 'history.enc')
LEGACY_OUT = os.path.join(HERE, 'data.json')
LEGACY_HIST = os.path.join(HERE, 'history.json')

SITE_USER = 'admin'
KDF_SALT = b'jc-tj-2026-static-salt-v1'
KDF_ITER = 210000
CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)
TODAY = NOW.strftime('%Y-%m-%d')
YEAR = NOW.year
DAYS = 10                     # 含今天，往前 9 天（滚动窗口不挤掉展示过的历史）
SOURCE = '500彩票网竞彩足球（国内数据）'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*; q=0.01',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Referer': 'https://live.500.com/jczq.php',
}
BASE = 'https://live.500.com/jczq.php'


def fetch_text(url, timeout=20):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(3000000)
    # 500 全站 GBK 系，个别页面混入少量非 GBK 字节，用 replace 容错；
    # 若整段 try/except 退回 latin1 会变成 ¡¾¾º²Ê 乱码。
    return raw.decode('gb18030', errors='replace')


def txt(x):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', x or ''))).strip()


def full_date(md):
    """'11-16' 补全年份；跨年时按月份差修正。"""
    try:
        mm, dd = md.split('-')
        m, d = int(mm), int(dd)
        y = YEAR
        if NOW.month >= 11 and m <= 2:
            y += 1
        elif NOW.month <= 2 and m >= 11:
            y -= 1
        return '%04d-%02d-%02d' % (y, m, d)
    except Exception:
        return md


def parse_team(td):
    """主队/客队单元格 → (中文队名, 让球数, 500球队ID, 页面标注排名)。"""
    s = td
    lm = re.search(r'<span[^>]*class="sp_(?:sr|rq)"[^>]*>\s*\(([+-]?\d+(?:\.\d+)?)\)\s*</span>', s, re.I)
    line = lm.group(1) if lm else ''
    am = re.search(r'<a[^>]*href="[^"]*/team/(\d+)/[^"]*"[^>]*>(.*?)</a>', s, re.S | re.I)
    tid = am.group(1) if am else ''
    name = txt(am.group(2)) if am else ''
    if not name:
        name = txt(re.sub(r'<span[^>]*>.*?</span>', '', s, flags=re.S | re.I))
    # 单元格里的 [n] 是联赛内排名（真实数据，来自 500 竞彩页）
    rm = re.search(r'<span[^>]*class="gray"[^>]*>\s*\[(\d+)\]\s*</span>', s, re.I)
    rank = int(rm.group(1)) if rm else None
    return name, line, tid, rank


def parse_odds(text):
    """解析页面内嵌的 var liveOddsList = {...};
    key 是 fid，value 含：
      sp   竞彩官方「胜负奖金」= 胜/平/负（与体彩官网固定奖金一致）
      rqsp 竞彩官方「让球奖金」= 让球胜/平/负
      0    平均欧赔；3=36*；5=澳*；280=皇*；293=威***；1055=另一家
    """
    m = re.search(r'var\s+liveOddsList\s*=\s*(\{.*?\})\s*;', text, re.S)
    if not m:
        return {}
    try:
        raw = json.loads(m.group(1))
    except Exception:
        return {}
    out = {}
    for fid, v in raw.items():
        if not isinstance(v, dict):
            continue
        def triple(k):
            a = v.get(k) or []
            if len(a) < 3:
                return None
            try:
                return {'h': float(a[0]), 'd': float(a[1]), 'a': float(a[2])}
            except (TypeError, ValueError):
                return None
        out[str(fid)] = {
            'sp': triple('sp'), 'rqsp': triple('rqsp'),
            'euro': triple('0'), 'macau': triple('5'),
            'william': triple('293'), 'ladbrokes': triple('3'),
            'crown': triple('280'), 'snaitech': triple('1055'),
            # 全部公司原始赔率，用于离散度 / 凯利方差计算（key 沿用 500 页面标识）
            'companies': {k: triple(k) for k in ('0', '3', '5', '280', '293', '1055')},
        }
    return out


def parse_500(text, sale_date):
    """解析 500 彩票网竞彩足球表格（14 列）。sale_date 为 ?e= 指定的销售日。"""
    odds_book = parse_odds(text)
    out = []
    for attrs, fid, tr in re.findall(r'<tr([^>]*id="a(\d+)"[^>]*)>(.*?)</tr>', text, re.S | re.I):
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S | re.I)
        if len(tds) < 11:
            continue
        c = [txt(x) for x in tds]
        num = c[0]
        if not re.match(r'^周[一二三四五六日]\d{3}$', num):
            continue

        lg, rnd, tm = c[1], c[2], c[3]
        st = c[4] or '未开赛'
        if st == '完':
            st = '已完成'

        h, line, hid, rank_h = parse_team(tds[5])
        a, _, aid, rank_a = parse_team(tds[7])
        if not (h and a):
            continue
        sm = re.search(r'\bsid="(\d+)"', attrs)
        sid = sm.group(1) if sm else ''

        dt = tm.split()
        date = full_date(dt[0]) if dt else sale_date
        time = dt[1] if len(dt) > 1 else ''

        # 第 6 列 <div class="pk">：clt1/clt3 是全场比分主客进球，中间是盘口中文
        pk = tds[6]
        g1 = re.search(r'class="clt1"[^>]*>\s*(\d+)\s*<', pk)
        g3 = re.search(r'class="clt3"[^>]*>\s*(\d+)\s*<', pk)
        hcap = re.search(r'data-ao="([^"]*)"', pk)
        full = '%s:%s' % (g1.group(1), g3.group(1)) if (g1 and g3) else ''
        handicap_text = hcap.group(1) if hcap else txt(re.sub(r'<a[^>]*class="clt[13]"[^>]*>.*?</a>', '', pk, flags=re.S | re.I))

        # 第 8 列：半场比分；第 10 列：胜平负赛果（sel_result 默认「胜负」）
        half = re.sub(r'\s+', '', c[8]) if len(c) > 8 else ''
        result = c[10] if len(c) > 10 else ''
        result = re.sub(r'\s+', '', result)
        if result not in ('胜', '平', '负'):
            result = ''

        # 让球胜平负：按全场比分与让球数推算，明确标注为推算
        result_rq = ''
        if full and line and st == '已完成':
            try:
                hg, ag = [int(v) for v in full.split(':')]
                d = hg + float(line) - ag
                result_rq = '胜' if d > 0 else ('平' if d == 0 else '负')
            except Exception:
                pass

        if st != '已完成':
            # 未开赛场次该列含义不明，不展示比分与赛果
            half = full = result = result_rq = ''

        ob = odds_book.get(fid, {})
        out.append({
            'num': num, 'weekday': num[:2],
            'lg': lg, 'round': rnd, 'date': date, 'time': time,
            'h': h, 'a': a, 'line': line,
            'handicap_text': handicap_text,
            'full': full, 'half': half,
            'result': result, 'result_rq': result_rq,
            'st': st,
            # sp = 竞彩官方胜平负奖金（与体彩官网固定奖金一致）
            'odds': ob.get('sp') or {'h': None, 'd': None, 'a': None},
            'odds_rq': ob.get('rqsp'),
            'odds_euro': ob.get('euro'),      # 平均欧赔
            'odds_macau': ob.get('macau'),    # 澳*
            'odds_william': ob.get('william'),
            'odds_books': {
                '平均欧赔': ob.get('euro'), '澳*': ob.get('macau'),
                '威***': ob.get('william'), '皇*': ob.get('crown'),
            },
            'odds_companies': ob.get('companies') or {},
            'fid': fid, 'sid': sid, 'hid': hid, 'aid': aid,
            'rank_h': rank_h, 'rank_a': rank_a,
            'sale_date': sale_date,
        })
    out.sort(key=lambda x: (x['weekday'], x['num']))
    return out


def page_sale_date(text):
    """页面 <select id="sel_expect"> 自己报的销售日；用来识别 ?e= 未生效而返回默认页。"""
    m = re.search(r'<select[^>]*id="sel_expect"[^>]*>(.*?)</select>', text, re.S | re.I)
    if not m:
        return ''
    body = m.group(1)
    sel = re.search(r'<option[^>]*value="(\d{4}-\d{2}-\d{2})"[^>]*selected', body, re.I)
    if sel:
        return sel.group(1)
    opts = re.findall(r'<option[^>]*value="(\d{4}-\d{2}-\d{2})"', body, re.I)
    return opts[0] if opts else ''


TRADE = 'https://trade.500.com/jczq/index.php'
# 竞彩五个玩法的固定奖金页；date= 指定销售日，数据在 <p data-type data-value data-sp> 里
PLAY_PAGES = ((269, '胜平负/让球'), (270, '总进球'), (271, '比分'), (272, '半全场'))
SPF_MAP = {'3': '胜', '1': '平', '0': '负'}
BQC_MAP = {'3': '胜', '1': '平', '0': '负'}


def parse_play_page(text):
    """按 data-fixtureid 切块，抽出该场所有玩法的 (选项, 奖金)。"""
    out = {}
    marks = list(re.finditer(r'data-fixtureid="(\d+)"', text))
    for i, m in enumerate(marks):
        fid = m.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        block = text[m.start():end]
        rec = {}
        for tag in re.findall(r'<p[^>]*>', block):
            dt = re.search(r'data-type="([^"]+)"', tag)
            dv = re.search(r'data-value="([^"]+)"', tag)
            ds = re.search(r'data-sp="([^"]+)"', tag)
            if not (dt and dv and ds):
                continue
            kind = dt.group(1)
            if kind not in ('spf', 'nspf', 'jqs', 'bf', 'bqc'):
                continue
            rec.setdefault(kind, []).append((dv.group(1), ds.group(1)))
        if rec:
            out[fid] = rec
    return out


def norm_plays(raw):
    """把页面原始 value 转成中文标签，输出前端好渲染的数组。"""
    def num(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    def arr(kind, keymap=None, suffix=''):
        return [{'k': keymap.get(k, k + suffix) if keymap else k, 'v': num(v)}
                for k, v in (raw.get(kind) or [])]

    out = {
        # 页面里 nspf=胜平负（不让球），spf=让球胜平负；按实际奖金值对齐官方口径
        'spf': arr('nspf', SPF_MAP),
        'rqsp': arr('spf', SPF_MAP),
        'jqs': [{'k': (k + ' 球' if k != '7' else '7+ 球'), 'v': num(v)}
                for k, v in (raw.get('jqs') or [])],
        'bf': arr('bf'),
        'bqc': [{'k': (BQC_MAP.get(k.split('-')[0], k.split('-')[0]) +
                       BQC_MAP.get(k.split('-')[-1], k.split('-')[-1])),
                 'v': num(v)} for k, v in (raw.get('bqc') or []) if '-' in k],
    }
    return {k: v for k, v in out.items() if v}


# 竞彩比分玩法 31 项里的 26 个具体比分，其余归「胜其它 / 平其它 / 负其它」
STD_BF = ('1:0', '2:0', '2:1', '3:0', '3:1', '3:2', '4:0', '4:1', '4:2',
          '5:0', '5:1', '5:2', '0:0', '1:1', '2:2', '3:3',
          '0:1', '0:2', '1:2', '0:3', '1:3', '2:3', '0:4', '1:4', '2:4',
          '0:5', '1:5', '2:5')


def fill_results(m, checked_at=''):
    """完赛后自动回填：全场比分、胜平负、让球胜平负、半全场、总进球、比分。

    全部按竞彩官方口径由真实比分推算，并附上该场对应玩法的官方固定奖金。
    只改「赛果」字段，绝不动任何赔率/盘口原始数据。
    """
    st = m.get('st') or ''
    full = str(m.get('full') or '').strip()
    fm = re.match(r'^(\d+)\s*[:：]\s*(\d+)$', full)
    if not fm or not re.search(r'已完成|已结束|FT', st):
        m['settled'] = False
        m['results'] = []
        return False

    hg, ag = int(fm.group(1)), int(fm.group(2))
    res = '胜' if hg > ag else ('平' if hg == ag else '负')

    # 半全场：半场胜平负 + 全场胜平负
    hm = re.match(r'^\s*(\d+)\s*[-:：]\s*(\d+)\s*$', str(m.get('half') or ''))
    res_half, bqc = '', ''
    if hm:
        hh, ha = int(hm.group(1)), int(hm.group(2))
        res_half = '胜' if hh > ha else ('平' if hh == ha else '负')
        bqc = res_half + res

    # 让球胜平负：主队让球数加在全场比分上
    rq, rq_note = '', ''
    line = m.get('line')
    if line:
        try:
            d = hg + float(line) - ag
            rq = '胜' if d > 0 else ('平' if d == 0 else '负')
            rq_note = '主队让球 %s' % line
        except (TypeError, ValueError):
            rq_note = '让球数格式异常'
    else:
        rq_note = '该场未提供让球数'

    tot = hg + ag
    jqs = '7+ 球' if tot >= 7 else '%d 球' % tot
    bf = '%d:%d' % (hg, ag)
    if bf not in STD_BF:
        bf = res + '其它'

    plays = m.get('plays') or {}

    def prize(kind, k):
        for it in (plays.get(kind) or []):
            if it.get('k') == k and it.get('v'):
                return round(float(it['v']), 2)
        return None

    items = [
        {'key': 'full', 'name': '全场比分', 'value': '%d:%d' % (hg, ag),
         'prize': None, 'note': '90 分钟含伤停补时，不含加时/点球'},
        {'key': 'spf', 'name': '胜平负', 'value': res,
         'prize': prize('spf', res), 'note': '不让球，按全场比分'},
        {'key': 'rqsp', 'name': '让球胜平负', 'value': rq or '—',
         'prize': prize('rqsp', rq) if rq else None, 'note': rq_note},
        {'key': 'bqc', 'name': '半全场', 'value': bqc or '—',
         'prize': prize('bqc', bqc) if bqc else None,
         'note': '半场 %s ＋ 全场 %s' % (res_half or '—', res) if bqc else '半场比分未提供'},
        {'key': 'jqs', 'name': '总进球', 'value': jqs,
         'prize': prize('jqs', jqs), 'note': '全场双方进球合计，7 球及以上归 7+'},
        {'key': 'bf', 'name': '比分', 'value': bf,
         'prize': prize('bf', bf), 'note': '按竞彩 31 项比分口径'},
    ]

    m['result'] = res
    m['result_rq'] = rq
    m['result_half'] = res_half
    m['result_bqc'] = bqc
    m['result_jqs'] = jqs
    m['result_bf'] = bf
    m['goal_total'] = tot
    m['settled'] = True
    m['results'] = items
    if checked_at:
        m['settled_at'] = checked_at
    return True


def grab_plays(day):
    """抓某销售日 4 个玩法页，按 fid 合并。"""
    book, stats = {}, []
    for pid, name in PLAY_PAGES:
        url = '%s?playid=%d&g=2&date=%s' % (TRADE, pid, day)
        try:
            text = fetch_text(url, timeout=25)
            got = parse_play_page(text)
            stats.append({'pid': pid, 'name': name, 'rows': len(got)})
            for fid, rec in got.items():
                book.setdefault(fid, {}).update(rec)
        except Exception as e:
            stats.append({'pid': pid, 'name': name,
                          'err': '%s: %s' % (type(e).__name__, str(e)[:80])})
    return {fid: norm_plays(rec) for fid, rec in book.items()}, stats


ODDS_JSON = 'https://live.500.com/json/odds.php'
# 500 竞彩页盘口浮层用的两家公司：cid=5 是页面标注的「澳*」（澳门），
# cid=3 是默认参考盘（Be****）。接口对非 5 的 cid 一律回落默认，
# 因此免费源里只能稳定区分这两家，页面会如实标注。
HKMO_COMPANIES = (('澳门', '5'), ('香港', '3'))


def fetch_json(url, timeout=15):
    h = dict(HEADERS)
    h['Accept'] = 'application/json,*/*'
    h['X-Requested-With'] = 'XMLHttpRequest'
    h['Referer'] = 'https://live.500.com/jczq.php'
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(200000).decode('utf-8', errors='replace'))


def norm_hkmo(j):
    """odds.php 返回 [亚, 欧, 大] 三组，每组 [初盘 f, 当前 e]，各 3 项。
    500 自己的口径：e-f < 0 标 down（降水），>0 标 up（升水）。"""
    if not isinstance(j, list):
        return None
    out = {}
    specs = (('eu', 1, ('胜', '平', '负')),
             ('asia', 0, ('主水', '盘口', '客水')),
             ('ou', 2, ('大球', '盘口', '小球')))
    for key, idx, names in specs:
        if len(j) <= idx:
            continue
        pair = j[idx]
        if not (isinstance(pair, list) and len(pair) >= 2):
            continue
        f, e = pair[0], pair[1]
        if not (isinstance(f, list) and isinstance(e, list) and len(f) >= 3 and len(e) >= 3):
            continue
        items = []
        for k in range(3):
            fv, ev = f[k], e[k]
            if isinstance(fv, (int, float)) and isinstance(ev, (int, float)):
                diff = round(float(ev) - float(fv), 3)
                trend = 'down' if diff < 0 else ('up' if diff > 0 else 'flat')
            else:
                diff = None
                trend = 'text'   # 亚盘/大小球中间是中文盘口，不做数值差
            items.append({'k': names[k], 'f': fv, 'c': ev, 'd': diff, 't': trend})
        out[key] = items
    return out or None


def grab_hkmo(fids):
    """按 fid 并发抓两家公司的初盘/当前盘口。"""
    jobs = [(fid, label, cid) for fid in fids for label, cid in HKMO_COMPANIES]

    def one(job):
        fid, label, cid = job
        try:
            j = fetch_json('%s?fid=%s&cid=%s' % (ODDS_JSON, fid, cid))
            return fid, label, norm_hkmo(j)
        except Exception:
            return fid, label, None

    book, ok, fail = {}, 0, 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fid, label, data in ex.map(one, jobs):
            if data:
                book.setdefault(fid, {})[label] = data
                ok += 1
            else:
                fail += 1
    return book, {'ok': ok, 'fail': fail}


LEAGUE = 'https://liansai.500.com/zuqiu-%s/'
DETAIL = 'https://live.500.com/detail.php?fid=%s&r=1'


def grab_standings(sids):
    """抓联赛页的真实积分榜（排名/赛/胜/平/负/积分）与主客场场均进球。"""
    def one(sid):
        if not sid:
            return None, None
        try:
            s = fetch_text(LEAGUE % sid, timeout=14)
        except Exception:
            return sid, None
        teams = {}
        # 积分榜行：<td class="first">1</td><td><a href="/team/1296/" ...>仙台七夕</a></td>...
        pat = (r'<tr>\s*<td[^>]*class="first"[^>]*>\s*(\d+)\s*</td>\s*'
               r'<td[^>]*>\s*<a[^>]*href="/team/(\d+)/"[^>]*>(.*?)</a>\s*</td>\s*'
               r'<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>\s*'
               r'<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>\s*'
               r'<td[^>]*>\s*(\d+)\s*</td>',)
        for m in re.finditer(pat[0], s, re.S | re.I):
            rank, tid, nm, pl, w, dr, l, pts = m.groups()
            teams[tid] = {'name': txt(nm), 'rank': int(rank), 'played': int(pl),
                          'w': int(w), 'd': int(dr), 'l': int(l), 'pts': int(pts)}
        gh = re.search(r'主场场均进球:\s*([\d.]+)', s)
        ga = re.search(r'客场场均进球:\s*([\d.]+)', s)
        gt = re.search(r'场均总进球:\s*([\d.]+)', s)
        title = (re.search(r'<title>(.*?)</title>', s, re.S | re.I) or [None, ''])[1]
        return sid, {'teams': teams, 'team_count': len(teams),
                     'goal_avg': {'home': float(gh.group(1)) if gh else None,
                                  'away': float(ga.group(1)) if ga else None,
                                  'total': float(gt.group(1)) if gt else None},
                     'title': txt(title)[:60]}
    out = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for sid, data in ex.map(one, sorted(set(s for s in sids if s))):
            if data:
                out[sid] = data
    return out


def grab_details(fids):
    """抓比赛详情页：当前/上赛季排名、预计首发阵容、赛后技术统计（真实情报）。"""
    def one(fid):
        try:
            s = fetch_text(DETAIL % fid, timeout=14)
        except Exception:
            return fid, None
        ranks = [int(x) for x in re.findall(r'当前排名:\s*(\d+)', s)]
        prevs = [int(x) for x in re.findall(r'上赛季[^:<]{0,30}排名:\s*(\d+)', s)]
        # 预计首发阵容：两块 box_side，各含一个 table
        blocks = re.findall(r'<div class="title">预计首发阵容</div>(.*?)</div>\s*</div>', s, re.S | re.I)
        lineups = []
        for b in blocks[:2]:
            players = [txt(x).strip() for x in
                       re.findall(r'<td[^>]*>\s*([^<>]*\(.*?\))\s*</td>', b, re.S | re.I)]
            lineups.append([p for p in players if p])
        # 球队技术统计：<td>12</td><td>射门</td><td>14</td>
        tech = {}
        for m in re.finditer(r'<td[^>]*>\s*(\d+)\s*</td>\s*<td[^>]*bgcolor="#e4e4e4"[^>]*>\s*([^<>]+?)\s*</td>\s*<td[^>]*>\s*(\d+)\s*</td>', s, re.S | re.I):
            k = txt(m.group(2)).strip()
            if k and len(k) < 12:
                tech[k] = [int(m.group(1)), int(m.group(3))]
        return fid, {'rank_h': ranks[0] if len(ranks) > 0 else None,
                     'rank_a': ranks[1] if len(ranks) > 1 else None,
                     'prev_h': prevs[0] if len(prevs) > 0 else None,
                     'prev_a': prevs[1] if len(prevs) > 1 else None,
                     'lineup_h': lineups[0] if len(lineups) > 0 else [],
                     'lineup_a': lineups[1] if len(lineups) > 1 else [],
                     'tech': tech}
    out = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for fid, data in ex.map(one, sorted(set(fids))):
            if data:
                out[fid] = data
    return out


def build_form_index(all_rows):
    """按 500 球队 ID 聚合真实赛果，形成每队近 5 场战绩（近况）。"""
    idx = {}
    for r in all_rows:
        if not r.get('full') or not re.match(r'^\d+:\d+$', r['full']):
            continue
        hg, ag = [int(v) for v in r['full'].split(':')]
        for tid, me, opp, gf, ga in ((r.get('hid'), r['h'], r['a'], hg, ag),
                                     (r.get('aid'), r['a'], r['h'], ag, hg)):
            if not tid:
                continue
            res = '胜' if gf > ga else ('平' if gf == ga else '负')
            idx.setdefault(str(tid), []).append({
                'date': r.get('date', ''), 'opp': opp, 'score': '%d:%d' % (gf, ga),
                'res': res, 'gf': gf, 'ga': ga})
    for k in idx:
        idx[k].sort(key=lambda x: x['date'], reverse=True)
        idx[k] = idx[k][:5]
    return idx


def form_rate(items):
    # 样本不足 3 场时不计入，避免「1 场全胜 = 100 分」把强弱分拉偏
    if not items or len(items) < 3:
        return None
    pts = sum(3 if x['res'] == '胜' else (1 if x['res'] == '平' else 0) for x in items)
    return round(pts / (len(items) * 3) * 100, 1)


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def build_intel(r, standings, details, form_idx, prev_intel=None):
    """用真实排名/积分/赔率/近况/情报算强弱分；缺失因素直接跳过并重归一权重。"""
    det = details.get(r.get('fid'), {}) or {}
    tab = standings.get(r.get('sid'), {}) or {}
    teams = tab.get('teams', {})
    th = teams.get(str(r.get('hid') or ''))
    ta = teams.get(str(r.get('aid') or ''))
    n = tab.get('team_count') or 20

    rank_h = det.get('rank_h') or (th or {}).get('rank') or r.get('rank_h')
    rank_a = det.get('rank_a') or (ta or {}).get('rank') or r.get('rank_a')
    o = r.get('odds') or {}
    prob = {'h': None, 'd': None, 'a': None}
    try:
        inv = [1.0 / float(o[k]) for k in ('h', 'd', 'a') if o.get(k)]
        if len(inv) == 3 and sum(inv) > 0:
            prob = {'h': inv[0] / sum(inv), 'd': inv[1] / sum(inv), 'a': inv[2] / sum(inv)}
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    fh = form_idx.get(str(r.get('hid') or ''), [])
    fa = form_idx.get(str(r.get('aid') or ''), [])
    fh_rate, fa_rate = form_rate(fh), form_rate(fa)

    def rank_score(rk):
        return clamp((n - rk) / max(1, n - 1) * 100) if rk else None

    def pts_rate(t):
        return clamp(t['pts'] / max(1, t['played'] * 3) * 100) if t and t.get('played') else None

    # 港澳盘口异动（真实市场情报）
    hk = r.get('hkmo') or {}
    sync = []
    for k in ('胜', '平', '负'):
        a = next((i for i in ((hk.get('澳门') or {}).get('eu') or []) if i['k'] == k), None)
        b = next((i for i in ((hk.get('香港') or {}).get('eu') or []) if i['k'] == k), None)
        if a and b and a.get('t') == 'down' and b.get('t') == 'down':
            sync.append(k)

    specs = [
        ('联赛排名', 0.25, rank_score(rank_h), rank_score(rank_a)),
        ('赛季积分率', 0.25, pts_rate(th), pts_rate(ta)),
        ('胜平负奖金隐含概率', 0.35,
         prob['h'] * 100 if prob['h'] is not None else None,
         prob['a'] * 100 if prob['a'] is not None else None),
        ('近 5 场真实战绩（≥3 场才计入）', 0.15, fh_rate, fa_rate),
    ]
    used = [x for x in specs if x[2] is not None and x[3] is not None]
    wsum = sum(x[1] for x in used) or 1

    def side(i):
        return round(sum(x[1] / wsum * x[i] for x in used), 1) if used else 50.0

    sh, sa = side(2), side(3)
    diff = round(sh - sa, 1)
    market = ('主队' if (prob['h'] or 0) > (prob['a'] or 0) else '客队') if prob['h'] is not None else ''
    model_side = '主队' if diff >= 0 else '客队'
    if abs(diff) >= 12 and market and market != model_side:
        # 综合模型与官方奖金隐含方向相反时如实标注，不硬套「强打弱」
        tag = '多因素分歧 · 综合偏' + model_side + '，奖金偏' + market
    elif diff >= 12:
        tag = '强队打弱队 · 主队占优'
    elif diff <= -12:
        tag = '强队打弱队 · 客队占优'
    else:
        tag = '势均力敌'

    factors = []
    for name, w, hv, av in specs:
        factors.append({'name': name, 'weight': round(w / wsum * 100),
                        'home': round(hv, 1) if hv is not None else None,
                        'away': round(av, 1) if av is not None else None,
                        'used': hv is not None and av is not None})

    intel = {
        'rank_h': rank_h, 'rank_a': rank_a,
        'prev_h': det.get('prev_h'), 'prev_a': det.get('prev_a'),
        'table_h': th, 'table_a': ta,
        'league_title': tab.get('title', ''),
        'goal_avg': tab.get('goal_avg'),
        'prob': {k: (round(v * 100, 1) if v is not None else None) for k, v in prob.items()},
        'form_h': fh, 'form_a': fa,
        'form_rate_h': fh_rate, 'form_rate_a': fa_rate,
        'lineup_h': det.get('lineup_h', []), 'lineup_a': det.get('lineup_a', []),
        'tech': det.get('tech', {}),
        'market_sync': sync,
        'injury': {'status': '未提供',
                   'note': '500 免费源未提供伤停/停赛名单，此因素未计入（不编造）'},
        'score_h': sh, 'score_a': sa, 'diff': diff, 'tag': tag,
        'factors': factors,
        'model': '排名25% + 赛季积分率25% + 胜平负奖金隐含概率35% + 近5场真实赛果15%（不足3场不计）；缺项自动剔除并重归一权重。杯赛/国家队赛事无积分榜时按详情页排名计。数据模型推算，仅供参考。',
    }
    if not used and prev_intel:
        return prev_intel
    return intel


COMPANY_LABELS = {'0': '平均欧赔', '3': '36*', '5': '澳*',
                  '280': '皇*', '293': '威***', '1055': 'SNAI*'}
SELS = ('胜', '平', '负')
SEL_KEY = {'胜': 'h', '平': 'd', '负': 'a'}


def implied(triple):
    """去掉水位后的隐含概率（1/赔率 归一化）。"""
    if not triple:
        return None
    try:
        inv = [1.0 / float(triple[k]) for k in ('h', 'd', 'a')]
    except (TypeError, ValueError, KeyError):
        return None
    s = sum(inv)
    if s <= 0:
        return None
    return {'h': inv[0] / s, 'd': inv[1] / s, 'a': inv[2] / s}


def pois_pmf(k, mu):
    return math.exp(-mu) * mu ** k / math.factorial(k)


def poisson_model(mu_h, mu_a, maxg=8):
    ph = [pois_pmf(i, mu_h) for i in range(maxg + 1)]
    pa = [pois_pmf(j, mu_a) for j in range(maxg + 1)]
    acc = {'h': 0.0, 'd': 0.0, 'a': 0.0}
    scores = []
    for i in range(maxg + 1):
        for j in range(maxg + 1):
            p = ph[i] * pa[j]
            acc['h' if i > j else ('d' if i == j else 'a')] += p
            scores.append(('%d:%d' % (i, j), p))
    scores.sort(key=lambda x: -x[1])
    return {'mu_h': round(mu_h, 2), 'mu_a': round(mu_a, 2),
            'h': round(acc['h'] * 100, 1), 'd': round(acc['d'] * 100, 1),
            'a': round(acc['a'] * 100, 1),
            'top': [{'k': k, 'p': round(p * 100, 2)} for k, p in scores[:6]],
            'basis': 'μ = 60%% 近况攻守均值 + 40%% 联赛主/客场场均进球（样本不足 3 场时纯用联赛基准）'}


def mu_pair(fh, fa, league):
    lh = (league or {}).get('home') or 1.35
    la = (league or {}).get('away') or 1.15
    if len(fh) >= 3 and len(fa) >= 3:
        hgf = sum(x['gf'] for x in fh) / len(fh)
        hga = sum(x['ga'] for x in fh) / len(fh)
        agf = sum(x['gf'] for x in fa) / len(fa)
        aga = sum(x['ga'] for x in fa) / len(fa)
        mh = (hgf + aga) / 2 * 0.6 + lh * 0.4
        ma = (agf + hga) / 2 * 0.6 + la * 0.4
    else:
        mh, ma = lh, la
    return max(0.35, min(3.6, mh)), max(0.35, min(3.6, ma))


def dispersion(companies):
    """跨公司隐含概率的离散度 = 凯利方差的直观口径。"""
    probs = {}
    for cid, t in (companies or {}).items():
        p = implied(t)
        if p:
            probs[COMPANY_LABELS.get(cid, cid)] = p
    if len(probs) < 3:
        return {'n': len(probs), 'level': '样本不足', 'avg_cv': None, 'items': {}}
    items = {}
    for s in ('h', 'd', 'a'):
        arr = [p[s] for p in probs.values()]
        mean = sum(arr) / len(arr)
        sd = statistics.pstdev(arr)
        items[s] = {'mean': round(mean * 100, 2), 'sd': round(sd * 100, 2),
                    'cv': round(sd / mean * 100, 1) if mean else 0}
    avg = sum(items[s]['cv'] for s in items) / 3
    return {'n': len(probs), 'items': items, 'avg_cv': round(avg, 1),
            'level': '低分歧' if avg < 4 else ('中分歧' if avg < 9 else '高分歧')}


def kelly_model(sp, companies):
    """凯利值 = 竞彩官方奖金 × 各公司去水隐含概率；方差反映庄家口径分歧。"""
    if not sp:
        return None
    rows = []
    for cid, t in (companies or {}).items():
        p = implied(t)
        if not p:
            continue
        rows.append({'c': COMPANY_LABELS.get(cid, cid),
                     'h': sp['h'] * p['h'], 'd': sp['d'] * p['d'], 'a': sp['a'] * p['a']})
    if len(rows) < 2:
        return None
    vals = {s: round(sum(r[s] for r in rows) / len(rows), 3) for s in ('h', 'd', 'a')}
    var = {s: round(statistics.pvariance([r[s] for r in rows]), 4) for s in ('h', 'd', 'a')}
    gut = min(vals, key=vals.get)
    return {'values': vals, 'var': var, 'n': len(rows),
            'gut': SELS[('h', 'd', 'a').index(gut)],
            'by_company': [{'c': r['c'], **{s: round(r[s], 3) for s in ('h', 'd', 'a')}} for r in rows],
            'basis': 'K = 官方固定奖金 × 该公司去水隐含概率；K 越低代表庄家在该结果上赔付压力越小'}


def same_odds(r, all_rows):
    """历史同指：找官方胜赔相近（±0.18）且已完赛的真实场次，统计赛果分布。"""
    sp = (r.get('odds') or {}).get('h')
    if not sp:
        return {'sample': 0, 'pick': None, 'note': '当前奖金未取到'}
    cand = [x for x in all_rows
            if x.get('fid') != r.get('fid') and x.get('result')
            and (x.get('odds') or {}).get('h')
            and abs(x['odds']['h'] - sp) <= 0.18]
    dist = {s: 0 for s in SELS}
    for x in cand:
        if x['result'] in dist:
            dist[x['result']] += 1
    n = len(cand)
    pct = {s: round(dist[s] / n * 100, 1) for s in SELS} if n else {}
    pick = max(pct, key=pct.get) if n >= 3 else None
    return {'sample': n, 'dist': dist, 'pct': pct, 'pick': pick,
            'note': ('同指样本 %d 场，倾向「%s」' % (n, pick)) if pick else
                    ('同指样本仅 %d 场，不足 3 场不下结论' % n)}


def extreme_form(items):
    if not items:
        return None
    best = max(items, key=lambda x: x['gf'] - x['ga'])
    worst = min(items, key=lambda x: x['gf'] - x['ga'])
    streak, cur = 0, ''
    for x in items:
        if x['res'] == cur:
            streak += 1
        else:
            cur, streak = x['res'], 1
    return {'best': '%s %s %s' % (best['score'], 'vs', best['opp']),
            'worst': '%s %s %s' % (worst['score'], 'vs', worst['opp']),
            'streak': cur * streak if cur in SELS else '',
            'n': len(items)}


def movement(r):
    hk = r.get('hkmo') or {}
    items, sync = [], []
    for name in ('澳门', '香港'):
        for it in ((hk.get(name) or {}).get('eu') or []):
            if it.get('d') is not None:
                items.append({'book': name, 'k': it['k'], 'f': it['f'],
                              'c': it['c'], 'd': it['d'], 't': it['t']})
    for k in SELS:
        a = next((i for i in items if i['book'] == '澳门' and i['k'] == k), None)
        b = next((i for i in items if i['book'] == '香港' and i['k'] == k), None)
        if a and b and a['t'] == 'down' and b['t'] == 'down':
            sync.append(k)
    items.sort(key=lambda x: -abs(x['d']))
    return {'items': items, 'sync': sync,
            'max_delta': items[0]['d'] if items else None,
            'abnormal': bool(sync) or (abs(items[0]['d']) >= 0.25 if items else False)}


def build_market(r, standings, form_idx, all_rows):
    """庄家视角 / 离散 / 凯利 / 泊松 / 历史同指 / 五维雷达 —— 全部由真实公开数据推导。"""
    intel = r.get('intel') or {}
    companies = r.get('odds_companies') or {}
    sp = r.get('odds') or {}
    tab = standings.get(r.get('sid'), {}) or {}
    fh = form_idx.get(str(r.get('hid') or ''), [])
    fa = form_idx.get(str(r.get('aid') or ''), [])

    disp = dispersion(companies)
    kel = kelly_model(sp if all(sp.get(k) for k in ('h', 'd', 'a')) else None, companies)
    # 庄家水位：官方奖金三项倒数之和（>1 的部分即庄家抽水）
    try:
        over = round(sum(1.0 / float(sp[k]) for k in ('h', 'd', 'a')), 4)
    except (TypeError, ValueError, KeyError, ZeroDivisionError):
        over = None
    imp_avg = implied({'h': sp.get('h'), 'd': sp.get('d'), 'a': sp.get('a')}) if all(sp.get(k) for k in ('h', 'd', 'a')) else None
    p_book = {s: round((imp_avg or {}).get(SEL_KEY[s], 0) * 100, 1) for s in SELS}

    mh, ma = mu_pair(fh, fa, tab.get('goal_avg'))
    pois = poisson_model(mh, ma)
    p_pois = {s: pois[SEL_KEY[s]] for s in SELS}
    hot = {s: round(p_book[s] - p_pois[s], 1) for s in SELS}

    same = same_odds(r, all_rows)
    mov = movement(r)
    ext_h, ext_a = extreme_form(fh), extreme_form(fa)

    # 五维雷达（主队 / 客队各 5 项，0-100）
    market_dim = 65 if '胜' in mov['sync'] else (35 if '负' in mov['sync'] else 50)
    radar = {
        'labels': ['排名积分', '近况', '奖金隐含', '盘口支撑', '泊松'],
        'home': [round(intel.get('score_h') or 50, 1),
                 round(intel.get('form_rate_h') or 50, 1), p_book['胜'],
                 market_dim, p_pois['胜']],
        'away': [round(intel.get('score_a') or 50, 1),
                 round(intel.get('form_rate_a') or 50, 1), p_book['负'],
                 100 - market_dim, p_pois['负']],
    }

    # 差异分析（三方概率）
    model_side = round((intel.get('score_h') or 50), 1)
    diff = {
        'labels': ['胜', '平', '负'],
        'book': [p_book[s] for s in SELS],
        'poisson': [p_pois[s] for s in SELS],
        'model': [round(model_side, 1), round(100 - abs(model_side - 50) * 0.4, 1),
                  round(100 - model_side, 1)],
    }

    hottest = max(p_book, key=p_book.get)
    pois_top = max(p_pois, key=p_pois.get)
    alerts = []
    if disp.get('avg_cv') is not None and disp['avg_cv'] >= 9:
        alerts.append('跨公司离散度高（CV %s%%），庄家口径分歧明显' % disp['avg_cv'])
    if pois_top != hottest:
        alerts.append('泊松倾向「%s」但奖金热门是「%s」，存在冷门结构' % (pois_top, hottest))
    if any(abs(v) >= 12 for v in hot.values()):
        alerts.append('冷热偏差 ≥12 个百分点（%s）' %
                      '、'.join('%s %+g' % (k, v) for k, v in hot.items() if abs(v) >= 12))
    if mov['sync']:
        alerts.append('港澳同步降水：' + ' / '.join(mov['sync']))
    if not alerts:
        alerts.append('未检出显著异动')

    compass_dir = '主胜' if p_book['胜'] >= p_book['负'] else '客胜'
    compass = '%s · %s · 离散%s' % (compass_dir,
              ('强' if abs((intel.get('diff') or 0)) >= 12 else '均衡'), disp['level'])

    # 综合概率（用于串关精选）：50% 奖金隐含 + 30% 泊松 + 20% 模型，再扣离散惩罚
    cv = disp.get('avg_cv') or 0
    synth = {}
    for s in SELS:
        synth[s] = round(0.5 * p_book[s] + 0.3 * p_pois[s] +
                         0.2 * diff['model'][SELS.index(s)] - min(cv, 15) * 0.5, 1)
        synth[s] = max(1.0, synth[s])

    return {
        'companies': {COMPANY_LABELS.get(k, k): v for k, v in companies.items() if v},
        'book_overround': over,
        'book_implied': p_book,
        'dispersion': disp,
        'kelly': kel,
        'poisson': pois,
        'hot_cold': hot,
        'same_odds': same,
        'movement': mov,
        'extreme': {'home': ext_h, 'away': ext_a},
        'radar': radar,
        'diff': diff,
        'alerts': alerts,
        'compass': compass,
        'synth': synth,
        'signals': {
            '庄家视角': ('官方奖金隐含总概率 %s（>1 为庄家水位）' % over) if over else '未提供',
            '大数据': '基于本站归档 %d 场真实赛果与 6 家公司赔率' % len([x for x in all_rows if x.get('result')]),
            '离散数据': disp['level'] + (' · CV %s%%' % disp['avg_cv'] if disp.get('avg_cv') is not None else ''),
            '凯利方差': ('最低 K=%s，方差 胜%s/平%s/负%s' %
                        (kel['gut'], kel['var']['h'], kel['var']['d'], kel['var']['a'])) if kel else '公司赔率不足',
            '历史同指': same['note'],
            '必发指数': '未提供（免费源无 Betfair 成交量/成交价）',
            '球队情报': '排名 %s/%s · 首发 %s/%s 人 · 赛后技术统计 %s 项' %
                        (intel.get('rank_h') or '—', intel.get('rank_a') or '—',
                         len(intel.get('lineup_h') or []), len(intel.get('lineup_a') or []),
                         len(intel.get('tech') or {})),
            '极限战绩': ('主 %s ／ 客 %s' % (ext_h['best'] if ext_h else '样本不足',
                                          ext_a['best'] if ext_a else '样本不足')),
            '异动赛事': ('⚠ ' + '、'.join(mov['sync']) + ' 同步降水') if mov['sync'] else '无同步降水',
            '伤停身价': '未提供（500 免费源无伤停名单与球员身价）',
            '凯利寻胆': ('「%s」K 最低' % kel['gut']) if kel else '未提供',
            '同指精选': ('「%s」（%d 场同指）' % (same['pick'], same['sample'])) if same['pick'] else same['note'],
            '离散分歧': disp['level'],
            '必发倾向': '未提供',
            '冷门预警': alerts[0] if alerts else '无',
            '泊松分布': 'μ主 %s / μ客 %s · 胜%s%% 平%s%% 负%s%%' %
                        (pois['mu_h'], pois['mu_a'], pois['h'], pois['d'], pois['a']),
            '五维雷达': '排名/近况/奖金/盘口/泊松 五维评分见展开面板',
            '差异分析': '奖金 vs 泊松 vs 模型 三方概率对照见展开面板',
            '冷热偏差': '、'.join('%s %+g' % (k, v) for k, v in hot.items()),
            '市场异动': ('最大变盘 %+g' % mov['max_delta']) if mov['max_delta'] is not None else '无',
            '走势寻踪': ' → '.join('%s%s %s→%s' % (i['book'][0], i['k'], i['f'], i['c'])
                                for i in mov['items'][:4]) or '无',
            '罗盘定位': compass,
        },
        'unavailable': [
            {'name': '必发指数 / 必发倾向', 'reason': 'Betfair 成交量与成交价需付费/授权数据源，500 免费页不提供'},
            {'name': '伤停身价', 'reason': '500 免费源未提供伤停名单，也没有球员身价字段'},
            {'name': '外部高精确预测模型', 'reason': '本站只用可核验的公开数据自建模型，不接入第三方付费预测服务'},
        ],
        'model_note': '全部指标由 6 家公司真实赔率、竞彩官方固定奖金、联赛场均进球、真实赛果与港澳盘口异动推导，可复现；非外部高精度模型，不构成投注建议。',
    }


def build_parlay_recs(day_matches, all_rows):
    """按「价值 × 质量」精选 2串1 / 3串1 / 4串1，不再盲目堆低赔胆。

    旧版按综合概率降序取最高项 → 全是 1.1~1.4 的稳胆，组合赔率只有 1.38，
    性价比极低。新版：
      1) 单腿赔率限定 1.45–2.80（排除超低赔，也排除纯博冷）
      2) 硬门槛：综合概率 ≥40、市场隐含 ≥28、离散 CV ≤9、≥2 条真实信号支持
      3) 价值分 EV = 当前赔率 × 综合概率 / 100（每 1 元期望回报，越高越划算）
      4) 质量分 = 0.5×EV×100 + 0.3×综合概率 + 0.2×信号分 − 离散惩罚
      5) 组合赔率目标带 2串1≥2.20 / 3串1≥3.60 / 4串1≥6.00，不达标则用
         高价值候补替换掉最低赔腿；达标腿不够就不硬凑，直接写「无法组成」
    """
    ODD_MIN, ODD_MAX = 1.40, 2.80
    P_MIN, BOOK_MIN, CV_MAX, SIG_MIN = 38.0, 26.0, 10.0, 2
    QUALITY_MIN = 40.0
    COMBO_TARGET = {2: 2.20, 3: 3.60, 4: 6.00}
    COMBO_CAP = {2: 5.00, 3: 10.00, 4: 18.00}

    def leg_prize(r, sel):
        for it in ((r.get('plays') or {}).get('spf') or []):
            if it.get('k') == sel and it.get('v'):
                return round(float(it['v']), 2)
        return None

    pool, backup = [], []
    for r in day_matches:
        m = r.get('market')
        sp = r.get('odds') or {}
        if not m or not all(sp.get(k) for k in ('h', 'd', 'a')):
            continue
        disp = m.get('dispersion') or {}
        cv = float(disp.get('avg_cv') or 0)
        if disp.get('level') == '高分歧' or cv > CV_MAX:
            continue
        pois_top = max(SELS, key=lambda z: m['poisson'][SEL_KEY[z]])
        sync = (m.get('movement') or {}).get('sync', [])
        same = m.get('same_odds') or {}
        kelly = m.get('kelly') or {}

        cands = []
        for s in SELS:
            k = SEL_KEY[s]
            odds = round(float(sp[k]), 2)
            p = float(m['synth'][s])
            book = float(m['book_implied'][s])
            pois = float(m['poisson'][SEL_KEY[s]])
            if not (ODD_MIN <= odds <= ODD_MAX):
                continue
            if p < P_MIN or book < BOOK_MIN:
                continue
            if abs(book - pois) > 22:      # 模型与盘口明显打架，不硬选
                continue
            why, sig = [], 0
            if kelly.get('gut') == s:
                why.append('凯利寻胆'); sig += 1
            if same.get('pick') == s and same.get('sample', 0) >= 3:
                why.append('同指精选 %d 场' % same['sample']); sig += 1
            if s == pois_top:
                why.append('泊松首位'); sig += 1
            if disp.get('level') == '低分歧':
                why.append('离散低'); sig += 1
            if s in sync:
                why.append('港澳同步降水'); sig += 1
            if sig < SIG_MIN:
                continue
            ev = round(odds * p / 100.0, 3)   # 价值分：每 1 元期望回报
            quality = round(0.5 * ev * 100 + 0.3 * p +
                            0.2 * min(sig, 5) * 12 - min(cv, 15) * 0.6, 1)
            if quality < QUALITY_MIN:
                continue
            why.append('价值分 %s' % ev)
            cands.append({'sel': s, 'odds': odds, 'prob': round(p, 1),
                          'book': round(book, 1), 'pois': round(pois, 1),
                          'ev': ev, 'quality': quality, 'signals': sig,
                          'cv': round(cv, 1), 'why': why, 'r': r})
        if not cands:
            continue
        cands.sort(key=lambda x: -x['quality'])
        pool.append(cands[0])
        if len(cands) > 1:
            backup.append(cands[1])

    def done_of(p):
        return bool(p['r'].get('result'))

    # 未开赛优先；历史日全是已完赛，作为回溯模拟
    pending = [p for p in pool if not done_of(p)]
    finished = [p for p in pool if done_of(p)]
    ordered = sorted(pending, key=lambda x: -x['quality']) + \
              sorted(finished, key=lambda x: -x['quality'])
    ordered_backup = sorted(
        [b for b in backup if not any(b['r'].get('fid') == p['r'].get('fid') for p in ordered[:4])],
        key=lambda x: -x['ev'])

    recs = {}
    for n in (2, 3, 4):
        target = COMBO_TARGET[n]
        if len(ordered) < n:
            recs['%d串1' % n] = {
                'ok': False,
                'note': '高质量候选仅 %d 场，不足 %d 场（不硬凑低赔或低质场次）' % (len(ordered), n),
                'qualified': len(ordered), 'combo_target': target,
            }
            continue

        chosen = ordered[:n][:]
        used = {p['r'].get('fid') for p in chosen}
        combo = 1.0
        for p in chosen:
            combo *= p['odds']
        swaps = 0
        # 双向校准：低于目标带 → 用高价值候补换掉最低赔腿；
        # 高于上限带 → 用高质量候补换掉最高赔腿，避免滑向博冷长串
        while swaps < n * 2 and (combo < target or combo > COMBO_CAP[n]):
            if combo < target:
                victim = min(chosen, key=lambda x: x['odds'])
                need_higher = True
            else:
                victim = max(chosen, key=lambda x: x['odds'])
                need_higher = False
            cand = next((b for b in ordered_backup
                         if b['r'].get('fid') not in used
                         and b['quality'] >= QUALITY_MIN
                         and (b['odds'] > victim['odds'] if need_higher
                              else b['odds'] < victim['odds'])), None)
            if not cand:
                cand = next((p for p in ordered[n:]
                             if p['r'].get('fid') not in used
                             and (p['odds'] > victim['odds'] if need_higher
                                  else p['odds'] < victim['odds'])), None)
            if not cand:
                break
            chosen = [cand if p is victim else p for p in chosen]
            used.discard(victim['r'].get('fid')); used.add(cand['r'].get('fid'))
            combo = 1.0
            for p in chosen:
                combo *= p['odds']
            swaps += 1

        joint = 1.0
        out_legs = []
        for p in chosen:
            r, lg = p['r'], p
            joint *= lg['prob'] / 100.0
            done = done_of(p)
            res = r.get('result') or ''
            hit = (lg['sel'] == res) if done else None
            out_legs.append({
                'num': r.get('num', ''), 'lg': r.get('lg', ''),
                'match': '%s vs %s' % (r.get('h', ''), r.get('a', '')),
                'sel': lg['sel'], 'odds': lg['odds'],
                'book_implied': lg['book'], 'poisson': lg['pois'],
                'synth': lg['prob'], 'why': lg['why'],
                'ev': lg['ev'], 'quality': lg['quality'],
                'signals': lg['signals'], 'cv': lg['cv'],
                'state': '已完赛 · 赛果已回填' if done else '未开赛 · 待回填',
                'result': res, 'hit': hit,
                'full': r.get('full') or '', 'half': r.get('half') or '',
                'rq_result': r.get('result_rq') or '',
                'bqc': r.get('result_bqc') or '',
                'jqs': r.get('result_jqs') or '',
                'bf': r.get('result_bf') or '',
                'sel_prize': leg_prize(r, lg['sel']),
                'actual_prize': next(
                    (i['prize'] for i in (r.get('results') or []) if i['key'] == 'spf'), None),
                'settled_at': r.get('settled_at') or '',
            })
        combo = round(combo, 2)
        ev = round(combo * joint, 3)
        avg_synth = sum(x['synth'] for x in out_legs) / n
        avg_quality = sum(x['quality'] for x in out_legs) / n
        avg_cv = sum(x['cv'] for x in out_legs) / n
        alerts = sum(len((p['r'].get('market') or {}).get('alerts') or []) for p in chosen)

        hit_n = sum(1 for x in out_legs if x['hit'] is True)
        miss_n = sum(1 for x in out_legs if x['hit'] is False)
        pending_n = sum(1 for x in out_legs if x['hit'] is None)
        done_n = hit_n + miss_n
        if miss_n:
            parlay_hit, hit_icon = '未全中', '✗'
            hit_note = '%d/%d 腿命中，%d 腿未中，串关已破' % (hit_n, n, miss_n)
        elif done_n == n:
            parlay_hit, hit_icon = '全中', '✓'
            hit_note = '%d/%d 腿全部命中' % (hit_n, n)
        elif done_n:
            parlay_hit, hit_icon = '待开赛', '⏳'
            hit_note = '已完赛 %d/%d 腿且全部命中，其余 %d 腿待开赛' % (done_n, n, pending_n)
        else:
            parlay_hit, hit_icon = '待开赛', '⏳'
            hit_note = '%d 腿均未开赛，完赛后自动回填并判定' % n

        recs['%d串1' % n] = {
            'ok': True, 'legs': out_legs,
            'combo_odds': combo, 'combo_target': target, 'combo_cap': COMBO_CAP[n],
            'combo_ok': target <= combo <= COMBO_CAP[n], 'swaps': swaps,
            'joint_prob': round(joint * 100, 2),
            'ev': ev, 'avg_synth': round(avg_synth, 1),
            'avg_quality': round(avg_quality, 1),
            'avg_ev': round(sum(x['ev'] for x in out_legs) / n, 3),
            'confidence': '较高' if avg_quality >= 55 and avg_synth >= 45 else
                          ('中' if avg_quality >= 48 else '偏低'),
            'risk': '低' if avg_cv < 4 and alerts <= n else ('中' if avg_cv < 7 else '高'),
            'avg_cv': round(avg_cv, 1), 'backtest': done_n,
            'hit_legs': hit_n, 'miss_legs': miss_n, 'pending_legs': pending_n,
            'parlay_hit': parlay_hit, 'hit_icon': hit_icon, 'hit_note': hit_note,
            'return_2yuan': round(2 * combo, 2) if parlay_hit == '全中' else None,
            'note': ('含 %d 场已完赛（历史回溯模拟）' % done_n) if done_n else '全部为未开赛场次',
        }

    recs['_meta'] = {
        'pool': len(pool), 'qualified': len(ordered),
        'pending': len(pending), 'finished': len(finished),
        'generated_at': NOW.strftime('%Y-%m-%d %H:%M:%S (北京时间)'),
        'odds_band': '单腿赔率 1.40–2.80：排除 1.1~1.3 的超低赔稳胆，也排除纯博冷',
        'quality_rule': ('硬门槛：综合概率 ≥38%、市场隐含 ≥26%、离散 CV ≤10、≥2 条真实信号'
                         '（凯利寻胆 / 同指 / 泊松首位 / 离散低 / 港澳同步降水）、模型与盘口不相反。'
                         '价值分 EV = 当前赔率 × 综合概率 ÷ 100；'
                         '质量分 = 0.5×EV×100 + 0.3×综合概率 + 0.2×信号分 − 离散惩罚，需 ≥40。'),
        'combo_target': {2: 2.20, 3: 3.60, 4: 6.00},
        'combo_cap': {2: 5.00, 3: 10.00, 4: 18.00},
        'selection_note': ('选腿按「价值 × 质量」降序，不再按概率高低堆低赔胆；'
                           '组合赔率低于目标带时用高价值候补抬高，高于上限带时压回，'
                           '目标带 2串1 2.20–5.00 / 3串1 3.60–10.00 / 4串1 6.00–18.00；'
                           '达标候选不足就不硬凑，直接标注无法组成。'),
        'basis': '综合概率 = 50% 奖金隐含 + 30% 泊松 + 20% 强弱模型 − 离散惩罚；本版按价值分与质量分选腿，兼顾回报与把握',
        'ev_note': '期望指数 = 组合赔率 × 联合概率；竞彩官方奖金含返奖水位，多串后 <1 属正常，仅用于横向比较不同组合，不代表判断失误。',
        'backtest_note': ('历史回溯：每腿「所选选项 vs 实际胜平负赛果」自动判定 ✓命中 / ✗未命中；'
                          '串关须全部腿命中才算全中，任一腿未中即未全中。'
                          '注意：历史日期的精选是「用当前模型对当日历史数据做的回溯模拟」，'
                          '不是当时实时发出的推荐，命中结论只用于检验模型，不代表历史实盘。'),
        'disclaimer': '数据模型筛选演示，不是市场高精度预测，也不构成投注建议。',
    }
    return recs

def grab_day(day, allow_default=True):
    """抓一个销售日。返回 (rows, tried, 页面真实销售日)。

    ?e= 偶发读超时，重试 3 次；历史日**禁止**回落默认页——默认页是「当前期」，
    回落后页面自报的销售日会变成今天，导致该历史日被当成重复期跳过（11-17 曾因此丢失）。
    """
    tried = []
    e_url = BASE + '?e=' + day
    for attempt in range(3):
        try:
            text = fetch_text(e_url, timeout=18 + attempt * 6)
            real = page_sale_date(text)
            rows = parse_500(text, real or day)
            tried.append({'url': 'e', 'http': 200, 'rows': len(rows),
                          'page_date': real, 'attempt': attempt + 1})
            if rows:
                return rows, tried, real
        except urllib.error.HTTPError as e:
            tried.append({'url': 'e', 'http': e.code, 'attempt': attempt + 1})
        except Exception as e:
            tried.append({'url': 'e', 'err': '%s: %s' % (type(e).__name__, str(e)[:80]),
                          'attempt': attempt + 1})
    if allow_default:
        try:
            text = fetch_text(BASE)
            real = page_sale_date(text)
            rows = parse_500(text, real or day)
            tried.append({'url': 'default', 'http': 200, 'rows': len(rows), 'page_date': real})
            if rows:
                return rows, tried, real
        except urllib.error.HTTPError as e:
            tried.append({'url': 'default', 'http': e.code})
        except Exception as e:
            tried.append({'url': 'default', 'err': '%s: %s' % (type(e).__name__, str(e)[:80])})
    return [], tried, ''


def _aesgcm():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM


def site_key(passphrase=None):
    """由账号 + 密码派生 32 字节密钥；密码缺失返回 None（不覆盖已有密文）。"""
    pw = passphrase if passphrase is not None else os.environ.get('SITE_PASS', '')
    if not pw:
        return None
    material = (SITE_USER + '\x00' + pw).encode('utf-8')
    return hashlib.pbkdf2_hmac('sha256', material, KDF_SALT, KDF_ITER, 32)


def encrypt_obj(obj, key):
    """AES-256-GCM 加密，输出可公开存放的 JSON 包装（不含任何明文数据）。"""
    aes = _aesgcm()(key)
    nonce = os.urandom(12)
    plain = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    ct = aes.encrypt(nonce, plain, None)
    return {
        'v': 1, 'alg': 'AES-256-GCM', 'kdf': 'PBKDF2-HMAC-SHA256',
        'iter': KDF_ITER,
        'salt': base64.b64encode(KDF_SALT).decode(),
        'iv': base64.b64encode(nonce).decode(),
        'ct': base64.b64encode(ct).decode(),
        'bytes': len(plain),
    }


def decrypt_obj(wrap, key):
    aes = _aesgcm()(key)
    nonce = base64.b64decode(wrap['iv'])
    plain = aes.decrypt(nonce, base64.b64decode(wrap['ct']), None)
    return json.loads(plain.decode('utf-8'))


def safe_load(path):
    """读上一轮产物：优先解密 .enc；兼容旧明文 json；都失败返回空。"""
    key = site_key()
    if key:
        try:
            wrap = json.load(open(path, encoding='utf-8'))
            if wrap.get('ct'):
                return decrypt_obj(wrap, key)
        except Exception:
            pass
    legacy = path[:-4] + '.json' if path.endswith('.enc') else path
    try:
        return json.load(open(legacy, encoding='utf-8'))
    except Exception:
        return {}


def write_encrypted(obj, path, key):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(encrypt_obj(obj, key), f, ensure_ascii=False, separators=(',', ':'))
    # 明文绝不留在工作区，避免误传进公开仓库
    for legacy in (LEGACY_OUT, LEGACY_HIST):
        try:
            os.remove(legacy)
        except OSError:
            pass


def main():
    cipher_key = site_key()   # 注意：下面循环里的 key 是「销售日键」，不要复用此名
    if cipher_key is None:
        print('警告：未配置 SITE_PASS，跳过本轮加密输出（保留现有密文，不写明文）')
        return
    now = NOW.strftime('%Y-%m-%d %H:%M:%S')
    dates = [(NOW - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(DAYS)]
    WEEK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

    prev = safe_load(OUT)
    prev_hist = safe_load(HIST)
    prev_days = {d['date']: d for d in (prev_hist.get('days') or []) if d.get('matches')}

    days, probe, seen = [], [], set()
    for day in dates:
        rows, tried, real = grab_day(day, allow_default=(day == TODAY))
        # 页面回落（或 ?e= 未生效）时自报的是「当前期」，绝不能冒充历史销售日
        req_mismatch = bool(real and real != day)
        if req_mismatch and rows:
            if day in prev_days:
                rows = prev_days[day]['matches']
                tried.append({'fallback': '页面回落到 %s，沿用本日上次成功归档' % real})
            else:
                tried.append({'drop': '页面回落到 %s 且本日无历史归档，本轮不写入（不冒充）' % real})
                rows = []
            real = day
            req_mismatch = False
        key = real or day
        if not rows and day in prev_days:
            rows = prev_days[day]['matches']
            tried.append({'fallback': '沿用上次成功抓取'})
        # 四个玩法页（胜平负/让球、总进球、比分、半全场），按 fid 合并
        play_stats = []
        if rows:
            book, play_stats = grab_plays(key)
            prev_by_fid = {}
            if key in prev_days:
                prev_by_fid = {m.get('fid'): m for m in prev_days[key]['matches'] if m.get('fid')}
            hit = 0
            for r in rows:
                p = book.get(r.get('fid'))
                if not p and r.get('fid') in prev_by_fid:
                    p = prev_by_fid[r['fid']].get('plays')
                if p:
                    r['plays'] = p
                    hit += 1
                    # 玩法页是官方投注页，用它补齐 jczq.php 内嵌表缺的奖金
                    def first(kind, k):
                        for it in (p.get(kind) or []):
                            if it['k'] == k and it['v'] is not None:
                                return it['v']
                        return None
                    if r.get('odds', {}).get('h') is None:
                        r['odds'] = {'h': first('spf', '胜'), 'd': first('spf', '平'),
                                     'a': first('spf', '负')}
                    if not r.get('odds_rq'):
                        r['odds_rq'] = {'h': first('rqsp', '胜'), 'd': first('rqsp', '平'),
                                        'a': first('rqsp', '负')}
            print('    玩法赔率 %d/%d 场' % (hit, len(rows)))
            # 港澳盘口：初盘 / 当前 / 升降水，按 fid 关联
            fids = [r['fid'] for r in rows if r.get('fid')]
            hk_book, hk_stat = grab_hkmo(fids)
            hk_hit = 0
            for r in rows:
                h = hk_book.get(r.get('fid'))
                if not h and r.get('fid') in prev_by_fid:
                    h = prev_by_fid[r['fid']].get('hkmo')
                if h:
                    r['hkmo'] = h
                    hk_hit += 1
            print('    港澳盘口 %d/%d 场（请求 成功%d 失败%d）'
                  % (hk_hit, len(rows), hk_stat['ok'], hk_stat['fail']))
            play_stats.append({'name': '港澳盘口', 'ok': hk_stat['ok'], 'fail': hk_stat['fail']})
        probe.append({'date': day, 'page_date': real, 'tried': tried,
                      'rows': len(rows), 'plays': play_stats})
        if rows and key in seen:
            print('%-12s -> %s 与已有销售期重复，跳过' % (day, key))
            continue
        if rows:
            seen.add(key)
            label = '今天' if day == TODAY else ('昨天' if day == dates[1] else '')
            label = (label + ' · 当前期') if (req_mismatch and label) else label
            if req_mismatch and not label:
                label = '当前期'
            try:
                wd = WEEK[datetime.strptime(key, '%Y-%m-%d').weekday()]
            except Exception:
                wd = ''
            nums = [r['num'] for r in rows if r.get('num')]
            span = (nums[0] + '–' + nums[-1]) if nums else ''
            days.append({
                'date': key, 'weekday': wd, 'label': label,
                'count': len(rows), 'num_span': span, 'matches': rows,
                'probe': tried,
            })
        print('%-12s -> %-12s %-4s %2d 场 %s' %
              (day, key, days[-1]['weekday'] if days else '', len(rows),
               tried[0].get('rows', tried[0].get('http', ''))))

    # 完赛后自动回填赛果：比分 / 胜平负 / 让球胜平负 / 半全场 / 总进球 / 比分玩法
    all_rows = [m for d in days for m in d['matches']]
    prev_settled = {}
    for d in (prev_hist.get('days') or []):
        for m in (d.get('matches') or []):
            if m.get('fid') and m.get('settled_at'):
                prev_settled[m['fid']] = m['settled_at']
    settled_hit = 0
    for m in all_rows:
        first = prev_settled.get(m.get('fid')) or (now + ' (北京时间)')
        if fill_results(m, first):
            settled_hit += 1
    print('赛果自动回填：%d/%d 场已完成（含比分/胜平负/让球/半全场/总进球/比分玩法）'
          % (settled_hit, len(all_rows)))

    # 真实情报 + 强弱对战模型：全部销售日共用一份球队近况索引
    form_idx = build_form_index(all_rows)
    standings = grab_standings([m.get('sid') for m in all_rows])
    details = grab_details([m.get('fid') for m in all_rows])
    prev_intel = {}
    for d in (prev_hist.get('days') or []):
        for m in (d.get('matches') or []):
            if m.get('fid') and m.get('intel'):
                prev_intel[m['fid']] = m['intel']
    intel_hit = 0
    for d in days:
        for m in d['matches']:
            m['intel'] = build_intel(m, standings, details, form_idx,
                                     prev_intel.get(m.get('fid')))
            if any(f['used'] for f in (m['intel'].get('factors') or [])):
                intel_hit += 1
    print('情报/强弱模型：联赛页 %d 个 · 详情页 %d 场 · 有真实因素的场次 %d/%d'
          % (len(standings), len(details), intel_hit, len(all_rows)))

    # 庄家视角 / 离散 / 凯利 / 泊松 / 历史同指 / 五维雷达 + 2串1~4串1 精选
    market_hit = 0
    for d in days:
        for m in d['matches']:
            m['market'] = build_market(m, standings, form_idx, all_rows)
            if (m['market'].get('dispersion') or {}).get('n', 0) >= 3:
                market_hit += 1
        d['parlay_recs'] = build_parlay_recs(d['matches'], all_rows)
    recs = days[0].get('parlay_recs', {}) if days else {}
    print('市场模型：%d/%d 场含 ≥3 家公司赔率 · 精选 %s'
          % (market_hit, len(all_rows),
             '、'.join('%s:%s' % (k, '有' if v.get('ok') else '缺')
                       for k, v in recs.items() if not k.startswith('_'))))

    # data.json：今天（兼容旧逻辑）
    today_rows = days[0]['matches'] if days else []
    if today_rows:
        data = {
            'status': 'ok', 'kind': 'domestic', 'source': SOURCE,
            'updated_at': now + ' (北京时间)', 'upstream_time': '',
            'date': TODAY, 'count': len(today_rows), 'matches': today_rows,
            'parlay_recs': recs, 'probe': probe,
            'note': '国内 500 彩票网；完赛后自动回填全场比分/胜平负/让球胜平负/半全场/总进球/比分赛果及对应官方奖金；全部玩法固定奖金 + 港澳盘口 + 情报强弱模型 + 庄家视角指标 + 2串1~4串1 精选，按销售日归档供 6 个 Tab 共享历史。',
        }
    elif prev.get('matches'):
        data = dict(prev)
        data['status'] = 'ok'
        data['stale'] = True
        data['probe'] = probe
    else:
        data = {'status': 'pending', 'kind': '', 'source': '', 'updated_at': '',
                'upstream_time': '', 'date': '', 'count': 0, 'matches': [], 'probe': probe}
    write_encrypted(data, OUT, cipher_key)

    # history.enc：最近 8 个销售日，供页面按日期切换
    hist = {
        'status': 'ok' if days else 'pending',
        'kind': 'domestic', 'source': SOURCE,
        'updated_at': now + ' (北京时间)',
        'day_count': len(days), 'days': days,
        'note': '按竞彩销售日归档；完赛自动回填 results（比分/胜平负/让球胜平负/半全场/总进球/比分玩法 + 中奖奖金）与 settled_at；另含全部玩法奖金、港澳盘口、情报强弱模型（intel）、市场模型（market）与串关精选（parlay_recs），供 6 个 Tab 按日期切换查看历史。',
    }
    write_encrypted(hist, HIST, cipher_key)
    print('已写出加密 data.enc 与 history.enc（%d 个日期，密码未落盘）' % len(days))


if __name__ == '__main__':
    main()
