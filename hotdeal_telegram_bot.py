"""
애드픽 핫딜/추천상품 자동 수집 -> 텔레그램 알림 봇

- 애드픽 쇼핑메이트 API에서 핫딜/추천상품 데이터를 주기적으로 수집
- 새로운 상품만 골라서 텔레그램으로 알림 전송
- 알림을 받으면 당신이 복사해서 카카오톡 오픈채팅방에 붙여넣기

설정 필요한 값 3개 (아래 CONFIG 부분에 입력):
1. AFFID: 애드픽 회원 아이디(affid)
2. TELEGRAM_BOT_TOKEN: BotFather에서 발급받은 토큰
3. TELEGRAM_CHAT_ID: 본인 chat_id
"""

import requests
import time
import json
import os
from datetime import datetime

# ============ CONFIG ============
# GitHub Actions에서는 Secrets로 주입된 환경변수를 사용합니다.
# 로컬 PC에서 직접 돌릴 때는 환경변수가 없으면 아래 기본값을 사용합니다.
AFFID = os.environ.get("ADPICK_AFFID", "여기에_애드픽_affid_입력")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "여기에_텔레그램_봇_토큰_입력")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "여기에_본인_chat_id_입력")

# 로컬 PC에서 무한 루프로 돌릴 때만 사용되는 값 (GitHub Actions에서는 미사용)
CHECK_INTERVAL_SECONDS = 300  # 5분마다 확인 (API 제한: 최소 60초 이상)

SENT_LOG_FILE = "sent_items.json"  # 중복 알림 방지용 기록 파일
SEND_DELAY_SECONDS = 2  # 메시지 하나 보내고 다음 보내기 전 대기 시간 (도배 방지)
MAX_ITEMS_PER_RUN = 10  # 실행 1회당 보낼 최대 개수 (넘는 건 다음 실행에서 이어서 전송)

# GitHub Actions 환경인지 자동 감지 (Actions가 자동으로 설정해주는 값)
RUNNING_ON_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
# =======================================================

HOTDEAL_API = "https://adpick.co.kr/apis/sdk_shopping_hotdeal.php"
RECOMMEND_API = "https://adpick.co.kr/apis/sdk_shopping_recommend.php"


def load_sent_log():
    """이미 보낸 상품 ID 목록 불러오기 (중복 알림 방지)"""
    if os.path.exists(SENT_LOG_FILE):
        with open(SENT_LOG_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_sent_log(sent_ids):
    """보낸 상품 ID 목록 저장 (최근 500개만 유지)"""
    trimmed = list(sent_ids)[-500:]
    with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)


