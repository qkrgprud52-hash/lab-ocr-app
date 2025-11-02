import streamlit as st
import requests, base64, re, pandas as pd, json
from urllib.parse import quote
from datetime import datetime, timedelta, date, timezone, time as dtime

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

# 휴지통 테이블(선택) — 없으면 소프트삭제
TRASH_TABLE_ID        = st.secrets.get("TRASH_TABLE_ID", "")
TRASH_TABLE_NAME      = st.secrets.get("TRASH_TABLE_NAME", "Lab OCR Trash")

IMGBB_KEY             = st.secrets.get("IMGBB_KEY", "")
DEFAULT_GCP_KEY       = st.secrets.get("GCP_KEY", "")

# =========================
# 호환용 datetime 입력 헬퍼 (Streamlit 구버전 대응)
# =========================
def datetime_input_compat(label: str, default_dt: datetime) -> datetime:
    d = st.date_input(f"{label} (날짜)", value=default_dt.date())
    t_default = default_dt.time().replace(microsecond=0)
    t = st.time_input(f"{label} (시간)", value=t_default)
    if isinstance(t, dtime):
        combined = datetime.combine(d, t)
        try:
            return combined.replace(tzinfo=default_dt.tzinfo)
        except Exception:
            return combined
    return default_dt

# =========================
# 유틸
# =========================
def show_df(df: pd.DataFrame):
    df2 = df.copy()
    df2.index = range(1, len(df2) + 1)  # 1부터 시작
    df2.index.name = "No."
    st.dataframe(df2, use_container_width=True)

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

def at_get_record(base_id, table_id_or_name, record_id: str):
    url = f"https://api.airtable.com/v0/{base_id}/{table_id_or_name}/{record_id}"
    r = requests.get(url, headers=at_headers(), timeout=20)
    if r.status_code == 200:
        return r.json()
    return None

def at_update_record(base_id, table_id_or_name, record_id: str, fields: dict):
    url = f"https://api.airtable.com/v0/{base_id}/{table_id_or_name}/{record_id}"
    r = requests.patch(url, json={"fields": fields}, headers=at_headers(), timeout=20)
    return r

def at_delete_record(base_id, table_id_or_name, record_id: str):
    url = f"https://api.airtable.com/v0/{base_id}/{table_id_or_name}/{record_id}"
    r = requests.delete(url, headers=at_headers(), timeout=20)
    return r

def ensure_material_record(cas_no: str, name_guess: str = ""):
    """Materials에 CAS 없으면 자동 생성"""
    if not cas_no:
        return None
    mref = table_ref(MATERIALS_TABLE_ID, MATERIALS_TABLE_NAME)
    try:
        rec = at_find_one(AIRTABLE_BASE_ID, mref, formula=f"{{CAS}} = '{cas_no}'")
        if rec:
            return rec  # 이미 있음
        payload = {"fields": {"CAS": cas_no}}
        if name_guess:
            payload["fields"]["name"] = name_guess[:100]
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{mref}"
        r = requests.post(url, json=payload, headers=at_headers(), timeout=20)
        if r.status_code in (200, 201):
            return r.json()
    except:
        pass
    return None

def set_material_name_if_missing(cas_no: str, mats_idx: dict, name_hint: str = ""):
    """Materials에 name이 없으면 PubChem 조회해 채움(가능하면)"""
    if not cas_no:
        return
    mref = table_ref(MATERIALS_TABLE_ID, MATERIALS_TABLE_NAME)
    current = mats_idx.get(cas_no, {})
    if current.get("name"):
        return
    name_found = None
    try:
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{cas_no}/property/Title,IUPACName/JSON"
        r = requests.get(url, timeout=12)
        if r.status_code == 200:
            js = r.json()
            props = js.get("PropertyTable", {}).get("Properties", [])
            if props:
                p = props[0]
                name_found = p.get("Title") or p.get("IUPACName")
    except:
        pass
    if not name_found:
        name_found = (name_hint or "").strip()
        if "\n" in name_found:
            name_found = name_found.split("\n", 1)[0]
        name_found = name_found[:100]
    if not name_found:
        return
    try:
        rec = at_find_one(AIRTABLE_BASE_ID, mref, formula=f"{{CAS}} = '{cas_no}'")
        if rec:
            rid = rec["id"]
            at_update_record(AIRTABLE_BASE_ID, mref, rid, {"name": name_found})
        else:
            requests.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{mref}",
                json={"fields": {"CAS": cas_no, "name": name_found}},
                headers=at_headers(), timeout=20
            )
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

