"""
리밸런서 클라우드 자동 업데이트 스크립트
- GitHub Actions가 정해진 시간마다 이 스크립트를 실행합니다.
- 비밀 값(구글 인증 정보)은 전부 환경변수(깃허브 Secrets)에서 읽어옵니다.
  이 파일 자체에는 어떤 비밀번호·키도 들어있지 않습니다(공개 저장소에 올라가도 안전).
- app.py의 시세·추세 계산 로직을 그대로 옮겨왔습니다(계산 결과가 PC와 항상 같도록).
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _datetime

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
LOGO_LOGIC_VERSION = 2
GOOGLE_FOLDER_NAME = "Rebalancer"
_PROGRESS = {"stage": "", "done": 0, "total": 0}  # 병렬 처리 진행상황(참고용, 콘솔에는 안 씀)

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
        return resp.read().decode("utf-8", errors="replace")


def yahoo_chart(symbol, rng="1d", interval="1m", include_prepost=True):
    # 심볼은 여기서 한 번만 인코딩. (^ 같은 특수문자 대응, 이중 인코딩 방지)
    sym = urllib.parse.quote(symbol, safe="")
    pp = "true" if include_prepost else "false"
    path = f"/v8/finance/chart/{sym}?range={rng}&interval={interval}&includePrePost={pp}"
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            return json.loads(http_get("https://" + host + path))
        except Exception as e:
            last = e
    raise last


def yahoo_price(symbol):
    """환율/단순 현재가: 차트 meta 에서 여러 필드 폴백으로 추출"""
    j = yahoo_chart(symbol, "1d", "1m")
    meta = j["chart"]["result"][0]["meta"]
    for k in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
        if meta.get(k) is not None:
            return float(meta[k])
    raise ValueError("가격 필드 없음")


def domain_candidates_from_name(name):
    """회사명에서 도메인 후보들을 생성.
    예: 'Centrus Energy Corp.' -> ['centrus.com', 'centrusenergy.com']
        'Teradyne, Inc.'       -> ['teradyne.com']
    한 개만 찍지 않고 여러 개를 만들어 Clearbit 에서 차례로 시도한다."""
    if not name:
        return []
    n = name.lower()
    for junk in [",", ".", "'", "&"]:
        n = n.replace(junk, " ")
    stop = {"inc","incorporated","corp","corporation","co","company","ltd","limited",
            "plc","holdings","holding","group","the","sa","nv","ag","class"}
    words = [w for w in n.split() if w and w not in stop]
    if not words:
        return []
    out = []
    # 첫 단어 (가장 흔한 형태)
    out.append(words[0] + ".com")
    # 첫 두 단어 결합 (Centrus Energy -> centrusenergy.com)
    if len(words) >= 2:
        out.append(words[0] + words[1] + ".com")
    # 첫 단어가 너무 짧으면 두 단어 결합을 우선
    if len(words[0]) <= 3 and len(out) > 1:
        out[0], out[1] = out[1], out[0]
    # 중복 제거(순서 유지)
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d); uniq.append(d)
    return uniq


def guess_domain_from_name(name):
    c = domain_candidates_from_name(name)
    return c[0] if c else None


def wikipedia_website(name):
    """위키피디아에서 공식 홈페이지 도메인을 찾아본다 (인증 불필요)."""
    if not name:
        return None
    try:
        q = urllib.parse.quote(name)
        url = ("https://en.wikipedia.org/w/api.php?action=query&format=json"
               f"&prop=extlinks&ellimit=50&titles={q}&redirects=1")
        j = json.loads(http_get(url))
        pages = j.get("query", {}).get("pages", {})
        for _, pg in pages.items():
            for el in pg.get("extlinks", []):
                link = el.get("*", "")
                if not link.startswith("http"):
                    continue
                d = link.split("//")[-1].split("/")[0].replace("www.", "")
                # 위키/외부 참조 도메인 제외
                if any(bad in d for bad in ("wikipedia", "wikimedia", "doi.org",
                                            "archive.org", "sec.gov", "google.",
                                            "bloomberg", "reuters", "nytimes",
                                            "twitter", "facebook", "linkedin")):
                    continue
                return d
    except Exception:
        pass
    return None


def resolve_us_domain(symbol, name):
    """미국 종목의 회사 도메인을 여러 경로로 시도."""
    # 1) 위키피디아 공식 홈페이지
    d = wikipedia_website(name)
    if d:
        return d
    # 2) 회사명 기반 추정
    return guess_domain_from_name(name)


def yahoo_us_stock(symbol):
    """미국 종목: 현재가 + 종목명 + 로고용 도메인 + 프리마켓/애프터마켓 정보.
    chart 엔드포인트를 우선 사용한다. (quote/quoteSummary 는 야후가
    크럼블 인증을 요구하기 시작해 대부분 실패하므로 보조로만 시도.)

    프리마켓/애프터마켓 가격은 meta 의 별도 필드로 오지 않고(실측 결과
    marketState/preMarketPrice 필드 자체가 없었음), currentTradingPeriod 로
    지금이 어느 세션인지 판정한 뒤 1분봉 캔들의 가장 최근 값을 그 세션의
    가격으로 사용한다. 프리마켓·애프터마켓 시간에는 이 실시간 값을 그대로
    포트폴리오 평가에 쓰는 메인 가격(price)으로도 사용한다(요청에 따름).

    반환: (price, name, domain, extras)
      extras = {"marketState":..., "preMarketPrice":..., "preMarketChangePercent":...,
                "postMarketPrice":..., "postMarketChangePercent":...} (있는 값만 포함)"""
    price = name = domain = None
    extras = {}

    try:
        j = yahoo_chart(symbol, "1d", "1m")
        res = j["chart"]["result"][0]
        meta = res["meta"]
        name = meta.get("longName") or meta.get("shortName")

        reg_price = None
        for k in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
            if meta.get(k) is not None:
                reg_price = float(meta[k]); break
        prev_close = meta.get("previousClose")
        prev_close = float(prev_close) if prev_close is not None else reg_price
        price = reg_price

        # 지금이 프리/정규/애프터마켓 중 언제인지, 실제 구간 시각과 비교해 판정
        now = time.time()
        ctp = meta.get("currentTradingPeriod") or {}

        def _in(period_name):
            p = ctp.get(period_name)
            if not p or p.get("start") is None or p.get("end") is None:
                return False
            return p["start"] <= now < p["end"]

        session = None
        if _in("pre"):
            session = "PRE"
        elif _in("regular"):
            session = "REGULAR"
        elif _in("post"):
            session = "POST"

        if session in ("PRE", "POST"):
            # 1분봉 중 가장 최근의 유효한 종가를 그 세션의 실시간 가격으로 사용
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            last_close = None
            for c in closes:
                if c is not None:
                    last_close = float(c)
            if last_close is not None:
                extras["marketState"] = session
                pct = None
                if prev_close:
                    pct = (last_close - prev_close) / prev_close * 100
                if session == "PRE":
                    extras["preMarketPrice"] = last_close
                    if pct is not None:
                        extras["preMarketChangePercent"] = pct
                else:
                    extras["postMarketPrice"] = last_close
                    if pct is not None:
                        extras["postMarketChangePercent"] = pct
                # 프리마켓/애프터마켓 값을 평가용 메인 가격으로도 사용
                price = last_close
    except Exception:
        pass

    # 이름/도메인 보강 — 인증 없는 search 엔드포인트 사용 (실패해도 무시)
    if not name or True:
        try:
            for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
                try:
                    url = (f"https://{host}/v1/finance/search"
                           f"?q={urllib.parse.quote(symbol, safe='')}"
                           f"&quotesCount=1&newsCount=0")
                    j = json.loads(http_get(url))
                    quotes = j.get("quotes", [])
                    if quotes:
                        q0 = quotes[0]
                        if not name:
                            name = (q0.get("longname") or q0.get("shortname")
                                    or q0.get("longName") or q0.get("shortName"))
                    break
                except Exception:
                    continue
        except Exception:
            pass

    if price is None:
        raise ValueError("가격을 찾지 못함 (야후 응답 없음)")
    # 로고용 도메인 확보 (여러 경로 시도)
    if not domain:
        domain = resolve_us_domain(symbol, name)
    return price, (name or symbol), domain, extras


def yahoo_index(symbol):
    """지수 1년 최고점 + 현재가"""
    j = yahoo_chart(symbol, "1y", "1d")
    res = j["chart"]["result"][0]
    now = res["meta"]["regularMarketPrice"]
    highs = res["indicators"]["quote"][0]["high"]
    high = max(h for h in highs if h is not None)
    return high, now


# ==============================================================
#  QQQ 일별 데이터 + 추세(매도) 지표
# ==============================================================
def yahoo_daily(symbol, rng="2y"):
    """일별 OHLCV 리스트를 [{date,o,h,l,c,v}, ...] 로 반환 (과거→현재)"""
    j = yahoo_chart(symbol, rng, "1d")
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    import datetime as _dt
    out = []
    for i, t in enumerate(ts):
        o, h, l, c, v = (q["open"][i], q["high"][i], q["low"][i],
                         q["close"][i], q["volume"][i])
        if None in (o, h, l, c):
            continue
        d = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%d")
        out.append({"date": d, "o": o, "h": h, "l": l, "c": c,
                    "v": v or 0})
    return out


TREND_SYMBOLS = ["QQQ", "SOXX", "EWY"]
TREND_SYMBOLS = ["QQQ", "SOXX", "EWY"]

def update_trend_history(data, symbol, keep_days=500, force=False):
    """지정 심볼의 일별 이력을 받아 병합, 최근 keep_days(약 2년)만 유지.
       data['hist'][symbol] 에 저장.
       하루 단위로만 바뀌는 데이터라, 오늘 이미 받았으면 건너뛴다(속도 개선).
       force=True 면 무조건 다시 받는다."""
    store = data.setdefault("hist", {})
    meta = data.setdefault("histFetched", {})
    today = _datetime.now().strftime("%Y-%m-%d")
    if not force and meta.get(symbol) == today and store.get(symbol):
        return store[symbol]   # 오늘 이미 받음 → 네트워크 호출 생략
    hist = store.get(symbol, [])
    have = {r["date"] for r in hist}
    fresh = yahoo_daily(symbol, "2y")
    for r in fresh:
        if r["date"] not in have:
            hist.append(r)
    hist.sort(key=lambda r: r["date"])
    if len(hist) > keep_days:
        hist = hist[-keep_days:]
    store[symbol] = hist
    meta[symbol] = today
    return hist


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(-n, 0):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    ag, al = gains / n, losses / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(-n, 0):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return sum(trs) / n


def _psar_series(highs, lows, step=0.02, mx=0.20):
    n = len(highs)
    if n < 3:
        return None
    bull = True
    af = step
    ep = highs[0]
    sar = lows[0]
    out = [sar]
    for i in range(1, n):
        prev = sar
        sar = prev + af * (ep - prev)
        if bull:
            sar = min(sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if highs[i] > ep:
                ep = highs[i]; af = min(af + step, mx)
            if lows[i] < sar:
                bull = False; sar = ep; ep = lows[i]; af = step
        else:
            sar = max(sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if lows[i] < ep:
                ep = lows[i]; af = min(af + step, mx)
            if highs[i] > sar:
                bull = True; sar = ep; ep = highs[i]; af = step
        out.append(sar)
    return out


def _cum_vwap(hist, start_idx):
    """start_idx 부터 누적 VWAP (전형가×거래량 누적 / 거래량 누적)"""
    pv = vv = 0.0
    out = []
    for i, r in enumerate(hist):
        tp = (r["h"] + r["l"] + r["c"]) / 3
        if i >= start_idx:
            pv += tp * r["v"]; vv += r["v"]
        out.append(pv / vv if vv else None)
    return out


def _finalize_atr_state(closes, highs, lows):
    """ATR 규칙 상태 기계. 과거→현재로 한 번 순회하며 상태를 만든다.
       반환: 'none'|'watch'|'sell'|'emergency'|'locked'

       규칙:
       - 2ATR 스탑선(기준 고점 − 2ATR) 아래 3일 연속 → 그 '다음날'부터
         'sell' 5거래일 유지. 이탈 1~3일차 자체는 'watch'.
       - 하루 2.5ATR 급락 → 그 '다음날'부터 'emergency' 5거래일 유지.
       - 둘이 겹치면 emergency 우선.
       - 유지 5일이 끝나면 6개월 신고가 또는 20일선 상향 돌파(둘 중 먼저
         오는 것) 전까지 'locked'.

       기준 고점(스탑선 계산용) 재설정: 잠금이 한 번 해제된 이후로는,
       스탑선의 기준 고점을 '해제 시점 이후의 최고가'로 다시 잡는다.
       큰 폭 하락 후에는 크래시 이전의 옛 고점이 스탑선을 비현실적으로
       높게 고정시켜, 완전히 옛 고점을 회복하기 전까지 회복 국면 내내
       매도로 잡히는 문제가 있었음. 첫 하락(아직 해제 사이클을 겪지
       않음)에는 기존대로 절대 6개월 고점을 그대로 사용한다.
    """
    n = len(closes)
    if n < 40:
        return "none"

    def atr_at(i):
        return _atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], 14)

    def stop_at(i, ref_start):
        a = atr_at(i)
        if a is None:
            return None
        lo = max(ref_start, i - 125)
        return max(highs[lo:i + 1]) - 2 * a

    below = 0            # 스탑선 아래 연속일수(오늘까지)
    hold = 0             # 유지 남은 일수
    kind = None          # 'sell' | 'emergency'
    locked = False
    prev_high6 = None
    ref_start = 0         # 스탑선 기준 고점 창의 시작 인덱스(리셋 시 앞당겨짐)

    for i in range(n):
        # 절대 6개월 고점(신고가 판정용 — 리셋과 무관하게 항상 진짜 절대 고점을 봄)
        high6 = max(highs[max(0, i - 125):i + 1])
        # 잠금 해제: (a) 6개월 신고가 갱신, 또는 (b) 20일 이동평균선 상향 돌파(종가>MA20).
        #   대세 하락장·장기 박스권에서 신고가만 기다리면 잠금이 몇 달씩
        #   풀리지 않는 것을 막기 위한 보조 조건.
        if locked:
            new_high = (prev_high6 is not None and high6 > prev_high6 + 1e-9)
            m20 = _sma(closes[:i + 1], 20)
            ma_cross = (m20 is not None and closes[i] > m20)
            if new_high or ma_cross:
                locked = False
                # 해제 순간 스탑선 이탈 연속일수를 0으로 초기화하고,
                # 스탑선 기준 고점 창도 이 시점부터 새로 시작(옛 고점 무시).
                below = 0
                ref_start = i

        # 오늘 하루 유지 카운트 감소(어제 발동분이 오늘까지 이어짐)
        if hold > 0:
            hold -= 1
            if hold == 0:
                # 방금 유지가 끝남 → 신고가 전까지 잠금
                kind = None
                locked = True

        a = atr_at(i)
        line = stop_at(i, ref_start)

        # 어제까지 3일 연속 이탈이었는지(오늘 '다음날 발동' 판정)
        # below 는 아직 '어제까지' 값 → 갱신 전에 확인
        trig_sell = (below >= 3) and not locked

        # 어제 급락(2.5ATR)이 있었는지 → 오늘 발동
        trig_emg = False
        if i >= 1:
            a_prev = atr_at(i - 1)
            if a_prev and i >= 2 and (closes[i - 2] - closes[i - 1]) >= 2.5 * a_prev:
                trig_emg = True
        trig_emg = trig_emg and not locked

        # 발동 처리 (비상 우선)
        if trig_emg:
            hold = 5
            kind = "emergency"
            locked = False
        elif trig_sell and kind != "emergency":
            if hold <= 0:
                hold = 5
                kind = "sell"

        # 오늘 이탈 여부로 연속일수 갱신(다음날 판정에 사용)
        if line is not None and closes[i] <= line:
            below += 1
        else:
            below = 0

        prev_high6 = high6

    # 최종 상태
    if hold > 0 and kind == "emergency":
        return "emergency"
    if hold > 0 and kind == "sell":
        return "sell"
    if locked:
        return "locked"
    if 1 <= below <= 3:
        return "watch"
    return "none"


def compute_signals_for(hist):
    """5개 매도 지표를 계산해 {지표:{state,detail}} 로 반환.
       state: 'none' | 'watch'(매도 고려) | 'sell'(매도)"""
    if len(hist) < 40:
        return None
    closes = [r["c"] for r in hist]
    highs = [r["h"] for r in hist]
    lows = [r["l"] for r in hist]
    c = closes[-1]
    sig = {}

    # 1) RSI(14): 42 미만 매도, 42~50 매도 고려(비례 배분), 50 이상 정상
    r = _rsi(closes, 14)
    if r is None:
        sig["RSI"] = {"state": "none", "detail": "-", "score": 0}
    elif r < 42:
        sig["RSI"] = {"state": "sell", "detail": f"RSI {r:.1f}", "score": 2.0}
    elif r < 50:
        frac = (50 - r) / (50 - 42)
        sig["RSI"] = {"state": "watch", "detail": f"RSI {r:.1f}", "score": round(2.0 * frac, 2)}
    else:
        sig["RSI"] = {"state": "none", "detail": f"RSI {r:.1f}", "score": 0}

    # 2) PSAR: 점이 주가 위로 반전(발생 후 5일 지속). 중간 단계 없음
    sars = _psar_series(highs, lows)
    if sars:
        flip_idx = None
        for i in range(1, len(sars)):
            if sars[i - 1] < lows[i - 1] and sars[i] > highs[i]:
                flip_idx = i
        strong = False
        if flip_idx is not None and (len(sars) - 1 - flip_idx) <= 5:
            strong = True
        sig["PSAR"] = {"state": "sell" if strong else "none",
                       "detail": "매도 반전" if strong else "-"}
    else:
        sig["PSAR"] = {"state": "none", "detail": "-"}

    # 3) VWAP(누적, 6개월=126일 기준 시작): 종가<VWAP 강력(발생 후 5일),
    #    0.5% 이내 근접 고려
    start = max(0, len(hist) - 126)
    vw = _cum_vwap(hist, start)
    v_now, v_prev = vw[-1], vw[-2] if len(vw) >= 2 else None
    if v_now:
        cross_idx = None
        for i in range(1, len(vw)):
            if vw[i - 1] and vw[i] and closes[i - 1] >= vw[i - 1] and closes[i] < vw[i]:
                cross_idx = i
        recent_cross = cross_idx is not None and (len(vw) - 1 - cross_idx) <= 5
        if c < v_now and recent_cross:
            sig["VWAP"] = {"state": "sell", "detail": "VWAP 이탈"}
        elif c < v_now:
            sig["VWAP"] = {"state": "sell", "detail": "VWAP 아래"}
        elif c <= v_now * 1.005:
            sig["VWAP"] = {"state": "watch", "detail": "VWAP 근접"}
        else:
            sig["VWAP"] = {"state": "none", "detail": "-"}
    else:
        sig["VWAP"] = {"state": "none", "detail": "-"}

    # 4) ATR 규칙: 별도 상태 기계 함수로 판정
    atr_state = _finalize_atr_state(closes, highs, lows)
    detail_map = {
        "emergency": "하루 2.5ATR 급락",
        "sell": "2ATR 이탈",
        "watch": "2ATR 이탈(관찰)",
        "locked": "잠금(신고가 대기)",
        "none": "-",
    }
    sig["ATR"] = {"state": atr_state, "detail": detail_map.get(atr_state, "-")}

    # 5) 이평선: 5·20 동시 이탈 3일 연속 강력, 이탈했으나 3일 미만 고려 (상태 지속)
    below_days = 0
    for i in range(len(closes) - 1, -1, -1):
        m5 = _sma(closes[:i + 1], 5)
        m20 = _sma(closes[:i + 1], 20)
        if m5 and m20 and closes[i] < m5 and closes[i] < m20:
            below_days += 1
        else:
            break
    if below_days >= 3:
        sig["MA"] = {"state": "sell", "detail": f"이탈 {below_days}일"}
    elif below_days >= 1:
        sig["MA"] = {"state": "watch", "detail": f"이탈 {below_days}일"}
    else:
        sig["MA"] = {"state": "none", "detail": "-"}

    return sig


def composite_from_signals(sig):
    """세부 판단을 가중 점수화해 합산하고 100점 비례로 환산, 4단계 종합 신호 산출.
       가중치(중복성 완화): 추세계열(PSAR/VWAP/MA)은 낮게, RSI/ATR은 높게.
       ATR 비상(emergency)은 4점. ATR 잠금(locked) 시엔 ATR 점수만 0점 처리하고
       만점(분모)은 9로 그대로 유지 — 잠금 여부로 다른 지표의 비중이
       왜곡되지 않도록 함(잠금 중엔 최대 5/9≈56점 '매도 고려'까지만 가능).
       반환: {score(0~100), raw, level, label}
       level: none|watch|sell|strong"""
    if not sig:
        return None
    # 지표별 상태→점수 표 (locked은 0점)
    trend_pts = {"none": 0, "watch": 0.5, "sell": 1}          # PSAR, VWAP, MA
    rsi_pts = {"none": 0, "watch": 1, "sell": 2}               # RSI
    atr_pts = {"none": 0, "watch": 1, "sell": 2, "emergency": 4, "locked": 0}  # ATR

    def st(k):
        return sig.get(k, {}).get("state", "none")

    rsi_score = sig.get("RSI", {}).get("score")
    if rsi_score is None:
        rsi_score = rsi_pts.get(st("RSI"), 0)

    raw = (trend_pts.get(st("PSAR"), 0)
           + trend_pts.get(st("VWAP"), 0)
           + trend_pts.get(st("MA"), 0)
           + rsi_score
           + atr_pts.get(st("ATR"), 0))
    # 만점: 추세 3(1+1+1) + RSI 2 + ATR 비상 4 = 9 (잠금 여부와 무관하게 고정)
    MAX = 9.0
    score = round(raw / MAX * 100)
    if score <= 40:
        level, label = "none", "정상"
    elif score <= 70:
        level, label = "watch", "매도 고려"
    elif score <= 90:
        level, label = "sell", "매도"
    else:
        level, label = "strong", "강력 매도"
    return {"score": score, "raw": round(raw, 1), "level": level, "label": label}


def record_trend_history(data):
    """오늘 날짜로 QQQ/SOXX/EWY 종합 점수를 trendHistory 에 기록(하루 1건, 덮어쓰기).
       PC 앱의 추세 신호 팝업 그래프와 데이터 소스를 공유한다."""
    from datetime import date as _date
    today = _date.today().isoformat()
    store = data.setdefault("trendHistory", {})
    sig = data.get("trendSignals") or {}
    for sym in TREND_SYMBOLS:
        comp = (sig.get(sym) or {}).get("composite")
        if not comp:
            continue
        arr = store.setdefault(sym, [])
        score = comp.get("score")
        updated = False
        for row in arr:
            if row["date"] == today:
                row["score"] = score
                updated = True
                break
        if not updated:
            arr.append({"date": today, "score": score})
        arr.sort(key=lambda r: r["date"])


def compute_trend_signals(data):
    """QQQ/SOXX/EWY 각각의 세부 판단 + 종합 신호를 계산.
       반환: {symbol: {details:{...}, composite:{...}}}"""
    store = data.get("hist", {})
    out = {}
    for sym in TREND_SYMBOLS:
        hist = store.get(sym, [])
        details = compute_signals_for(hist)
        if details is None:
            out[sym] = {"details": None, "composite": None}
        else:
            out[sym] = {"details": details,
                        "composite": composite_from_signals(details)}
    return out


def naver_price(code):
    """국내 종목 현재가 (NXT 우선, 실패 시 일반).
       클라우드 환경에서만 실패하는 경우를 진단하기 위해, 실패 시
       원인(예외 또는 빈 응답)을 콘솔에 남긴다(깃허브 Actions 로그에서 확인 가능)."""
    code = code.zfill(6)
    last_reason = None
    for q in (f"NXT_{code}", code):
        try:
            url = ("https://polling.finance.naver.com/api/realtime"
                   f"?query=SERVICE_ITEM:{q}")
            txt = http_get(url)
            m = re.search(r'"nv":\s*(\d+)', txt)
            if m:
                return float(m.group(1))
            last_reason = f"응답은 받았으나 'nv' 필드를 못 찾음. 응답 앞부분: {txt[:200]!r}"
        except Exception as e:
            last_reason = f"요청 자체가 실패함: {e}"
    print(f"  [경고] naver_price({code}) 실패 — {last_reason}")
    return None


def naver_afterhours(code):
    """국내 종목 시간외 단일가(NXT 애프터마켓, nxtOverMarketPriceInfo).
       실측 결과 이 정보는 콤마 포함 문자열로 온다(예: "257,500").
       반환: (price, change_percent) 또는 정보가 없으면 (None, None)."""
    code = code.zfill(6)
    try:
        url = ("https://polling.finance.naver.com/api/realtime"
               f"?query=SERVICE_ITEM:{code}")
        txt = http_get(url)
        j = json.loads(txt)
        datas = j["result"]["areas"][0]["datas"]
        if not datas:
            return None, None
        info = datas[0].get("nxtOverMarketPriceInfo")
        if not info:
            return None, None
        raw = info.get("overPrice")
        if raw is None:
            return None, None
        price = float(str(raw).replace(",", ""))
        pct = None
        fr = info.get("fluctuationsRatio")
        if fr is not None:
            try:
                pct = float(fr)
            except (TypeError, ValueError):
                pct = None
        return price, pct
    except Exception:
        return None, None


def naver_name(code):
    code = code.zfill(6)
    try:
        txt = http_get(f"https://finance.naver.com/item/main.naver?code={code}")
        m = re.search(r"<title>([^:<]+)", txt)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def refresh_market(data):
    """지수·환율 갱신"""
    warnings = []
    try:
        h, n = yahoo_index("^KS11")
        data["market"]["kospiHigh"], data["market"]["kospiNow"] = h, n
    except Exception as e:
        warnings.append(f"KOSPI 실패: {e}")
    try:
        h, n = yahoo_index("^IXIC")
        data["market"]["nasdaqHigh"], data["market"]["nasdaqNow"] = h, n
    except Exception as e:
        warnings.append(f"NASDAQ 실패: {e}")
    try:
        data["market"]["usdKrw"] = yahoo_price("USDKRW=X")
    except Exception as e:
        warnings.append(f"환율 실패: {e}")
    return warnings


def _refresh_one_holding(h):
    """종목 하나의 현재가(및 필요 시 이름·로고)를 갱신. 스레드에서 병렬 호출됨.
       h 를 제자리에서 수정하고, 실패 시 예외를 올린다."""
    code = str(h.get("code", "")).strip()
    if not code:
        return
    # 로고 계산 로직이 바뀔 때마다 이 값을 올리면, 낡은 캐시(계좌마다 따로
    # 저장돼 있어 서로 다른 결과로 굳어 있던 것)를 한 번 강제로 다시 계산해
    # 통일시킨다. 사용자가 로고를 직접 클릭해 고른 경우(logoPinned)는
    # 이 강제 재계산에서 제외해 그 선택을 계속 존중한다.
    need_logo = (not h.get("logos")
                 or (h.get("logoVer") != LOGO_LOGIC_VERSION and not h.get("logoPinned")))
    if h.get("market") == "US":
        price, name, domain, extras = yahoo_us_stock(code)
        h["price"] = price
        # 프리마켓/애프터마켓 정보(있을 때만). 정규장 중엔 대개 비어 있음.
        if extras.get("preMarketPrice") is not None:
            h["preMarketPrice"] = extras["preMarketPrice"]
            h["preMarketChangePercent"] = extras.get("preMarketChangePercent")
        else:
            h.pop("preMarketPrice", None); h.pop("preMarketChangePercent", None)
        if extras.get("postMarketPrice") is not None:
            h["postMarketPrice"] = extras["postMarketPrice"]
            h["postMarketChangePercent"] = extras.get("postMarketChangePercent")
        else:
            h.pop("postMarketPrice", None); h.pop("postMarketChangePercent", None)
        if extras.get("marketState"):
            h["marketState"] = extras["marketState"]
        stored = (h.get("name") or "").strip()
        if (not stored) or stored.upper() == code.upper():
            if name:
                h["name"] = name
                stored = name
        # 로고는 한 번 정해지면 바뀌지 않으므로, 이미 있으면 재계산을 건너뛴다(속도 개선).
        if need_logo:
            lookup_name = name or stored
            doms = []
            if domain:
                doms.append(domain)
            for d in domain_candidates_from_name(lookup_name):
                if d not in doms:
                    doms.append(d)
            cands = []
            for d in doms:
                cands.append(f"https://logo.clearbit.com/{d}")
            slug = (lookup_name or "").lower()
            for junk in [",", ".", "'", "&"]:
                slug = slug.replace(junk, " ")
            parts = [w for w in slug.split()
                     if w not in ("inc", "incorporated", "corp", "corporation",
                                  "co", "company", "ltd", "limited", "plc",
                                  "holdings", "holding", "group", "the",
                                  "technologies", "technology", "class")]
            if parts:
                cands.append("https://s3-symbol-logo.tradingview.com/"
                             + parts[0] + "--big.svg")
                if len(parts) >= 2:
                    cands.append("https://s3-symbol-logo.tradingview.com/"
                                 + parts[0] + "-" + parts[1] + "--big.svg")
            for d in doms:
                cands.append(f"https://icons.duckduckgo.com/ip3/{d}.ico")
            h["logos"] = cands
            h["logo"] = cands[0] if cands else ""
            h["logoVer"] = LOGO_LOGIC_VERSION
    else:  # KR
        p = naver_price(code)
        if p is not None:
            h["price"] = p
        if not h.get("name"):
            nm = naver_name(code)
            if nm:
                h["name"] = nm
        # 시간외 단일가 — 있으면 참고용으로 저장하고, 평가용 현재가에도 반영
        # (미국 프리마켓/애프터마켓과 동일한 방침)
        try:
            ah, ah_pct = naver_afterhours(code)
        except Exception:
            ah, ah_pct = None, None
        if ah is not None:
            h["afterHoursPrice"] = ah
            h["afterHoursChangePercent"] = ah_pct
            h["price"] = ah
        else:
            h.pop("afterHoursPrice", None)
            h.pop("afterHoursChangePercent", None)
        if need_logo:
            c6 = code.zfill(6)
            h["logos"] = [
                f"https://ssl.pstatic.net/imgstock/fn/real/logo/stock/Stock{c6}.svg",
                f"https://static.toss.im/png-icons/securities/icn-sec-fill-{c6}.png",
                f"https://thumb.tossinvest.com/image/resized/96x0/https%3A%2F%2Fstatic.toss.im%2Fpng-icons%2Fsecurities%2Ficn-sec-fill-{c6}.png",
            ]
            h["logo"] = h["logos"][0]
            h["logoVer"] = LOGO_LOGIC_VERSION


def refresh_prices(data):
    """모든 계좌·종목 현재가 갱신. 여러 종목을 스레드 풀로 병렬 조회(속도 개선).
       동시 요청은 야후·네이버의 차단을 피하려 8개로 제한.
       진행 상황을 _PROGRESS 에 기록해 프론트가 조회할 수 있게 한다."""
    warnings = []
    tasks = []  # (계좌명, code, holding)
    for acc in data["accounts"]:
        for h in acc.get("holdings", []):
            if str(h.get("code", "")).strip():
                tasks.append((acc.get("name", ""), h.get("code"), h))
    _PROGRESS["done"] = 0
    _PROGRESS["total"] = len(tasks)
    if not tasks:
        return warnings
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_refresh_one_holding, h): (an, code)
                for (an, code, h) in tasks}
        for fut in as_completed(futs):
            an, code = futs[fut]
            try:
                fut.result()
            except Exception as e:
                warnings.append(f"{an}/{code} 실패: {e}")
            _PROGRESS["done"] += 1
    return warnings


# --------------------------------------------------------------
#  가상 계산 스냅샷
#  - "계좌 현황"의 📸 버튼을 누른 시점의 계좌별 보유 종목(수량·그때 주가)을
#    통째로 얼려서 저장. 실제 계좌에서 그 종목을 다 팔아 사라지더라도,
def _google_http(method, url, access_token=None, body=None, content_type=None, timeout=25):
    """구글 API 호출 공통 헬퍼. 응답이 JSON이면 dict로, 아니면 원본 bytes로 반환."""
    headers = {"User-Agent": UA}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if content_type:
        headers["Content-Type"] = content_type
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw


def _google_find_folder(access_token):
    q = (f"name='{GOOGLE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' "
         "and trashed=false")
    url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
        {"q": q, "fields": "files(id,name)"})
    res = _google_http("GET", url, access_token)
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _google_create_folder(access_token):
    body = json.dumps({"name": GOOGLE_FOLDER_NAME,
                       "mimeType": "application/vnd.google-apps.folder"})
    res = _google_http("POST", "https://www.googleapis.com/drive/v3/files",
                       access_token, body=body, content_type="application/json")
    return res["id"]


def _google_find_file(access_token, folder_id):
    q = f"name='portfolio.json' and '{folder_id}' in parents and trashed=false"
    url = "https://www.googleapis.com/drive/v3/files?" + urllib.parse.urlencode(
        {"q": q, "fields": "files(id,name,modifiedTime)"})
    res = _google_http("GET", url, access_token)
    files = res.get("files", [])
    return files[0] if files else None


def _google_upload_new(access_token, folder_id, content_bytes):
    """최초 업로드(메타데이터+내용을 멀티파트로 함께 전송)."""
    boundary = "rebalancer_boundary_9f2c9e"
    metadata = json.dumps({"name": "portfolio.json", "parents": [folder_id]})
    head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
           f"{metadata}\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n")
    tail = f"\r\n--{boundary}--"
    body = head.encode("utf-8") + content_bytes + tail.encode("utf-8")
    url = ("https://www.googleapis.com/upload/drive/v3/files"
          "?uploadType=multipart&fields=id,modifiedTime")
    return _google_http("POST", url, access_token, body=body,
                        content_type=f"multipart/related; boundary={boundary}")


def _google_upload_update(access_token, file_id, content_bytes):
    """기존 파일 내용 갱신."""
    url = (f"https://www.googleapis.com/upload/drive/v3/files/{file_id}"
          "?uploadType=media&fields=id,modifiedTime")
    return _google_http("PATCH", url, access_token, body=content_bytes,
                        content_type="application/json")


def _google_download(access_token, file_id):
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    return _google_http("GET", url, access_token)




def _refresh_access_token(client_id, client_secret, refresh_token):
    """리프레시 토큰으로 새 액세스 토큰 발급 (브라우저 없이, 자동화용)."""
    body = urllib.parse.urlencode({
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    tok = _google_http("POST", "https://oauth2.googleapis.com/token",
                       body=body, content_type="application/x-www-form-urlencoded")
    if "access_token" not in tok:
        raise RuntimeError(f"액세스 토큰 발급 실패: {tok}")
    return tok["access_token"]


def main():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        print("[오류] GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN "
             "환경변수(깃허브 Secrets)가 설정되어 있지 않습니다.")
        sys.exit(1)

    print(f"[{_datetime.now()}] 액세스 토큰 발급 중...")
    access_token = _refresh_access_token(client_id, client_secret, refresh_token)

    print("Rebalancer 폴더 확인 중...")
    folder_id = _google_find_folder(access_token) or _google_create_folder(access_token)

    print("portfolio.json 확인 중...")
    remote = _google_find_file(access_token, folder_id)
    if not remote:
        print("[오류] 드라이브에서 portfolio.json 을 찾지 못했습니다. "
             "PC 리밸런서에서 먼저 한 번 저장해주세요.")
        sys.exit(1)

    print("최신 데이터 다운로드 중...")
    data = _google_download(access_token, remote["id"])
    if not isinstance(data, dict) or "accounts" not in data:
        print(f"[오류] 다운로드한 파일 형식이 올바르지 않습니다: {str(data)[:200]}")
        sys.exit(1)

    warnings = []
    print("지수·환율 갱신 중...")
    warnings += refresh_market(data)
    print("종목 시세 갱신 중...")
    warnings += refresh_prices(data)
    print("추세 지표(QQQ/SOXX/EWY) 갱신 중...")
    for sym in TREND_SYMBOLS:
        try:
            update_trend_history(data, sym)
        except Exception as e:
            warnings.append(f"{sym} 데이터 실패: {e}")
    try:
        data["trendSignals"] = compute_trend_signals(data)
        record_trend_history(data)
    except Exception as e:
        warnings.append(f"추세 지표 계산 실패: {e}")

    print("구글 드라이브에 업로드 중...")
    content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    _google_upload_update(access_token, remote["id"], content)

    if warnings:
        print("일부 경고:")
        for w in warnings:
            print("  -", w)
    print(f"[{_datetime.now()}] 완료.")


if __name__ == "__main__":
    main()
