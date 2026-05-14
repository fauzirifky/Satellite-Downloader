from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import ee
import pandas as pd
import streamlit as st

try:
    from .export_satellite_support import fetch_region_centroids, initialize_earth_engine, list_regions
    from .satellite_general_workbook import (
        GROUP_ORDER,
        GROUP_SPECS,
        VARIABLE_SPECS,
        SatelliteAreaConfig,
        SatelliteWorkbookConfig,
        workbook_config_to_payload,
        build_satellite_workbook_resumable,
        cache_status,
        selected_groups_from_variables,
    )
except ImportError:
    from export_satellite_support import fetch_region_centroids, initialize_earth_engine, list_regions
    from satellite_general_workbook import (
        GROUP_ORDER,
        GROUP_SPECS,
        VARIABLE_SPECS,
        SatelliteAreaConfig,
        SatelliteWorkbookConfig,
        workbook_config_to_payload,
        build_satellite_workbook_resumable,
        cache_status,
        selected_groups_from_variables,
    )


APP_DIR = Path(__file__).resolve().parent
CACHE_ROOT = APP_DIR / "satellite_cache_runs"
BOUNDARY_INPUT_ROOT = APP_DIR / "boundary_inputs"
BANDUNG_KELURAHAN_GEOJSON = APP_DIR / "sample_boundaries" / "bandung_kelurahan.geojson"
JOB_ROOT = APP_DIR / "satellite_jobs"
MIN_OBSERVATION_DATE = date(2000, 1, 1)
MAX_OBSERVATION_DATE = date(2035, 12, 31)
DEFAULT_VARIABLES = ["temp_mean", "humidity", "solar_radiation", "rainfall", "ndvi"]
GROUP_LABELS = {
    "climate": "Iklim",
    "rainfall": "Curah Hujan",
    "ndvi": "NDVI",
    "evi": "EVI",
    "pollution": "Polusi",
    "wave": "Gelombang Laut",
}


class UserInputError(ValueError):
    pass


def read_credentials_snapshot() -> dict:
    cred_path = Path(ee.oauth.get_credentials_path())
    snapshot = {
        "path": str(cred_path),
        "exists": cred_path.exists(),
        "project": "",
    }
    if cred_path.exists():
        try:
            payload = json.loads(cred_path.read_text(encoding="utf-8"))
            snapshot["project"] = str(payload.get("project", "")).strip()
        except Exception:
            snapshot["project"] = ""
    return snapshot


def stored_project_id() -> str:
    return read_credentials_snapshot().get("project", "").strip()


def ensure_session_defaults() -> None:
    st.session_state.setdefault("gee_auth_url", "")
    st.session_state.setdefault("gee_auth_url_host", "")
    st.session_state.setdefault("gee_code_verifier", "")
    st.session_state.setdefault("gee_auth_mode", "localhost")
    st.session_state.setdefault("gee_link_mode", "")
    st.session_state.setdefault("gee_flow", None)
    st.session_state.setdefault("gee_project", stored_project_id())
    st.session_state.setdefault("target_parent_region_children_input", "Lampung")
    st.session_state.setdefault("selected_variables_general", DEFAULT_VARIABLES)
    st.session_state.setdefault("custom_boundary_region_input", "")