def fetch_api(url):
    """애드픽 API 호출 (공통 함수)"""
    try:
        response = requests.get(url, params={"affid": AFFID}, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[에러] API 호출 실패 ({url}): {e}")
        return []


def calc_discount_rate(price_sale, price_org):
    """할인율 계산 (price_sale, price_org는 '4,800' 같은 콤마 포함 문자열)"""
    try:
        sale = int(str(price_sale).replace(",", ""))
        org = int(str(price_org).replace(",", ""))
        if org > 0 and org > sale:
            return round((1 - sale / org) * 100)
    except (ValueError, TypeError):
        pass
    return None


def normalize_items(raw_data, source_name):
    """
    실제 애드픽 응답 구조:
    [ { "title": "...", "description": "...", "list": [ {product_name, mall_name, price_sale, price_org, commission, buyurl, ...}, ... ] } ]
    """
    items = []

    # 최상위가 리스트이고, 그 안에 {"list": [...]} 객체가 들어있는 구조
    if isinstance(raw_data, list) and raw_data:
        product_list = raw_data[0].get("list", [])
    elif isinstance(raw_data, dict):
        product_list = raw_data.get("list", [])
    else:
        product_list = []

    for item in product_list:
        buyurl = item.get("buyurl", "")
        # buyurl 안의 offer 값으로 고유 ID 생성 (같은 상품 재알림 방지용)
        item_id = buyurl.split("offer=")[-1].split("&")[0] if "offer=" in buyurl else buyurl

        discount = calc_discount_rate(item.get("price_sale"), item.get("price_org"))

        items.append({
            "id": f"{source_name}_{item_id}",
            "title": item.get("product_name", "상품명 없음"),
            "price_sale": item.get("price_sale", ""),
            "price_org": item.get("price_org", ""),
            "discount": discount,
            "commission": item.get("commission", ""),
            "mall": item.get("mall_name", ""),
            "link": buyurl,
            "source": source_name,
        })
    return items


SOURCE_LABELS = {
    "hotdeal": "🔥 핫딜상품",
    "recommend": "🔔 추천상품",
}


def format_single_message(item):
    """상품 1개짜리 텔레그램 메시지 포맷 (개별 전송용, 바로 복붙 가능)"""
    label = SOURCE_LABELS.get(item["source"], "상품")
    discount = f" (↓{item['discount']}%)" if item["discount"] else ""
    price_org = f" (정가 {item['price_org']}원)" if item["price_org"] and item["price_org"] != item["price_sale"] else ""
    return (
        f"{label}\n"
        f"🏪 {item['mall']}\n"
        f"📌 {item['title']}\n"
        f"💰 {item['price_sale']}원{discount}{price_org}\n"
        f"🔗 {item['link']}"
    )


def send_telegram(message):
    """텔레그램으로 메시지 전송"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        }, timeout=10)
        if resp.status_code == 200:
            print("[전송 완료] 텔레그램 알림 발송됨")
        else:
            print(f"[에러] 텔레그램 전송 실패: {resp.text}")
    except Exception as e:
        print(f"[에러] 텔레그램 전송 중 오류: {e}")


def run_once():
    """한 번 수집 -> 신규 상품만 필터 -> 1개씩 개별 전송"""
    sent_ids = load_sent_log()

    hotdeal_raw = fetch_api(HOTDEAL_API)
    recommend_raw = fetch_api(RECOMMEND_API)

    all_items = normalize_items(hotdeal_raw, "hotdeal") + normalize_items(recommend_raw, "recommend")

    new_items = [item for item in all_items if item["id"] not in sent_ids]

    if not new_items:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 새로운 항목 없음")
        return

    # 한 번에 너무 많이 몰아 보내지 않도록 매 실행마다 상한선 적용
    # 상한 넘는 나머지는 sent_ids에 기록하지 않으므로 다음 실행 때 이어서 전송됨
    if len(new_items) > MAX_ITEMS_PER_RUN:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 신규 {len(new_items)}건 중 {MAX_ITEMS_PER_RUN}건만 이번에 전송, 나머지는 다음 실행에서 이어서 전송")
        to_send = new_items[:MAX_ITEMS_PER_RUN]
    else:
        to_send = new_items

    for item in to_send:
        message = format_single_message(item)
        send_telegram(message)
        time.sleep(SEND_DELAY_SECONDS)  # 텔레그램 과호출/도배 방지

    # 실제로 전송에 성공한 항목만 기록 (안 보낸 건 다음 실행 때 다시 시도됨)
    sent_ids.update(item["id"] for item in to_send)
    save_sent_log(sent_ids)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 신규 {len(new_items)}건 중 {len(to_send)}건 전송 완료")


def main():
    if RUNNING_ON_GITHUB_ACTIONS:
        # GitHub Actions: 1회만 실행하고 종료 (스케줄러가 반복 호출을 담당)
        print("GitHub Actions 환경에서 1회 실행")
        run_once()
    else:
        # 로컬 PC: 무한 루프로 계속 실행
        print("애드픽 핫딜 -> 텔레그램 봇 시작 (로컬 모드)")
        print(f"확인 주기: {CHECK_INTERVAL_SECONDS}초마다\n")
        while True:
            run_once()
            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
