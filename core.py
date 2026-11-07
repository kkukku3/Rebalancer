# -*- coding: utf-8 -*-
"""
리밸런서 공통 모듈 (core.py)
- PC의 app.py 와 깃허브의 cloud_update.py 가 똑같이 이 파일을 불러 쓴다.
  → 계산·시세 로직을 고칠 때는 이 파일 하나만 고치고, PC 폴더와 깃허브 저장소
    두 곳에 같은 파일을 넣으면 된다.
- 표준 라이브러리만 사용 (pip 설치 불필요).

데이터는 두 파일로 나뉜다.
  portfolio.json : 사람이 고치는 것 (설정, 계좌, 종목 수량·평단가, 섹터, 가상계산 기록)
  prices.json    : 자동으로 갱신되는 것 (시세, 지수·환율, 추세 지표, 수익 기록)
자동 업데이트(cloud_update.py)는 prices.json 만 쓰므로, 사람이 고친 내용을 덮어쓰지 않는다.
"""

import copy
import hashlib
import json
import random
import re
import ssl
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone, time as dtime

# ==============================================================
#  설정
# ==============================================================
# RSI 계산 방식. "simple" = 기존 방식(최근 14일 단순 평균),
#                "wilder" = 증권사·트레이딩뷰 차트와 같은 표준 방식.
# 바꾸면 PC 폴더와 깃허브 저장소의 core.py 를 둘 다 바꿔야 한다.
RSI_METHOD = "wilder"

# 시세(야후·네이버) 조회 때 SSL 인증서 검증이 실패하면(학교·회사망의 보안 장비 등)
# 검증 없이 한 번 더 시도할지. 시세는 공개 정보라 허용해 둔다.
# 구글·깃허브 통신은 이 값과 무관하게 항상 검증한다.
ALLOW_INSECURE_MARKET_DATA = True

LOGO_LOGIC_VERSION = 2
DATA_VERSION = 2
TREND_SYMBOLS = ["QQQ", "SOXX", "EWY"]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
FOLDER_NAME = "Rebalancer"
PORTFOLIO_FILE = "portfolio.json"
PRICES_FILE = "prices.json"

# 종목(holding)에 붙어 있던 '자동 갱신' 필드들 → prices.json 의 quotes 로 옮겨감
QUOTE_FIELDS = ["price", "preMarketPrice", "preMarketChangePercent",
                "postMarketPrice", "postMarketChangePercent",
                "afterHoursPrice", "afterHoursChangePercent", "marketState"]
# prices.json 에 들어가는 최상위 키
PRICES_KEYS = ["market", "quotes", "hist", "histFetched", "trendSignals",
               "trendHistory", "history", "updatedAt"]
# 예전 버전이 남긴, 지금은 쓰지 않는 키
LEGACY_KEYS = ["qqq", "qqqSignals"]


# ==============================================================
#  시간 (외부 라이브러리 없이 한국·미국 동부 시간 계산)
# ==============================================================
KST = timezone(timedelta(hours=9), "KST")


def utcnow():
    return datetime.now(timezone.utc)


def _first_sunday(year, month):
    d = datetime(year, month, 1)
    return 1 + (6 - d.weekday()) % 7


def _us_dst(utc_dt):
    """미국 동부 서머타임 여부 (3월 둘째 일요일 ~ 11월 첫째 일요일)."""
    y = utc_dt.year
    start = datetime(y, 3, _first_sunday(y, 3) + 7, 7, 0, tzinfo=timezone.utc)
    end = datetime(y, 11, _first_sunday(y, 11), 6, 0, tzinfo=timezone.utc)
    return start <= utc_dt < end


def to_et(utc_dt):
    off = -4 if _us_dst(utc_dt) else -5
    return utc_dt.astimezone(timezone(timedelta(hours=off)))


def to_kst(utc_dt):
    return utc_dt.astimezone(KST)


def trading_day(now=None):
    """수익·추세 기록에 쓰는 '거래일' 날짜.
    한국시간 새벽 6시 30분에 날짜가 바뀐다 → 밤사이 미국장 마감(겨울 06:00, 여름 05:00)까지
    그날 기록에 들어간다. (예: 금요일 밤~토요일 새벽 미국장 → 금요일 기록)"""
    now = now or utcnow()
    return (to_kst(now) - timedelta(hours=6, minutes=30)).date().isoformat()


def is_record_day(day_iso):
    """주말(토·일) 날짜에는 기록을 남기지 않는다."""
    return datetime.strptime(day_iso, "%Y-%m-%d").weekday() < 5


def market_window_open(now=None):
    """한국장(07:55~20:10 KST, NXT 포함) 또는 미국장(03:55~20:10 ET, 프리·애프터 포함)
    중 하나라도 열려 있는 시간인지. 공휴일은 따지지 않는다(열려 있다고 보고 실행)."""
    now = now or utcnow()
    k = to_kst(now)
    if k.weekday() < 5 and dtime(7, 55) <= k.time() <= dtime(20, 10):
        return True
    e = to_et(now)
    if e.weekday() < 5 and dtime(3, 55) <= e.time() <= dtime(20, 10):
        return True
    return False


