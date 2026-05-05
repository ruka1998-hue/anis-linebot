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
    TextMessage, FlexMessage, FlexContainer,
    QuickReply, QuickReplyItem, MessageAction
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

# 多步驟記帳狀態
record_state = {}  # {user_id: {"step": "category/amount/note", "data": {}}}

# ── 設定 ──────────────────────────────────────────────────
LINE_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").strip()
GEMINI_KEY  = os.environ.get("GEMINI_API_KEY", "").strip()
SHEET_URL   = os.environ.get("SHEET_URL", "https://docs.google.com/spreadsheets/d/1XQYryy0tMl-nuOKFLEotpLtaEDRymyDaHN6DNrJdMOc/edit").strip()
USER_ID     = os.environ.get("LINE_USER_ID", "").strip()
GEMINI_MODEL = "gemini-flash-latest"

# Debug
print(f"[DEBUG] TOKEN length: {len(LINE_TOKEN)}")
print(f"[DEBUG] TOKEN start: {LINE_TOKEN[:15]}...")
print(f"[DEBUG] TOKEN end: ...{LINE_TOKEN[-15:]}")

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

def safe_float(val):
    try: return float(str(val).replace(",","").replace(" ",""))
    except: return None

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

def get_budget_limit():
    """從 Google Sheets 讀取月預算上限"""
    try:
        sh = get_sheet()
        ws = sh.worksheet("Budget")
        rows = ws.get_all_values()
        if not rows or len(rows) < 2: return 0
        headers = [h.strip().lower() for h in rows[0]]
        if "totalbudget" in headers:
            idx = headers.index("totalbudget")
            v = rows[1][idx] if len(rows[1]) > idx else "0"
            return float(str(v).replace(",","")) or 0
        return 0
    except: return 0

def check_budget_alert():
    """檢查本月支出是否超過預算，超過就推播提醒"""
    if not USER_ID: return
    limit = get_budget_limit()
    if not limit: return
    spend, _ = get_month_stats()
    pct = spend / limit * 100
    # 80% 警告、100% 超標
    if pct >= 100:
        msg = f"🚨 指揮官！本月支出已超出預算！\n\n預算：NT${int(limit):,}\n實際：NT${int(spend):,}\n超支：NT${int(spend-limit):,}\n\n要認真檢討了。"
        send_message(USER_ID, msg)
    elif pct >= 80:
        msg = f"⚠️ 指揮官，本月預算已用了 {pct:.0f}%！\n\n預算：NT${int(limit):,}\n已花：NT${int(spend):,}\n剩餘：NT${int(limit-spend):,}\n\n接下來要注意一點。"
        send_message(USER_ID, msg)

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
    """嘗試從文字解析快速記帳，例如「午餐 85」「50 健身」"""
    text = text.strip()
    # 忽略指令
    if text in ["今天","今日","本月","這個月","說明","help","功能","昨天","月報"]:
        return None
    # 純數字不解析（避免誤判為快速記帳）
    try:
        float(text.replace(",",""))
        return None
    except: pass
    patterns = [
        r'^(\d+(?:\.\d+)?)\s+(.+)$',   # 數字在前：50 健身
        r'^(.+?)\s+(\d+(?:\.\d+)?)$',  # 文字在前：健身 50
        r'^(\d+(?:\.\d+)?)(.+)$',      # 無空格數字在前：50健身
        r'^(.+?)(\d+(?:\.\d+)?)$',     # 無空格文字在前：健身50
    ]
    for p in patterns:
        m = re.match(p, text)
        if m:
            g1, g2 = m.group(1), m.group(2)
            try:
                amt = float(g1); note = g2.strip()
                if not note: continue
            except:
                try:
                    amt = float(g2); note = g1.strip()
                    if not note: continue
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
    schedule.every().day.at("08:00").do(morning_remind)        # 早上8點
    schedule.every().day.at("21:00").do(evening_remind)        # 晚上9點提醒記帳
    schedule.every().monday.at("08:00").do(weekly_report)      # 週一早上週報
    schedule.every().day.at("22:00").do(check_budget_alert)    # 每晚10點檢查預算
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

