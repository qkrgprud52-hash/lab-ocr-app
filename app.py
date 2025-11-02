import streamlit as st
import requests, base64, re
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
    Materials에 CAS가 없으면 자동 생성 (name만 대충 채워두고, 지정수량/단위/유별/밀도는 비워둠)
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
# 제4류 지정수량(고정값)
# =========================
LEGAL_LIMITS_L = {
    "특수인화물": 100.0,
    "제1석유류(비수용성)": 600.0,
    "제1석유류(수용성)": 1700.0,
    "알코올류": 4100.0,
}

# 내장 간이 밀도 (g/mL) & 유별 매핑 (없으면 Materials 값을 사용)
BUILTIN_CHEM = {
    # CAS        name_hint,         hazard_class,         density_g_per_ml
    "64-17-5":   ("Ethanol",        "알코올류",           0.789),
    "67-63-0":   ("Isopropanol",    "알코올류",           0.786),
    "67-56-1":   ("Methanol",       "알코올류",           0.792),
    "67-64-1":   ("Acetone",        "제1석유류(수용성)",  0.791),
    "75-05-8":   ("Acetonitrile",   "제1석유류(수용성)",  0.786),
    "108-88-3":  ("Toluene",        "제1석유류(비수용성)",0.867),
    "110-54-3":  ("n-Hexane",       "제1석유류(비수용성)",0.655),
    "60-29-7":   ("Diethyl ether",  "특수인화물",         0.713),
}

def load_materials_index():
    """Materials를 CAS 키로 묶어 name, designated_qty, unit, hazard_class, density 제공"""
    mref = table_ref(MATERIALS_TABLE_ID, MATERIALS_TABLE_NAME)
    mats = at_get_all(AIRTABLE_BASE_ID, mref)
    out = {}
    for r in mats:
        f = r.get("fields",{})
        cas = (f.get("CAS") or "").strip()
        if not cas:
            continue
        out[cas] = {
            "name": f.get("name",""),
            "designated_qty": f.get("designated_qty"),
            "unit": (f.get("Unit") or f.get("unit") or ""),      # 대소문자 대응
            "hazard_class": f.get("hazard_class",""),
            "density_g_per_ml": f.get("density_g_per_ml"),
        }
    return out

def classify_hazard(cas: str, mats_idx: dict) -> str | None:
    """Materials.hazard_class 우선, 없으면 내장 매핑 사용"""
    if cas in mats_idx and mats_idx[cas].get("hazard_class"):
        return mats_idx[cas]["hazard_class"]
    if cas in BUILTIN_CHEM and BUILTIN_CHEM[cas][1]:
        return BUILTIN_CHEM[cas][1]
    return None

def get_density(cas: str, mats_idx: dict) -> float | None:
    """Materials.density_g_per_ml 우선, 없으면 내장 매핑"""
    if cas in mats_idx and mats_idx[cas].get("density_g_per_ml"):
        try:
            return float(mats_idx[cas]["density_g_per_ml"])
        except:
            pass
    if cas in BUILTIN_CHEM and BUILTIN_CHEM[cas][2]:
        return BUILTIN_CHEM[cas][2]
    return None

def to_liters(amount, unit: str, density_g_per_ml: float | None) -> float | None:
    """단위를 L로 변환. g/kg은 밀도 필요, mL는 1000으로 나눔, L은 그대로."""
    if amount is None or unit is None:
        return None
    unit = unit.strip()
    try:
        val = float(amount)
    except:
        return None

    if unit == "L":
        return val
    if unit == "mL":
        return val / 1000.0
    if unit == "g":
        if density_g_per_ml and density_g_per_ml > 0:
            return (val / density_g_per_ml) / 1000.0
        return None
    if unit == "kg":
        if density_g_per_ml and density_g_per_ml > 0:
            g = val * 1000.0
            return (g / density_g_per_ml) / 1000.0
        return None
    # EA, cyl 등은 부피 환산 불가 → None
    return None