def us_last_close_utc(now=None):
    """지금 이전의 가장 최근 미국 정규장 마감(평일 16:00 ET) 시각(UTC)."""
    now = now or utcnow()
    d = to_et(now).date()
    for _ in range(8):
        noon = datetime(d.year, d.month, d.day, 17, 0, tzinfo=timezone.utc)
        off = -4 if _us_dst(noon) else -5
        close = datetime(d.year, d.month, d.day, 16, 0, tzinfo=timezone.utc) - timedelta(hours=off)
        if d.weekday() < 5 and close <= now:
            return close
        d = d - timedelta(days=1)
    return now - timedelta(days=7)


def kr_use_afterhours(now=None):
    """국내 시간외(NXT 애프터마켓) 가격을 평가 가격으로 쓸지.
    정규장 시간(평일 08:00~15:30)에는 쓰지 않는다 — 전날 저녁 값이 남아 있어도 무시."""
    k = to_kst(now or utcnow())
    if k.weekday() >= 5:
        return True
    return not (dtime(8, 0) <= k.time() < dtime(15, 30))


def parse_iso(s):
    try:
        if not s:
            return None
        s = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None  # 예전 형식('2026-11-05')은 시각 정보가 없어 무효 처리
        return dt
    except Exception:
        return None


def iso_utc(dt=None):
    return (dt or utcnow()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ==============================================================
#  HTTP
# ==============================================================
_SSL_VERIFIED = ssl.create_default_context()
_SSL_INSECURE = ssl.create_default_context()
_SSL_INSECURE.check_hostname = False
_SSL_INSECURE.verify_mode = ssl.CERT_NONE


def _is_ssl_error(e):
    if isinstance(e, ssl.SSLError):
        return True
    if isinstance(e, urllib.error.URLError) and isinstance(getattr(e, "reason", None), ssl.SSLError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(e)


def http_get(url, timeout=15):
    """시세용 GET. 인증서 검증 실패 시에만(설정에 따라) 검증 없이 재시도."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_VERIFIED) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        if ALLOW_INSECURE_MARKET_DATA and _is_ssl_error(e):
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_INSECURE) as resp:
                return resp.read().decode("utf-8", errors="replace")
        raise


def api_request(method, url, headers=None, body=None, timeout=25):
    """구글·깃허브용 요청. 항상 인증서를 검증한다. (status, raw bytes) 반환."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    data = None
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_VERIFIED) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8", errors="replace")
        except Exception:
            err = ""
        raise ApiError(e.code, f"{method} {url.split('?')[0]} → HTTP {e.code} {e.reason}. 응답: {err[:300]}")
    except Exception as e:
        if _is_ssl_error(e):
            raise ApiError(0, f"SSL 인증서 확인 실패({url.split('?')[0]}). 학교·회사망의 보안 장비가 "
                              f"통신을 가로채고 있을 수 있어요. 다른 네트워크에서 시도해 보세요. ({e})")
        raise


class ApiError(RuntimeError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def _json_or_raw(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw


# ==============================================================
#  구글 OAuth / 드라이브
# ==============================================================
def refresh_access_token(client_id, client_secret, refresh_token):
    """리프레시 토큰으로 액세스 토큰 발급. (access_token, expires_in) 반환."""
    body = urllib.parse.urlencode({
        "refresh_token": refresh_token, "client_id": client_id,
        "client_secret": client_secret, "grant_type": "refresh_token"})
    _, raw = api_request("POST", "https://oauth2.googleapis.com/token",
                         {"Content-Type": "application/x-www-form-urlencoded"}, body)
    tok = _json_or_raw(raw)
    if not isinstance(tok, dict) or "access_token" not in tok:
        raise RuntimeError(f"액세스 토큰 발급 실패: {tok}")
    return tok["access_token"], tok.get("expires_in", 3600)


class Drive:
    """구글 드라이브 'Rebalancer' 폴더의 portfolio.json / prices.json 을 다룬다.
    token_fn() 은 유효한 액세스 토큰 문자열을 돌려주는 함수."""

    API = "https://www.googleapis.com/drive/v3/files"
    UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

    def __init__(self, token_fn):
        self.token_fn = token_fn

    def _call(self, method, url, body=None, ctype=None):
        headers = {"Authorization": f"Bearer {self.token_fn()}"}
        if ctype:
            headers["Content-Type"] = ctype
        _, raw = api_request(method, url, headers, body)
        return _json_or_raw(raw)

    def find_folder(self):
        q = (f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' "
             "and trashed=false")
        res = self._call("GET", self.API + "?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name)", "orderBy": "createdTime"}))
        files = res.get("files", []) if isinstance(res, dict) else []
        return files[0]["id"] if files else None

    def create_folder(self):
        body = json.dumps({"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"})
        return self._call("POST", self.API, body, "application/json")["id"]

    def ensure_folder(self):
        return self.find_folder() or self.create_folder()

    def list_files(self, folder_id):
        """{파일이름: {id, modifiedTime}} — 같은 이름이 여러 개면 가장 최근 것."""
        q = (f"'{folder_id}' in parents and trashed=false and "
             f"(name='{PORTFOLIO_FILE}' or name='{PRICES_FILE}')")
        res = self._call("GET", self.API + "?" + urllib.parse.urlencode(
            {"q": q, "fields": "files(id,name,modifiedTime)", "orderBy": "modifiedTime desc"}))
        out = {}
        for f in (res.get("files", []) if isinstance(res, dict) else []):
            out.setdefault(f["name"], {"id": f["id"], "modifiedTime": f.get("modifiedTime")})
        return out

    def get_meta(self, file_id):
        """파일의 modifiedTime 확인. 지워졌으면 None."""
        try:
            m = self._call("GET", f"{self.API}/{file_id}?fields=id,modifiedTime,trashed")
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        if not isinstance(m, dict) or m.get("trashed"):
            return None
        return m

    def download_json(self, file_id):
        data = self._call("GET", f"{self.API}/{file_id}?alt=media")
        if isinstance(data, (bytes, bytearray)):
            data = json.loads(data.decode("utf-8"))
        return data

    def upload_new(self, folder_id, name, content_bytes):
        boundary = "rebalancer_boundary_9f2c9e"
        metadata = json.dumps({"name": name, "parents": [folder_id]})
        head = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{metadata}\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n")
        body = head.encode("utf-8") + content_bytes + f"\r\n--{boundary}--".encode("utf-8")
        return self._call("POST", self.UPLOAD + "?uploadType=multipart&fields=id,modifiedTime",
                          body, f"multipart/related; boundary={boundary}")

    def upload_update(self, file_id, content_bytes):
        return self._call("PATCH", f"{self.UPLOAD}/{file_id}?uploadType=media&fields=id,modifiedTime",
                          content_bytes, "application/json")


def dumps(data):
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


# ==============================================================
#  야후 / 네이버 시세
# ==============================================================
def yahoo_chart(symbol, rng="1d", interval="1m", include_prepost=True):
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
    j = yahoo_chart(symbol, "1d", "1m")
    meta = j["chart"]["result"][0]["meta"]
    for k in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
        if meta.get(k) is not None:
            return float(meta[k])
    raise ValueError("가격 필드 없음")


_NAME_STOP = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
              "plc", "holdings", "holding", "group", "the", "sa", "nv", "ag", "class"}


