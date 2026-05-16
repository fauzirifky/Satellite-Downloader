from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

try:
    from .export_satellite_support import (
        DATASET_ROWS,
        GROUP_OUTPUT_COLUMNS,
        build_periods,
        collect_support_dataframe,
        output_columns_for_metric,
        resolve_regions,
    )
except ImportError:
    from export_satellite_support import (
        DATASET_ROWS,
        GROUP_OUTPUT_COLUMNS,
        build_periods,
        collect_support_dataframe,
        output_columns_for_metric,
        resolve_regions,
    )


CACHE_MANIFEST_NAME = "manifest.json"
FINAL_SHEET_NAME = "Selected_Data"
COMMON_COLUMNS = [
    "country",
    "region_name",
    "region_id",
    "gaul_code",
    "admin_level",
    "frequency",
    "period_start",
    "period_end",
    "period_label",
]

GROUP_SPECS: Dict[str, Dict[str, Any]] = {
    "climate": {
        "sheet_name": "Climate",
        "columns": GROUP_OUTPUT_COLUMNS["climate"],
    },
    "rainfall": {
        "sheet_name": "Rainfall",
        "columns": GROUP_OUTPUT_COLUMNS["rainfall"],
    },
    "ndvi": {
        "sheet_name": "NDVI",
        "columns": GROUP_OUTPUT_COLUMNS["ndvi"],
    },
    "evi": {
        "sheet_name": "EVI",
        "columns": GROUP_OUTPUT_COLUMNS["evi"],
    },
    "pollution": {
        "sheet_name": "Pollution",
        "columns": GROUP_OUTPUT_COLUMNS["pollution"],
    },
    "wave": {
        "sheet_name": "Wave",
        "columns": GROUP_OUTPUT_COLUMNS["wave"],
    },
}

VARIABLE_SPECS: Dict[str, Dict[str, str]] = {
    "temp_mean": {"group": "climate", "column": "climate_temp_mean_c", "label": "Suhu rata-rata"},
    "temp_min": {"group": "climate", "column": "climate_temp_min_c", "label": "Suhu minimum"},
    "temp_max": {"group": "climate", "column": "climate_temp_max_c", "label": "Suhu maksimum"},
    "dewpoint": {"group": "climate", "column": "climate_dewpoint_mean_c", "label": "Dew point"},
    "humidity": {"group": "climate", "column": "climate_relative_humidity_pct", "label": "Kelembaban relatif"},
    "wind_speed": {"group": "climate", "column": "climate_wind_speed_10m_ms", "label": "Kecepatan angin 10m"},
    "pressure": {"group": "climate", "column": "climate_mean_sea_level_pressure_pa", "label": "Tekanan permukaan laut"},
    "solar_radiation": {
        "group": "climate",
        "column": "climate_solar_radiation_flux_w_m2",
        "label": "Radiasi matahari",
    },
    "rainfall": {"group": "rainfall", "column": "rainfall_chirps_mm", "label": "Curah hujan"},
    "ndvi": {"group": "ndvi", "column": "ndvi_mean", "label": "NDVI"},
    "evi": {"group": "evi", "column": "evi_mean", "label": "EVI"},
    "no2": {"group": "pollution", "column": "pollution_no2_tropo_mol_m2", "label": "NO2 troposfer"},
    "co": {"group": "pollution", "column": "pollution_co_mol_m2", "label": "CO"},
    "aerosol": {"group": "pollution", "column": "pollution_aerosol_index", "label": "Aerosol index"},
    "wave_height": {"group": "wave", "column": "wave_sig_height_m", "label": "Tinggi gelombang signifikan"},
    "wave_period": {"group": "wave", "column": "wave_mean_period_s", "label": "Periode gelombang"},
}

GROUP_ORDER = ["climate", "rainfall", "ndvi", "evi", "pollution", "wave"]


