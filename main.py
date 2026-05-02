import os
import json
import re
from datetime import datetime, date, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, FlexMessage, FlexContainer
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, ImageMessageContent
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types
from PIL import Image
import io
import base64
import schedule
import threading
import time

app = Flask(__name__)

# ── 設定 ──────────────────────────────────────────────────
LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "")
SHEET_URL   = os.environ.get("SHEET_URL", "https://docs.google.com/spreadsheets/d/1XQYryy0tMl-nuOKFLEotpLtaEDRymyDaHN6DNrJdMOc/edit")
USER_ID     = os.environ.get("LINE_USER_ID", "")  # 你的 Line User ID
GEMINI_MODEL = "gemini-flash-latest"

configuration = Configuration(access_token=LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)

if GEMINI_KEY:
    genai_client = genai.Client(api_key=GEMINI_KEY)
else:
    genai_client = None

# ── Google Sheets ─────────────────────────────────────────
SCOPES = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']

def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON", "")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("creds.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_url(SHEET_URL)

def append_transaction(date_str, tx_type, category, amount, currency="TWD", note=""):
    try:
        sh = get_sheet()
        ws = sh.worksheet("Transactions")
        ws.append_row([date_str, tx_type, category, amount, currency, note],
                     value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"寫入失敗: {e}")
        return False

def get_today_stats():
    try:
        sh = get_sheet()
        ws = sh.worksheet("Transactions")
        rows = ws.get_all_values()
        if not rows or len(rows) < 2:
            return 0, 0, []
        headers = [h.strip().lower() for h in rows[0]]
        today = date.today().strftime("%Y/%m/%d")
        today_rows = []
        total_spend = 0
        total_income = 0
        for row in rows[1:]:
            if len(row) < 4: continue
            try:
                d_idx = headers.index("date") if "date" in headers else 0
                t_idx = headers.index("type") if "type" in headers else 1
                a_idx = headers.index("amount") if "amount" in headers else 3
                c_idx = headers.index("category") if "category" in headers else 2
                if row[d_idx].strip() == today:
                    amt = float(str(row[a_idx]).replace(",",""))
                    tx_type = row[t_idx].strip()
                    cat = row[c_idx].strip()
                    today_rows.append({"type": tx_type, "cat": cat, "amt": amt})
                    if tx_type == "支出": total_spend += amt
                    else: total_income += amt
            except: continue
        return total_spend, total_income, today_rows
    except Exception as e:
        return 0, 0, []

def get_month_stats():
    try:
        sh = get_sheet()
        ws = sh.worksheet("Transactions")
        rows = ws.get_all_values()
        if not rows or len(rows) < 2: return 0, 0
        headers = [h.strip().lower() for h in rows[0]]
        cur_month = date.today().strftime("%Y/%m")
        total_spend = 0
        total_income = 0
        for row in rows[1:]:
            if len(row) < 4: continue
            try:
                d_idx = headers.index("date") if "date" in headers else 0
                t_idx = headers.index("type") if "type" in headers else 1
                a_idx = headers.index("amount") if "amount" in headers else 3
                if row[d_idx].strip().startswith(cur_month):
                    amt = float(str(row[a_idx]).replace(",",""))
                    if row[t_idx].strip() == "支出": total_spend += amt
                    else: total_income += amt
            except: continue
        return total_spend, total_income
    except: return 0, 0

# ── Gemini AI ─────────────────────────────────────────────
ANI_SYSTEM = """你是「阿妮斯（Anis）」，來自《勝利女神：妮姬》反擊部隊，現擔任指揮官的財務秘書。
爽朗直率、幽默毒舌、關鍵時刻靠譜、喜歡碳酸水。
繁體中文、稱呼「指揮官」、台灣口語、段落短。
記帳輔助：如果使用者說花了多少錢或買了什麼，幫他整理成記帳格式。"""