def domain_candidates_from_name(name):
    if not name:
        return []
    n = name.lower()
    for junk in [",", ".", "'", "&"]:
        n = n.replace(junk, " ")
    words = [w for w in n.split() if w and w not in _NAME_STOP]
    if not words:
        return []
    out = [words[0] + ".com"]
    if len(words) >= 2:
        out.append(words[0] + words[1] + ".com")
    if len(words[0]) <= 3 and len(out) > 1:
        out[0], out[1] = out[1], out[0]
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def wikipedia_website(name):
    if not name:
        return None
    try:
        q = urllib.parse.quote(name)
        url = ("https://en.wikipedia.org/w/api.php?action=query&format=json"
               f"&prop=extlinks&ellimit=50&titles={q}&redirects=1")
        j = json.loads(http_get(url))
        for _, pg in j.get("query", {}).get("pages", {}).items():
            for el in pg.get("extlinks", []):
                link = el.get("*", "")
                if not link.startswith("http"):
                    continue
                d = link.split("//")[-1].split("/")[0].replace("www.", "").lower()
                if any(bad in d for bad in ("wikipedia", "wikimedia", "doi.org", "archive.org",
                                            "sec.gov", "google.", "bloomberg", "reuters",
                                            "nytimes", "twitter", "facebook", "linkedin")):
                    continue
                if _DOMAIN_RE.match(d):
                    return d
    except Exception:
        pass
    return None


def yahoo_us_stock(symbol, need_name=True):
    """미국 종목 현재가 + (필요하면) 종목명 + 프리/애프터마켓 정보.
    반환: (price, name, extras)"""
    price = name = None
    extras = {}
    try:
        j = yahoo_chart(symbol, "1d", "1m")
        res = j["chart"]["result"][0]
        meta = res["meta"]
        name = meta.get("longName") or meta.get("shortName")
        reg_price = None
        for k in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
            if meta.get(k) is not None:
                reg_price = float(meta[k])
                break
        price = reg_price
        now = time.time()
        ctp = meta.get("currentTradingPeriod") or {}

        def _in(pn):
            p = ctp.get(pn)
            return bool(p) and p.get("start") is not None and p.get("end") is not None \
                and p["start"] <= now < p["end"]

        session = "PRE" if _in("pre") else ("REGULAR" if _in("regular") else ("POST" if _in("post") else None))
        if session in ("PRE", "POST"):
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            last_close = None
            for c in quote.get("close") or []:
                if c is not None:
                    last_close = float(c)
            if last_close is not None:
                extras["marketState"] = session
                # 등락률 기준 = 마지막 정규장 종가 (프리마켓: 전날 종가, 애프터마켓: 오늘 종가)
                pct = (last_close - reg_price) / reg_price * 100 if reg_price else None
                if session == "PRE":
                    extras["preMarketPrice"] = last_close
                    extras["preMarketChangePercent"] = pct
                else:
                    extras["postMarketPrice"] = last_close
                    extras["postMarketChangePercent"] = pct
                price = last_close
        elif session == "REGULAR":
            extras["marketState"] = "REGULAR"
    except Exception:
        pass

    if not name and need_name:
        for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
            try:
                url = (f"https://{host}/v1/finance/search?q={urllib.parse.quote(symbol, safe='')}"
                       f"&quotesCount=1&newsCount=0")
                quotes = json.loads(http_get(url)).get("quotes", [])
                if quotes:
                    q0 = quotes[0]
                    name = (q0.get("longname") or q0.get("shortname")
                            or q0.get("longName") or q0.get("shortName"))
                break
            except Exception:
                continue
    if price is None:
        raise ValueError("가격을 찾지 못함 (야후 응답 없음)")
    return price, name, extras


