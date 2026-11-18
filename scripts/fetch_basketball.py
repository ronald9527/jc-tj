#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩篮球数据抓取脚本
数据源：500 体育竞彩篮球（trade.500.com/jclq）
输出：data/basketball.json —— 编号/队名/让分/大小分/12项胜分差/赛果
"""
import urllib.request
import json
import re
import os
import time
from datetime import date, timedelta

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://trade.500.com/jclq/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

PLAYIDS = {
    '313': '胜负',
    '314': '让分胜负',
    '315': '大小分',
    '316': '胜分差',
}


def fetch(url, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            for enc in ('utf-8', 'gbk', 'gb2312'):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode('utf-8', errors='ignore')
        except Exception as e:
            last = e
            print(f"  [retry {i+1}] {url} -> {e}")
            time.sleep(5)
    print(f"  [FAIL] {url} -> {last}")
    return ""


def parse_list(html):
    """解析比赛列表：编号 / 星期前缀 / 主客队 / 开赛时间 / 各玩法 data-id"""
    matches = {}
    # 行结构：<tr ... data-id="123456789" ...><td ...>周三301</td>...
    trs = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in trs:
        name_m = re.search(r'class="[^"]*team[^"]*"[^>]*>([^<]+)<', body)
        # 编号：如 周三301
        num_m = re.search(r'>([周][一二三四五六日]\d{3})<', body)
        if not num_m:
            continue
        num = num_m.group(1).strip()
        # 队名
        teams = re.findall(r'<span[^>]*>([^<]+)</span>', body)
        # 开赛时间
        time_m = re.search(r'(\d{2}:\d{2})', body)
        # 玩法 data-id 映射（每个玩法 td 带 data-id 相同赛事 id）
        matches[num] = {
            "match_id": mid,
            "num": num,
            "home": teams[0].strip() if len(teams) > 0 else "",
            "away": teams[1].strip() if len(teams) > 1 else "",
            "kickoff": time_m.group(1) if time_m else "",
            "odds": {},
            "result": {},
        }
    return matches


def parse_odds(html, matches):
    """从玩法页抓取赔率，按 data-k 或 data-id 归到对应场次"""
    # 胜负
    sps = re.findall(r'data-sp="([\d.]+)"[^>]*data-k="(\d+)"', html)
    for sp, k in sps:
        for m in matches.values():
            if m["match_id"] == k:
                m["odds"].setdefault("胜负", []).append(float(sp))
    # 让分胜负（含让分值）
    hsp = re.findall(r'data-hsp="([-\d.]+)"', html)
    # 胜分差 12 项
    sd = re.findall(r'data-sd="(\d+)"[^>]*data-sp="([\d.]+)"', html)
    return matches


def parse_result(html):
    """解析开奖赛果：主客得分 / 胜负 / 让分胜负 / 大小分 / 胜分差区间"""
    results = {}
    rows = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in rows:
        score_m = re.search(r'(\d+)[^\d]+(\d+)', body)
        res_m = re.search(r'class="bd_result[^"]*">([^<]+)<', body)
        results[mid] = {
            "score": f"{score_m.group(1)}-{score_m.group(2)}" if score_m else "",
            "result": res_m.group(1).strip() if res_m else "",
        }
    return results


def main():
    today = date.today()
    dates = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(4)]
    all_data = {"dates": {}, "updated": time.strftime('%Y-%m-%d %H:%M:%S')}

    for d in dates:
        print(f"\n=== 抓取 {d} ===")
        by_play = {}
        for playid, name in PLAYIDS.items():
            url = f"https://trade.500.com/jclq/?playid={playid}&g=2&date={d}"
            html = fetch(url)
            if not html:
                print(f"  [skip] {name} 页面为空")
                continue
            if name == '胜负':
                matches = parse_list(html)
                by_play['matches'] = matches
            by_play.setdefault('html_' + name, len(html))
            time.sleep(2)

        matches = by_play.get('matches', {})
        # 赛果回填（胜负页 body 里含比分）
        if matches:
            url = f"https://trade.500.com/jclq/?playid=313&g=2&date={d}"
            html = fetch(url)
            results = parse_result(html)
            for m in matches.values():
                if m["match_id"] in results:
                    m["result"] = results[m["match_id"]]

        all_data["dates"][d] = {
            "matches": list(matches.values()) if matches else [],
            "html_len": {k: v for k, v in by_play.items() if k.startswith('html_')},
            "count": len(matches),
        }
        print(f"  => 场次 {len(matches)}")
        for m in list(matches.values())[:3]:
            print(f"     {m['num']} {m['home']} vs {m['away']} {m['kickoff']}")

    os.makedirs("data", exist_ok=True)
    out = "data/basketball.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 已保存 {out}")
    print(json.dumps(all_data, ensure_ascii=False, indent=2)[:2000])


if __name__ == '__main__':
    main()