# ===== 휴지통(Undo) 관련 =====
def trash_enabled() -> bool:
    return bool(TRASH_TABLE_ID or TRASH_TABLE_NAME)

def trash_ref() -> str:
    return table_ref(TRASH_TABLE_ID, TRASH_TABLE_NAME)

def save_to_trash(orig_record: dict) -> bool:
    """
    휴지통 테이블에 원본을 JSON으로 저장.
    휴지통 테이블 필수 필드:
      - original_record_id (single line)
      - deleted_at (date/time)
      - raw (long text)
    """
    if not trash_enabled() or not orig_record:
        return False
    try:
        tref = trash_ref()
        fields = {
            "original_record_id": orig_record.get("id", ""),
            "deleted_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z"),
            "raw": json.dumps(orig_record, ensure_ascii=False)
        }
        r = requests.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{tref}",
            json={"fields": fields}, headers=at_headers(), timeout=20
        )
        return r.status_code in (200, 201)
    except:
        return False

# 제4류 지정수량(고정값)
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
    try:
        mats = at_get_all(AIRTABLE_BASE_ID, mref)
    except Exception as e:
        st.warning(f"Materials 로드 실패: {e}")
        mats = []
    out = {}
    for r in mats:
        f = r.get("fields",{})
        cas = (f.get("CAS") or "").strip()
        if not cas:
            continue
        out[cas] = {
            "name": f.get("name",""),
            "designated_qty": f.get("designated_qty"),
            "unit": (f.get("Unit") or f.get("unit") or ""),
            "hazard_class": f.get("hazard_class",""),
            "density_g_per_ml": f.get("density_g_per_ml"),
        }
    return out

def classify_hazard(cas: str, mats_idx: dict) -> str | None:
    if cas in mats_idx and mats_idx[cas].get("hazard_class"):
        return mats_idx[cas]["hazard_class"]
    if cas in BUILTIN_CHEM and BUILTIN_CHEM[cas][1]:
        return BUILTIN_CHEM[cas][1]
    return None

def get_density(cas: str, mats_idx: dict) -> float | None:
    if cas in mats_idx and mats_idx[cas].get("density_g_per_ml"):
        try:
            return float(mats_idx[cas]["density_g_per_ml"])
        except:
            pass
    if cas in BUILTIN_CHEM and BUILTIN_CHEM[cas][2]:
        return BUILTIN_CHEM[cas][2]
    return None