def yahoo_index(symbol):
    """지수 1년 최고점 + 현재가"""
    j = yahoo_chart(symbol, "1y", "1d")
    res = j["chart"]["result"][0]
    now = res["meta"]["regularMarketPrice"]
    highs = res["indicators"]["quote"][0]["high"]
    return max(h for h in highs if h is not None), now


def yahoo_daily(symbol, rng="2y"):
    j = yahoo_chart(symbol, rng, "1d")
    res = j["chart"]["result"][0]
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c):
            continue
        d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        out.append({"date": d, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    return out


def _naver_realtime(q):
    url = f"https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:{q}"
    return http_get(url)


def naver_price(code):
    """국내 종목 현재가 (NXT 우선, 실패 시 일반)."""
    code = code.zfill(6)
    last_reason = None
    for q in (f"NXT_{code}", code):
        try:
            txt = _naver_realtime(q)
            m = re.search(r'"nv":\s*(\d+)', txt)
            if m:
                return float(m.group(1))
            last_reason = f"'nv' 필드 없음. 응답 앞부분: {txt[:120]!r}"
        except Exception as e:
            last_reason = f"요청 실패: {e}"
    print(f"  [경고] naver_price({code}) 실패 — {last_reason}")
    return None


def naver_afterhours(code):
    """국내 시간외 단일가(NXT 애프터마켓). (price, pct) 또는 (None, None)."""
    try:
        j = json.loads(_naver_realtime(code.zfill(6)))
        datas = j["result"]["areas"][0]["datas"]
        if not datas:
            return None, None
        info = datas[0].get("nxtOverMarketPriceInfo")
        if not info or info.get("overPrice") is None:
            return None, None
        price = float(str(info["overPrice"]).replace(",", ""))
        pct = None
        try:
            pct = float(info.get("fluctuationsRatio"))
        except (TypeError, ValueError):
            pass
        return price, pct
    except Exception:
        return None, None


def naver_name(code):
    try:
        txt = http_get(f"https://finance.naver.com/item/main.naver?code={code.zfill(6)}")
        m = re.search(r"<title>([^:<]+)", txt)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


# ==============================================================
#  종목 키 / 로고
# ==============================================================
def quote_key(market, code):
    code = str(code or "").strip().upper()
    if market == "US":
        return "US:" + code
    return "KR:" + code.zfill(6)


def holding_key(h):
    return quote_key("US" if h.get("market") == "US" else "KR", h.get("code"))


def logos_us(name, domain):
    doms = []
    if domain:
        doms.append(domain)
    for d in domain_candidates_from_name(name):
        if d not in doms:
            doms.append(d)
    cands = [f"https://logo.clearbit.com/{d}" for d in doms]
    slug = (name or "").lower()
    for junk in [",", ".", "'", "&"]:
        slug = slug.replace(junk, " ")
    parts = [w for w in slug.split()
             if w not in (_NAME_STOP | {"technologies", "technology"}) and re.match(r"^[a-z0-9-]+$", w)]
    if parts:
        cands.append(f"https://s3-symbol-logo.tradingview.com/{parts[0]}--big.svg")
        if len(parts) >= 2:
            cands.append(f"https://s3-symbol-logo.tradingview.com/{parts[0]}-{parts[1]}--big.svg")
    for d in doms:
        cands.append(f"https://icons.duckduckgo.com/ip3/{d}.ico")
    return cands


def logos_kr(code):
    c6 = str(code).zfill(6)
    return [
        f"https://ssl.pstatic.net/imgstock/fn/real/logo/stock/Stock{c6}.svg",
        f"https://static.toss.im/png-icons/securities/icn-sec-fill-{c6}.png",
        f"https://thumb.tossinvest.com/image/resized/96x0/https%3A%2F%2Fstatic.toss.im%2Fpng-icons%2Fsecurities%2Ficn-sec-fill-{c6}.png",
    ]


def refresh_quote(key, old, now=None):
    """종목 하나의 시세를 받아 새 quote dict 를 돌려준다(old 는 이전 값)."""
    old = old or {}
    market, code = key.split(":", 1)
    q = {k: v for k, v in old.items() if k in ("name", "logos", "logoVer")}
    need_logo = not q.get("logos") or q.get("logoVer") != LOGO_LOGIC_VERSION
    if market == "US":
        price, name, extras = yahoo_us_stock(code, need_name=not q.get("name"))
        q["price"] = price
        q.update(extras)
        if name:
            q["name"] = name
        if need_logo:
            nm = q.get("name") or code
            q["logos"] = logos_us(nm, wikipedia_website(nm))
            q["logoVer"] = LOGO_LOGIC_VERSION
    else:
        p = naver_price(code)
        if p is None:
            if old.get("price") is None:
                raise ValueError("네이버 시세 없음")
            p = old.get("price")
        q["price"] = p
        if not q.get("name"):
            nm = naver_name(code)
            if nm:
                q["name"] = nm
        if kr_use_afterhours(now):
            ah, ah_pct = naver_afterhours(code)
            if ah is not None:
                q["afterHoursPrice"] = ah
                q["afterHoursChangePercent"] = ah_pct
                q["price"] = ah
        if need_logo:
            q["logos"] = logos_kr(code)
            q["logoVer"] = LOGO_LOGIC_VERSION
    q["at"] = iso_utc(now)
    return q


def held_keys(portfolio):
    keys = []
    for acc in portfolio.get("accounts", []):
        for h in acc.get("holdings", []):
            if str(h.get("code", "")).strip():
                k = holding_key(h)
                if k not in keys:
                    keys.append(k)
    return keys


def refresh_quotes(portfolio, prices, progress=None, now=None):
    """보유 중인 모든 종목의 시세 갱신(종목코드당 1번만 조회, 8개 동시)."""
    warnings = []
    quotes = prices.get("quotes") or {}
    keys = held_keys(portfolio)
    new_quotes = {}
    if progress is not None:
        progress.update({"done": 0, "total": len(keys)})
    if keys:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(refresh_quote, k, quotes.get(k), now): k for k in keys}
            for fut in as_completed(futs):
                k = futs[fut]
                try:
                    new_quotes[k] = fut.result()
                except Exception as e:
                    warnings.append(f"{k} 실패: {e}")
                    if k in quotes:
                        new_quotes[k] = quotes[k]   # 실패하면 이전 값 유지
                if progress is not None:
                    progress["done"] = progress.get("done", 0) + 1
    prices["quotes"] = new_quotes   # 더 이상 보유하지 않는 종목은 정리됨
    return warnings


def refresh_market(prices):
    warnings = []
    m = prices.setdefault("market", {})
    try:
        m["kospiHigh"], m["kospiNow"] = yahoo_index("^KS11")
    except Exception as e:
        warnings.append(f"KOSPI 실패: {e}")
    try:
        m["nasdaqHigh"], m["nasdaqNow"] = yahoo_index("^IXIC")
    except Exception as e:
        warnings.append(f"NASDAQ 실패: {e}")
    try:
        m["usdKrw"] = yahoo_price("USDKRW=X")
    except Exception as e:
        warnings.append(f"환율 실패: {e}")
    return warnings


# ==============================================================
#  추세(매도) 지표
# ==============================================================
def update_trend_history(prices, symbol, now=None, keep_days=500, force=False):
    """일별 이력(prices['hist'][symbol]) 갱신.
    - 미국 정규장이 마감(16:00 ET)된 뒤 한 번만 새로 받는다.
    - 장이 진행 중인 오늘의 미완성 봉은 저장하지 않는다.
    - 이미 있는 날짜도 새 값으로 교체한다(예전에 장중 값이 굳어버린 날짜도 바로잡힘)."""
    now = now or utcnow()
    store = prices.setdefault("hist", {})
    meta = prices.setdefault("histFetched", {})
    fetched = parse_iso(meta.get(symbol))
    last_close = us_last_close_utc(now)
    if not force and store.get(symbol) and fetched and fetched >= last_close + timedelta(minutes=15):
        return store[symbol]
    fresh = yahoo_daily(symbol, "2y")
    et = to_et(now)
    if et.weekday() < 5 and et.time() < dtime(16, 0):
        today = et.date().isoformat()
        fresh = [r for r in fresh if r["date"] != today]
    by_date = {r["date"]: r for r in store.get(symbol, [])}
    for r in fresh:
        by_date[r["date"]] = r
    hist = sorted(by_date.values(), key=lambda r: r["date"])[-keep_days:]
    store[symbol] = hist
    meta[symbol] = iso_utc(now)
    return hist


def _sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _rsi(closes, n=14, method=None):
    method = method or RSI_METHOD
    if len(closes) < n + 1:
        return None
    if method == "wilder":
        ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        ag = sum(max(x, 0) for x in ch[:n]) / n
        al = sum(max(-x, 0) for x in ch[:n]) / n
        for x in ch[n:]:
            ag = (ag * (n - 1) + max(x, 0)) / n
            al = (al * (n - 1) + max(-x, 0)) / n
    else:
        gains = losses = 0.0
        for i in range(-n, 0):
            c = closes[i] - closes[i - 1]
            if c >= 0:
                gains += c
            else:
                losses -= c
        ag, al = gains / n, losses / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def _atr(highs, lows, closes, n=14):
    if len(closes) < n + 1:
        return None
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(-n, 0)]
    return sum(trs) / n


