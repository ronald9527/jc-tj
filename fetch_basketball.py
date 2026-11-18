#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竞彩篮球数据抓取
数据来源：500 彩票网竞彩篮球 trade.500.com/jclq
运行环境：GitHub Actions（沙盒出口受限，仅在 Actions runner 上可真实抓取）
产物：basketball.enc（AES-256-GCM 加密，密钥取自 SITE_PASS）
"""
import os, sys, json, re, time, traceback
from datetime import date, timedelta

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://trade.500.com/jclq/',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

def log(msg):
    print(msg, flush=True)


def fetch(url, retries=3):
    """用标准库抓取，避免额外依赖"""
    import urllib.request
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
            log(f"  [retry {i+1}] {url} -> {e}")
            time.sleep(4)
    log(f"  [FAIL] {url} -> {last}")
    return ""


def parse_schedule(html, date_str):
    """解析赛程：编号/星期/主客队/开赛时间/让分/大小/胜分差12项"""
    rows = []
    # 500 篮球页每场比赛在一个 .betC 或 data-id 行里
    blocks = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in blocks:
        num_m = re.search(r'>([周][一二三四五六日]\s?\d{2,3})<', body)
        if not num_m:
            continue
        raw_num = num_m.group(1).strip().replace(' ', '')
        # 归一化成 周一301 这种
        num = raw_num
        teams = re.findall(r'<span[^>]*>([^<]+)</span>', body)
        kick_m = re.search(r'(\d{1,2}:\d{2})', body)
        # 让分 / 大小分
        handicap_m = re.search(r'data-k="1"[^>]*data-sp="([-\d.]+)"', body)
        total_m = re.search(r'data-k="2"[^>]*data-sp="([-\d.]+)"', body)
        # 胜负赔率
        sf_sp = re.findall(r'data-k="(\d)"\s+data-sp="([\d.]+)"', body)
        sf = {}
        for k, sp in sf_sp[:2]:
            sf['lose' if k == '1' else 'win'] = float(sp)
        rows.append({
            "match_id": mid,
            "num": num,
            "home": teams[0].strip() if teams else "",
            "away": teams[1].strip() if len(teams) > 1 else "",
            "kickoff": kick_m.group(1) if kick_m else "",
            "handicap": float(handicap_m.group(1)) if handicap_m else None,
            "total": float(total_m.group(1)) if total_m else None,
            "sf_sp": sf,
            "date": date_str,
            "settled": False,
        })
    return rows


def parse_result(html):
    """解析开奖赛果，返回 {match_id: {score, result}}"""
    out = {}
    blocks = re.findall(r'<tr[^>]*data-id="(\d+)"[^>]*>(.*?)</tr>', html, re.S)
    for mid, body in blocks:
        sc = re.search(r'>(\d{2,3})\s*[:\-]\s*(\d{2,3})<', body)
        res = re.search(r'class="bd_result[^"]*">([^<]+)<', body)
        out[mid] = {
            "score": f"{sc.group(1)}-{sc.group(2)}" if sc else "",
            "result": res.group(1).strip() if res else "",
        }
    return out


def derive_key(passwd):
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    import hashlib
    kdf = PBKDF2HMAC(hashes.SHA256(), 32, b'jc-tj', 200000)
    return kdf.derive(passwd.encode())


def encrypt(payload, passwd):
    key = derive_key(passwd)
    aes = AESGCM(key)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, json.dumps(payload, ensure_ascii=False).encode(), None)
    return base64.b64encode(nonce + ct).decode()


import base64


def main():
    out = {
        "updated_at": time.strftime('%Y-%m-%d %H:%M:%S'),
        "source": "500彩票网竞彩篮球 trade.500.com/jclq",
        "days": {},
        "fake": True,          # 默认标记为兜底，抓到真数据才置 False
        "error": None,
    }

    today = date.today()
    dates = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(4)]

    for d in dates:
        log(f"\n=== {d} ===")
        # 胜负玩法页（含赛程列表）
        html = fetch(f"https://trade.500.com/jclq/?playid=313&g=2&date={d}")
        matches = parse_schedule(html, d) if html else []
        log(f"  场次 {len(matches)}")
        if html and matches:
            out["fake"] = False   # 抓到真实内容才置 False
        time.sleep(2)

        # 回填赛果
        res_html = fetch(f"https://trade.500.com/jclq/?playid=313&g=2&date={d}")
        if res_html:
            for mid, r in parse_result(res_html).items():
                for m in matches:
                    if m["match_id"] == mid:
                        m["score"] = r["score"]
                        m["result"] = r["result"]
                        m["settled"] = bool(r["score"])
                        break
        out["days"][d] = {"date": d, "count": len(matches), "matches": matches}

    # 明文快照（不入版本库，rm 掉）
    with open("basketball.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 加密
    passwd = os.environ.get("SITE_PASS")
    if not passwd:
        log("[WARN] 未设置 SITE_PASS，仅输出明文 basketball.json，不生成 enc")
        out["error"] = "SITE_PASS not set"
        with open("basketball.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return

    if not HAVE_CRYPTO:
        log("[WARN] 缺少 cryptography 库，无法加密")
        out["error"] = "no cryptography lib"
        with open("basketball.json", "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return

    total = sum(v["count"] for v in out["days"].values())
    settled = sum(1 for v in out["days"].values() for m in v["matches"] if m.get("settled"))
    enc = encrypt(out, passwd)
    with open("basketball.enc", "w", encoding="utf-8") as f:
        f.write(enc)
    log(f"[OK] 篮球加密完成：{total} 场抓取，{settled} 场已开奖，fake={out['fake']}")


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log("[FATAL] 抓取脚本异常：")
        traceback.print_exc()
        # 写一个明确标记的失败文件，避免误用兜底数据为真实数据
        with open("basketball.enc", "w") as f:
            f.write("")
        sys.exit(1)
