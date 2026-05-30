import json
import calendar
import csv
import uuid
import hashlib
import collections
import platform
from datetime import date, datetime, timedelta
from io import StringIO, BytesIO
from pathlib import Path

import streamlit as st
from fpdf import FPDF

st.set_page_config(page_title="Ödeme Takip", page_icon="💳", layout="wide")

# ── Font (Streamlit Cloud'da Linux) ───────────────────────────────────────
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/ARIALUNI.TTF",
]
FONT_PATH = next((f for f in _FONT_CANDIDATES if Path(f).exists()), None)

DEFAULT_SETTINGS = {
    "monthly_budget": 0.0,
    "pin_hash": None,
    "categories": ["Kira", "Faturalar", "Abonelikler", "Kredi Kartı",
                   "Ulaşım", "Sağlık", "Eğitim", "Diğer"],
    "currencies": {"USD": 38.0, "EUR": 42.0, "GBP": 50.0},
}

PERIOD_LABELS = {
    "monthly":   "Aylık",
    "quarterly": "3 Aylık",
    "biannual":  "6 Aylık",
    "yearly":    "Yıllık",
}

MONTH_TR   = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
              "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
DAYS_TR    = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
CURRENCIES = ["TRY", "USD", "EUR", "GBP"]
CUR_SYM    = {"TRY": "₺", "USD": "$", "EUR": "€", "GBP": "£"}


# ── Supabase bağlantısı ───────────────────────────────────────────────────

@st.cache_resource
def get_supabase():
    try:
        from supabase import create_client
        url = st.secrets["SUPABASE_URL"]
        key = st.secrets["SUPABASE_KEY"]
        return create_client(url, key)
    except Exception as e:
        return None


def load_data():
    sb = get_supabase()
    if sb is None:
        return {"payments": [], "settings": DEFAULT_SETTINGS.copy()}
    try:
        result = sb.table("odeme_data").select("data").eq("id", "main").execute()
        if result.data:
            data = result.data[0]["data"]
            if isinstance(data, str):
                data = json.loads(data)
            if "settings" not in data:
                data["settings"] = DEFAULT_SETTINGS.copy()
            else:
                for k, v in DEFAULT_SETTINGS.items():
                    data["settings"].setdefault(k, v)
            return data
    except Exception as e:
        st.error(f"Veri yüklenemedi: {e}")
    return {"payments": [], "settings": DEFAULT_SETTINGS.copy()}


def save_data(data):
    sb = get_supabase()
    if sb is None:
        st.error("Veritabanı bağlantısı yok.")
        return
    try:
        sb.table("odeme_data").upsert({"id": "main", "data": data}).execute()
        # session_state'i güncelle
        st.session_state["_data_cache"] = data
    except Exception as e:
        st.error(f"Veri kaydedilemedi: {e}")


def notify(msg, icon="✅"):
    st.session_state["_toast"] = (msg, icon)


# ── Yardımcı ──────────────────────────────────────────────────────────────

def parse_date(s):
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(s)


def days_until(d):
    return (d - date.today()).days


def add_months(d, months):
    month = d.month - 1 + months
    year  = d.year + month // 12
    month = month % 12 + 1
    day   = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def next_due(d, period):
    return add_months(d, {"monthly": 1, "quarterly": 3, "biannual": 6, "yearly": 12}[period])


def to_try(amount, currency, rates):
    if not currency or currency == "TRY":
        return amount
    return amount * rates.get(currency, 1.0)


def fmt_amount(p, rates):
    sym = CUR_SYM.get(p.get("currency", "TRY"), "₺")
    s   = f"{p['amount']:,.2f} {sym}"
    if p.get("currency", "TRY") != "TRY":
        s += f"  ({to_try(p['amount'], p['currency'], rates):,.2f} ₺)"
    return s


def status_badge(p):
    if p["paid"]:
        return "✅ Ödendi"
    diff = days_until(parse_date(p["due_date"]))
    if diff < 0:  return f"🔴 {abs(diff)} gün gecikti"
    if diff <= 3: return f"🟡 {diff} gün kaldı"
    if diff <= 7: return f"🟠 {diff} gün kaldı"
    return f"🔵 {diff} gün kaldı"


def next_month_forecast(payments):
    today    = date.today()
    nm_start = add_months(today.replace(day=1), 1)
    nm_end   = nm_start.replace(day=calendar.monthrange(nm_start.year, nm_start.month)[1])
    total, count = 0.0, 0
    for p in payments:
        d = parse_date(p["due_date"])
        if not p["paid"] and nm_start <= d <= nm_end:
            total += p["amount"]; count += 1
    return total, count