def _psar_series(highs, lows, step=0.02, mx=0.20):
    n = len(highs)
    if n < 3:
        return None
    bull, af, ep, sar = True, step, highs[0], lows[0]
    out = [sar]
    for i in range(1, n):
        prev = sar
        sar = prev + af * (ep - prev)
        if bull:
            sar = min(sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if highs[i] > ep:
                ep = highs[i]
                af = min(af + step, mx)
            if lows[i] < sar:
                bull, sar, ep, af = False, ep, lows[i], step
        else:
            sar = max(sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if lows[i] < ep:
                ep = lows[i]
                af = min(af + step, mx)
            if highs[i] > sar:
                bull, sar, ep, af = True, ep, highs[i], step
        out.append(sar)
    return out


def _cum_vwap(hist, start_idx):
    pv = vv = 0.0
    out = []
    for i, r in enumerate(hist):
        tp = (r["h"] + r["l"] + r["c"]) / 3
        if i >= start_idx:
            pv += tp * r["v"]
            vv += r["v"]
        out.append(pv / vv if vv else None)
    return out


def _finalize_atr_state(closes, highs, lows):
    """ATR 규칙 상태 기계 (기존 app.py 와 동일). 'none'|'watch'|'sell'|'emergency'|'locked'"""
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

    below = hold = 0
    kind = None
    locked = False
    prev_high6 = None
    ref_start = 0
    for i in range(n):
        high6 = max(highs[max(0, i - 125):i + 1])
        if locked:
            new_high = prev_high6 is not None and high6 > prev_high6 + 1e-9
            m20 = _sma(closes[:i + 1], 20)
            if new_high or (m20 is not None and closes[i] > m20):
                locked = False
                below = 0
                ref_start = i
        if hold > 0:
            hold -= 1
            if hold == 0:
                kind = None
                locked = True
        line = stop_at(i, ref_start)
        trig_sell = (below >= 3) and not locked
        trig_emg = False
        if i >= 2:
            a_prev = atr_at(i - 1)
            if a_prev and (closes[i - 2] - closes[i - 1]) >= 2.5 * a_prev:
                trig_emg = True
        trig_emg = trig_emg and not locked
        if trig_emg:
            hold, kind, locked = 5, "emergency", False
        elif trig_sell and kind != "emergency" and hold <= 0:
            hold, kind = 5, "sell"
        if line is not None and closes[i] <= line:
            below += 1
        else:
            below = 0
        prev_high6 = high6
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
    """5개 매도 지표 (기존 app.py 와 동일 규칙)."""
    if len(hist) < 40:
        return None
    closes = [r["c"] for r in hist]
    highs = [r["h"] for r in hist]
    lows = [r["l"] for r in hist]
    c = closes[-1]
    sig = {}

    r = _rsi(closes, 14)
    if r is None:
        sig["RSI"] = {"state": "none", "detail": "-", "score": 0}
    elif r < 42:
        sig["RSI"] = {"state": "sell", "detail": f"RSI {r:.1f}", "score": 2.0}
    elif r < 50:
        sig["RSI"] = {"state": "watch", "detail": f"RSI {r:.1f}", "score": round(2.0 * (50 - r) / 8, 2)}
    else:
        sig["RSI"] = {"state": "none", "detail": f"RSI {r:.1f}", "score": 0}

    sars = _psar_series(highs, lows)
    if sars:
        flip_idx = None
        for i in range(1, len(sars)):
            if sars[i - 1] < lows[i - 1] and sars[i] > highs[i]:
                flip_idx = i
        strong = flip_idx is not None and (len(sars) - 1 - flip_idx) <= 5
        sig["PSAR"] = {"state": "sell" if strong else "none", "detail": "매도 반전" if strong else "-"}
    else:
        sig["PSAR"] = {"state": "none", "detail": "-"}

    start = max(0, len(hist) - 126)
    vw = _cum_vwap(hist, start)
    v_now = vw[-1]
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

    atr_state = _finalize_atr_state(closes, highs, lows)
    detail_map = {"emergency": "하루 2.5ATR 급락", "sell": "2ATR 이탈", "watch": "2ATR 이탈(관찰)",
                  "locked": "잠금(신고가 대기)", "none": "-"}
    sig["ATR"] = {"state": atr_state, "detail": detail_map.get(atr_state, "-")}

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
    if not sig:
        return None
    trend_pts = {"none": 0, "watch": 0.5, "sell": 1}
    rsi_pts = {"none": 0, "watch": 1, "sell": 2}
    atr_pts = {"none": 0, "watch": 1, "sell": 2, "emergency": 4, "locked": 0}

    def st(k):
        return sig.get(k, {}).get("state", "none")

    rsi_score = sig.get("RSI", {}).get("score")
    if rsi_score is None:
        rsi_score = rsi_pts.get(st("RSI"), 0)
    raw = (trend_pts.get(st("PSAR"), 0) + trend_pts.get(st("VWAP"), 0) + trend_pts.get(st("MA"), 0)
           + rsi_score + atr_pts.get(st("ATR"), 0))
    score = round(raw / 9.0 * 100)
    if score <= 40:
        level, label = "none", "정상"
    elif score <= 70:
        level, label = "watch", "매도 고려"
    elif score <= 90:
        level, label = "sell", "매도"
    else:
        level, label = "strong", "강력 매도"
    return {"score": score, "raw": round(raw, 1), "level": level, "label": label}


def compute_trend_signals(prices):
    store = prices.get("hist", {})
    out = {}
    for sym in TREND_SYMBOLS:
        details = compute_signals_for(store.get(sym, []))
        out[sym] = {"details": details, "composite": composite_from_signals(details) if details else None}
    return out


# ==============================================================
#  포트폴리오 ⇄ 화면용 데이터 (병합 / 분리)
# ==============================================================
def new_account_id(existing=()):
    while True:
        i = "a" + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(7))
        if i not in existing:
            return i


def _name_id(name, seen):
    """변환 때 쓰는 id: 계좌 이름에서 항상 같은 값이 나온다
    (PC와 드라이브에서 따로 변환해도 같은 계좌는 같은 id가 되도록)."""
    base = "a" + hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:7]
    i, aid = 1, base
    while aid in seen:
        i += 1
        aid = f"{base}{i}"
    return aid


