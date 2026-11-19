#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取500竞彩篮球赛果，回填到 basketball.json"""
import json, re, urllib.request, time, sys, os

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BASE = "https://trade.500.com/jclq/"

def fetch(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.500.com/"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            if i == retries - 1:
                raise
            print(f"    retry {i+1}: {e}")
            time.sleep(2)
    return ""

def log(msg):
    print(msg, flush=True)

def parse_results(html):
    """从500页面解析所有比赛的比分"""
    results = {}
    # 找所有行
    rows = re.findall(r'<tr[^>]*data-matchid="(\d+)"[^>]*>(.*?)</tr>', html, re.DOTALL)
    for mid, row in rows:
        num_match = re.search(r'class="num"[^>]*>([^<]+)<', row)
        if not num_match:
            continue
        num = num_match.group(1).strip()
        # 比分
        score_match = re.search(r'class="score[^"]*"[^>]*>([^<]+)<', row)
        if score_match:
            score = score_match.group(1).strip()
            if re.match(r'^\d+\s*[:\uff1a]\s*\d+$', score):
                results[num] = score
    return results

def compute_result(m):
    """根据比分计算赛果"""
    score_str = m.get("score", "")
    sm = re.match(r'^\s*(\d+)\s*[:\uff1a]\s*(\d+)\s*$', score_str)
    if not sm:
        return None
    
    away_s = int(sm.group(1))
    home_s = int(sm.group(2))
    
    if away_s > home_s:
        sf_result = "客胜"
    else:
        sf_result = "主胜"
    
    # 让分胜负
    rfsf = m.get("rfsf", {})
    line = rfsf.get("line", 0)
    if line:
        away_adj = away_s - line
        if away_adj > home_s:
            rfsf_result = "客胜"
        elif away_adj < home_s:
            rfsf_result = "主胜"
        else:
            rfsf_result = "平"
    else:
        rfsf_result = sf_result
    
    # 大小分
    dxf = m.get("dxf", {})
    total_line = dxf.get("line", 0)
    total = away_s + home_s
    if total_line:
        if total > total_line:
            dxf_result = "大分"
        elif total < total_line:
            dxf_result = "小分"
        else:
            dxf_result = "平"
    else:
        dxf_result = ""
    
    # 胜分差
    diff = abs(away_s - home_s)
    if away_s > home_s:
        if diff <= 5: sfc_result = "客胜1-5"
        elif diff <= 10: sfc_result = "客胜6-10"
        elif diff <= 15: sfc_result = "客胜11-15"
        elif diff <= 20: sfc_result = "客胜16-20"
        elif diff <= 25: sfc_result = "客胜21-25"
        else: sfc_result = "客胜26+"
    else:
        if diff <= 5: sfc_result = "主胜1-5"
        elif diff <= 10: sfc_result = "主胜6-10"
        elif diff <= 15: sfc_result = "主胜11-15"
        elif diff <= 20: sfc_result = "主胜16-20"
        elif diff <= 25: sfc_result = "主胜21-25"
        else: sfc_result = "主胜26+"
    
    return {
        "away": away_s,
        "home": home_s,
        "sf": sf_result,
        "rf": rfsf_result,
        "dx": dxf_result,
        "sfc": sfc_result
    }

def main():
    if not os.path.exists("basketball.json"):
        log("basketball.json 不存在，跳过")
        return
    
    with open("basketball.json", encoding="utf-8") as f:
        data = json.load(f)
    
    # 收集未完赛的比赛
    pending = []
    for date_str, day_data in data["days"].items():
        for m in day_data["matches"]:
            if not m.get("settled", False) and not m.get("score", ""):
                pending.append((date_str, m))
    
    log(f"待回填: {len(pending)} 场")
    if not pending:
        log("无需回填")
        return
    
    # 需要抓取的日期
    dates_needed = set(d for d, m in pending)
    all_scores = {}
    
    for date_str in sorted(dates_needed):
        for g in ["1", "2"]:
            url = f"{BASE}?date={date_str}&g={g}&playid=275"
            try:
                html = fetch(url)
                log(f"  GET {url} -> {len(html)} bytes")
                scores = parse_results(html)
                log(f"    解析到 {len(scores)} 场比分")
                all_scores[date_str] = {**all_scores.get(date_str, {}), **scores}
                time.sleep(1)
            except Exception as e:
                log(f"  ERROR {url}: {e}")
    
    # 回填
    changed = 0
    for date_str, m in pending:
        day_scores = all_scores.get(date_str, {})
        if m["match_id"] in day_scores:
            m["score"] = day_scores[m["match_id"]]
            m["away_score"] = int(re.match(r'(\d+)', day_scores[m["match_id"]]).group(1))
            m["home_score"] = int(re.search(r'[:\uff1a](\d+)', day_scores[m["match_id"]]).group(1))
            m["settled"] = True
            m["result"] = compute_result(m)
            changed += 1
            log(f"  回填 {m['match_id']}: {m['away']} {m['away_score']}:{m['home_score']} {m['home']} -> {m['result']['sf']}/{m['result']['rf']}/{m['result']['dx']}/{m['result']['sfc']}")
    
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    
    with open("basketball.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    log(f"\n回填完成: {changed} 场已更新")
    log(f"总计: {sum(len(d['matches']) for d in data['days'].values())} 场, 已开奖 {sum(1 for d in data['days'].values() for m in d['matches'] if m.get('settled'))} 场")

if __name__ == "__main__":
    main()