def make_qr(url):
    try:
        import qrcode
        img = qrcode.make(url)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


# ── PIN ───────────────────────────────────────────────────────────────────

def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()


def check_pin(pin_input, stored_hash):
    return hash_pin(pin_input) == stored_hash


def show_pin_screen(pin_hash):
    _, mid, _ = st.columns([1, 1.5, 1])
    with mid:
        st.markdown("## 🔒 Ödeme Takip")
        pin_input = st.text_input("PIN", type="password", max_chars=8, key="pin_input")
        if st.button("Giriş Yap", type="primary", use_container_width=True):
            if check_pin(pin_input, pin_hash):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Yanlış PIN.")


# ── Takvim HTML ───────────────────────────────────────────────────────────

def render_calendar_html(year, month, payments):
    pay_days = collections.defaultdict(list)
    for p in payments:
        d = parse_date(p["due_date"])
        if d.year == year and d.month == month:
            pay_days[d.day].append(p)
    today = date.today()
    css = """<style>
.ot-cal{font-family:-apple-system,sans-serif;max-width:720px}
.ot-cal table{width:100%;border-collapse:collapse}
.ot-cal th{background:#f0f4ff;padding:8px;font-size:.85em;color:#555;text-align:center;border:1px solid #dde}
.ot-cal td{border:1px solid #eee;padding:6px 4px;vertical-align:top;min-height:64px;width:14.2%}
.dn{font-weight:700;font-size:.9em;color:#333;margin-bottom:2px}
.dt{background:#deeeff}.dh{background:#fffbeb}
.pi{font-size:.68em;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:92px}
</style>"""
    tbl = f"{css}<div class='ot-cal'><table><tr>"
    tbl += "".join(f"<th>{d}</th>" for d in DAYS_TR) + "</tr>"
    for week in calendar.monthcalendar(year, month):
        tbl += "<tr>"
        for day in week:
            if day == 0:
                tbl += "<td></td>"; continue
            cls = ""
            if date(year, month, day) == today: cls += " dt"
            if pay_days[day]:                   cls += " dh"
            tbl += f"<td class='{cls.strip()}'><div class='dn'>{day}</div>"
            for p in pay_days[day][:3]:
                tbl += f"<div class='pi'>{'✅' if p['paid'] else '🔴'} {p['name'][:13]}</div>"
            if len(pay_days[day]) > 3:
                tbl += f"<div class='pi'>+{len(pay_days[day])-3} daha…</div>"
            tbl += "</td>"
        tbl += "</tr>"
    tbl += "</table></div>"
    return tbl


# ── PDF ───────────────────────────────────────────────────────────────────