def ensure_account_ids(portfolio, from_name=False):
    """계좌마다 고유 id 부여(이름이 바뀌어도 수익 기록이 이어지도록). 바뀌었으면 True."""
    changed = False
    seen = set()
    for acc in portfolio.get("accounts", []):
        aid = acc.get("id")
        if not aid or aid in seen:
            acc["id"] = _name_id(acc.get("name", ""), seen) if from_name else new_account_id(seen)
            changed = True
        seen.add(acc["id"])
    return changed


def apply_quote(h, q):
    """화면용: 종목에 시세 필드를 채운다."""
    if not q:
        return
    for f in QUOTE_FIELDS:
        if f in q and q[f] is not None:
            h[f] = q[f]
        else:
            h.pop(f, None)
    code = str(h.get("code", "")).strip()
    nm = (h.get("name") or "").strip()
    if q.get("name") and (not nm or nm.upper() == code.upper()):
        h["name"] = q["name"]
    cands = list(q.get("logos") or [])
    if h.get("logoPinned") and h.get("logo"):
        cands = [h["logo"]] + [c for c in cands if c != h["logo"]]
    elif cands:
        h["logo"] = cands[0]
    if cands:
        h["logos"] = cands


def merge_view(portfolio, prices):
    """portfolio + prices → 화면(예전 portfolio.json 모양)용 데이터."""
    v = copy.deepcopy(portfolio or {})
    prices = prices or {}
    quotes = prices.get("quotes") or {}
    for k in ("market", "trendSignals", "trendHistory", "history", "updatedAt"):
        if k in prices:
            v[k] = copy.deepcopy(prices[k])
    v.setdefault("market", {})
    v.setdefault("history", [])
    for acc in v.get("accounts", []):
        for h in acc.get("holdings", []):
            apply_quote(h, quotes.get(holding_key(h)))
    return v