def to_liters(amount, unit: str, density_g_per_ml: float | None) -> float | None:
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
    return None  # EA, cyl 등은 환산 불가

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
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📷 기록 (OCR/저장)",
    "📦 재고 현황",
    "🏭 위험물(제4류) 현황",
    "🔄 입출고 로그",
    "🗃️ 휴지통(복원)"
])

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

    st.markdown("### ⏱ 거래 일시 (수정 가능)")
    now_local = datetime.now().astimezone()
    tx_time_input = datetime_input_compat("거래일시", now_local)

    st.markdown("### 📦 수량")
    colQ1, colQ2 = st.columns([1,1])
    qty = colQ1.number_input("수량", min_value=0.0, step=1.0, format="%.0f")  # 정수 입력
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

        # CAS → 물질명 자동 채움(가능 시 Materials에 반영)
        mats_idx = load_materials_index()
        set_material_name_if_missing(cas_no, mats_idx, name_hint=text)

        ready = bool(text and dept and lab and bld and room and io_type and (qty>=0))
        if not ready:
            st.info("ℹ OCR/메타/수량을 채우면 저장할 수 있어요.")

        if st.button("💾 Airtable에 저장", disabled=not ready):
            sign = +1 if io_type=="입고" else -1  # 출고/반품/폐기 → 음수
            img_url = upload_to_imgbb(img_bytes, uploaded_file.name)
            # ISO8601(UTC) 저장
            tx_dt_utc = tx_time_input.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

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
                "unit": unit,
                "tx_time": tx_dt_utc,   # Airtable에 동일 이름 Date/Time 필드 권장
                "deleted": False,       # 소프트삭제 플래그(없으면 Airtable에 생성)
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
# TAB2: 📦 재고 현황 — CAS별 / 실험실별
# =========================
with tab2:
    subt1, subt2 = st.tabs(["🔬 CAS별", "🏫 실험실별"])

    # 공통 데이터 로딩
    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        st.error("Airtable secrets가 필요합니다.")
    else:
        tx_ref  = table_ref(AIRTABLE_TABLE_ID, AIRTABLE_TABLE_NAME)
        try:
            with st.spinner("🔄 데이터 불러오는 중…"):
                tx = at_get_all(AIRTABLE_BASE_ID, tx_ref)
                mats_idx = load_materials_index()
        except Exception as e:
            st.error(f"불러오기 실패: {e}")
            tx, mats_idx = [], {}

        # 삭제된(소프트삭제) 제외
        def not_deleted(rec):
            f = rec.get("fields",{})
            return not bool(f.get("deleted", False))
        tx = [r for r in tx if not_deleted(r)]

    # ---------- CAS별 ----------
    with subt1:
        st.caption("CAS별 재고합계만 표시 (지정수량/비율 제거).")
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

        rows = []
        for (cas, unit), qty_sum in sums.items():
            m = mats_idx.get(cas, {})
            rows.append({
                "CAS": cas,
                "물질명": m.get("name",""),
                "재고합계": f"{int(round(qty_sum))}",
                "단위": unit,
                "메모": ""
            })

        rows.sort(key=lambda r: int(r["재고합계"]) if r["재고합계"] else 0, reverse=True)

        if rows:
            df = pd.DataFrame(rows)
            show_df(df)
            st.download_button("📥 CSV로 내려받기 (CAS별)",
                               df.to_csv(index=False).encode("utf-8-sig"),
                               file_name="inventory_by_cas.csv", mime="text/csv")
        else:
            st.caption("표시할 데이터가 없습니다.")

    # ---------- 실험실별 ----------
    with subt2:
        st.caption("실험실별 재고를 **L 단위로 환산**(가능한 항목)하여 요약과 상세를 제공합니다.")
        sum_lab = {}
        detail = []
        skipped = []

        for r in tx:
            f = r.get("fields",{})
            cas = (f.get("CAS") or "").strip()
            q   = f.get("qty")
            unit= f.get("unit")
            bld = f.get("building","")
            room= f.get("room","")
            lab = f.get("lab","")
            if not cas or q is None or not unit:
                continue

            dens = get_density(cas, mats_idx)
            Lval = to_liters(q, unit, dens)
            if Lval is None:
                skipped.append({"CAS": cas, "qty": q, "unit": unit, "building": bld, "room": room, "lab": lab})
                continue

            key = (bld, room, lab)
            sum_lab[key] = sum_lab.get(key, 0.0) + float(Lval)

            m = mats_idx.get(cas, {})
            detail.append({
                "건물": bld, "호수": room, "실험실": lab,
                "CAS": cas, "물질명": m.get("name",""),
                "환산보유량(L)": f"{int(round(Lval))}",
                "원수량": f"{int(round(float(q)))}", "원단위": unit
            })

        rows_sum = [
            {"건물": k[0], "호수": k[1], "실험실": k[2], "총보유량(L)": f"{int(round(v))}"}
            for k,v in sum_lab.items()
        ]
        rows_sum.sort(key=lambda r: int(r["총보유량(L)"]) if r["총보유량(L)"] else 0, reverse=True)

        st.markdown("#### 🧾 실험실별 요약 (L)")
        if rows_sum:
            df_sum = pd.DataFrame(rows_sum)
            show_df(df_sum)
            st.download_button("📥 CSV로 내려받기 (실험실 요약)",
                               df_sum.to_csv(index=False).encode("utf-8-sig"),
                               file_name="inventory_by_lab_summary.csv", mime="text/csv")
        else:
            st.caption("요약할 데이터가 없습니다.")

        st.markdown("#### 🔎 실험실별 상세 (CAS)")
        if detail:
            df_det = pd.DataFrame(detail)
            df_det["__sort__"] = df_det["환산보유량(L)"].apply(lambda x: int(x) if str(x).isdigit() else 0)
            df_det = df_det.sort_values(by=["건물","호수","실험실","__sort__"], ascending=[True, True, True, False]).drop(columns="__sort__")
            show_df(df_det)
            st.download_button("📥 CSV로 내려받기 (실험실 상세)",
                               df_det.to_csv(index=False).encode("utf-8-sig"),
                               file_name="inventory_by_lab_detail.csv", mime="text/csv")
        else:
            st.caption("상세 데이터가 없습니다.")

        if skipped:
            with st.expander("⚠️ 환산 불가 항목 보기 (밀도/단위 문제)"):
                show_df(pd.DataFrame(skipped))