# 類別頁面
CAT_PAGES = [
    [   # 第1頁：餐飲交通
        ("🍱外食", "🍱 餐飲-外食"),
        ("🏠自煮", "🏠 餐飲-自煮"),
        ("☕飲料", "☕ 餐飲-飲料咖啡"),
        ("🚌大眾運輸", "🚌 交通-大眾運輸"),
        ("⛽油費停車", "🚗 交通-油費停車"),
        ("✈️機票旅費", "✈️ 交通-機票旅費"),
        ("➡️更多類別", "__NEXT_PAGE__"),
        ("✏️自訂類別", "__CUSTOM__"),
    ],
    [   # 第2頁：購物娛樂
        ("👕衣物", "👕 購物-衣物"),
        ("🛒日用品", "🛒 購物-日用品"),
        ("📱3C", "📱 購物-3C"),
        ("🎮手遊課金", "🎮 娛樂-手遊課金"),
        ("🎬電影演唱會", "🎬 娛樂-電影演唱會"),
        ("📺訂閱服務", "📺 娛樂-訂閱服務"),
        ("➡️更多類別", "__NEXT_PAGE2__"),
        ("⬅️上一頁", "__PREV_PAGE__"),
    ],
    [   # 第3頁：其他
        ("🏥醫療健康", "🏥 醫療健康"),
        ("📚教育進修", "📚 教育進修"),
        ("🏠房租", "🏠 居住-房租"),
        ("💡水電瓦斯", "💡 居住-水電瓦斯"),
        ("💳保險", "💳 保險"),
        ("🎁人情禮金", "🎁 人情禮金"),
        ("❓其他支出", "❓ 其他支出"),
        ("⬅️上一頁", "__PREV_PAGE2__"),
    ],
]