def extract_portfolio(view):
    """화면용 데이터 → portfolio.json 에 저장할 부분만."""
    p = copy.deepcopy(view or {})
    for k in list(p.keys()):
        if k in PRICES_KEYS or k in LEGACY_KEYS or k.startswith("_"):
            p.pop(k, None)
    for acc in p.get("accounts", []):
        for h in acc.get("holdings", []):
            for f in QUOTE_FIELDS + ["logos", "logoVer"]:
                h.pop(f, None)
            if not h.get("logoPinned"):
                h.pop("logo", None)
    p["version"] = DATA_VERSION
    return p


def is_migrated(portfolio):
    accs = portfolio.get("accounts", [])
    return portfolio.get("version") == DATA_VERSION and all(a.get("id") for a in accs)


def _logo_fits(logos, name):
    """로고 후보 첫 주소에 회사 이름 첫 단어가 들어 있으면 그럴듯한 목록으로 본다."""
    if not logos or not name:
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", name.lower()) if w and w not in _NAME_STOP]
    return bool(words) and words[0] in logos[0].lower()


def migrate(old, prices=None):
    """예전 portfolio.json(시세가 섞인 하나짜리) → (portfolio, prices) 로 나눈다.
    이미 새 형식이면 그대로 돌려준다. 반환: (portfolio, prices, changed)"""
    old = copy.deepcopy(old or {})
    prices = copy.deepcopy(prices or {})
    changed = ensure_account_ids(old, from_name=True)
    legacy = any(k in old for k in PRICES_KEYS + LEGACY_KEYS) or old.get("version") != DATA_VERSION
    if not legacy and not changed:
        return old, prices, False
    name_to_id = {a.get("name"): a["id"] for a in old.get("accounts", [])}

    # 1) 시세 → quotes (종목코드당 하나. 로고 후보 목록은 사용자가 고정하지 않은 쪽을 우선)
    quotes = prices.setdefault("quotes", {})
    fresh_keys = set()
    for pinned_pass in (False, True):
        for acc in old.get("accounts", []):
            for h in acc.get("holdings", []):
                if not str(h.get("code", "")).strip():
                    continue
                k = holding_key(h)
                if k in quotes and k not in fresh_keys:
                    continue   # prices.json 에 이미 있던 값이 우선
                q = quotes.get(k)
                if q is None:
                    q = {f: h[f] for f in QUOTE_FIELDS if h.get(f) is not None}
                    if h.get("name"):
                        q["name"] = h["name"]
                    quotes[k] = q
                    fresh_keys.add(k)
                if h.get("logos") and bool(h.get("logoPinned")) == pinned_pass and \
                        (not q.get("logos") or (not _logo_fits(q["logos"], q.get("name"))
                                                and _logo_fits(h["logos"], q.get("name")))):
                    q["logos"] = list(h["logos"])
                    if h.get("logoVer") is not None:
                        q["logoVer"] = h["logoVer"]

    # 2) 지수·추세·이력 → prices (prices 쪽에 이미 있으면 그쪽 우선)
    for k in ("market", "hist", "histFetched", "trendSignals", "trendHistory"):
        if k in old and k not in prices:
            prices[k] = old[k]
    # histFetched 는 예전 형식(날짜만)이면 다음 갱신 때 새로 받도록 둔다.

    # 3) 수익 기록: 계좌 이름 → 계좌 id 로 키 변환, 날짜별로 합침
    def conv(row):
        return {"date": row["date"],
                "profits": {name_to_id.get(n, n): v for n, v in (row.get("profits") or {}).items()}}
    by_date = {}
    for row in old.get("history", []) or []:
        by_date[row["date"]] = conv(row)
    for row in prices.get("history", []) or []:
        by_date[row["date"]] = conv(row)
    prices["history"] = sorted(by_date.values(), key=lambda r: r["date"])

    # 4) 가상 계산 기록: 계좌 이름 키 → id 키 (+ 기록 당시 이름 보관)
    snap = old.get("snapshot")
    if isinstance(snap, dict) and snap.get("accounts") and not snap.get("names"):
        accs, names = {}, {}
        for n, rows in snap["accounts"].items():
            aid = name_to_id.get(n, n)
            accs[aid] = rows
            names[aid] = n
        snap["accounts"], snap["names"] = accs, names

    prices.setdefault("updatedAt", None)
    return extract_portfolio(old), prices, True