# 포맷터: 정수/퍼센트 문자열
def fmt_int(x) -> str:
    try:
        return f"{int(round(float(x)))}"
    except:
        return ""

def fmt_pct(ratio) -> str:
    if ratio is None:
        return ""
    try:
        return f"{int(round(float(ratio)*100))}%"
    except:
        return ""

# =========================
# 탭
# =========================
tab1, tab2, tab3 = st.tabs(["📷 기록 (OCR/저장)", "📊 재고/지정수량", "🏷️ 위험물(제4류) 현황"])

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
    qty = colQ1.number_input("수량", min_value=0.0, step=1.0, format="%.0f")  # 정수 입력 표시
    unit = colQ2.selectbox("단위", ["g","mL","L","kg","EA","cyl"],
                           index=["g","mL","L","kg","EA","cyl"].index(st.session_state.last["unit"]))

    st.divider()

    if uploaded_file and gcp_key:
        with st.spinner("🔎 OCR 분석 중…"):
            img_bytes = uploaded_file.getvalue()
            url = f"https://vision.googleapis.com/v1/images:annotate?key={gcp_key}"
            payload = {"requests": [{
                "image": {"content": base64.b64encode(img_bytes).decode("utf-8")},
                "features": [{"type": "TEXT_DETECTION"}]
            }]}
            ocr_json = requests.post(url, json=payload, timeout=40).json()

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
# TAB2: 재고/지정수량 (CAS별) — 정수/퍼센트 표기
# =========================
with tab2:
    st.info("이 탭은 `Lab OCR Results`의 수량(qty)을 합산하고, `Materials`의 지정수량과 비교해 비율을 계산합니다. (정수/%)")

    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        st.error("Airtable secrets가 필요합니다."); st.stop()

    tx_ref  = table_ref(AIRTABLE_TABLE_ID, AIRTABLE_TABLE_NAME)
    try:
        with st.spinner("🔄 데이터 불러오는 중…"):
            tx = at_get_all(AIRTABLE_BASE_ID, tx_ref)
            mats_idx = load_materials_index()
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

    # 표 구성 (정수/퍼센트 표기)
    disp_rows = []
    csv_rows  = []
    for (cas, unit), qty_sum in sums.items():
        m = mats_idx.get(cas, {})
        dqty  = m.get("designated_qty")
        dunit = m.get("unit")
        ratio = None
        note  = ""
        if dqty and dunit and unit and dunit==unit:
            ratio = (qty_sum / float(dqty)) if float(dqty)>0 else None
        else:
            note = "마스터 지정수량/단위 불일치 또는 누락"

        # 표시는 정수/퍼센트
        disp_rows.append({
            "CAS": cas,
            "물질명": m.get("name",""),
            "재고합계": fmt_int(qty_sum),
            "단위": unit,
            "지정수량": fmt_int(dqty) if dqty is not None else "",
            "지정단위": dunit or "",
            "비율": fmt_pct(ratio),
            "메모": note
        })
        # CSV도 같은 형식으로 저장
        csv_rows.append(disp_rows[-1].copy())

    # 정렬: 비율 높은 순 (문자열이므로 정렬키 별도로)
    def ratio_val(pct_str):
        if not pct_str: return -1
        try:
            return int(pct_str.replace("%",""))
        except:
            return -1
    disp_rows.sort(key=lambda r: -ratio_val(r["비율"]))

    st.markdown("#### 📈 CAS별 재고 / 지정수량 비율")
    if not disp_rows:
        st.caption("표시할 데이터가 없습니다. 기록 탭에서 먼저 저장해 주세요.")
    else:
        df = pd.DataFrame(disp_rows)
        st.dataframe(df, use_container_width=True)
        st.download_button("📥 CSV로 내려받기",
                           pd.DataFrame(csv_rows).to_csv(index=False).encode("utf-8-sig"),
                           file_name="inventory_vs_designated.csv", mime="text/csv")