# =========================
# TAB3: 위험물(제4류) 현황 — 요약(유별) + 세부(CAS별, 위험물류명 표시)
# =========================
with tab3:
    st.info("제4류 위험물 기준으로, 창고 전체 저장량(L)을 유별별로 합산하고, CAS별 상세(위험물류명 포함)도 제공합니다.")

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

    # 삭제 제외
    def not_deleted(rec):
        f = rec.get("fields",{})
        return not bool(f.get("deleted", False))
    tx = [r for r in tx if not_deleted(r)]

    subtA, subtB = st.tabs(["📦 유별 요약", "🔎 CAS 상세"])

    # ----- 유별 요약 -----
    with subtA:
        by_class = {}
        skipped  = []
        for r in tx:
            f = r.get("fields",{})
            cas = (f.get("CAS") or "").strip()
            q   = f.get("qty")
            unit= f.get("unit")
            if not cas or q is None or not unit:
                continue
            dens = get_density(cas, mats_idx)
            Lval = to_liters(q, unit, dens)
            if Lval is None:
                skipped.append({"CAS": cas, "qty": q, "unit": unit})
                continue
            hclass = classify_hazard(cas, mats_idx)
            if not hclass:
                hclass = "미분류"
            by_class[hclass] = by_class.get(hclass, 0.0) + Lval

        disp_rows2, csv_rows2 = [], []
        order = ["특수인화물", "제1석유류(비수용성)", "제1석유류(수용성)", "알코올류", "미분류"]
        for key in order:
            cur = by_class.get(key, 0.0)
            limit = LEGAL_LIMITS_L.get(key, 0.0)
            ratio = (cur / limit) if (limit and limit>0) else None
            remain = max(limit - cur, 0.0) if limit else 0.0
            status = ("초과" if ratio is not None and ratio>=1.0 else
                      "경고" if ratio is not None and ratio>=0.5 else
                      "주의" if ratio is not None and ratio>=0.2 else "정상")

            row = {
                "구분": key,
                "현재보유량(L)": fmt_int(cur),
                "지정수량(L)": fmt_int(limit),
                "잔여허용량(L)": fmt_int(remain),
                "비율": fmt_pct(ratio) if ratio is not None else "",
                "상태": status
            }
            disp_rows2.append(row); csv_rows2.append(row.copy())

        st.markdown("#### 📦 제4류 위험물 저장량 현황 (유별 합계)")
        if not disp_rows2:
            st.caption("표시할 데이터가 없습니다.")
        else:
            df2 = pd.DataFrame(disp_rows2)
            show_df(df2)
            st.download_button("📥 CSV로 내려받기 (제4류 유별 요약)",
                               pd.DataFrame(csv_rows2).to_csv(index=False).encode("utf-8-sig"),
                               file_name="hazard_class_4_summary.csv", mime="text/csv")

        if skipped:
            with st.expander("⚠️ 환산 불가 항목 보기"):
                show_df(pd.DataFrame(skipped))

    # ----- CAS 상세(위험물류명 표시) -----
    with subtB:
        sums = {}
        detail_rows = []
        for r in tx:
            f = r.get("fields",{})
            cas = (f.get("CAS") or "").strip()
            q   = f.get("qty")
            unit= f.get("unit")
            if not cas or q is None or not unit:
                continue
            dens = get_density(cas, mats_idx)
            Lval = to_liters(q, unit, dens)
            if Lval is None:
                continue
            key = (cas,)
            sums[key] = sums.get(key, 0.0) + float(Lval)

        for (cas,) , Lsum in sums.items():
            m = mats_idx.get(cas, {})
            hclass = classify_hazard(cas, mats_idx) or "미분류"
            limit = LEGAL_LIMITS_L.get(hclass, 0.0)
            remain = max(limit - Lsum, 0.0) if limit else 0.0
            detail_rows.append({
                "CAS": cas,
                "물질명": m.get("name",""),
                "위험물류명": hclass,
                "재고합계(L)": fmt_int(Lsum),
                "지정수량(L)": fmt_int(limit),
                "잔여허용량(L)": fmt_int(remain),
            })

        detail_rows.sort(key=lambda r: int(r["재고합계(L)"]) if r["재고합계(L)"] else 0, reverse=True)

        st.markdown("#### 🔎 CAS별 상세 (위험물류명 포함)")
        if detail_rows:
            dfh = pd.DataFrame(detail_rows)
            show_df(dfh)
            st.download_button("📥 CSV로 내려받기 (제4류 CAS 상세)",
                               dfh.to_csv(index=False).encode("utf-8-sig"),
                               file_name="hazard_cas_detail.csv", mime="text/csv")
        else:
            st.caption("표시할 데이터가 없습니다.")