# ==============================================================
#  계산 (원본 엑셀 수식) / 기록
# ==============================================================
def target_stock_ratio(s, m):
    peak = s["peakStockRatio"]
    dd = s["maxDrawdownAssumption"]
    kd = (m["kospiNow"] - m["kospiHigh"]) / m["kospiHigh"] if m.get("kospiHigh") else 0
    nd = (m["nasdaqNow"] - m["nasdaqHigh"]) / m["nasdaqHigh"] if m.get("nasdaqHigh") else 0
    weighted = nd * s.get("nasdaqWeight", 0.85) + kd * s.get("kospiWeight", 0.25)
    return min(1.0, peak - (1 - peak) * weighted / dd)


def account_total(view, acc):
    fx = (view.get("market") or {}).get("usdKrw") or 0
    kr = us = 0.0
    for h in acc.get("holdings", []):
        val = (h.get("qty") or 0) * (h.get("price") or 0)
        if h.get("market") == "US":
            us += val * fx
        else:
            kr += val
    cash = (acc.get("cashKRW") or 0) + (acc.get("cashUSD") or 0) * fx
    return kr + us + cash


def record_history(portfolio, prices, now=None):
    """거래일 기준으로 계좌별 총수익을 prices['history'] 에 기록(같은 날은 덮어쓰기).
    키 = 계좌 id. 주말 날짜에는 기록하지 않는다."""
    day = trading_day(now)
    if not is_record_day(day):
        return False
    view = merge_view(portfolio, prices)
    profits = {}
    for acc in view.get("accounts", []):
        profits[acc.get("id") or acc.get("name")] = round(account_total(view, acc) - (acc.get("seed") or 0))
    hist = prices.setdefault("history", [])
    for row in hist:
        if row["date"] == day:
            row["profits"] = profits
            return True
    hist.append({"date": day, "profits": profits})
    hist.sort(key=lambda r: r["date"])
    return True


def record_trend_history(prices, now=None):
    day = trading_day(now)
    if not is_record_day(day):
        return
    store = prices.setdefault("trendHistory", {})
    sig = prices.get("trendSignals") or {}
    for sym in TREND_SYMBOLS:
        comp = (sig.get(sym) or {}).get("composite")
        if not comp:
            continue
        arr = store.setdefault(sym, [])
        for row in arr:
            if row["date"] == day:
                row["score"] = comp.get("score")
                break
        else:
            arr.append({"date": day, "score": comp.get("score")})
        arr.sort(key=lambda r: r["date"])


def run_update(portfolio, prices, progress=None, now=None):
    """시세 갱신 한 번 전체(지수·환율 → 종목 → 추세 → 기록). prices 를 제자리에서 고친다.
    PC의 Update 버튼과 클라우드 자동 업데이트가 똑같이 이 함수를 쓴다."""
    now = now or utcnow()
    prog = progress if progress is not None else {}
    prog.update({"stage": "지수·환율", "done": 0, "total": 0})
    warnings = refresh_market(prices)
    prog.update({"stage": "종목 시세"})
    warnings += refresh_quotes(portfolio, prices, prog, now)
    prog.update({"stage": "추세 신호", "done": 0, "total": len(TREND_SYMBOLS)})
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(update_trend_history, prices, s, now): s for s in TREND_SYMBOLS}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                warnings.append(f"{futs[fut]} 데이터 실패: {e}")
            prog["done"] = prog.get("done", 0) + 1
    try:
        prices["trendSignals"] = compute_trend_signals(prices)
        record_trend_history(prices, now)
    except Exception as e:
        warnings.append(f"추세 지표 실패: {e}")
    try:
        record_history(portfolio, prices, now)
    except Exception as e:
        warnings.append(f"수익 기록 실패: {e}")
    prices["updatedAt"] = iso_utc(now)
    prog.update({"stage": "", "done": 0, "total": 0})
    return warnings
