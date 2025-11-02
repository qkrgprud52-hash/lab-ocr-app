import streamlit as st
import requests, base64, re, json, math
from urllib.parse import quote
import pandas as pd

# =========================
# 기본 UI 설정
# =========================
st.set_page_config(page_title="연구실 시약 OCR / 재고 관리", page_icon="🧪", layout="wide")
st.markdown("""
<style>
.stButton>button {background:#16a34a;color:white;border:none;border-radius:10px;padding:0.6rem 1rem;font-weight:600;}
.stButton>button:hover {background:#15803d;}
.block-container {padding-top:1.1rem; padding-bottom:2rem;}
</style>
""", unsafe_allow_html=True)
st.title("🧪 연구실 시약 OCR / 재고 관리")

# =========================
# Secrets (Streamlit → Secrets)
# =========================
AIRTABLE_TOKEN        = st.secrets.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID      = st.secrets.get("AIRTABLE_BASE_ID", "")

# 기록 테이블(트랜잭션)
AIRTABLE_TABLE_ID     = st.secrets.get("AIRTABLE_TABLE_ID", "")                 # tbl... 형태 권장
AIRTABLE_TABLE_NAME   = st.secrets.get("AIRTABLE_TABLE_NAME", "Lab OCR Results")

# 마스터 테이블(Materials)
MATERIALS_TABLE_ID    = st.secrets.get("MATERIALS_TABLE_ID", "")
MATERIALS_TABLE_NAME  = st.secrets.get("MATERIALS_TABLE_NAME", "Materials")

IMGBB_KEY             = st.secrets.get("IMGBB_KEY", "")
DEFAULT_GCP_KEY       = st.secrets.get("GCP_KEY", "")

# =========================
# 유틸
# =========================
CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
def extract_cas(text: str) -> str:
    m = CAS_RE.search(text or "")
    return m.group(0) if m else ""

def table_ref(table_id, table_name):
    return table_id or quote(table_name, safe="")

def at_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}