# =========================
# TAB4: 🔄 입출고 로그 — 표 안에서 바로 삭제/일시수정 (Undo 지원)
# =========================
with tab4:
    st.info("표 안에서 '삭제' 체크하거나 '새 일시'를 수정한 뒤, 아래 '선택 항목 적용' 버튼을 누르세요. (Airtable에 tx_time 필드가 있어야 합니다)")

    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        st.error("Airtable secrets가 필요합니다."); st.stop()

    tx_ref  = table_ref(AIRTABLE_TABLE_ID, AIRTABLE_TABLE_NAME)

    # 기본 기간: 최근 30일
    today = date.today()
    default_start = today - timedelta(days=30)
    colf1, colf2 = st.columns(2)
    start_d = colf1.date_input("시작일", value=default_start)
    end_d   = colf2.date_input("종료일", value=today)

    # 원본 데이터 로드
    try:
        with st.spinner("🔄 데이터 불러오는 중…"):
            tx = at_get_all(AIRTABLE_BASE_ID, tx_ref)
            mats_idx = load_materials_index()
    except Exception as e:
        st.error(f"불러오기 실패: {e}")
        tx, mats_idx = [], {}

    # 소프트삭제 제외
    def not_deleted(rec):
        f = rec.get("fields",{})
        return not bool(f.get("deleted", False))
    tx = [r for r in tx if not_deleted(r)]

    # 표시/편집용 데이터 구성
    def pick_time(fields, created_iso):
        t = fields.get("tx_time")
        return t if t else (created_iso or "")

    def in_range_iso(iso_str: str) -> bool:
        if not iso_str:
            return True
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z","+00:00")).date()
            return (start_d <= dt <= end_d)
        except:
            return True

    rows_for_editor = []
    orig_time_map = {}  # record_id -> iso string (원래 값 비교용)

    for r in tx:
        rid = r.get("id")
        ct  = r.get("createdTime")
        f   = r.get("fields",{})
        iso = pick_time(f, ct)
        if not in_range_iso(iso):
            continue

        cas = (f.get("CAS") or "").strip()
        name = mats_idx.get(cas, {}).get("name","")
        qty = f.get("qty")
        unit= f.get("unit","")
        io  = f.get("io_type","")
        bld = f.get("building","")
        room= f.get("room","")
        lab = f.get("lab","")

        # 편집용 datetime 값 (naive로 표시 → 저장 시 UTC로 변환)
        try:
            base_dt = datetime.fromisoformat(iso.replace("Z","+00:00"))
        except:
            base_dt = datetime.now().astimezone()
        new_dt_default = base_dt.astimezone().replace(microsecond=0).replace(tzinfo=None)

        orig_time_map[rid] = iso
        rows_for_editor.append({
            "record_id": rid,
            "일시(현재)": iso.replace("T"," ").replace("Z",""),
            "새_일시": new_dt_default,     # 편집 가능
            "구분": io,
            "CAS": cas,
            "물질명": name,
            "수량": f"{int(round(float(qty))) if qty is not None else ''}",
            "단위": unit,
            "건물": bld,
            "호수": room,
            "실험실": lab,
            "삭제": False,                 # 체크박스
        })

    if not rows_for_editor:
        st.caption("표시할 데이터가 없습니다. 기간을 넓혀보세요.")
        st.stop()

    df_edit = pd.DataFrame(rows_for_editor)
    df_edit.index = range(1, len(df_edit) + 1)
    df_edit.index.name = "No."

    edited = st.data_editor(
        df_edit,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "record_id": st.column_config.TextColumn("record_id", disabled=True, help="Airtable 내부 ID"),
            "일시(현재)": st.column_config.TextColumn("일시(현재)", disabled=True),
            "새_일시": st.column_config.DatetimeColumn("새 일시(수정 가능)"),
            "구분": st.column_config.TextColumn("구분", disabled=True),
            "CAS": st.column_config.TextColumn("CAS", disabled=True),
            "물질명": st.column_config.TextColumn("물질명", disabled=True),
            "수량": st.column_config.TextColumn("수량", disabled=True),
            "단위": st.column_config.TextColumn("단위", disabled=True),
            "건물": st.column_config.TextColumn("건물", disabled=True),
            "호수": st.column_config.TextColumn("호수", disabled=True),
            "실험실": st.column_config.TextColumn("실험실", disabled=True),
            "삭제": st.column_config.CheckboxColumn("삭제"),
        },
        hide_index=False,
        key="edit_logs_grid",
    )

    cola, colb = st.columns([1,3])
    apply_btn = cola.button("✅ 선택 항목 적용")

    def to_utc_iso(dt_val: datetime) -> str:
        """에디터에서 넘어온 naive datetime을 로컬타임으로 간주 → UTC Z로 변환"""
        if dt_val is None:
            return ""
        if dt_val.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo
            dt_val = dt_val.replace(tzinfo=local_tz)
        return dt_val.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

    if apply_btn:
        updated, deleted, soft_deleted, errors = 0, 0, 0, 0
        for idx, row in edited.iterrows():
            rid = row.get("record_id")
            if not rid:
                continue

            # 삭제 우선 처리
            if bool(row.get("삭제", False)):
                try:
                    # 휴지통 사용 가능하면 원본 백업 후 물리 삭제
                    if trash_enabled():
                        orig = at_get_record(AIRTABLE_BASE_ID, tx_ref, rid)
                        ok_backup = save_to_trash(orig)
                        if not ok_backup:
                            errors += 1
                            continue
                        r = at_delete_record(AIRTABLE_BASE_ID, tx_ref, rid)
                        if r.status_code in (200, 202):
                            deleted += 1
                        else:
                            errors += 1
                    else:
                        # 소프트 삭제(필드 'deleted' = True)
                        r = at_update_record(AIRTABLE_BASE_ID, tx_ref, rid, {"deleted": True})
                        if r.status_code in (200, 201):
                            soft_deleted += 1
                        else:
                            errors += 1
                except Exception:
                    errors += 1
                continue

            # 일시 수정 처리: 변경 여부 판단
            new_dt = row.get("새_일시")
            orig_iso = orig_time_map.get(rid, "")
            new_iso = to_utc_iso(new_dt) if isinstance(new_dt, datetime) else ""
            if new_iso and (new_iso != orig_iso):
                try:
                    r = at_update_record(AIRTABLE_BASE_ID, tx_ref, rid, {"tx_time": new_iso})
                    if r.status_code in (200, 201):
                        updated += 1
                    else:
                        errors += 1
                except Exception:
                    errors += 1

        # 결과 메시지
        msg = []
        if updated: msg.append(f"🕒 일시 수정 {updated}건")
        if deleted: msg.append(f"🗑️ 삭제(휴지통으로 이동) {deleted}건")
        if soft_deleted: msg.append(f"🗂️ 소프트삭제 {soft_deleted}건")
        if errors:  msg.append(f"⚠️ 오류 {errors}건")
        if not msg: msg = ["변경 사항이 없습니다."]
        st.success(" / ".join(msg))
        st.rerun()