# =========================
# TAB3: 위험물(제4류) 현황 — 창고 전체 모니터링 (정수/퍼센트 + 잔여허용량)
# =========================
with tab3:
    st.info("제4류 위험물 기준으로, 창고 전체 저장량(L)을 유별별로 합산해 지정수량과 비교합니다. (정수/%, 잔여허용량 포함)")

    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        st.error("Airtable secrets가 필요합니다."); st.stop()

    tx_ref = table_ref(AIRTABLE_TABLE_ID, AIRTABLE_TABLE_NAME)

    try:
        with st.spinner("🔄 데이터 불러오는 중…"):
            tx = at_get_all(AIRTABLE_BASE_ID, tx_ref)
            mats_idx = load_materials_index()
    except Exception as e:
        st.error(f"불러오기 실패: {e}")
        st.stop()

    # CAS별 부피(L) 합계 + 유별 분류
    by_class = {}  # {haz_class: liters(float)}
    unknown  = 0.0
    skipped  = []  # 환산 불가 목록

    for r in tx:
        f = r.get("fields",{})
        cas = (f.get("CAS") or "").strip()
        qty = f.get("qty")
        unit = f.get("unit")
        if not cas or qty is None or not unit:
            continue

        dens = get_density(cas, mats_idx)  # 우선 Materials, 없으면 내장
        Lval = to_liters(qty, unit, dens)
        if Lval is None:
            skipped.append({"CAS": cas, "qty": qty, "unit": unit, "reason": "밀도없음/환산불가"})
            continue

        hclass = classify_hazard(cas, mats_idx)  # 우선 Materials, 없으면 내장
        if not hclass:
            unknown += Lval
            continue

        by_class[hclass] = by_class.get(hclass, 0.0) + Lval

    # 결과 테이블 (정수/퍼센트 + 잔여허용량)
    disp_rows2 = []
    csv_rows2  = []
    order = ["특수인화물", "제1석유류(비수용성)", "제1석유류(수용성)", "알코올류"]
    for key in order:
        cur = by_class.get(key, 0.0)
        limit = LEGAL_LIMITS_L.get(key, 0.0)
        ratio = (cur / limit) if (limit and limit>0) else None
        remain = max(limit - cur, 0.0) if limit else 0.0
        status = ("초과" if ratio is not None and ratio>=1.0 else
                  "경고" if ratio is not None and ratio>=0.5 else
                  "주의" if ratio is not None and ratio>=0.2 else "정상")

        disp_rows2.append({
            "구분": key,
            "현재보유량(L)": fmt_int(cur),
            "지정수량(L)": fmt_int(limit),
            "잔여허용량(L)": fmt_int(remain),
            "비율": fmt_pct(ratio),
            "상태": status
        })
        csv_rows2.append(disp_rows2[-1].copy())

    st.markdown("#### 📦 제4류 위험물 저장량 현황")
    if not disp_rows2:
        st.caption("표시할 데이터가 없습니다.")
    else:
        df2 = pd.DataFrame(disp_rows2)
        st.dataframe(df2, use_container_width=True)
        st.download_button("📥 CSV로 내려받기 (제4류 현황)",
                           pd.DataFrame(csv_rows2).to_csv(index=False).encode("utf-8-sig"),
                           file_name="hazard_class_4_summary.csv", mime="text/csv")

    # 메모/부가정보
    colL, colR = st.columns([2,1])
    with colL:
        st.markdown("##### ℹ️ 환산/분류 메모")
        st.write("- g/kg → L 환산에는 물질별 **density_g_per_ml(밀도)** 값이 필요합니다. Materials에 추가하면 정확도가 올라갑니다.")
        st.write("- **hazard_class**를 Materials에 지정하면 내장 추정보다 우선합니다.")
        if unknown > 0:
            st.warning(f"유별 미분류로 집계된 양이 있습니다. (분류되지 않은 총량: {fmt_int(unknown)} L)")
    with colR:
        if skipped:
            st.markdown("##### ⚠️ 환산 불가 목록")
            st.dataframe(pd.DataFrame(skipped))