def generate_pdf(payments_list, period_label, start_d, end_d, rates):
    pdf = FPDF()
    if FONT_PATH:
        pdf.add_font("AU", "",  FONT_PATH)
        pdf.add_font("AU", "B", FONT_PATH)
        fn = "AU"
    else:
        fn = "Helvetica"
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page(); pdf.set_margins(15, 15, 15)

    pdf.set_font(fn, "B", 20); pdf.set_text_color(30, 60, 130)
    pdf.cell(0, 12, "Odeme Takip Raporu", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(fn, "", 10); pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, f"Donem: {period_label}  |  {start_d.strftime('%d.%m.%Y')} - {end_d.strftime('%d.%m.%Y')}",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, f"Olusturulma: {date.today().strftime('%d.%m.%Y')}",
             align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    paid_p  = [p for p in payments_list if p["paid"]]
    pend_p  = [p for p in payments_list if not p["paid"]]
    paid_a  = sum(to_try(p["amount"], p.get("currency","TRY"), rates) for p in paid_p)
    pend_a  = sum(to_try(p["amount"], p.get("currency","TRY"), rates) for p in pend_p)
    total_a = paid_a + pend_a
    rate_s  = f"{len(paid_p)/len(payments_list)*100:.0f}%" if payments_list else "0%"

    boxes = [("Toplam Odeme",f"{len(payments_list)} adet",(220,235,255)),
             ("Odenen",f"{paid_a:,.2f} TL",(220,255,230)),
             ("Bekleyen",f"{pend_a:,.2f} TL",(255,245,210)),
             ("Odeme Orani",rate_s,(240,230,255))]
    cw = (pdf.w - 2*pdf.l_margin)/4
    for label,_,fill in boxes:
        pdf.set_fill_color(*fill); pdf.set_font(fn,"B",9); pdf.set_text_color(60,60,60)
        pdf.cell(cw,7,label,border=1,fill=True,align="C")
    pdf.ln()
    for _,value,fill in boxes:
        pdf.set_fill_color(*fill); pdf.set_font(fn,"",10); pdf.set_text_color(30,30,30)
        pdf.cell(cw,9,value,border=1,fill=True,align="C")
    pdf.ln(8)

    col_w=[43,24,28,24,25,18,28]
    headers=["Odeme Adi","Kategori","Tutar (TL)","Vade","Durum","Tekrar","Odeme Tar."]
    pdf.set_fill_color(40,80,180); pdf.set_text_color(255,255,255); pdf.set_font(fn,"B",9)
    for i,h in enumerate(headers):
        pdf.cell(col_w[i],8,h,border=1,fill=True,align="C")
    pdf.ln()

    pdf.set_text_color(0,0,0)
    for i,p in enumerate(sorted(payments_list,key=lambda x:parse_date(x["due_date"]))):
        pdf.set_fill_color(245,248,255) if i%2==0 else pdf.set_fill_color(255,255,255)
        pdf.set_font(fn,"",8)
        if p["paid"]:                               durum="Odendi"
        elif parse_date(p["due_date"])<date.today():durum="Gecikti"
        else:                                       durum="Bekliyor"
        tekrar=PERIOD_LABELS.get(p.get("recurring_period"),"-") if p.get("recurring") else "-"
        amt_try=to_try(p["amount"],p.get("currency","TRY"),rates)
        vals=[p["name"][:18],p.get("category","")[:10],f"{amt_try:,.2f}",
              p["due_date"],durum,tekrar,p.get("paid_date") or "-"]
        fill=(i%2==0)
        for j,v in enumerate(vals):
            pdf.cell(col_w[j],7,str(v),border=1,fill=fill)
        pdf.ln()

    pdf.set_fill_color(210,225,255); pdf.set_font(fn,"B",9); pdf.set_text_color(30,30,30)
    pdf.cell(col_w[0]+col_w[1],7,"TOPLAM",border=1,fill=True,align="R")
    pdf.cell(col_w[2],7,f"{total_a:,.2f}",border=1,fill=True,align="C")
    for w in col_w[3:]: pdf.cell(w,7,"",border=1,fill=True)
    pdf.ln(10)
    pdf.set_y(-13); pdf.set_font(fn,"",8); pdf.set_text_color(140,140,140)
    pdf.cell(0,6,f"Odeme Takip Sistemi  -  Sayfa {pdf.page_no()}",align="C")
    return bytes(pdf.output())


# ── CSV ───────────────────────────────────────────────────────────────────

def generate_csv(payments_list, rates):
    out = StringIO(); w = csv.writer(out)
    w.writerow(["Ödeme Adı","Kategori","Tutar","Para Birimi","Tutar (TL)",
                "Vade","Durum","Ödeme Tarihi","Tekrarlı","Periyot","Not"])
    for p in sorted(payments_list, key=lambda x: parse_date(x["due_date"])):
        cur=p.get("currency","TRY")
        w.writerow([p["name"],p.get("category",""),f"{p['amount']:.2f}",cur,
                    f"{to_try(p['amount'],cur,rates):.2f}",p["due_date"],
                    "Ödendi" if p["paid"] else "Bekliyor",p.get("paid_date") or "",
                    "Evet" if p.get("recurring") else "Hayır",
                    PERIOD_LABELS.get(p.get("recurring_period"),"") if p.get("recurring") else "",
                    p.get("note") or ""])
    return out.getvalue().encode("utf-8-sig")


# ── CSS ───────────────────────────────────────────────────────────────────

st.markdown("""
<style>
div[data-testid="stForm"] {
    background-color: #f8f9fb;
    border-radius: 12px;
    padding: 20px;
    border: 1px solid #e0e0e0;
}
@media (max-width: 768px) {
    div[data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
    div[data-testid="stHorizontalBlock"] > div { min-width: 100% !important; flex: 1 1 100% !important; }
    button[kind="primary"] { min-height: 48px !important; font-size: 16px !important; }
    button[data-baseweb="tab"] { font-size: 11px !important; padding: 8px 4px !important; }
    div[data-testid="stForm"] { padding: 12px !important; }
    h1 { font-size: 1.4em !important; }
}
</style>
""", unsafe_allow_html=True)


# ── Ana uygulama ──────────────────────────────────────────────────────────

def main():
    # Supabase kontrolü
    if get_supabase() is None:
        st.error("⚠️ Veritabanı bağlantısı kurulamadı. Lütfen Streamlit Secrets ayarlarını kontrol edin.")
        with st.expander("Kurulum yardımı"):
            st.markdown("""
**secrets.toml dosyanıza şunları ekleyin:**
```toml
SUPABASE_URL = "https://xxxx.supabase.co"
SUPABASE_KEY = "your-service-role-key"
```
""")
        return

    # Veriyi yükle (session cache ile)
    if "_data_cache" not in st.session_state:
        st.session_state["_data_cache"] = load_data()
    data     = st.session_state["_data_cache"]
    payments = data["payments"]
    settings = data["settings"]
    rates    = settings.get("currencies", {})
    today    = date.today()

    # PIN kilidi
    pin_hash = settings.get("pin_hash")
    if pin_hash and not st.session_state.get("authenticated", False):
        show_pin_screen(pin_hash)
        return

    # Toast mesajı
    if "_toast" in st.session_state:
        msg, icon = st.session_state.pop("_toast")
        st.toast(msg, icon=icon)

    st.title("💳 Ödeme Takip")

    overdue = [p for p in payments if not p["paid"] and parse_date(p["due_date"]) < today]
    urgent  = [p for p in payments if not p["paid"] and 0 <= days_until(parse_date(p["due_date"])) <= 3]
    if overdue:
        st.error(f"⚠️ **{len(overdue)} gecikmiş:** {', '.join(p['name'] for p in overdue)}")
    if urgent:
        st.warning(f"⚡ **3 gün içinde:** {', '.join(p['name'] for p in urgent)}")

    pending_try      = sum(to_try(p["amount"],p.get("currency","TRY"),rates) for p in payments if not p["paid"])
    paid_try         = sum(to_try(p["amount"],p.get("currency","TRY"),rates) for p in payments if p["paid"])
    forecast, fc_cnt = next_month_forecast(payments)

    c1,c2,c3,c4,c5 = st.columns(5)
    c1.metric("Bekleyen",  f"{pending_try:,.2f} ₺")
    c2.metric("Ödenen",    f"{paid_try:,.2f} ₺")
    c3.metric("Gecikmiş",  f"{len(overdue)} adet")
    c4.metric("Bu Hafta",  f"{len(urgent)} adet")
    c5.metric("Gelecek Ay",f"{forecast:,.2f} ₺", help=f"{fc_cnt} ödeme")

    budget = settings.get("monthly_budget", 0.0)
    if budget > 0:
        this_month = sum(to_try(p["amount"],p.get("currency","TRY"),rates) for p in payments
                         if parse_date(p["due_date"]).replace(day=1)==today.replace(day=1))
        ratio = min(this_month/budget, 1.0)
        icon  = "🟢" if ratio < 0.75 else ("🟡" if ratio < 1.0 else "🔴")
        st.progress(ratio, text=f"{icon} Bütçe: {this_month:,.2f} ₺ / {budget:,.2f} ₺  (%{ratio*100:.0f})")

    st.divider()

    (tab_list, tab_add, tab_pay, tab_edit,
     tab_delete, tab_cal, tab_reports, tab_settings) = st.tabs([
        "📋 Ödemeler","➕ Yeni Ekle","✅ Ödeme Yap",
        "✏️ Düzenle", "🗑️ Sil",     "📅 Takvim",
        "📊 Raporlar","⚙️ Ayarlar",
    ])

    # ── Ödemeler ───────────────────────────────────────────────────────────
    with tab_list:
        f1,f2 = st.columns(2)
        filtre = f1.radio("Durum",["Tümü","Bekleyen","Ödendi","Gecikmiş"],horizontal=True,key="filtre")
        cat_f  = f2.selectbox("Kategori",["Tümü"]+settings.get("categories",[]),key="cat_filter")

        filtered = payments[:]
        if filtre=="Bekleyen":   filtered=[p for p in filtered if not p["paid"]]
        elif filtre=="Ödendi":   filtered=[p for p in filtered if p["paid"]]
        elif filtre=="Gecikmiş": filtered=[p for p in filtered if not p["paid"] and parse_date(p["due_date"])<today]
        if cat_f!="Tümü":        filtered=[p for p in filtered if p.get("category")==cat_f]
        filtered=sorted(filtered, key=lambda p: parse_date(p["due_date"]))

        if not filtered:
            st.info("Gösterilecek ödeme bulunamadı.")
        else:
            rows=[{"Ödeme Adı":p["name"],"Kategori":p.get("category","-"),
                   "Tutar":fmt_amount(p,rates),"Vade":p["due_date"],
                   "Durum":status_badge(p),"Ödeme Tar.":p.get("paid_date") or "-",
                   "Tekrar":PERIOD_LABELS.get(p.get("recurring_period"),"-") if p.get("recurring") else "-",
                   "Not":p.get("note") or "-"} for p in filtered]
            st.dataframe(rows,use_container_width=True,hide_index=True)

    # ── Yeni Ekle ──────────────────────────────────────────────────────────
    with tab_add:
        with st.form("add_form",clear_on_submit=True):
            st.subheader("Yeni Ödeme Ekle")
            col1,col2,col3=st.columns(3)
            name      = col1.text_input("Ödeme Adı",placeholder="Kira, Elektrik…")
            amount    = col2.number_input("Tutar",min_value=0.01,step=0.01,format="%.2f")
            currency  = col3.selectbox("Para Birimi",CURRENCIES)
            due       = col1.date_input("Vade Tarihi",value=today)
            category  = col2.selectbox("Kategori",settings.get("categories",["Diğer"]))
            note      = col3.text_input("Not (opsiyonel)")
            recurring = st.checkbox("Tekrarlı Ödeme")
            recurring_period=None
            if recurring:
                recurring_period=st.selectbox("Tekrar Periyodu",list(PERIOD_LABELS.keys()),
                                              format_func=lambda k:PERIOD_LABELS[k])
            submitted=st.form_submit_button("Ekle",type="primary")

        if submitted:
            if not name.strip():
                st.error("Ödeme adı boş olamaz.")
            else:
                data["payments"].append({
                    "id":str(uuid.uuid4())[:8],"name":name.strip(),"amount":float(amount),
                    "currency":currency,"category":category,
                    "due_date":due.strftime("%d.%m.%Y"),"note":note.strip(),
                    "paid":False,"paid_date":None,"recurring":recurring,"recurring_period":recurring_period,
                })
                save_data(data); notify(f"'{name}' eklendi!"); st.rerun()

    # ── Ödeme Yap (toplu) ──────────────────────────────────────────────────
    with tab_pay:
        pending=sorted([p for p in payments if not p["paid"]],key=lambda p:parse_date(p["due_date"]))
        if not pending:
            st.info("Bekleyen ödeme yok.")
        else:
            opt_map={p["id"]:f"{p['name']} — {fmt_amount(p,rates)} — {p['due_date']} ({status_badge(p)})"
                     for p in pending}
            selected_ids=st.multiselect("Ödenecek ödemeleri seçin",list(opt_map.keys()),
                                        format_func=lambda k:opt_map[k])
            paid_date_sel=st.date_input("Ödeme Tarihi",value=today,key="paid_date_input")
            if st.button("Seçilenleri Ödendi İşaretle",type="primary",disabled=not selected_ids):
                added=[]
                for p in data["payments"]:
                    if p["id"] in selected_ids:
                        p["paid"]=True; p["paid_date"]=paid_date_sel.strftime("%d.%m.%Y")
                        if p.get("recurring") and p.get("recurring_period"):
                            nd=next_due(parse_date(p["due_date"]),p["recurring_period"])
                            added.append({**p,"id":str(uuid.uuid4())[:8],
                                          "due_date":nd.strftime("%d.%m.%Y"),"paid":False,"paid_date":None})
                data["payments"].extend(added)
                save_data(data); notify(f"{len(selected_ids)} ödeme işaretlendi!"); st.rerun()

    # ── Düzenle ────────────────────────────────────────────────────────────
    with tab_edit:
        if not payments:
            st.info("Düzenlenecek ödeme yok.")
        else:
            sorted_p=sorted(payments,key=lambda p:parse_date(p["due_date"]))
            opt_e={p["id"]:f"{p['name']} — {fmt_amount(p,rates)} — {p['due_date']}" for p in sorted_p}
            edit_id=st.selectbox("Düzenlenecek ödeme",list(opt_e.keys()),format_func=lambda k:opt_e[k])
            target=next(p for p in payments if p["id"]==edit_id)
            cats=settings.get("categories",["Diğer"])
            cat_idx=cats.index(target.get("category","Diğer")) if target.get("category") in cats else 0
            cur_idx=CURRENCIES.index(target.get("currency","TRY")) if target.get("currency") in CURRENCIES else 0

            with st.form("edit_form"):
                col1,col2,col3=st.columns(3)
                new_name    =col1.text_input("Ad",value=target["name"])
                new_amount  =col2.number_input("Tutar",value=float(target["amount"]),min_value=0.01,step=0.01,format="%.2f")
                new_currency=col3.selectbox("Para Birimi",CURRENCIES,index=cur_idx)
                new_due     =col1.date_input("Vade",value=parse_date(target["due_date"]))
                new_cat     =col2.selectbox("Kategori",cats,index=cat_idx)
                new_note    =col3.text_input("Not",value=target.get("note") or "")
                save_btn    =st.form_submit_button("Kaydet",type="primary")

            if save_btn:
                for p in data["payments"]:
                    if p["id"]==edit_id:
                        p["name"]=new_name.strip(); p["amount"]=float(new_amount)
                        p["currency"]=new_currency; p["due_date"]=new_due.strftime("%d.%m.%Y")
                        p["category"]=new_cat; p["note"]=new_note.strip(); break
                save_data(data); notify("Ödeme güncellendi!"); st.rerun()

    # ── Sil ────────────────────────────────────────────────────────────────
    with tab_delete:
        if not payments:
            st.info("Silinecek ödeme yok.")
        else:
            sorted_p=sorted(payments,key=lambda p:parse_date(p["due_date"]))
            opt_d={p["id"]:f"{p['name']} — {fmt_amount(p,rates)} — {p['due_date']} ({'Ödendi' if p['paid'] else 'Bekliyor'})"
                   for p in sorted_p}
            del_id=st.selectbox("Silinecek ödeme",list(opt_d.keys()),format_func=lambda k:opt_d[k])
            target=next(p for p in payments if p["id"]==del_id)
            st.warning(f"**{target['name']}** silinecek. Emin misiniz?")
            if st.button("🗑️ Sil",type="primary"):
                data["payments"]=[p for p in payments if p["id"]!=del_id]
                save_data(data); notify("Ödeme silindi!",icon="🗑️"); st.rerun()

    # ── Takvim ─────────────────────────────────────────────────────────────
    with tab_cal:
        if "cal_year"  not in st.session_state: st.session_state.cal_year  = today.year
        if "cal_month" not in st.session_state: st.session_state.cal_month = today.month
        nav1,nav2,nav3=st.columns([1,3,1])
        if nav1.button("◀ Önceki"):
            if st.session_state.cal_month==1: st.session_state.cal_month=12; st.session_state.cal_year-=1
            else: st.session_state.cal_month-=1
            st.rerun()
        nav2.markdown(f"<h3 style='text-align:center'>{MONTH_TR[st.session_state.cal_month]} {st.session_state.cal_year}</h3>",
                      unsafe_allow_html=True)
        if nav3.button("Sonraki ▶"):
            if st.session_state.cal_month==12: st.session_state.cal_month=1; st.session_state.cal_year+=1
            else: st.session_state.cal_month+=1
            st.rerun()
        st.markdown(render_calendar_html(st.session_state.cal_year,st.session_state.cal_month,payments),
                    unsafe_allow_html=True)
        cal_pays=[p for p in payments
                  if parse_date(p["due_date"]).year==st.session_state.cal_year
                  and parse_date(p["due_date"]).month==st.session_state.cal_month]
        if cal_pays:
            st.divider()
            rows=[{"Ödeme Adı":p["name"],"Kategori":p.get("category","-"),
                   "Tutar":fmt_amount(p,rates),"Vade":p["due_date"],"Durum":status_badge(p)}
                  for p in sorted(cal_pays,key=lambda x:parse_date(x["due_date"]))]
            st.dataframe(rows,use_container_width=True,hide_index=True)

    # ── Raporlar ───────────────────────────────────────────────────────────
    with tab_reports:
        st.subheader("📊 Dönemsel Rapor")
        preset=st.radio("Dönem",["Bu Ay","Geçen Ay","Son 3 Ay","Son 6 Ay","Özel Aralık"],
                        horizontal=True,key="rep_preset")
        if preset=="Bu Ay":
            start_d=today.replace(day=1); end_d=today.replace(day=calendar.monthrange(today.year,today.month)[1])
            period_label=f"{MONTH_TR[today.month]} {today.year}"
        elif preset=="Geçen Ay":
            first=today.replace(day=1); lm=first-timedelta(days=1)
            start_d=lm.replace(day=1); end_d=lm; period_label=f"{MONTH_TR[lm.month]} {lm.year}"
        elif preset=="Son 3 Ay":
            start_d=add_months(today,-3); end_d=today; period_label="Son 3 Ay"
        elif preset=="Son 6 Ay":
            start_d=add_months(today,-6); end_d=today; period_label="Son 6 Ay"
        else:
            c1,c2=st.columns(2)
            start_d=c1.date_input("Başlangıç",value=today.replace(day=1),key="rep_s")
            end_d=c2.date_input("Bitiş",value=today,key="rep_e")
            period_label=f"{start_d.strftime('%d.%m.%Y')} – {end_d.strftime('%d.%m.%Y')}"

        period_payments=[p for p in payments if start_d<=parse_date(p["due_date"])<=end_d]
        if not period_payments:
            st.info(f"**{period_label}** döneminde ödeme bulunamadı.")
        else:
            paid_p=[p for p in period_payments if p["paid"]]
            pend_p=[p for p in period_payments if not p["paid"]]
            paid_a=sum(to_try(p["amount"],p.get("currency","TRY"),rates) for p in paid_p)
            pend_a=sum(to_try(p["amount"],p.get("currency","TRY"),rates) for p in pend_p)
            c1,c2,c3,c4=st.columns(4)
            c1.metric("Toplam",f"{len(period_payments)} adet")
            c2.metric("Ödenen",f"{paid_a:,.2f} ₺")
            c3.metric("Bekleyen",f"{pend_a:,.2f} ₺")
            c4.metric("Ödeme Oranı",f"{len(paid_p)/len(period_payments)*100:.0f}%")
            st.divider()

            import pandas as pd
            monthly_data={}
            for p in period_payments:
                d=parse_date(p["due_date"]); k=(d.year,d.month)
                amt=to_try(p["amount"],p.get("currency","TRY"),rates)
                if k not in monthly_data:
                    monthly_data[k]={"lbl":f"{MONTH_TR[d.month]} {d.year}","Ödenen (₺)":0.0,"Bekleyen (₺)":0.0}
                if p["paid"]: monthly_data[k]["Ödenen (₺)"]+=amt
                else:         monthly_data[k]["Bekleyen (₺)"]+=amt
            skeys=sorted(monthly_data)
            chart_df=pd.DataFrame({"Ödenen (₺)":[monthly_data[k]["Ödenen (₺)"] for k in skeys],
                                    "Bekleyen (₺)":[monthly_data[k]["Bekleyen (₺)"] for k in skeys]},
                                   index=[monthly_data[k]["lbl"] for k in skeys])
            st.markdown("**Aylık Dağılım**"); st.bar_chart(chart_df,height=260)

            cat_data=collections.defaultdict(float)
            for p in period_payments:
                cat_data[p.get("category","Diğer")]+=to_try(p["amount"],p.get("currency","TRY"),rates)
            if len(cat_data)>1:
                cat_df=pd.DataFrame({"Tutar (₺)":cat_data})
                st.markdown("**Kategori Dağılımı**"); st.bar_chart(cat_df,height=220)

            st.divider()
            rows=[{"Ödeme Adı":p["name"],"Kategori":p.get("category","-"),
                   "Tutar":fmt_amount(p,rates),"Vade":p["due_date"],
                   "Durum":status_badge(p),"Ödeme Tar.":p.get("paid_date") or "-",
                   "Tekrar":PERIOD_LABELS.get(p.get("recurring_period"),"-") if p.get("recurring") else "-"}
                  for p in sorted(period_payments,key=lambda x:parse_date(x["due_date"]))]
            st.dataframe(rows,use_container_width=True,hide_index=True)
            st.divider()
            e1,e2,_=st.columns([1,1,4])
            safe=period_label.replace(" ","_").replace("–","-")
            e1.download_button("📄 PDF",data=generate_pdf(period_payments,period_label,start_d,end_d,rates),
                               file_name=f"odeme_{safe}.pdf",mime="application/pdf")
            e2.download_button("📊 CSV",data=generate_csv(period_payments,rates),
                               file_name=f"odeme_{safe}.csv",mime="text/csv")

    # ── Ayarlar ────────────────────────────────────────────────────────────
    with tab_settings:
        st.subheader("⚙️ Ayarlar")

        with st.expander("💰 Aylık Bütçe Hedefi",expanded=True):
            new_budget=st.number_input("Aylık bütçe (₺)",value=float(settings.get("monthly_budget",0.0)),
                                       min_value=0.0,step=100.0,format="%.2f")
            if st.button("Bütçeyi Kaydet"):
                settings["monthly_budget"]=new_budget; data["settings"]=settings
                save_data(data); notify("Bütçe güncellendi!"); st.rerun()

        with st.expander("🏷️ Kategoriler"):
            cats=settings.get("categories",[])
            for i,cat in enumerate(cats[:]):
                c1,c2=st.columns([5,1]); c1.markdown(f"• {cat}")
                if c2.button("Sil",key=f"del_cat_{i}"):
                    cats.remove(cat); settings["categories"]=cats; data["settings"]=settings
                    save_data(data); st.rerun()
            new_cat=st.text_input("Yeni kategori",key="new_cat_input")
            if st.button("➕ Ekle") and new_cat.strip():
                if new_cat.strip() not in cats:
                    cats.append(new_cat.strip()); settings["categories"]=cats; data["settings"]=settings
                    save_data(data); notify(f"'{new_cat}' eklendi!"); st.rerun()

        with st.expander("💱 Döviz Kurları"):
            cur_rates=settings.get("currencies",{"USD":38.0,"EUR":42.0,"GBP":50.0})
            new_rates={}; cols=st.columns(len(cur_rates))
            for i,(cur,rate) in enumerate(cur_rates.items()):
                new_rates[cur]=cols[i].number_input(f"1 {cur} = ? ₺",value=float(rate),
                                                     min_value=0.01,step=0.5,format="%.2f",key=f"rate_{cur}")
            if st.button("Kurları Kaydet"):
                settings["currencies"]=new_rates; data["settings"]=settings
                save_data(data); notify("Kurlar güncellendi!"); st.rerun()

        with st.expander("🔒 PIN Kilidi"):
            pin_hash=settings.get("pin_hash")
            if pin_hash:
                st.info("PIN kilidi **aktif**.")
                with st.form("change_pin_form"):
                    old_pin=st.text_input("Mevcut PIN",type="password",max_chars=8)
                    new_pin=st.text_input("Yeni PIN",type="password",max_chars=8)
                    ch_btn,rm_btn=st.columns(2)
                    change=ch_btn.form_submit_button("Değiştir",type="primary")
                    remove=rm_btn.form_submit_button("Kaldır")
                if change:
                    if not check_pin(old_pin,pin_hash): st.error("Mevcut PIN yanlış.")
                    elif len(new_pin)<4:                st.error("En az 4 karakter.")
                    else:
                        settings["pin_hash"]=hash_pin(new_pin); data["settings"]=settings
                        save_data(data); notify("PIN değiştirildi!",icon="🔒"); st.rerun()
                if remove:
                    if not check_pin(old_pin,pin_hash): st.error("Mevcut PIN yanlış.")
                    else:
                        settings["pin_hash"]=None; data["settings"]=settings
                        st.session_state.authenticated=False
                        save_data(data); notify("PIN kaldırıldı!",icon="🔓"); st.rerun()
            else:
                st.info("PIN kilidi **aktif değil**.")
                with st.form("set_pin_form"):
                    new_pin=st.text_input("PIN Belirle (4-8 rakam)",type="password",max_chars=8)
                    new_pin2=st.text_input("PIN Tekrar",type="password",max_chars=8)
                    set_btn=st.form_submit_button("PIN Ayarla",type="primary")
                if set_btn:
                    if len(new_pin)<4:      st.error("En az 4 karakter.")
                    elif new_pin!=new_pin2: st.error("PIN'ler eşleşmiyor.")
                    else:
                        settings["pin_hash"]=hash_pin(new_pin); data["settings"]=settings
                        st.session_state.authenticated=True
                        save_data(data); notify("PIN ayarlandı!",icon="🔒"); st.rerun()

        with st.expander("📱 Telefona Ekle (iPhone)"):
            st.markdown("""
Uygulamayı iPhone ana ekranınıza ikon olarak ekleyebilirsiniz:

1. **Safari**'de uygulamayı açın
2. Alt menüden **Paylaş** 􀈂 düğmesine dokunun
3. **"Ana Ekrana Ekle"** seçeneğini seçin
4. İsim verin → **Ekle**'ye dokunun

Artık diğer uygulamalar gibi açılır! 📱
""")
            st.info("💡 Android için Chrome'da → Sağ üst menü → 'Ana ekrana ekle'")


main()