def at_get_all(base_id, table_id_or_name):
    """Airtable 전 레코드 조회 (페이지네이션 처리)"""
    out = []
    url = f"https://api.airtable.com/v0/{base_id}/{table_id_or_name}"
    params = {"pageSize": 100}
    while True:
        r = requests.get(url, headers=at_headers(), params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        out.extend(data.get("records", []))
        off = data.get("offset")
        if not off:
            break
        params["offset"] = off
    return out

def at_find_one(base_id, table_id_or_name, formula: str):
    """filterByFormula로 단건 조회"""
    url = f"https://api.airtable.com/v0/{base_id}/{table_id_or_name}"
    r = requests.get(url, headers=at_headers(),
                     params={"maxRecords": 1, "filterByFormula": formula},
                     timeout=20)
    r.raise_for_status()
    js = r.json()
    return js.get("records", [None])[0]

def ensure_material_record(cas_no: str, name_guess: str = ""):
    """
    Materials에 CAS가 없으면 자동 생성 (name만 대충 채워두고, 지정수량/단위는 비워둠)
    """
    if not cas_no:
        return
    mref = table_ref(MATERIALS_TABLE_ID, MATERIALS_TABLE_NAME)
    try:
        rec = at_find_one(AIRTABLE_BASE_ID, mref, formula=f"{{CAS}} = '{cas_no}'")
        if rec:
            return
        payload = {"fields": {"CAS": cas_no}}
        if name_guess:
            payload["fields"]["name"] = name_guess[:100]
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{mref}"
        requests.post(url, json=payload, headers=at_headers(), timeout=20)
    except:
        # 조용히 패스 (자동 보조 기능이라 필수는 아님)
        pass

def run_ocr(image_bytes: bytes, gcp_key: str) -> dict:
    url = f"https://vision.googleapis.com/v1/images:annotate?key={gcp_key}"
    payload = {"requests": [{
        "image": {"content": base64.b64encode(image_bytes).decode("utf-8")},
        "features": [{"type": "TEXT_DETECTION"}]
    }]}
    return requests.post(url, json=payload, timeout=40).json()

def upload_to_imgbb(image_bytes, filename: str) -> str | None:
    if not IMGBB_KEY:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        r = requests.post("https://api.imgbb.com/1/upload",
                          data={"key": IMGBB_KEY, "image": b64, "name": filename},
                          timeout=25)
        r.raise_for_status()
        return r.json()["data"]["url"]
    except:
        return None

def save_to_airtable(fields: dict):
    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        return False, "Airtable secrets 미설정"
    tref = table_ref(AIRTABLE_TABLE_ID, AIRTABLE_TABLE_NAME)
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{tref}"
    r = requests.post(url, json={"fields": fields}, headers=at_headers(), timeout=30)
    ok = r.status_code in (200, 201)
    return ok, (r.text if not ok else "OK")

# =========================
# 탭
# =========================
tab1, tab2 = st.tabs(["📷 기록 (OCR/저장)", "📊 재고/지정수량"])

# =========================
# TAB1: 기록 (OCR/저장)
# =========================
with tab1:
    if "last" not in st.session_state:
        st.session_state.last = {"dept":"","lab":"","bld":"","room":"","io":"입고","unit":"g"}

    uploaded_file = st.file_uploader("라벨 정면 사진 업로드", type=["jpg","jpeg","png"])
    gcp_key = st.text_input("🔑 Google Vision API Key (Secrets에 있으면 비워도 됨)",
                            value=DEFAULT_GCP_KEY, type="password")

    st.markdown("### 📋 메타 정보")
    colA,colB,colC = st.columns(3)
    colD,colE = st.columns(2)

    dept = colA.selectbox("학과",
        ["화학공학과","안전공학과","신소재공학과","기계시스템디자인공학과","기타(직접 입력)"],
        index=0)
    lab = colB.text_input("실험실명", value=st.session_state.last["lab"])
    bld = colC.selectbox("건물", ["청운관","제1공학관","제2공학관","어울림관","기타(직접 입력)"], index=0)
    room = colD.text_input("호수 (예: 203)", value=st.session_state.last["room"])
    io_type = colE.selectbox("입·출고 구분", ["입고","출고","반품","폐기"], index=0)

    if dept.endswith("직접 입력"):
        dept = colA.text_input("학과(직접 입력)", value=st.session_state.last["dept"])
    if bld.endswith("직접 입력"):
        bld = colC.text_input("건물(직접 입력)", value=st.session_state.last["bld"])

    st.markdown("### 📦 수량")
    colQ1, colQ2 = st.columns([1,1])
    qty = colQ1.number_input("수량", min_value=0.0, step=1.0, format="%.3f")
    unit = colQ2.selectbox("단위", ["g","mL","L","kg","EA","cyl"],
                           index=["g","mL","L","kg","EA","cyl"].index(st.session_state.last["unit"]))

    st.divider()

    if uploaded_file and gcp_key:
        with st.spinner("🔎 OCR 분석 중…"):
            img_bytes = uploaded_file.getvalue()
            ocr_json = run_ocr(img_bytes, gcp_key)

        text = ""
        try:
            text = ocr_json["responses"][0]["fullTextAnnotation"]["text"]
            st.success("✅ OCR 인식 성공")
            st.text_area("추출 텍스트", text, height=220)
        except Exception:
            st.error("⚠️ 텍스트 인식 실패 (원본 응답 아래)")
            st.json(ocr_json)

        cas_no = extract_cas(text) if text else ""
        st.code(f"🔎 CAS: {cas_no or '(없음)'}")

        ready = bool(text and dept and lab and bld and room and io_type and (qty>=0))
        if not ready:
            st.info("ℹ OCR/메타/수량을 채우면 저장할 수 있어요.")

        if st.button("💾 Airtable에 저장", disabled=not ready):
            sign = +1 if io_type=="입고" else -1  # 출고/반품/폐기 → 음수
            img_url = upload_to_imgbb(img_bytes, uploaded_file.name)
            fields = {
                "Name": uploaded_file.name,
                "ocr_text": text,
                "CAS": cas_no,
                "dept": dept,
                "lab": lab,
                "building": bld,
                "room": room,
                "io_type": io_type,
                "qty": sign * qty,
                "unit": unit
            }
            if img_url:
                fields["Attachments"] = [{"url": img_url, "filename": uploaded_file.name}]

            ok, msg = save_to_airtable(fields)
            if ok:
                # ✅ Materials 자동 생성 (없을 때만)
                ensure_material_record(cas_no, name_guess=text.splitlines()[0] if text else "")

                st.success("✅ 저장 완료!")
                st.session_state.last = {"dept":dept,"lab":lab,"bld":bld,"room":room,"io":io_type,"unit":unit}
            else:
                if "INVALID_MULTIPLE_CHOICE_OPTIONS" in msg:
                    st.error("❌ 드롭다운 옵션에 없는 값입니다. Airtable에서 옵션을 추가하세요.")
                else:
                    st.error(f"❌ 저장 실패: {msg}")
    else:
        st.caption("이미지와 Vision API Key를 입력하면 OCR을 시작합니다.")

# =========================
# TAB2: 재고/지정수량
# =========================
with tab2:
    st.info("이 탭은 `Lab OCR Results`의 수량(qty)을 합산하고, `Materials`의 지정수량과 비교해 비율을 계산합니다.")

    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        st.error("Airtable secrets가 필요합니다."); st.stop()

    tx_ref  = table_ref(AIRTABLE_TABLE_ID, AIRTABLE_TABLE_NAME)
    mat_ref = table_ref(MATERIALS_TABLE_ID, MATERIALS_TABLE_NAME)

    try:
        with st.spinner("🔄 데이터 불러오는 중…"):
            tx = at_get_all(AIRTABLE_BASE_ID, tx_ref)
            mats = at_get_all(AIRTABLE_BASE_ID, mat_ref)
    except Exception as e:
        st.error(f"불러오기 실패: {e}")
        st.stop()

    # 트랜잭션 합계(CAS+단위별)
    sums = {}
    for r in tx:
        f = r.get("fields",{})
        cas = (f.get("CAS") or "").strip()
        q   = f.get("qty")
        u   = f.get("unit")
        if not cas or q is None:
            continue
        key = (cas, u or "")
        sums[key] = sums.get(key, 0.0) + float(q)

    # 마스터(지정수량)
    master = {}
    for r in mats:
        f = r.get("fields",{})
        cas = (f.get("CAS") or "").strip()
        if not cas:
            continue
        master[cas] = {
            "name": f.get("name",""),
            "designated_qty": f.get("designated_qty"),
            "unit": f.get("unit","")
        }

    # 표 구성
    rows = []
    for (cas, unit), qty_sum in sums.items():
        m = master.get(cas, {})
        dqty  = m.get("designated_qty")
        dunit = m.get("unit")
        ratio = None
        note  = ""
        if dqty and dunit and unit and dunit==unit:
            ratio = (qty_sum / float(dqty)) if float(dqty)>0 else None
        else:
            note = "마스터 지정수량/단위 불일치 또는 누락"

        rows.append({
            "CAS": cas,
            "물질명": m.get("name",""),
            "재고합계": round(qty_sum,3),
            "단위": unit,
            "지정수량": dqty,
            "지정단위": dunit,
            "비율": (round(ratio,3) if ratio is not None else None),
            "메모": note
        })

    # 경고 높은 순 정렬
    def ratio_key(r):
        return -(r["비율"] if r["비율"] is not None else -1)
    rows.sort(key=ratio_key)

    st.markdown("#### 📈 CAS별 재고 / 지정수량 비율")
    if not rows:
        st.caption("표시할 데이터가 없습니다. 기록 탭에서 먼저 저장해 주세요.")
    else:
        # 행 색상 하이라이트
        def color_row(r):
            ratio = r["비율"]
            if ratio is None: return ""
            if ratio >= 1.0: return "background-color:#fecaca"  # 빨강
            if ratio >= 0.5: return "background-color:#fde68a"  # 노랑
            if ratio >= 0.2: return "background-color:#dcfce7"  # 연초록
            return ""

        df = pd.DataFrame(rows)
        st.dataframe(df.style.apply(lambda s: [color_row(r) for r in df.to_dict("records")], axis=0),
                     use_container_width=True)

        # ✅ CSV 다운로드
        st.download_button(
            "📥 CSV로 내려받기",
            df.to_csv(index=False).encode("utf-8-sig"),
            file_name="inventory_vs_designated.csv",
            mime="text/csv"
        )

        over = [r for r in rows if (r["비율"] is not None and r["비율"]>=1.0)]
        warn = [r for r in rows if (r["비율"] is not None and 0.5<=r["비율"]<1.0)]
        low  = [r for r in rows if (r["비율"] is not None and 0.2<=r["비율"]<0.5)]

        st.markdown("#### 🚨 요약")
        st.write(f"- 1.0 이상(초과) : **{len(over)}**건")
        st.write(f"- 0.5 이상      : **{len(warn)}**건")
        st.write(f"- 0.2 이상      : **{len(low)}**건")