@dataclass
class SatelliteAreaConfig:
    country: str
    admin_level: int = 0
    region_name: Optional[str] = None
    parent_region_name: Optional[str] = None
    all_regions: bool = False
    boundary_mode: str = "gaul"
    custom_geojson_path: Optional[str] = None
    custom_asset_id: Optional[str] = None
    custom_region_name_field: Optional[str] = None
    custom_region_id_field: Optional[str] = None
    custom_filter_field: Optional[str] = None
    custom_filter_value: Optional[str] = None
    frequency: str = "monthly"
    aggregation_strategy: str = "auto"
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    wave_buffer_km: float = 50.0


@dataclass
class SatelliteWorkbookConfig:
    area: SatelliteAreaConfig
    selected_variables: List[str]
    output_filename: str = "satellite_environment.xlsx"


def area_config_to_payload(area: SatelliteAreaConfig) -> Dict[str, Any]:
    return {
        "country": area.country,
        "admin_level": area.admin_level,
        "region_name": area.region_name,
        "parent_region_name": area.parent_region_name,
        "all_regions": area.all_regions,
        "boundary_mode": area.boundary_mode,
        "custom_geojson_path": area.custom_geojson_path,
        "custom_asset_id": area.custom_asset_id,
        "custom_region_name_field": area.custom_region_name_field,
        "custom_region_id_field": area.custom_region_id_field,
        "custom_filter_field": area.custom_filter_field,
        "custom_filter_value": area.custom_filter_value,
        "frequency": area.frequency,
        "aggregation_strategy": area.aggregation_strategy,
        "start_date": area.start_date.isoformat() if area.start_date else None,
        "end_date": area.end_date.isoformat() if area.end_date else None,
        "wave_buffer_km": area.wave_buffer_km,
    }


def area_config_from_payload(payload: Dict[str, Any]) -> SatelliteAreaConfig:
    start_date = payload.get("start_date")
    end_date = payload.get("end_date")
    return SatelliteAreaConfig(
        country=payload["country"],
        admin_level=int(payload.get("admin_level", 0)),
        region_name=payload.get("region_name"),
        parent_region_name=payload.get("parent_region_name"),
        all_regions=bool(payload.get("all_regions", False)),
        boundary_mode=payload.get("boundary_mode", "gaul"),
        custom_geojson_path=payload.get("custom_geojson_path"),
        custom_asset_id=payload.get("custom_asset_id"),
        custom_region_name_field=payload.get("custom_region_name_field"),
        custom_region_id_field=payload.get("custom_region_id_field"),
        custom_filter_field=payload.get("custom_filter_field"),
        custom_filter_value=payload.get("custom_filter_value"),
        frequency=payload.get("frequency", "monthly"),
        aggregation_strategy=payload.get("aggregation_strategy", "auto"),
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
        wave_buffer_km=float(payload.get("wave_buffer_km", 50.0)),
    )


def workbook_config_to_payload(config: SatelliteWorkbookConfig) -> Dict[str, Any]:
    return {
        "area": area_config_to_payload(config.area),
        "selected_variables": list(config.selected_variables),
        "output_filename": config.output_filename,
    }


def workbook_config_from_payload(payload: Dict[str, Any]) -> SatelliteWorkbookConfig:
    return SatelliteWorkbookConfig(
        area=area_config_from_payload(payload["area"]),
        selected_variables=list(payload.get("selected_variables", [])),
        output_filename=payload.get("output_filename", "satellite_environment.xlsx"),
    )


def selected_groups_from_variables(selected_variables: Iterable[str]) -> List[str]:
    groups = []
    seen = set()
    for variable_key in selected_variables:
        group = VARIABLE_SPECS[variable_key]["group"]
        if group not in seen:
            groups.append(group)
            seen.add(group)
    return [group for group in GROUP_ORDER if group in seen]


def workbook_cache_payload(config: SatelliteWorkbookConfig) -> Dict[str, Any]:
    return {
        "area": area_config_to_payload(config.area),
    }


def workbook_cache_key(config: SatelliteWorkbookConfig) -> str:
    payload = workbook_cache_payload(config)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()[:16]