# =========================
# TAB5: 🗃️ 휴지통(복원)
# =========================
with tab5:
    st.info("휴지통에 보관된 삭제 이력을 복원할 수 있습니다. 선택 후 '선택 항목 복원'을 누르세요.")

    if not trash_enabled():
        st.warning("휴지통 테이블이 설정되어 있지 않습니다. Secrets에 TRASH_TABLE_ID 또는 TRASH_TABLE_NAME을 설정하세요.")
        st.stop()

    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        st.error("Airtable secrets가 필요합니다."); st.stop()

    tx_ref   = table_ref(AIRTABLE_TABLE_ID, AIRTABLE_TABLE_NAME)
    trash_t  = trash_ref()

    # 휴지통 레코드 로드
    trash_recs = get_trash_all()
    if not trash_recs:
        st.caption("휴지통이 비어있습니다.")
        st.stop()

    # 표시용 테이블 구성
    disp = []
    for tr in trash_recs:
        tid = tr.get("id")
        f   = tr.get("fields", {})
        orig_id   = f.get("original_record_id", "")
        deleted_at= f.get("deleted_at", "")
        raw       = f.get("raw", "")

        cas = name = qty = unit = io = bld = room = lab = ""
        tx_time = ""
        # raw JSON 파싱
        try:
            js = json.loads(raw) if isinstance(raw, str) else raw
            fields = js.get("fields", {})
            cas   = (fields.get("CAS") or "")
            name  = fields.get("Name") or fields.get("name") or ""
            qty   = fields.get("qty")
            unit  = fields.get("unit","")
            io    = fields.get("io_type","")
            bld   = fields.get("building","")
            room  = fields.get("room","")
            lab   = fields.get("lab","")
            tx_time = fields.get("tx_time","") or js.get("createdTime","")
        except Exception:
            pass

        disp.append({
            "trash_id": tid,
            "삭제시각": deleted_at.replace("T"," ").replace("Z",""),
            "원본 record_id": orig_id,
            "일시": tx_time.replace("T"," ").replace("Z",""),
            "구분": io,
            "CAS": cas,
            "물질명(파일명)": name,
            "수량": f"{int(round(float(qty)))}" if qty not in (None,"") else "",
            "단위": unit,
            "건물": bld, "호수": room, "실험실": lab,
            "복원": False
        })

    df_trash = pd.DataFrame(disp)
    df_trash.index = range(1, len(df_trash) + 1)
    df_trash.index.name = "No."

    edited_trash = st.data_editor(
        df_trash,
        use_container_width=True,
        num_rows="fixed",
        column_config={
            "trash_id": st.column_config.TextColumn("trash_id", disabled=True),
            "삭제시각": st.column_config.TextColumn("삭제시각", disabled=True),
            "원본 record_id": st.column_config.TextColumn("원본 record_id", disabled=True),
            "일시": st.column_config.TextColumn("일시", disabled=True),
            "구분": st.column_config.TextColumn("구분", disabled=True),
            "CAS": st.column_config.TextColumn("CAS", disabled=True),
            "물질명(파일명)": st.column_config.TextColumn("물질명(파일명)", disabled=True),
            "수량": st.column_config.TextColumn("수량", disabled=True),
            "단위": st.column_config.TextColumn("단위", disabled=True),
            "건물": st.column_config.TextColumn("건물", disabled=True),
            "호수": st.column_config.TextColumn("호수", disabled=True),
            "실험실": st.column_config.TextColumn("실험실", disabled=True),
            "복원": st.column_config.CheckboxColumn("복원"),
        },
        hide_index=False,
        key="trash_editor_grid",
    )

    colx, coly = st.columns([1,3])
    restore_btn = colx.button("✅ 선택 항목 복원")

    if restore_btn:
        restored = removed = errors = 0
        # 원본 테이블 키
        for _, row in edited_trash.iterrows():
            if not bool(row.get("복원", False)):
                continue
            tid = row.get("trash_id")
            # 휴지통 레코드 상세를 다시 불러서 raw 이용
            try:
                rec = at_get_record(AIRTABLE_BASE_ID, trash_t, tid)
                if not rec:
                    errors += 1
                    continue
                f = rec.get("fields", {})
                raw = f.get("raw", "")
                js  = json.loads(raw) if isinstance(raw, str) else raw
                fields = (js or {}).get("fields", {})
                if not isinstance(fields, dict) or not fields:
                    errors += 1
                    continue

                # 안전하게 복원: 소프트삭제 흔적 제거
                fields.pop("deleted", None)
                # Airtable에 다시 생성
                r = at_create_record(AIRTABLE_BASE_ID, tx_ref, fields)
                if r.status_code in (200, 201):
                    restored += 1
                    # 휴지통에서 삭제
                    d = at_delete_record(AIRTABLE_BASE_ID, trash_t, tid)
                    if d.status_code in (200, 202):
                        removed += 1
                else:
                    errors += 1
            except Exception:
                errors += 1

        msg = []
        if restored: msg.append(f"♻️ 복원 {restored}건")
        if removed:  msg.append(f"🧹 휴지통 정리 {removed}건")
        if errors:   msg.append(f"⚠️ 오류 {errors}건")
        if not msg:  msg = ["변경 사항이 없습니다."]
        st.success(" / ".join(msg))
        st.rerun()