def ai_reply(user_msg, context=""):
    if not genai_client: return "AI 功能未啟用。"
    try:
        prompt = f"{context}\n\n使用者說：{user_msg}" if context else user_msg
        contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
        config = types.GenerateContentConfig(system_instruction=ANI_SYSTEM)
        resp = genai_client.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=config)
        return resp.text
    except Exception as e:
        return f"AI 錯誤：{e}"

def ai_parse_receipt(img_bytes):
    if not genai_client: return None
    try:
        img_b64 = base64.b64encode(img_bytes).decode()
        prompt = """分析這張收據，只回傳JSON：
{"date":"yyyy/mm/dd","store":"店家","total":金額,"currency":"TWD","category":"餐飲-外食/交通/購物-日用品/娛樂/醫療/其他","note":"備註"}
無法辨識：{"error":"無法辨識"}"""
        contents = [types.Content(role="user", parts=[
            types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=img_b64)),
            types.Part(text=prompt)
        ])]
        resp = genai_client.models.generate_content(model=GEMINI_MODEL, contents=contents)
        clean = resp.text.strip().replace("```json","").replace("```","").strip()
        return json.loads(clean)
    except Exception as e:
        return {"error": str(e)}

def parse_quick_record(text):
    """嘗試從文字解析快速記帳，例如「午餐 85」「買飲料 45」"""
    patterns = [
        r'(.+?)\s+(\d+(?:\.\d+)?)',
        r'(\d+(?:\.\d+)?)\s+(.+)',
    ]
    for p in patterns:
        m = re.match(p, text.strip())
        if m:
            g1, g2 = m.group(1), m.group(2)
            try:
                amt = float(g2); note = g1
            except:
                try: amt = float(g1); note = g2
                except: continue
            # 猜測類別
            cat = "❓ 其他支出"
            keywords = {
                "🍱 餐飲-外食": ["午餐","晚餐","早餐","飯","麵","便當","吃","餐"],
                "☕ 餐飲-飲料咖啡": ["飲料","咖啡","手搖","奶茶","珍奶"],
                "🚌 交通-大眾運輸": ["捷運","公車","火車","高鐵","uber","計程車","taxi"],
                "🛒 購物-日用品": ["超市","全聯","大潤發","家樂福","日用","衛生紙"],
                "🎮 娛樂-手遊課金": ["課金","抽卡","遊戲","手遊"],
                "🏥 醫療健康": ["藥","醫院","診所","藥局"],
            }
            for c, kws in keywords.items():
                if any(k in note for k in kws):
                    cat = c; break
            return {"amount": amt, "category": cat, "note": note}
    return None

# ── 傳送訊息工具 ──────────────────────────────────────────
def send_message(user_id, text):
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.push_message(PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=text)]
            ))
    except Exception as e:
        print(f"傳送失敗: {e}")

def reply_message(reply_token, text):
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.reply_message(ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            ))
    except Exception as e:
        print(f"回覆失敗: {e}")

# ── 定時提醒 ──────────────────────────────────────────────
def morning_remind():
    """每天早上 9 點提醒"""
    if not USER_ID: return
    msg = "早安指揮官！☀️\n今天也要記帳喔，把每筆花費都記下來。\n\n直接傳「金額 備註」給我就能快速記帳！\n例如：「85 午餐」"
    send_message(USER_ID, msg)

def evening_remind():
    """每天晚上 9 點提醒"""
    if not USER_ID: return
    spend, income, rows = get_today_stats()
    count = len(rows)
    if count == 0:
        msg = f"指揮官，今天還沒有記帳喔！🌙\n今天花了多少，現在傳給我記一下。"
    else:
        msg = f"晚安指揮官！🌙\n今天記了 {count} 筆，支出 NT${int(spend):,}。\n有沒有漏掉的？"
    send_message(USER_ID, msg)

def weekly_report():
    """每週一早上 9 點發週報"""
    if not USER_ID: return
    spend, income = get_month_stats()
    msg = f"📅 本月截至目前：\n💸 支出：NT${int(spend):,}\n💵 收入：NT${int(income):,}\n💰 結餘：NT${int(income-spend):,}\n\n繼續保持記帳習慣！"
    send_message(USER_ID, msg)