def workbook_cache_dir(cache_root: Path, config: SatelliteWorkbookConfig) -> Path:
    return cache_root / workbook_cache_key(config)


def read_cache_manifest(run_dir: Path) -> Dict[str, Any]:
    manifest_path = run_dir / CACHE_MANIFEST_NAME
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def write_cache_manifest(run_dir: Path, manifest: Dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / CACHE_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")


def group_sheet_path(run_dir: Path, group_name: str) -> Path:
    return run_dir / f"{GROUP_SPECS[group_name]['sheet_name']}.xlsx"


def write_single_sheet_excel(frame: pd.DataFrame, target_path: Path, sheet_name: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)


def read_single_sheet_excel(target_path: Path) -> pd.DataFrame:
    return pd.read_excel(target_path)


def expected_group_columns(group_name: str) -> List[str]:
    return COMMON_COLUMNS + GROUP_SPECS[group_name]["columns"]


def ensure_group_columns(frame: pd.DataFrame, group_name: str) -> pd.DataFrame:
    result = frame.copy()
    for column in expected_group_columns(group_name):
        if column not in result.columns:
            result[column] = pd.NA
    return result[expected_group_columns(group_name)].copy()


def is_valid_group_frame(frame: pd.DataFrame, group_name: str) -> bool:
    return all(column in frame.columns for column in GROUP_SPECS[group_name]["columns"])


def load_valid_cached_group_frame(target_path: Path, group_name: str) -> Optional[pd.DataFrame]:
    if not target_path.exists():
        return None
    frame = read_single_sheet_excel(target_path)
    if not is_valid_group_frame(frame, group_name):
        return None
    return ensure_group_columns(frame, group_name)


def cache_status(cache_root: Path, config: SatelliteWorkbookConfig) -> Dict[str, Any]:
    run_dir = workbook_cache_dir(cache_root, config)
    completed_groups = []
    for group_name in GROUP_ORDER:
        if load_valid_cached_group_frame(group_sheet_path(run_dir, group_name), group_name) is not None:
            completed_groups.append(group_name)
    return {
        "run_dir": str(run_dir),
        "cache_key": run_dir.name,
        "completed_groups": completed_groups,
        "manifest": read_cache_manifest(run_dir),
    }


def build_group_frame(config: SatelliteWorkbookConfig, group_name: str) -> pd.DataFrame:
    periods = build_periods(
        config.area.start_date,
        config.area.end_date,
        config.area.frequency,
    )
    regions_fc = resolve_regions(
        country=config.area.country,
        admin_level=config.area.admin_level,
        region_name=config.area.region_name,
        all_regions=config.area.all_regions,
        parent_region_name=config.area.parent_region_name,
        boundary_mode=config.area.boundary_mode,
        custom_geojson_path=config.area.custom_geojson_path,
        custom_asset_id=config.area.custom_asset_id,
        custom_region_name_field=config.area.custom_region_name_field,
        custom_region_id_field=config.area.custom_region_id_field,
        custom_filter_field=config.area.custom_filter_field,
        custom_filter_value=config.area.custom_filter_value,
    )
    namespace = SimpleNamespace(
        frequency=config.area.frequency,
        aggregation_strategy=config.area.aggregation_strategy,
        boundary_mode=config.area.boundary_mode,
        admin_level=config.area.admin_level,
        wave_buffer_km=config.area.wave_buffer_km,
    )
    frame = collect_support_dataframe(
        args=namespace,
        periods=periods,
        regions_fc=regions_fc,
        selected_groups=[group_name],
    )
    keep_columns = [
        column for column in COMMON_COLUMNS + GROUP_SPECS[group_name]["columns"] if column in frame.columns
    ]
    result = frame[keep_columns].copy()
    if "period_start" in result.columns:
        result["period_start"] = pd.to_datetime(result["period_start"])
    if "period_end" in result.columns:
        result["period_end"] = pd.to_datetime(result["period_end"])
    return ensure_group_columns(result, group_name)


def trim_group_frame_to_selected_variables(
    frame: pd.DataFrame,
    selected_variables: Iterable[str],
    group_name: str,
) -> pd.DataFrame:
    selected_columns: List[str] = []
    for variable_key in selected_variables:
        spec = VARIABLE_SPECS[variable_key]
        if spec["group"] != group_name:
            continue
        for column in output_columns_for_metric(spec["column"]):
            if column not in selected_columns:
                selected_columns.append(column)
    keep_columns = [column for column in COMMON_COLUMNS + selected_columns if column in frame.columns]
    return frame[keep_columns].copy()


def build_selected_data_sheet(
    group_frames: Dict[str, pd.DataFrame],
    selected_variables: Iterable[str],
) -> pd.DataFrame:
    merged: Optional[pd.DataFrame] = None
    merge_keys = COMMON_COLUMNS.copy()
    for group_name in GROUP_ORDER:
        if group_name not in group_frames:
            continue
        group_frame = trim_group_frame_to_selected_variables(group_frames[group_name], selected_variables, group_name)
        if merged is None:
            merged = group_frame.copy()
        else:
            add_columns = [
                column
                for column in group_frame.columns
                if column not in merge_keys and column not in merged.columns
            ]
            merged = merged.merge(
                group_frame[merge_keys + add_columns],
                how="outer",
                on=merge_keys,
            )
    if merged is None:
        return pd.DataFrame(columns=COMMON_COLUMNS)
    return merged.sort_values(["region_name", "period_start"]).reset_index(drop=True)


def metadata_frame(config: SatelliteWorkbookConfig) -> pd.DataFrame:
    selected_groups = selected_groups_from_variables(config.selected_variables)
    rows = [
        {"key": "country", "value": config.area.country},
        {"key": "boundary_mode", "value": config.area.boundary_mode},
        {"key": "admin_level", "value": config.area.admin_level},
        {"key": "region_name", "value": config.area.region_name or ""},
        {"key": "parent_region_name", "value": config.area.parent_region_name or ""},
        {"key": "all_regions", "value": config.area.all_regions},
        {"key": "custom_geojson_path", "value": config.area.custom_geojson_path or ""},
        {"key": "custom_asset_id", "value": config.area.custom_asset_id or ""},
        {"key": "custom_region_name_field", "value": config.area.custom_region_name_field or ""},
        {"key": "custom_region_id_field", "value": config.area.custom_region_id_field or ""},
        {"key": "custom_filter_field", "value": config.area.custom_filter_field or ""},
        {"key": "custom_filter_value", "value": config.area.custom_filter_value or ""},
        {"key": "frequency", "value": config.area.frequency},
        {"key": "aggregation_strategy", "value": config.area.aggregation_strategy},
        {"key": "start_date", "value": config.area.start_date.isoformat() if config.area.start_date else ""},
        {"key": "end_date", "value": config.area.end_date.isoformat() if config.area.end_date else ""},
        {"key": "wave_buffer_km", "value": config.area.wave_buffer_km},
        {"key": "selected_groups", "value": ", ".join(selected_groups)},
        {"key": "selected_variables", "value": ", ".join(config.selected_variables)},
    ]
    return pd.DataFrame(rows)


def coverage_frame(
    group_frames: Dict[str, pd.DataFrame],
    selected_variables: Iterable[str],
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for variable_key in selected_variables:
        spec = VARIABLE_SPECS[variable_key]
        group_name = spec["group"]
        column = spec["column"]
        frame = group_frames.get(group_name)
        if frame is None or column not in frame.columns:
            rows.append(
                {
                    "variable_key": variable_key,
                    "label": spec["label"],
                    "group": group_name,
                    "column": column,
                    "row_count": 0,
                    "non_null_count": 0,
                    "non_null_pct": 0.0,
                }
            )
            continue
        row_count = len(frame)
        non_null_count = int(frame[column].notna().sum())
        rows.append(
            {
                "variable_key": variable_key,
                "label": spec["label"],
                "group": group_name,
                "column": column,
                "row_count": row_count,
                "non_null_count": non_null_count,
                "non_null_pct": (non_null_count / row_count * 100.0) if row_count else 0.0,
            }
        )
    return pd.DataFrame(rows)


def workbook_bytes_from_frames(frames: Dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for sheet_name, frame in frames.items():
            frame.to_excel(writer, sheet_name=sheet_name[:31], index=False)
    buffer.seek(0)
    return buffer.getvalue()


def build_satellite_workbook_resumable(
    config: SatelliteWorkbookConfig,
    cache_root: Path,
    progress_callback: Optional[Callable[[int, int, str, str], None]] = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Any], Dict[str, Any]]:
    selected_groups = selected_groups_from_variables(config.selected_variables)
    total_steps = len(selected_groups) + 1
    run_dir = workbook_cache_dir(cache_root, config)
    manifest = read_cache_manifest(run_dir)
    manifest.update(
        {
            "cache_key": run_dir.name,
            "run_dir": str(run_dir),
            "output_filename": config.output_filename,
            "selected_groups": selected_groups,
            "selected_variables": config.selected_variables,
            "config": workbook_cache_payload(config),
        }
    )
    write_cache_manifest(run_dir, manifest)

    raw_group_frames: Dict[str, pd.DataFrame] = {}
    completed_groups: List[str] = []

    for step_index, group_name in enumerate(selected_groups, start=1):
        sheet_name = GROUP_SPECS[group_name]["sheet_name"]
        cached_sheet_path = group_sheet_path(run_dir, group_name)
        cached_frame = load_valid_cached_group_frame(cached_sheet_path, group_name)
        if cached_frame is not None:
            if progress_callback:
                progress_callback(step_index, total_steps, sheet_name, "memakai cache")
            raw_group_frames[group_name] = cached_frame
            completed_groups.append(group_name)
            continue

        if progress_callback:
            status = "mengambil data"
            if cached_sheet_path.exists():
                status = "cache lama tidak valid, menghitung ulang"
            progress_callback(step_index, total_steps, sheet_name, status)
        frame = build_group_frame(config, group_name)
        write_single_sheet_excel(frame, cached_sheet_path, sheet_name)
        raw_group_frames[group_name] = frame
        completed_groups.append(group_name)
        manifest["completed_groups"] = completed_groups
        write_cache_manifest(run_dir, manifest)

    frames: Dict[str, pd.DataFrame] = {}
    frames[FINAL_SHEET_NAME] = build_selected_data_sheet(raw_group_frames, config.selected_variables)
    for group_name in selected_groups:
        sheet_name = GROUP_SPECS[group_name]["sheet_name"]
        frames[sheet_name] = trim_group_frame_to_selected_variables(
            raw_group_frames[group_name],
            config.selected_variables,
            group_name,
        )
    frames["Coverage"] = coverage_frame(raw_group_frames, config.selected_variables)
    frames["Metadata"] = metadata_frame(config)
    frames["Sources"] = pd.DataFrame(
        [row for row in DATASET_ROWS if row["group"] in set(selected_groups) or row["group"] == "boundaries"]
    )

    if progress_callback:
        progress_callback(total_steps, total_steps, config.output_filename, "mengompilasi workbook")
    workbook_bytes = workbook_bytes_from_frames(frames)
    compiled_workbook_path = run_dir / config.output_filename
    compiled_workbook_path.write_bytes(workbook_bytes)

    manifest["completed_groups"] = completed_groups
    manifest["compiled_workbook_path"] = str(compiled_workbook_path)
    manifest["sheet_names"] = list(frames.keys())
    write_cache_manifest(run_dir, manifest)

    meta = {
        "sheet_names": list(frames.keys()),
        "selected_groups": selected_groups,
        "selected_variables": config.selected_variables,
        "coverage": frames["Coverage"].to_dict(orient="records"),
        "cache_key": run_dir.name,
        "cache_dir": str(run_dir),
        "compiled_workbook_path": str(compiled_workbook_path),
    }
    return frames, meta, manifest