def reply_with_categories(reply_token, msg="選擇支出類別：", page=0):
    """回覆帶有類別快速選擇按鈕"""
    cats = CAT_PAGES[page]
    items = []
    for label, cat_val in cats:
        items.append(QuickReplyItem(action=MessageAction(label=label, text=f"__CAT__{cat_val}")))
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            api.reply_message(ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=msg, quick_reply=QuickReply(items=items))]
            ))
    except Exception as e:
        print(f"quick reply 失敗: {e}")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event):
    text = event.message.text.strip()
    reply_token = event.reply_token
    today = date.today().strftime("%Y/%m/%d")

    # 指令處理
    user_id = event.source.user_id

    # 多步驟記帳流程
    if user_id in record_state:
        state = record_state[user_id]

        if state["step"] == "category" and text.startswith("__CAT__"):
            cat_val = text.replace("__CAT__", "")
            # 分頁控制
            if cat_val == "__NEXT_PAGE__":
                reply_with_categories(reply_token, "選擇支出類別（第2頁）：", page=1)
                return
            elif cat_val == "__NEXT_PAGE2__":
                reply_with_categories(reply_token, "選擇支出類別（第3頁）：", page=2)
                return
            elif cat_val == "__PREV_PAGE__":
                reply_with_categories(reply_token, "選擇支出類別（第1頁）：", page=0)
                return
            elif cat_val == "__PREV_PAGE2__":
                reply_with_categories(reply_token, "選擇支出類別（第2頁）：", page=1)
                return
            elif cat_val == "__CUSTOM__":
                record_state[user_id] = {"step": "custom_cat", "data": {}}
                reply_message(reply_token, "請輸入自訂類別名稱：\n例如：機車油費、寵物費用")
                return
            # 選完類別，問金額
            record_state[user_id] = {"step": "amount", "data": {"category": cat_val}}
            reply_message(reply_token, f"類別：{cat_val}\n\n💰 金額是多少？（直接傳數字）")
            return

        if state["step"] == "custom_cat":
            custom_cat = text.strip()
            record_state[user_id] = {"step": "amount", "data": {"category": custom_cat}}
            reply_message(reply_token, f"類別：{custom_cat}\n\n💰 金額是多少？（直接傳數字）")
            return

        elif state["step"] == "amount":
            amt = safe_float(text)
            if not amt:
                reply_message(reply_token, "請傳數字金額，例如：85")
                return
            record_state[user_id]["data"]["amount"] = amt
            record_state[user_id]["step"] = "note"
            reply_message(reply_token, f"金額：NT${int(amt):,}\n\n📌 備註是什麼？（或傳「略過」）")
            return

        elif state["step"] == "note":
            note = "" if text == "略過" else text
            data = record_state[user_id]["data"]
            del record_state[user_id]
            if append_transaction(today, "支出", data["category"], data["amount"], "TWD", note):
                reply_message(reply_token, f"✅ 記帳完成！\n{data['category']} NT${int(data['amount']):,}\n{note}")
            else:
                reply_message(reply_token, "記帳失敗，請稍後再試。")
            return

    if text in ["資產", "我的資產", "總資產", "帳戶", "淨資產"]:
        try:
            sh = get_sheet()
            total = 0
            details = []
            try:
                ws_a = sh.worksheet("Assets")
                rows = ws_a.get_all_values()
                if rows and len(rows) >= 2:
                    headers = [h.strip().lower() for h in rows[0]]
                    for row in rows[1:]:
                        if len(row) < 3: continue
                        try:
                            amt_idx = headers.index("amount") if "amount" in headers else 2
                            name_idx = headers.index("name") if "name" in headers else 1
                            curr_idx = headers.index("currency") if "currency" in headers else 3
                            v = float(str(row[amt_idx]).replace(",",""))
                            name = row[name_idx] if len(row) > name_idx else ""
                            curr = row[curr_idx].upper() if len(row) > curr_idx else "TWD"
                            rate = 32.5 if curr == "USD" else 0.215 if curr == "JPY" else 1.0
                            twd = v * rate
                            total += twd
                            if v != 0:
                                sign = "🔴" if twd < 0 else "・"
                                details.append(f"{sign}{name}：NT${int(twd):,}")
                        except: continue
            except: pass

            if not details:
                reply_message(reply_token, "找不到資產資料。")
                return

            detail_str = "\n".join(details[:10])
            cash_msg = f"💰 現金資產\n\n{detail_str}\n\n{'─'*15}\n小計：NT${int(total):,}\n\n📈 股票查詢中，稍等..."
            reply_message(reply_token, cash_msg)

            def fetch_stocks_push(cash_total, uid):
                try:
                    import yfinance as yf
                    sh2 = get_sheet()
                    ws_s = sh2.worksheet("Stocks")
                    srows = ws_s.get_all_values()
                    if not srows or len(srows) < 2:
                        send_message(uid, "股票分頁是空的。")
                        return
                    sheaders = [h.strip().lower() for h in srows[0]]
                    stock_total = 0
                    stock_lines = []
                    for row in srows[1:]:
                        if len(row) < 2: continue
                        try:
                            sym_idx = sheaders.index("symbol") if "symbol" in sheaders else 0
                            sha_idx = sheaders.index("shares") if "shares" in sheaders else 2
                            name_idx = sheaders.index("name") if "name" in sheaders else 1
                            sym = str(row[sym_idx]).strip().upper()
                            qty = float(str(row[sha_idx]).replace(",",""))
                            if not sym or not qty: continue
                            hist = yf.Ticker(sym).history(period="2d")["Close"]
                            if hist.empty: continue
                            price = float(hist.iloc[-1])
                            is_tw = sym.endswith(".TW") or sym.endswith(".TWO")
                            twd = price * qty * (1.0 if is_tw else 32.5)
                            stock_total += twd
                            name = row[name_idx] if len(row) > name_idx else sym
                            stock_lines.append(f"・{name}({sym})：NT${int(twd):,}")
                        except: continue
                    if stock_lines:
                        net = cash_total + stock_total
                        push_msg = f"📈 股票市值\n\n{"\n".join(stock_lines)}\n\n{'─'*15}\n股票小計：NT${int(stock_total):,}\n💰 淨資產合計：NT${int(net):,}"
                        send_message(uid, push_msg)
                    else:
                        send_message(uid, "找不到股票資料。")
                except Exception as e:
                    send_message(uid, f"股票查詢失敗：{e}")

            threading.Thread(target=fetch_stocks_push, args=(total, user_id), daemon=True).start()

        except Exception as e:
            reply_message(reply_token, f"查詢失敗：{e}")
        return

    if text in ["記帳", "手動記帳", "新增"]:
        record_state[user_id] = {"step": "category", "data": {}}
        reply_with_categories(reply_token, "選擇支出類別：")
        return

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

⚡ 快速記帳
直接傳：「85 午餐」或「50 健身」

📝 手動記帳
傳「記帳」→ 選類別 → 金額 → 備註

📊 查詢指令
・今天 → 今日支出
・本月 → 本月收支
・資產 → 資產總覽
・說明 → 這個說明

💬 自由對話
有理財問題直接問！

📷 拍照記帳
傳收據照片自動辨識！"""
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
@app.route("/ping", methods=["GET"])
def ping():
    """Keep-alive endpoint"""
    return "pong", 200

@app.route("/remind", methods=["GET","POST"])
def trigger_remind():
    """外部觸發提醒（用 cron-job.org 定時呼叫）"""
    t = request.args.get("type", "evening")
    if t == "morning": morning_remind()
    elif t == "weekly": weekly_report()
    else: evening_remind()
    return "ok", 200

if __name__ == "__main__":
    # 啟動排程執行緒
    t = threading.Thread(target=run_scheduler, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