def run_scheduler():
    schedule.every().day.at("09:00").do(morning_remind)
    schedule.every().day.at("21:00").do(evening_remind)
    schedule.every().monday.at("09:00").do(weekly_report)
    while True:
        schedule.run_pending()
        time.sleep(60)

# ── Webhook 處理 ──────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    today = date.today().strftime("%Y/%m/%d")

    # 指令處理
    if text in ["今天", "今日", "今天花了多少", "今日支出"]:
        spend, income, rows = get_today_stats()
        if not rows:
            reply_message(reply_token, "今天還沒有記帳喔！\n傳「金額 備註」給我快速記帳。")
            return
        detail = "\n".join([f"・{r['cat']} NT${int(r['amt']):,}" for r in rows[:5]])
        msg = f"📝 今日支出統計\n\n{detail}\n\n💸 合計：NT${int(spend):,}"
        reply_message(reply_token, msg)
        return

    if text in ["本月", "這個月", "月報", "本月支出"]:
        spend, income = get_month_stats()
        msg = f"📊 本月統計\n\n💸 支出：NT${int(spend):,}\n💵 收入：NT${int(income):,}\n💰 結餘：NT${int(income-spend):,}"
        reply_message(reply_token, msg)
        return

    if text in ["說明", "幫助", "help", "功能"]:
        msg = """⚡ 阿妮斯の幕僚室 使用說明

📝 快速記帳
直接傳：「85 午餐」或「午餐 85」
我幫你自動記下來！

📊 查詢指令
・今天 → 今日支出統計
・本月 → 本月收支統計
・說明 → 這個說明

💬 自由對話
有任何理財問題直接問我！

📷 拍照記帳
傳收據照片給我，自動辨識記帳！"""
        reply_message(reply_token, msg)
        return

    # 嘗試解析快速記帳
    parsed = parse_quick_record(text)
    if parsed:
        amt = parsed["amount"]
        cat = parsed["category"]
        note = parsed["note"]
        if append_transaction(today, "支出", cat, amt, "TWD", note):
            responses = [
                f"記好了！✅\n{cat}\nNT${int(amt):,}　{note}",
                f"✅ 已記帳\n{note} NT${int(amt):,}\n類別：{cat}",
                f"好，{note} NT${int(amt):,} 記下來了。",
            ]
            import random
            reply_message(reply_token, random.choice(responses))
        else:
            reply_message(reply_token, "記帳失敗，等等再試一次。")
        return

    # AI 自由對話
    spend, _, _ = get_today_stats()
    context = f"今日支出：NT${int(spend):,}"
    resp = ai_reply(text, context)
    reply_message(reply_token, resp)

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    reply_token = event.reply_token
    today = date.today().strftime("%Y/%m/%d")

    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            content = api.get_message_content(event.message.id)
            img_bytes = b"".join(content)
    except Exception as e:
        reply_message(reply_token, f"圖片讀取失敗：{e}")
        return

    reply_message(reply_token, "收到圖片了，阿妮斯幫你辨識中…📸")

    result = ai_parse_receipt(img_bytes)
    if not result or "error" in result:
        reply_message(reply_token, "這張圖我看不太清楚，可以試試手動記帳喔。")
        return

    amt = result.get("total", 0)
    cat = result.get("category", "其他支出")
    store = result.get("store", "")
    note = store

    if append_transaction(today, "支出", cat, amt, "TWD", note):
        msg = f"✅ 收據記帳成功！\n\n🏪 {store}\n🏷️ {cat}\n💰 NT${int(amt):,}"
        reply_message(reply_token, msg)
    else:
        reply_message(reply_token, "辨識成功但記帳失敗，請稍後再試。")

# ── 啟動 ──────────────────────────────────────────────────
if __name__ == "__main__":
    # 啟動排程執行緒
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