def save_uploaded_boundary_snapshot(uploaded_file) -> Optional[Path]:
    if uploaded_file is None:
        return None
    payload = uploaded_file.getvalue()
    digest = hashlib.sha1(payload).hexdigest()[:16]
    suffix = Path(uploaded_file.name or "boundary.geojson").suffix or ".geojson"
    safe_stem = Path(uploaded_file.name or "boundary").stem.replace(" ", "_")
    target = BOUNDARY_INPUT_ROOT / f"{digest}_{safe_stem}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def summarize_custom_geojson(boundary_path: Optional[str]) -> Optional[dict]:
    if not boundary_path:
        return None
    path = Path(boundary_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    features = payload.get("features", [])
    if not isinstance(features, list):
        return None
    property_keys = sorted(
        {
            key
            for feature in features[:50]
            for key in (feature.get("properties") or {}).keys()
        }
    )
    return {
        "path": str(path),
        "feature_count": len(features),
        "property_keys": property_keys,
    }


def geojson_property_values(
    boundary_path: Optional[str],
    property_name: str,
    filter_field: Optional[str] = None,
    filter_value: Optional[str] = None,
) -> List[str]:
    if not boundary_path or not property_name:
        return []
    path = Path(boundary_path)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    values = set()
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        if filter_field and filter_value is not None:
            if str(properties.get(filter_field, "")).strip() != str(filter_value).strip():
                continue
        value = str(properties.get(property_name, "")).strip()
        if value:
            values.add(value)
    return sorted(values)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_job_label(text: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in text.lower()).strip("-")
    return cleaned or "job"


def create_background_job(config: SatelliteWorkbookConfig, gee_project: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha1(json.dumps(workbook_config_to_payload(config), sort_keys=True).encode("utf-8")).hexdigest()[:8]
    label = safe_job_label(config.output_filename.replace(".xlsx", ""))
    job_dir = JOB_ROOT / f"{timestamp}-{label}-{digest}"
    job_dir.mkdir(parents=True, exist_ok=False)

    job_payload = {
        "created_at": now_iso(),
        "gee_project": gee_project,
        "cache_root": str(CACHE_ROOT),
        "config": workbook_config_to_payload(config),
    }
    (job_dir / "job_config.json").write_text(json.dumps(job_payload, indent=2, default=str), encoding="utf-8")
    (job_dir / "job_status.json").write_text(
        json.dumps(
            {
                "job_id": job_dir.name,
                "status": "queued",
                "created_at": now_iso(),
                "compiled_workbook_path": "",
                "error": "",
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return job_dir


def launch_background_job(job_dir: Path) -> None:
    log_path = job_dir / "job.log"
    with log_path.open("ab") as handle:
        subprocess.Popen(
            [sys.executable, str(APP_DIR / "satellite_job_runner.py"), "--job-dir", str(job_dir)],
            cwd=str(APP_DIR),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def read_job_status(job_dir: Path) -> Dict[str, Any]:
    status_path = job_dir / "job_status.json"
    if not status_path.exists():
        return {"job_id": job_dir.name, "status": "unknown"}
    return json.loads(status_path.read_text(encoding="utf-8"))


def read_job_config(job_dir: Path) -> Dict[str, Any]:
    config_path = job_dir / "job_config.json"
    if not config_path.exists():
        return {}
    return json.loads(config_path.read_text(encoding="utf-8"))


def recent_jobs(limit: int = 20) -> List[Dict[str, Any]]:
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for job_dir in sorted([path for path in JOB_ROOT.iterdir() if path.is_dir()], reverse=True)[:limit]:
        status = read_job_status(job_dir)
        config = read_job_config(job_dir)
        workbook_path = Path(status.get("compiled_workbook_path", "")) if status.get("compiled_workbook_path") else None
        rows.append(
            {
                "job_dir": str(job_dir),
                "job_id": job_dir.name,
                "status": status.get("status", "unknown"),
                "created_at": status.get("created_at", ""),
                "started_at": status.get("started_at", ""),
                "completed_at": status.get("completed_at", ""),
                "progress": f"{status.get('progress_step', 0)}/{status.get('progress_total', 0)}",
                "progress_label": status.get("progress_label", ""),
                "progress_status": status.get("progress_status", ""),
                "output_filename": (config.get("config", {}) or {}).get("output_filename", ""),
                "workbook_path": str(workbook_path) if workbook_path else "",
                "download_ready": bool(workbook_path and workbook_path.exists()),
                "log_path": str(job_dir / "job.log"),
                "error": status.get("error", ""),
            }
        )
    return rows


def tail_job_log(job_dir: Path, max_chars: int = 6000) -> str:
    log_path = job_dir / "job.log"
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


def render_job_history() -> None:
    st.subheader("6. Riwayat Job VPS")
    st.caption(
        "Job background tetap berjalan di server meski browser ditutup. Kembali ke halaman ini untuk cek progres "
        "dan unduh file hasilnya."
    )
    jobs = recent_jobs()
    if not jobs:
        st.info("Belum ada job background yang tercatat.")
        return

    for row in jobs:
        header = f"{row['job_id']} | {row['status']}"
        with st.expander(header, expanded=row["status"] in {"running", "failed"}):
            st.write(
                f"Output: `{row['output_filename'] or '-'}` | "
                f"Progress: `{row['progress']}` | "
                f"Tahap: `{row['progress_label']} - {row['progress_status']}`"
            )
            st.write(
                f"Dibuat: `{row['created_at'] or '-'}` | "
                f"Mulai: `{row['started_at'] or '-'}` | "
                f"Selesai: `{row['completed_at'] or '-'}`"
            )
            st.write(f"Log: `{row['log_path']}`")
            if row["download_ready"] and row["workbook_path"]:
                workbook_path = Path(row["workbook_path"])
                st.download_button(
                    f"Download {workbook_path.name}",
                    data=workbook_path.read_bytes(),
                    file_name=workbook_path.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"download_{row['job_id']}",
                    use_container_width=True,
                )
            if row["error"]:
                st.error(row["error"])
            log_text = tail_job_log(Path(row["job_dir"]))
            if log_text:
                st.code(log_text, language="text")


def resolve_effective_project(project: str) -> str:
    return (project or "").strip() or stored_project_id()


def credentials_status(project: str) -> tuple[bool, str]:
    try:
        effective_project = resolve_effective_project(project)
        if not effective_project:
            return False, "GEE Project ID masih kosong. Isi project yang sudah terdaftar di Earth Engine."
        initialize_earth_engine(project=effective_project, authenticate=False)
        ee.Number(1).getInfo()
        return True, f"Earth Engine siap dipakai dengan project: {effective_project}"
    except Exception as exc:
        return False, str(exc)


def auth_url_host(auth_url: str) -> str:
    return urlparse(auth_url).netloc.lower() if auth_url else ""


def auth_url_kind(auth_url: str) -> str:
    host = auth_url_host(auth_url)
    if not host:
        return "empty"
    if "accounts.google.com" in host:
        return "google_accounts"
    if "earthengine.google.com" in host or "code.earthengine.google.com" in host:
        return "earthengine_notebook"
    return "other"


def reset_auth_state() -> None:
    st.session_state["gee_auth_url"] = ""
    st.session_state["gee_auth_url_host"] = ""
    st.session_state["gee_code_verifier"] = ""
    st.session_state["gee_flow"] = None
    st.session_state["gee_link_mode"] = ""


def start_web_auth_flow(auth_mode: str) -> None:
    if auth_mode != "localhost":
        raise ValueError("Aplikasi ini hanya memakai auth web mode localhost.")
    reset_auth_state()
    flow = ee.oauth.Flow(auth_mode="localhost:0")
    st.session_state["gee_auth_url"] = flow.auth_url
    st.session_state["gee_auth_url_host"] = auth_url_host(flow.auth_url)
    st.session_state["gee_code_verifier"] = flow.code_verifier
    st.session_state["gee_flow"] = flow
    st.session_state["gee_auth_mode"] = auth_mode
    st.session_state["gee_link_mode"] = auth_mode


def finish_localhost_auth() -> None:
    flow = st.session_state.get("gee_flow")
    if flow is None:
        raise ValueError("Flow localhost belum dibuat. Klik tombol mulai autentikasi terlebih dahulu.")
    flow.save_code()
    reset_auth_state()


def persist_project_to_credentials(project: str) -> None:
    if not project:
        return
    cred_path = Path(ee.oauth.get_credentials_path())
    if not cred_path.exists():
        return
    payload = json.loads(cred_path.read_text(encoding="utf-8"))
    payload["project"] = project
    ee.oauth.write_private_json(str(cred_path), payload)


def render_auth_panel() -> None:
    st.subheader("1. Google Earth Engine")
    st.caption(
        "App ini mengambil data satelit umum. Isi project Earth Engine Anda, lalu autentikasi sekali."
    )
    project = st.text_input(
        "GEE Project ID",
        value=st.session_state.get("gee_project", ""),
        help="Project Google Cloud yang sudah diaktifkan untuk Earth Engine.",
    )
    st.session_state["gee_project"] = project
    remembered_project = stored_project_id()
    if remembered_project and not project.strip():
        st.info(f"Project tersimpan di kredensial: `{remembered_project}`")
    effective_project = resolve_effective_project(project)
    if effective_project:
        st.caption(f"Project efektif yang akan dipakai: `{effective_project}`")
    else:
        st.warning("Isi `GEE Project ID` terlebih dahulu.")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Mulai autentikasi web", use_container_width=True):
            try:
                if not effective_project:
                    raise ValueError(
                        "GEE Project ID masih kosong. Isi project Google Cloud yang sudah terdaftar di Earth Engine dulu."
                    )
                start_web_auth_flow("localhost")
            except Exception as exc:
                st.error(str(exc))
    with col2:
        if st.button("Cek koneksi GEE", use_container_width=True):
            ok, message = credentials_status(project)
            if ok:
                st.success(message)
            else:
                st.error(message)
    with col3:
        if st.button("Reset auth state", use_container_width=True):
            reset_auth_state()

    auth_url = st.session_state.get("gee_auth_url", "")
    if auth_url:
        if auth_url_kind(auth_url) != "google_accounts":
            st.error("Link autentikasi yang tersimpan bukan link Google Accounts.")
            st.code(auth_url, language="text")
        else:
            st.success(f"Link auth siap. Host terdeteksi: {st.session_state.get('gee_auth_url_host', '')}")
            st.markdown(f"[Buka halaman autentikasi Earth Engine]({auth_url})")
            st.caption(
                "Link yang benar harus membuka `accounts.google.com`. Setelah login selesai, kembali dan klik tombol berikut."
            )
            if st.button("Selesaikan auth localhost", use_container_width=True):
                try:
                    if not effective_project:
                        raise ValueError(
                            "GEE Project ID masih kosong. Isi project Google Cloud yang sudah terdaftar di Earth Engine dulu."
                        )
                    finish_localhost_auth()
                    persist_project_to_credentials(effective_project)
                    st.success("Token Earth Engine berhasil disimpan.")
                except Exception as exc:
                    st.error(str(exc))

    with st.expander("Diagnostik kredensial", expanded=False):
        st.json(read_credentials_snapshot())


def render_region_helper(
    project: str,
    label: str,
    country: str,
    admin_level: int,
    state_key: str,
    parent_region_name: Optional[str] = None,
    target_input_key: Optional[str] = None,
) -> None:
    with st.expander(label, expanded=False):
        caption = "Memuat daftar region dari GAUL Earth Engine agar nama wilayah cocok."
        if parent_region_name and admin_level == 2:
            caption += f" Filter parent saat ini: {parent_region_name}."
        st.caption(caption)
        if st.button(f"Muat region untuk {country} level {admin_level}", key=f"load_{state_key}"):
            try:
                effective_project = resolve_effective_project(project)
                initialize_earth_engine(project=effective_project or None, authenticate=False)
                regions = list_regions(country, admin_level, parent_region_name=parent_region_name)
                st.session_state[state_key] = regions
            except Exception as exc:
                st.error(str(exc))

        if st.session_state.get(state_key):
            selected = st.selectbox(
                "Daftar region tersedia",
                options=st.session_state[state_key],
                index=0,
                key=f"select_{state_key}",
            )
            if target_input_key and st.button("Pakai region ini", key=f"use_{state_key}"):
                st.session_state[target_input_key] = selected


def render_variable_selector() -> List[str]:
    st.subheader("3. Variabel Satelit")
    st.caption(
        "Centang variabel yang ingin Anda unduh. Untuk output mingguan/bulanan, app akan "
        "menghitung data harian dulu lalu merangkum hasilnya. Sheet grup akan menyertakan "
        "kolom utama, ringkasan min/max harian, dan jumlah hari valid."
    )
    selected_variables: List[str] = []
    default_set = set(st.session_state.get("selected_variables_general", DEFAULT_VARIABLES))
    for group_name in GROUP_ORDER:
        group_variables = [
            variable_key
            for variable_key, spec in VARIABLE_SPECS.items()
            if spec["group"] == group_name
        ]
        with st.expander(GROUP_LABELS[group_name], expanded=group_name in {"climate", "rainfall"}):
            st.caption(f"Sheet output: `{GROUP_SPECS[group_name]['sheet_name']}`")
            for variable_key in group_variables:
                checked = st.checkbox(
                    VARIABLE_SPECS[variable_key]["label"],
                    value=variable_key in default_set,
                    key=f"var_general_{variable_key}",
                )
                if checked:
                    selected_variables.append(variable_key)
    st.session_state["selected_variables_general"] = selected_variables
    return selected_variables


def main() -> None:
    st.set_page_config(page_title="Satellite Environment Downloader", layout="wide")
    ensure_session_defaults()

    st.title("Satellite Environment Downloader")
    st.write(
        "App ini khusus untuk mengunduh data satelit dan lingkungan umum per negara, provinsi, kabupaten/kota, "
        "atau boundary kustom seperti kelurahan, terpisah dari workflow dengue."
    )

    render_auth_panel()

    st.subheader("2. Wilayah dan Waktu")
    col1, col2 = st.columns(2)
    with col1:
        spatial_preset = st.selectbox(
            "Preset wilayah cepat",
            options=["manual", "bandung_kelurahan"],
            format_func=lambda mode: {
                "manual": "Manual / umum",
                "bandung_kelurahan": "Kelurahan Kota Bandung",
            }[mode],
        )
        region_name = None
        parent_region_name = None
        all_regions = False
        custom_geojson_path = None
        custom_asset_id = None
        custom_region_name_field = None
        custom_region_id_field = None
        custom_filter_field = None
        custom_filter_value = None

        if spatial_preset == "bandung_kelurahan":
            country = "Indonesia"
            boundary_mode = "custom_geojson"
            admin_level = 3
            custom_geojson_path = str(BANDUNG_KELURAHAN_GEOJSON)
            custom_region_name_field = "nama_kelurahan"
            custom_region_id_field = "id_kelurahan"
            st.text_input("Negara", value=country, disabled=True)
            st.text_input("Boundary preset", value=custom_geojson_path, disabled=True)
            if BANDUNG_KELURAHAN_GEOJSON.exists():
                boundary_summary = summarize_custom_geojson(custom_geojson_path)
                if boundary_summary:
                    st.caption(
                        f"Preset Bandung aktif: {boundary_summary['feature_count']} kelurahan dari file lokal."
                    )
            else:
                st.error(f"File preset Bandung tidak ditemukan: {BANDUNG_KELURAHAN_GEOJSON}")

            scope_mode = st.selectbox(
                "Mode kelurahan Bandung",
                options=["custom_all", "custom_single", "custom_union"],
                format_func=lambda mode: {
                    "custom_all": "Semua kelurahan",
                    "custom_single": "Satu kelurahan",
                    "custom_union": "Gabungkan semua kelurahan terpilih",
                }[mode],
            )
            kecamatan_values = geojson_property_values(custom_geojson_path, "nama_kecamatan")
            kecamatan_option = st.selectbox(
                "Filter kecamatan (opsional)",
                options=["Semua kecamatan"] + kecamatan_values,
            )
            if kecamatan_option != "Semua kecamatan":
                custom_filter_field = "nama_kecamatan"
                custom_filter_value = kecamatan_option

            if scope_mode == "custom_all":
                all_regions = True
                st.caption("Output akan menjadi data per kelurahan di Kota Bandung.")
            elif scope_mode == "custom_single":
                kelurahan_values = geojson_property_values(
                    custom_geojson_path,
                    "nama_kelurahan",
                    filter_field=custom_filter_field,
                    filter_value=custom_filter_value,
                )
                region_name = st.selectbox(
                    "Pilih kelurahan",
                    options=kelurahan_values if kelurahan_values else [""],
                )
            else:
                st.caption("Semua kelurahan yang lolos filter akan digabung menjadi satu area.")
        else:
            country = st.text_input("Negara", value="Indonesia")
            boundary_mode = st.selectbox(
                "Sumber batas wilayah",
                options=["gaul", "custom_geojson", "custom_asset"],
                format_func=lambda mode: {
                    "gaul": "GAUL bawaan Earth Engine",
                    "custom_geojson": "Boundary kustom dari GeoJSON",
                    "custom_asset": "Boundary kustom dari Earth Engine asset",
                }[mode],
            )

        if spatial_preset == "manual" and boundary_mode == "gaul":
            scope_mode = st.selectbox(
                "Mode spasial",
                options=["country", "single_region", "all_regions", "children_in_parent"],
                format_func=lambda mode: {
                    "country": "Negara sebagai satu area",
                    "single_region": "Satu wilayah",
                    "all_regions": "Semua wilayah pada level ini",
                    "children_in_parent": "Semua kabupaten/kota dalam provinsi",
                }[mode],
            )
            if scope_mode == "country":
                admin_level = 0
                st.caption("Output akan menjadi satu area tingkat negara.")
            elif scope_mode == "single_region":
                admin_level = st.selectbox(
                    "Level wilayah",
                    options=[1, 2],
                    format_func=lambda level: "Provinsi / State" if level == 1 else "Kabupaten / Kota / District",
                )
                if admin_level == 2:
                    render_region_helper(
                        project=st.session_state.get("gee_project", ""),
                        label="Bantuan pilih provinsi induk dari GEE",
                        country=country,
                        admin_level=1,
                        state_key="general_loaded_regions_parent",
                        target_input_key="general_parent_region_input",
                    )
                    parent_region_name = st.text_input(
                        "Nama provinsi induk (opsional, disarankan untuk level 2)",
                        key="general_parent_region_input",
                    )
                render_region_helper(
                    project=st.session_state.get("gee_project", ""),
                    label="Bantuan pilih wilayah dari GEE",
                    country=country,
                    admin_level=admin_level,
                    parent_region_name=parent_region_name or None,
                    state_key=f"general_loaded_regions_{admin_level}",
                    target_input_key="general_region_input",
                )
                region_name = st.text_input("Nama wilayah", key="general_region_input")
            elif scope_mode == "all_regions":
                admin_level = st.selectbox(
                    "Level output",
                    options=[1, 2],
                    format_func=lambda level: "Semua provinsi / state" if level == 1 else "Semua kabupaten / kota / district",
                )
                all_regions = True
            else:
                admin_level = 2
                all_regions = True
                render_region_helper(
                    project=st.session_state.get("gee_project", ""),
                    label="Bantuan pilih provinsi induk dari GEE",
                    country=country,
                    admin_level=1,
                    state_key="general_loaded_regions_children_parent",
                    target_input_key="general_parent_region_children_input",
                )
                parent_region_name = st.text_input(
                    "Nama provinsi induk",
                    key="general_parent_region_children_input",
                    value=st.session_state.get("general_parent_region_children_input", "Lampung"),
                )
                render_region_helper(
                    project=st.session_state.get("gee_project", ""),
                    label="Preview kabupaten/kota di dalam provinsi ini",
                    country=country,
                    admin_level=2,
                    parent_region_name=parent_region_name or None,
                    state_key="general_loaded_regions_children_preview",
                )
                st.caption("Contoh: `Indonesia` + `Lampung` untuk output per kabupaten/kota di Provinsi Lampung.")
        elif spatial_preset == "manual":
            admin_level = 3
            scope_mode = st.selectbox(
                "Mode spatial boundary kustom",
                options=["custom_all", "custom_single", "custom_union"],
                format_func=lambda mode: {
                    "custom_all": "Semua fitur pada boundary kustom",
                    "custom_single": "Satu fitur pada boundary kustom",
                    "custom_union": "Gabungkan semua fitur yang lolos filter",
                }[mode],
            )
            st.caption(
                "Mode ini cocok untuk kelurahan/desa. Dataset GAUL bawaan Earth Engine berhenti di kabupaten/kota."
            )
            if boundary_mode == "custom_geojson":
                local_boundary_path = st.text_input("Path GeoJSON lokal (opsional)")
                uploaded_boundary = st.file_uploader(
                    "Atau upload GeoJSON boundary",
                    type=["geojson", "json"],
                    key="custom_boundary_upload",
                )
                uploaded_boundary_path = save_uploaded_boundary_snapshot(uploaded_boundary)
                if uploaded_boundary_path is not None:
                    custom_geojson_path = str(uploaded_boundary_path)
                elif local_boundary_path.strip():
                    custom_geojson_path = str(Path(local_boundary_path).expanduser())
                boundary_summary = summarize_custom_geojson(custom_geojson_path)
                if boundary_summary:
                    st.caption(
                        f"Boundary termuat: {boundary_summary['feature_count']} fitur. "
                        f"Kolom properti: {', '.join(boundary_summary['property_keys'][:12]) or '-'}"
                    )
            else:
                custom_asset_id = st.text_input(
                    "Earth Engine table asset ID",
                    value="",
                    help="Contoh: projects/PROJECT/assets/bandung_kelurahan",
                )
                st.caption("Gunakan ini jika boundary kelurahan sudah diupload sebagai FeatureCollection asset di Earth Engine.")

            custom_region_name_field = st.text_input(
                "Field nama wilayah",
                value="name",
                help="Sesuaikan dengan nama kolom kelurahan pada boundary Anda, mis. name, NAMOBJ, atau KEL_NAME.",
            )
            custom_region_id_field = st.text_input(
                "Field ID wilayah (opsional)",
                value="",
                help="Opsional. Jika kosong, nama wilayah akan dipakai sebagai ID.",
            )
            custom_filter_field = st.text_input(
                "Field filter subset (opsional)",
                value="",
                help="Isi jika boundary memuat area lebih luas dan ingin difilter ke Kota Bandung.",
            )
            custom_filter_value = st.text_input(
                "Nilai filter subset (opsional)",
                value="",
                help="Contoh: Kota Bandung, BANDUNG, atau kode kota sesuai atribut boundary.",
            )

            if scope_mode == "custom_all":
                all_regions = True
                st.caption("Setiap polygon boundary akan menjadi satu wilayah output tersendiri, cocok untuk semua kelurahan di Kota Bandung.")
            elif scope_mode == "custom_single":
                region_name = st.text_input("Nama fitur / kelurahan", key="custom_boundary_region_input")
            else:
                st.caption("Semua fitur yang lolos filter akan digabung menjadi satu area tunggal.")

    with col2:
        frequency = st.selectbox("Frekuensi waktu", options=["daily", "weekly", "monthly"], index=2)
        st.caption(
            "Mode weekly/monthly dibangun dari data harian lebih dulu. "
            "Default pengamatan dibuka mulai tahun 2000 agar cocok untuk NDVI/EVI."
        )
        start_date = st.date_input(
            "Tanggal mulai",
            value=MIN_OBSERVATION_DATE,
            min_value=MIN_OBSERVATION_DATE,
            max_value=MAX_OBSERVATION_DATE,
        )
        end_date = st.date_input(
            "Tanggal akhir",
            value=date(2027, 12, 31),
            min_value=MIN_OBSERVATION_DATE,
            max_value=MAX_OBSERVATION_DATE,
        )
        wave_buffer_km = st.number_input("Buffer perairan untuk gelombang (km)", min_value=0.0, value=30.0, step=5.0)
        output_filename = st.text_input("Nama file Excel", value="satellite_environment.xlsx")
        st.caption(
            "Catatan dataset: NDVI/EVI efektif mulai sekitar 2000, polusi Sentinel-5P mulai 2018, "
            "sementara climate/rainfall bisa lebih awal. Picker waktu sekarang dibuka fleksibel 2000-2035."
        )

    custom_asset_id = (custom_asset_id or "").strip() or None
    custom_geojson_path = (custom_geojson_path or "").strip() or None
    custom_region_name_field = (custom_region_name_field or "").strip() or None
    custom_region_id_field = (custom_region_id_field or "").strip() or None
    custom_filter_field = (custom_filter_field or "").strip() or None
    custom_filter_value = (custom_filter_value or "").strip() or None

    def validate_spatial_inputs() -> None:
        if boundary_mode == "gaul":
            if scope_mode == "single_region" and not (region_name or "").strip():
                raise UserInputError("Isi nama wilayah target atau pilih dari bantuan region GEE.")
            if scope_mode == "children_in_parent" and not (parent_region_name or "").strip():
                raise UserInputError("Mode kabupaten/kota dalam provinsi membutuhkan nama provinsi induk.")
            return

        if not custom_region_name_field:
            raise UserInputError("Boundary kustom membutuhkan field nama wilayah.")
        if boundary_mode == "custom_geojson" and not custom_geojson_path:
            raise UserInputError("Pilih path GeoJSON lokal atau upload file GeoJSON boundary.")
        if boundary_mode == "custom_asset" and not custom_asset_id:
            raise UserInputError("Isi Earth Engine table asset ID untuk boundary kustom.")
        if scope_mode == "custom_single" and not (region_name or "").strip():
            raise UserInputError("Isi nama fitur/kelurahan yang ingin dipakai.")

    if st.button("Preview peta wilayah", use_container_width=True):
        try:
            validate_spatial_inputs()
            project = resolve_effective_project(st.session_state.get("gee_project", ""))
            if not project:
                raise UserInputError("GEE Project ID masih kosong.")
            initialize_earth_engine(project=project, authenticate=False)
            centroids = fetch_region_centroids(
                country=country,
                admin_level=admin_level,
                region_name=region_name,
                all_regions=all_regions,
                parent_region_name=parent_region_name,
                boundary_mode=boundary_mode,
                custom_geojson_path=custom_geojson_path,
                custom_asset_id=custom_asset_id,
                custom_region_name_field=custom_region_name_field,
                custom_region_id_field=custom_region_id_field,
                custom_filter_field=custom_filter_field,
                custom_filter_value=custom_filter_value,
            )
            if centroids.empty:
                st.warning("Tidak ada centroid wilayah yang berhasil dimuat.")
            else:
                st.success(f"Berhasil memuat {len(centroids)} centroid wilayah.")
                st.map(centroids.rename(columns={"latitude": "lat", "longitude": "lon"})[["lat", "lon"]])
                st.dataframe(centroids, use_container_width=True, height=280)
        except UserInputError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(str(exc))

    selected_variables = render_variable_selector()
    selected_groups = selected_groups_from_variables(selected_variables)
    st.caption(f"Kelompok data yang akan dipakai: {', '.join(selected_groups) if selected_groups else '-'}")

    config = SatelliteWorkbookConfig(
        area=SatelliteAreaConfig(
            country=country,
            admin_level=admin_level,
            region_name=region_name,
            parent_region_name=parent_region_name,
            all_regions=all_regions,
            boundary_mode=boundary_mode,
            custom_geojson_path=custom_geojson_path,
            custom_asset_id=custom_asset_id,
            custom_region_name_field=custom_region_name_field,
            custom_region_id_field=custom_region_id_field,
            custom_filter_field=custom_filter_field,
            custom_filter_value=custom_filter_value,
            frequency=frequency,
            start_date=start_date,
            end_date=end_date,
            wave_buffer_km=wave_buffer_km,
        ),
        selected_variables=selected_variables,
        output_filename=output_filename,
    )
    current_cache = cache_status(CACHE_ROOT, config)
    cached_selected_groups = [group for group in current_cache["completed_groups"] if group in selected_groups]
    st.caption(
        f"Cache run dir: `{current_cache['run_dir']}`. "
        f"Kelompok tersimpan: {len(cached_selected_groups)}/{len(selected_groups)}"
    )
    if cached_selected_groups:
        st.write(
            "Histori kelompok yang sudah tersimpan:",
            ", ".join(GROUP_LABELS[group] for group in cached_selected_groups),
        )

    st.subheader("5. Jalankan Proses")
    st.caption(
        "Gunakan mode background untuk VPS. Job akan tetap berjalan meski browser ditutup, dan hasilnya bisa diunduh "
        "lagi dari riwayat job."
    )
    col_run_1, col_run_2 = st.columns(2)

    if col_run_1.button("Generate di browser", type="primary", use_container_width=True):
        try:
            if not selected_variables:
                raise UserInputError("Pilih minimal satu variabel satelit.")
            validate_spatial_inputs()

            project = resolve_effective_project(st.session_state.get("gee_project", ""))
            if not project:
                raise UserInputError("GEE Project ID masih kosong.")
            initialize_earth_engine(project=project, authenticate=False)

            progress_box = st.empty()
            progress_bar = st.progress(0)

            def progress_callback(step: int, total: int, label: str, status: str) -> None:
                ratio = min(max(step / max(total, 1), 0.0), 1.0)
                progress_bar.progress(ratio)
                progress_box.info(f"[{step}/{total}] {label} - {status}")

            with st.spinner("Mengambil data satelit dan menyimpan cache per kelompok..."):
                _frames, meta, manifest = build_satellite_workbook_resumable(
                    config=config,
                    cache_root=CACHE_ROOT,
                    progress_callback=progress_callback,
                )
                workbook_path = Path(manifest["compiled_workbook_path"])
                workbook_bytes = workbook_path.read_bytes()

            st.success("Workbook satelit berhasil dibuat.")
            st.json(meta)
            if meta.get("coverage"):
                st.dataframe(pd.DataFrame(meta["coverage"]), use_container_width=True)
            st.download_button(
                "Download Excel",
                data=workbook_bytes,
                file_name=output_filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
            st.caption(f"Workbook final tersimpan di: {workbook_path}")
        except UserInputError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(str(exc))
            st.code(traceback.format_exc(), language="python")

    if col_run_2.button("Jalankan di background VPS", use_container_width=True):
        try:
            if not selected_variables:
                raise UserInputError("Pilih minimal satu variabel satelit.")
            validate_spatial_inputs()
            project = resolve_effective_project(st.session_state.get("gee_project", ""))
            if not project:
                raise UserInputError("GEE Project ID masih kosong.")
            job_dir = create_background_job(config, project)
            launch_background_job(job_dir)
            st.success(
                "Job background berhasil dibuat. Anda bisa menutup browser; proses akan tetap berjalan di VPS. "
                f"Job ID: `{job_dir.name}`"
            )
        except UserInputError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(str(exc))
            st.code(traceback.format_exc(), language="python")

    render_job_history()


if __name__ == "__main__":
    main()
