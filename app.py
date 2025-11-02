# =========================
# TAB4: 🔄 입출고 로그 — 표 안에서 바로 삭제/일시수정 (data_editor)
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

        # 편집용 datetime 값 (local tz로 보정)
        try:
            base_dt = datetime.fromisoformat(iso.replace("Z","+00:00"))
        except:
            base_dt = datetime.now().astimezone()
        # 에디터에 보이는 값은 tz없을 수 있으니 naive로 내려줘도 됨(편집 후 재조합 시 UTC로 저장)
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

    # 1부터 시작 인덱스
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

    # 적용 버튼
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
        updated, deleted, errors = 0, 0, 0
        for idx, row in edited.iterrows():
            rid = row.get("record_id")
            if not rid:
                continue

            # 삭제 우선 처리
            if bool(row.get("삭제", False)):
                try:
                    r = at_delete_record(AIRTABLE_BASE_ID, tx_ref, rid)
                    if r.status_code in (200, 202):
                        deleted += 1
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
        if deleted: msg.append(f"🗑️ 삭제 {deleted}건")
        if errors:  msg.append(f"⚠️ 오류 {errors}건")
        if not msg: msg = ["변경 사항이 없습니다."]
        st.success(" / ".join(msg))
        st.rerun()
