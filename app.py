import streamlit as st
import requests
import base64
import re
from urllib.parse import quote

st.set_page_config(page_title="연구실 시약 OCR 기록 시스템", page_icon="🧪", layout="wide")
st.title("🧪 연구실 시약 OCR 기록 시스템 (입·출고 관리)")

# =========================
# 1) Secrets 불러오기
# =========================
AIRTABLE_TOKEN       = st.secrets.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID     = st.secrets.get("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_ID    = st.secrets.get("AIRTABLE_TABLE_ID", "")
IMGBB_KEY            = st.secrets.get("IMGBB_KEY", "")
DEFAULT_GCP_KEY      = st.secrets.get("GCP_KEY", "")

# =========================
# 2) OCR 관련
# =========================
CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")

def extract_cas(text: str) -> str:
    m = CAS_RE.search(text or "")
    return m.group(0) if m else ""

def run_ocr(image_bytes: bytes, gcp_key: str) -> dict:
    """Google Vision OCR 실행"""
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    url = f"https://vision.googleapis.com/v1/images:annotate?key={gcp_key}"
    payload = {
        "requests": [{
            "image": {"content": base64_image},
            "features": [{"type": "TEXT_DETECTION"}]
        }]
    }
    return requests.post(url, json=payload, timeout=30).json()

def upload_to_imgbb(image_bytes, filename: str) -> str | None:
    """imgbb에 이미지 업로드 → URL 반환"""
    if not IMGBB_KEY:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        res = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_KEY, "image": b64, "name": filename},
            timeout=20
        )
        res.raise_for_status()
        return res.json()["data"]["url"]
    except:
        return None

def save_to_airtable(fields: dict):
    """Airtable 저장"""
    if not (AIRTABLE_TOKEN and AIRTABLE_BASE_ID):
        st.error("❌ Airtable Secrets가 설정되지 않았습니다.")
        return False, None

    headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
    table_ref = AIRTABLE_TABLE_ID or quote("Lab OCR Results", safe="")
    endpoint = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_ref}"

    r = requests.post(endpoint, json={"fields": fields}, headers=headers, timeout=20)
    ok = r.status_code in (200, 201)
    return ok, (r.json() if ok else r.text)


# =========================
# 3) UI 입력 영역
# =========================
uploaded_file = st.file_uploader("📷 시약 라벨 이미지 업로드", type=["jpg", "jpeg", "png"])
gcp_key = st.text_input("🔑 Google Vision API Key (Secrets에 저장 시 비워도 됨)", value=DEFAULT_GCP_KEY, type="password")

st.subheader("📌 시약 입·출고 정보 입력")

colA, colB = st.columns(2)
with colA:
    io_type = st.radio("입출고 구분", ["입고", "출고"])
    dept = st.selectbox("학과", ["화학공학과", "안전공학과", "신소재공학과", "기계시스템디자인공학과"])
    building = st.selectbox("건물명", ["청운관", "제1공학관", "제2공학관", "어울림관"])

with colB:
    lab = st.text_input("실험실명 (예: 전기화학에너지소재연구실)")
    room = st.text_input("호수 (예: 203, B105 등)")


# =========================
# 4) OCR 실행
# =========================
if uploaded_file and gcp_key:
    st.info("🔍 OCR 분석 중입니다... 잠시만 기다려주세요!")
    img_bytes = uploaded_file.getvalue()
    ocr_json = run_ocr(img_bytes, gcp_key)

    try:
        text = ocr_json["responses"][0]["fullTextAnnotation"]["text"]
        st.success("✅ OCR 인식 성공")
        st.text_area("📄 추출된 텍스트", text, height=260)
    except:
        st.error("❌ 텍스트를 인식하지 못했습니다")
        st.json(ocr_json)
        text = ""

    if text:
        cas_no = extract_cas(text)
        st.code(f"🔎 추출된 CAS 번호: {cas_no or '(없음)'}")

        if st.button("💾 Airtable에 저장하기", type="primary"):
            img_url = upload_to_imgbb(img_bytes, uploaded_file.name)
            fields = {
                "ocr_text": text,
                "CAS": cas_no,
                "dept": dept,
                "lab": lab,
                "building": building,
                "room": room,
                "io_type": io_type,
            }
            if img_url:
                fields["Attachments"] = [{"url": img_url, "filename": uploaded_file.name}]

            ok, res = save_to_airtable(fields)
            if ok:
                st.success("✅ Airtable 저장 완료!")
            else:
                st.error(f"❌ 저장 실패: {res}")


else:
    st.caption("이미지와 API Key를 입력하면 OCR 분석이 시작됩니다.")
