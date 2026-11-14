# -*- coding: utf-8 -*-
"""
리밸런서 클라우드 자동 업데이트 (GitHub Actions 에서 실행)
- cron-job.org 가 워크플로를 1분마다 깨우면 이 스크립트가 돈다.
- 구글 드라이브 'Rebalancer' 폴더에서
    portfolio.json  → 읽기만 한다 (어떤 종목을 갖고 있는지 확인용)
    prices.json     → 시세·지수·추세·수익 기록을 갱신해서 다시 올린다
  그래서 사람이 PC·모바일에서 고친 portfolio.json 을 덮어쓰는 일이 없다.
- 한국장·미국장이 모두 닫힌 시간(주말 포함)에는 아무것도 하지 않고 바로 끝난다.
  (FORCE_RUN=1 이면 시간과 무관하게 실행)
- 비밀 값은 전부 환경변수(깃허브 Secrets)에서 읽는다. 이 파일에는 키가 없다.
- 계산 로직은 같은 폴더의 core.py 에 있다(PC 와 같은 파일).
"""
import os
import socket
import ssl
import sys
import time
import urllib.error

import core


def _transient(e):
    """일시적인 통신 문제(시간 초과·서버 5xx·연결 끊김)인가? 로그인 만료·설정 오류는 해당 없음."""
    if isinstance(e, core.ApiError):
        return e.status in (0, 408, 429) or e.status >= 500
    if isinstance(e, urllib.error.HTTPError):
        return e.code in (408, 429) or e.code >= 500
    return isinstance(e, (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError, OSError))


def main():
    """일시적인 통신 오류는 한 번 더 시도하고, 그래도 안 되면 이번 회차만 건너뛴다(실패 메일 방지).
    1분마다 다시 돌기 때문에 한 번 건너뛰어도 다음 회차가 이어받는다. 진짜 문제(로그인 만료 등)는 실패로 남긴다."""
    last = None
    for i in range(2):
        try:
            return _run()
        except Exception as e:
            if not _transient(e):
                raise
            last = e
            print(f"[재시도 {i + 1}/2] 일시적인 통신 오류: {e}")
            time.sleep(4)
    print(f"[건너뜀] 통신 오류가 이어져 이번 회차는 건너뜁니다(다음 회차에 다시 시도): {last}")
    return 0


def _run():
    now = core.utcnow()
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    stamp = core.to_kst(now).strftime("%Y-%m-%d %H:%M KST")
    if not force and not core.market_window_open(now):
        print(f"[{stamp}] 한국장·미국장 모두 닫힌 시간이라 건너뜁니다.")
        return 0

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        print("[오류] GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN "
              "깃허브 Secrets 가 설정되어 있지 않습니다.")
        return 1

    print(f"[{stamp}] 액세스 토큰 발급 중...")
    try:
        token, _ = core.refresh_access_token(client_id, client_secret, refresh_token)
    except Exception as e:
        if _transient(e):
            raise            # 통신 문제면 main() 이 재시도/건너뜀 처리
        print(f"[오류] 구글 로그인 실패 — PC 리밸런서에서 '키 재인증'을 눌러주세요.\n  {e}")
        return 1
    drive = core.Drive(lambda: token)

    folder_id = drive.find_folder()
    if not folder_id:
        print("[오류] 구글 드라이브에 'Rebalancer' 폴더가 없습니다. PC 리밸런서를 먼저 실행해주세요.")
        return 1
    files = drive.list_files(folder_id)
    if core.PORTFOLIO_FILE not in files:
        print("[오류] portfolio.json 이 없습니다. PC 리밸런서에서 먼저 한 번 저장해주세요.")
        return 1

    portfolio = drive.download_json(files[core.PORTFOLIO_FILE]["id"])
    if not isinstance(portfolio, dict) or "accounts" not in portfolio:
        print(f"[오류] portfolio.json 형식이 올바르지 않습니다: {str(portfolio)[:200]}")
        return 1
    if not core.is_migrated(portfolio):
        # 새 버전으로 바꾸는 도중(저장소는 바꿨고 PC는 아직)에만 생기는 상태 → 실패 메일이 쏟아지지 않게 정상 종료
        print("[대기] 드라이브의 데이터가 아직 예전 형식입니다. 새 버전 PC 리밸런서(app.py)를 "
              "한 번 실행하면 자동으로 변환됩니다. 그 전까지는 아무것도 쓰지 않습니다.")
        return 0

    prices = {}
    if core.PRICES_FILE in files:
        prices = drive.download_json(files[core.PRICES_FILE]["id"]) or {}

    t0 = time.time()
    warnings = core.run_update(portfolio, prices, now=now)
    print(f"시세 갱신 완료 ({time.time() - t0:.1f}초, 종목 {len(prices.get('quotes', {}))}개)")

    content = core.dumps(prices)
    if core.PRICES_FILE in files:
        drive.upload_update(files[core.PRICES_FILE]["id"], content)
    else:
        drive.upload_new(folder_id, core.PRICES_FILE, content)
    print("prices.json 업로드 완료")

    if warnings:
        print("일부 경고:")
        for w in warnings:
            print("  -", w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
