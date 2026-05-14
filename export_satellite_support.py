#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import ee
import pandas as pd


ONE_DAY = timedelta(days=1)
ERA5_START = date(1940, 1, 1)
CHIRPS_START = date(1981, 1, 1)
MODIS_START = date(2000, 2, 24)
S5P_START = date(2018, 7, 4)

GAUL_LEVEL_DATASETS = {
    1: "FAO/GAUL/2015/level1",
    2: "FAO/GAUL/2015/level2",
}
LEVEL_NAME_FIELDS = {
    1: "ADM1_NAME",
    2: "ADM2_NAME",
}
LEVEL_CODE_FIELDS = {
    1: "ADM1_CODE",
    2: "ADM2_CODE",
}
PARENT_NAME_FIELDS = {
    2: "ADM1_NAME",
}

SCALE_BY_GROUP = {
    "climate": 27_830,
    "rainfall": 5_566,
    "ndvi": 1_000,
    "evi": 500,
    "wave": 27_830,
    "pollution": 5_000,
}
DEFAULT_PERIOD_BATCH_SIZE = 52
DEFAULT_PERIOD_BATCH_SIZE_BY_GROUP = {
    "climate": 52,
    "rainfall": 52,
    "ndvi": 26,
    "evi": 26,
    "wave": 8,
    "pollution": 26,
}
TILE_SCALE_BY_GROUP = {
    "climate": 4,
    "rainfall": 4,
    "ndvi": 4,
    "evi": 4,
    "wave": 8,
    "pollution": 4,
}

GROUP_BANDS = {
    "climate": [
        "climate_temp_mean_c",
        "climate_temp_min_c",
        "climate_temp_max_c",
        "climate_dewpoint_mean_c",
        "climate_relative_humidity_pct",
        "climate_wind_speed_10m_ms",
        "climate_mean_sea_level_pressure_pa",
        "climate_solar_radiation_flux_w_m2",
    ],
    "rainfall": ["rainfall_chirps_mm"],
    "ndvi": ["ndvi_mean"],
    "evi": ["evi_mean"],
    "wave": ["wave_sig_height_m", "wave_mean_period_s"],
    "pollution": [
        "pollution_no2_tropo_mol_m2",
        "pollution_co_mol_m2",
        "pollution_aerosol_index",
    ],
}

PRIMARY_AGGREGATION_BY_METRIC = {
    "rainfall_chirps_mm": "sum",
}

IGNORE_REDUCED_KEYS = {
    "system:index",
    "country",
    "region_id",
    "region_name",
    "admin_level",
    "gaul_code",
}


def metric_summary_columns(metric_name: str) -> List[str]:
    columns: List[str] = []
    if PRIMARY_AGGREGATION_BY_METRIC.get(metric_name) == "sum":
        columns.append(f"{metric_name}_daily_mean")
    columns.extend(
        [
            f"{metric_name}_daily_min",
            f"{metric_name}_daily_max",
            f"{metric_name}_valid_days",
        ]
    )
    return columns


def output_columns_for_metric(metric_name: str) -> List[str]:
    return [metric_name] + metric_summary_columns(metric_name)


def group_output_columns(group_name: str) -> List[str]:
    columns: List[str] = []
    for metric_name in GROUP_BANDS[group_name]:
        columns.extend(output_columns_for_metric(metric_name))
    return columns


GROUP_OUTPUT_COLUMNS = {
    group_name: group_output_columns(group_name)
    for group_name in GROUP_BANDS
}

DATASET_ROWS = [
    {
        "group": "climate",
        "dataset_id": "ECMWF/ERA5/HOURLY",
        "variables": "temperature_2m, dewpoint_temperature_2m, u_component_of_wind_10m, v_component_of_wind_10m, mean_sea_level_pressure",
        "notes": "Reanalysis pendukung iklim; diringkas harian dulu dari data hourly, lalu diagregasi ke weekly/monthly bila diminta.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_HOURLY",
    },
    {
        "group": "rainfall",
        "dataset_id": "UCSB-CHG/CHIRPS/DAILY",
        "variables": "precipitation",
        "notes": "Curah hujan harian berbasis satelit + stasiun; weekly/monthly dihitung dari rangkaian harian.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/UCSB-CHG_CHIRPS_DAILY",
    },
    {
        "group": "ndvi",
        "dataset_id": "MODIS/061/MOD09GA",
        "variables": "sur_refl_b01, sur_refl_b02, state_1km",
        "notes": "NDVI dihitung harian dari reflectance merah dan NIR setelah masking awan sederhana, lalu diringkas ke weekly/monthly bila diminta.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD09GA",
    },
    {
        "group": "evi",
        "dataset_id": "MODIS/MOD09GA_006_EVI",
        "variables": "EVI",
        "notes": "Enhanced Vegetation Index (EVI) harian dari MODIS, lalu diringkas ke weekly/monthly bila diminta.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/MODIS_MOD09GA_006_EVI",
    },
    {
        "group": "wave",
        "dataset_id": "ECMWF/ERA5/HOURLY",
        "variables": "significant_height_of_combined_wind_waves_and_swell, mean_wave_period",
        "notes": "Gelombang laut diambil dari field gelombang ERA5 hourly, diringkas harian dulu, lalu diagregasi ke weekly/monthly bila diminta.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/ECMWF_ERA5_HOURLY",
    },
    {
        "group": "pollution",
        "dataset_id": "COPERNICUS/S5P/OFFL/L3_NO2",
        "variables": "tropospheric_NO2_column_number_density",
        "notes": "Polusi udara NO2 troposfer dari Sentinel-5P; weekly/monthly dibangun dari observasi harian yang tersedia.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_NO2",
    },
    {
        "group": "pollution",
        "dataset_id": "COPERNICUS/S5P/OFFL/L3_CO",
        "variables": "CO_column_number_density",
        "notes": "Polusi udara CO dari Sentinel-5P; weekly/monthly dibangun dari observasi harian yang tersedia.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_CO",
    },
    {
        "group": "pollution",
        "dataset_id": "COPERNICUS/S5P/OFFL/L3_AER_AI",
        "variables": "absorbing_aerosol_index",
        "notes": "Proxy aerosol/udara kotor dari Sentinel-5P; weekly/monthly dibangun dari observasi harian yang tersedia.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_AER_AI",
    },
    {
        "group": "boundaries",
        "dataset_id": "FAO/GAUL/2015/level1 and level2, atau boundary kustom GeoJSON/EE asset",
        "variables": "ADM0_NAME, ADM1_NAME, ADM2_NAME, atau field custom seperti nama kelurahan",
        "notes": "GAUL bawaan Earth Engine cocok sampai kabupaten/kota. Untuk kelurahan/desa gunakan boundary kustom dari GeoJSON atau Earth Engine table asset.",
        "docs_url": "https://developers.google.com/earth-engine/datasets/catalog/FAO_GAUL_2015_level2",
    },
]


@dataclass(frozen=True)
class Period:
    start: date
    end_exclusive: date

    @property
    def end_inclusive(self) -> date:
        return self.end_exclusive - ONE_DAY

    @property
    def label(self) -> str:
        return f"{self.start.isoformat()}__{self.end_inclusive.isoformat()}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ambil time series satelit/reanalysis pendukung dengue "
            "dan ekspor langsung ke Excel."
        )
    )
    parser.add_argument("--country", required=True, help="Nama negara sesuai GAUL, mis. 'Sri Lanka'.")
    parser.add_argument(
        "--region-name",
        help="Nama daerah pada admin level yang dipilih, mis. 'Colombo' atau 'Western'.",
    )
    parser.add_argument(
        "--admin-level",
        type=int,
        choices=[0, 1, 2],
        default=1,
        help="0 = negara, 1 = provinsi/state, 2 = district/kabupaten/kota.",
    )
    parser.add_argument(
        "--parent-region-name",
        help="Opsional. Nama region induk, mis. provinsi untuk membatasi daftar kabupaten/kota.",
    )
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Ambil semua daerah pada admin level yang dipilih dalam negara tersebut.",
    )
    parser.add_argument(
        "--list-regions",
        action="store_true",
        help="Tampilkan daftar nama daerah yang tersedia lalu selesai.",
    )
    parser.add_argument("--start-date", help="Tanggal awal inklusif, format YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Tanggal akhir inklusif, format YYYY-MM-DD.")
    parser.add_argument(
        "--frequency",
        choices=["daily", "weekly", "monthly"],
        default="monthly",
        help="Resolusi waktu keluaran.",
    )
    parser.add_argument("--output", help="Path file Excel keluaran, mis. outputs/sri_lanka.xlsx")
    parser.add_argument(
        "--gee-project",
        help="Google Cloud project untuk Earth Engine. Jika sudah diset via earthengine set_project, boleh dikosongkan.",
    )
    parser.add_argument(
        "--authenticate",
        action="store_true",
        help="Panggil flow autentikasi Earth Engine sebelum inisialisasi.",
    )
    parser.add_argument(
        "--wave-buffer-km",
        type=float,
        default=50.0,
        help="Buffer pesisir untuk statistik gelombang. 0 untuk tanpa buffer.",
    )
    parser.add_argument(
        "--beach-slope-beta",
        type=float,
        help=(
            "Opsional. Kemiringan pantai untuk menghitung proxy run-up "
            "Stockdon berbasis Hs dan periode gelombang."
        ),
    )
    parser.add_argument("--dengue-file", help="CSV/XLSX dengue opsional untuk digabungkan.")
    parser.add_argument("--dengue-sheet", help="Nama sheet jika file dengue adalah Excel.")
    parser.add_argument(
        "--dengue-date-col",
        default="date",
        help="Nama kolom tanggal pada file dengue.",
    )
    parser.add_argument(
        "--dengue-region-col",
        help="Nama kolom wilayah pada file dengue. Jika kosong, digabung per tanggal saja.",
    )
    parser.add_argument(
        "--dengue-value-col",
        default="dengue_cases",
        help="Nama kolom nilai/kasus dengue pada file dengue.",
    )
    parser.add_argument(
        "--dengue-agg",
        choices=["sum", "mean", "max", "min"],
        default="sum",
        help="Cara agregasi dengue saat resolusi mingguan/bulanan.",
    )
    args = parser.parse_args()
    validate_args(args, parser)
    return args


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.region_name and args.all_regions:
        parser.error("Gunakan salah satu: --region-name atau --all-regions, bukan keduanya.")

    if not args.list_regions and (not args.start_date or not args.end_date):
        parser.error("--start-date dan --end-date wajib jika tidak memakai --list-regions.")

    if not args.list_regions and not args.output:
        parser.error("--output wajib jika tidak memakai --list-regions.")

    if args.beach_slope_beta is not None and not (0 < args.beach_slope_beta <= 1):
        parser.error("--beach-slope-beta harus berada di interval (0, 1].")


def parse_iso_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def first_day_next_month(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def build_periods(start_date: date, end_date: date, frequency: str) -> List[Period]:
    if end_date < start_date:
        raise ValueError("Tanggal akhir harus >= tanggal awal.")

    periods: List[Period] = []
    cursor = start_date
    final_exclusive = end_date + ONE_DAY

    while cursor < final_exclusive:
        if frequency == "daily":
            next_cursor = cursor + ONE_DAY
        elif frequency == "weekly":
            next_cursor = cursor + timedelta(days=7)
        elif frequency == "monthly":
            next_cursor = first_day_next_month(cursor)
        else:
            raise ValueError(f"Frequency tidak dikenal: {frequency}")

        next_cursor = min(next_cursor, final_exclusive)
        periods.append(Period(start=cursor, end_exclusive=next_cursor))
        cursor = next_cursor

    return periods


def initialize_earth_engine(project: Optional[str], authenticate: bool) -> None:
    try:
        if authenticate:
            ee.Authenticate()
        if project:
            ee.Initialize(project=project)
        else:
            ee.Initialize()
    except Exception as exc:  # pragma: no cover - bergantung environment pengguna
        detail = str(exc)
        if "Not signed up for Earth Engine or project is not registered" in detail:
            hint = (
                "Gagal inisialisasi Earth Engine. Project GEE belum terdaftar untuk Earth Engine "
                "atau Earth Engine API belum aktif. Pastikan Anda memakai Google Cloud project yang "
                "sudah diregistrasi untuk Earth Engine dan isi project itu di app atau lewat "
                "'earthengine set_project <PROJECT_ID>'. "
                "Panduan resmi: https://developers.google.com/earth-engine/guides/access"
            )
        else:
            hint = (
                "Gagal inisialisasi Earth Engine. Jalankan ulang dengan --authenticate "
                "atau lakukan 'earthengine authenticate' dan set project dengan "
                "'earthengine set_project <PROJECT_ID>' atau gunakan --gee-project."
            )
        hint = f"{hint}\nDetail asli: {detail}"
        raise RuntimeError(hint) from exc


def build_country_filter(country: str) -> ee.Filter:
    return ee.Filter.eq("ADM0_NAME", country)


def build_parent_region_filter(admin_level: int, parent_region_name: Optional[str]) -> Optional[ee.Filter]:
    if not parent_region_name:
        return None
    parent_field = PARENT_NAME_FIELDS.get(admin_level)
    if not parent_field:
        return None
    return ee.Filter.eq(parent_field, parent_region_name)


def load_geojson_object(geojson_path: str) -> Dict[str, Any]:
    path = Path(geojson_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"File GeoJSON tidak ditemukan: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection":
        raise ValueError("GeoJSON harus bertipe FeatureCollection.")
    return payload


def geojson_feature_list(geojson_object: Dict[str, Any]) -> List[Dict[str, Any]]:
    features = geojson_object.get("features", [])
    if not isinstance(features, list) or not features:
        raise ValueError("GeoJSON tidak memiliki fitur polygon yang bisa dipakai.")
    return features


def normalize_custom_property_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def build_custom_filter_predicate(
    filter_field: Optional[str],
    filter_value: Optional[str],
):
    if not filter_field or filter_value is None or str(filter_value).strip() == "":
        return None
    wanted = str(filter_value).strip()

    def _predicate(feature_dict: Dict[str, Any]) -> bool:
        properties = feature_dict.get("properties", {}) or {}
        return normalize_custom_property_value(properties.get(filter_field)) == wanted

    return _predicate


def standardize_custom_region_feature(
    feature: ee.Feature,
    country: str,
    region_name_field: str,
    region_id_field: Optional[str],
    admin_level: int,
) -> ee.Feature:
    feature = ee.Feature(feature)
    region_name_value = ee.String(feature.get(region_name_field))
    region_id_source = region_id_field or region_name_field
    region_id_value = ee.String(feature.get(region_id_source))
    return (
        ee.Feature(feature.geometry())
        .copyProperties(feature)
        .set("country", country)
        .set("region_name", region_name_value)
        .set("region_id", region_id_value)
        .set("gaul_code", region_id_value)
        .set("admin_level", admin_level)
    )


def build_custom_regions_from_geojson(
    geojson_path: str,
    country: str,
    region_name_field: str,
    region_id_field: Optional[str],
    region_name: Optional[str],
    all_regions: bool,
    filter_field: Optional[str],
    filter_value: Optional[str],
    admin_level: int = 3,
) -> ee.FeatureCollection:
    geojson_object = load_geojson_object(geojson_path)
    features = geojson_feature_list(geojson_object)
    filter_predicate = build_custom_filter_predicate(filter_field, filter_value)
    if filter_predicate is not None:
        features = [feature for feature in features if filter_predicate(feature)]
    if region_name:
        wanted_name = region_name.strip()
        features = [
            feature
            for feature in features
            if normalize_custom_property_value((feature.get("properties") or {}).get(region_name_field)) == wanted_name
        ]
    if not features:
        raise ValueError("Tidak ada fitur custom boundary yang cocok dengan filter saat ini.")

    ee_features: List[ee.Feature] = []
    for feature_dict in features:
        properties = feature_dict.get("properties", {}) or {}
        geometry = feature_dict.get("geometry")
        if not geometry:
            raise ValueError("Salah satu fitur GeoJSON tidak memiliki geometry.")
        name_value = normalize_custom_property_value(properties.get(region_name_field))
        if not name_value:
            raise ValueError(f"Field nama wilayah '{region_name_field}' tidak ditemukan pada salah satu fitur.")
        region_id_source = region_id_field or region_name_field
        region_id_value = normalize_custom_property_value(properties.get(region_id_source)) or name_value
        ee_feature = ee.Feature(ee.Geometry(geometry), properties).set(
            "country",
            country,
            "region_name",
            name_value,
            "region_id",
            region_id_value,
            "gaul_code",
            region_id_value,
            "admin_level",
            admin_level,
        )
        ee_features.append(ee_feature)

    if all_regions:
        return ee.FeatureCollection(ee_features)

    if region_name:
        merged_label = region_name.strip()
    elif filter_value and str(filter_value).strip():
        merged_label = str(filter_value).strip()
    else:
        merged_label = country
    merged_region = ee.FeatureCollection(ee_features).geometry()
    return ee.FeatureCollection(
        [
            ee.Feature(merged_region)
            .set("country", country)
            .set("region_name", merged_label)
            .set("region_id", merged_label)
            .set("gaul_code", merged_label)
            .set("admin_level", admin_level)
        ]
    )


def build_custom_regions_from_asset(
    asset_id: str,
    country: str,
    region_name_field: str,
    region_id_field: Optional[str],
    region_name: Optional[str],
    all_regions: bool,
    filter_field: Optional[str],
    filter_value: Optional[str],
    admin_level: int = 3,
) -> ee.FeatureCollection:
    fc = ee.FeatureCollection(asset_id)
    if filter_field and filter_value is not None and str(filter_value).strip():
        fc = fc.filter(ee.Filter.eq(filter_field, str(filter_value).strip()))

    if all_regions:
        return fc.map(
            lambda feat: standardize_custom_region_feature(
                feat,
                country=country,
                region_name_field=region_name_field,
                region_id_field=region_id_field,
                admin_level=admin_level,
            )
        )

    if region_name:
        fc = fc.filter(ee.Filter.eq(region_name_field, region_name.strip()))
        merged_label = region_name.strip()
    elif filter_value and str(filter_value).strip():
        merged_label = str(filter_value).strip()
    else:
        merged_label = country

    return ee.FeatureCollection(
        [
            ee.Feature(fc.geometry())
            .set("country", country)
            .set("region_name", merged_label)
            .set("region_id", merged_label)
            .set("gaul_code", merged_label)
            .set("admin_level", admin_level)
        ]
    )


def resolve_regions(
    country: str,
    admin_level: int,
    region_name: Optional[str],
    all_regions: bool,
    parent_region_name: Optional[str] = None,
    boundary_mode: str = "gaul",
    custom_geojson_path: Optional[str] = None,
    custom_asset_id: Optional[str] = None,
    custom_region_name_field: Optional[str] = None,
    custom_region_id_field: Optional[str] = None,
    custom_filter_field: Optional[str] = None,
    custom_filter_value: Optional[str] = None,
) -> ee.FeatureCollection:
    if boundary_mode == "gaul":
        return build_regions(
            country=country,
            admin_level=admin_level,
            region_name=region_name,
            all_regions=all_regions,
            parent_region_name=parent_region_name,
        )
    if boundary_mode == "custom_geojson":
        if not custom_geojson_path or not custom_region_name_field:
            raise ValueError("Mode custom GeoJSON membutuhkan path file dan nama field wilayah.")
        return build_custom_regions_from_geojson(
            geojson_path=custom_geojson_path,
            country=country,
            region_name_field=custom_region_name_field,
            region_id_field=custom_region_id_field,
            region_name=region_name,
            all_regions=all_regions,
            filter_field=custom_filter_field,
            filter_value=custom_filter_value,
            admin_level=admin_level,
        )
    if boundary_mode == "custom_asset":
        if not custom_asset_id or not custom_region_name_field:
            raise ValueError("Mode custom asset membutuhkan asset ID dan nama field wilayah.")
        return build_custom_regions_from_asset(
            asset_id=custom_asset_id,
            country=country,
            region_name_field=custom_region_name_field,
            region_id_field=custom_region_id_field,
            region_name=region_name,
            all_regions=all_regions,
            filter_field=custom_filter_field,
            filter_value=custom_filter_value,
            admin_level=admin_level,
        )
    raise ValueError(f"Mode boundary tidak dikenal: {boundary_mode}")


def standardize_region_feature(
    feature: ee.Feature,
    admin_level: int,
    country: str,
) -> ee.Feature:
    name_field = LEVEL_NAME_FIELDS[admin_level]
    code_field = LEVEL_CODE_FIELDS[admin_level]
    feature = ee.Feature(feature)
    return (
        ee.Feature(feature.geometry())
        .copyProperties(feature)
        .set("country", country)
        .set("region_name", feature.get(name_field))
        .set("region_id", ee.Number(feature.get(code_field)).format("%.0f"))
        .set("gaul_code", ee.Number(feature.get(code_field)).format("%.0f"))
        .set("admin_level", admin_level)
    )


def build_regions(
    country: str,
    admin_level: int,
    region_name: Optional[str],
    all_regions: bool,
    parent_region_name: Optional[str] = None,
) -> ee.FeatureCollection:
    country_filter = build_country_filter(country)
    if admin_level == 0:
        country_geom = ee.FeatureCollection(GAUL_LEVEL_DATASETS[1]).filter(country_filter).geometry()
        return ee.FeatureCollection(
            [
                ee.Feature(country_geom)
                .set("country", country)
                .set("region_name", country)
                .set("region_id", country)
                .set("gaul_code", country)
                .set("admin_level", 0)
            ]
        )

    level_fc = ee.FeatureCollection(GAUL_LEVEL_DATASETS[admin_level]).filter(country_filter)
    parent_filter = build_parent_region_filter(admin_level, parent_region_name)
    if parent_filter is not None:
        level_fc = level_fc.filter(parent_filter)

    if all_regions:
        return level_fc.map(lambda feat: standardize_region_feature(feat, admin_level, country))

    if region_name:
        filtered = level_fc.filter(ee.Filter.eq(LEVEL_NAME_FIELDS[admin_level], region_name))
        return ee.FeatureCollection(
            [
                ee.Feature(filtered.geometry())
                .set("country", country)
                .set("region_name", region_name)
                .set("region_id", region_name)
                .set("gaul_code", region_name)
                .set("admin_level", admin_level)
            ]
        )

    country_geom = ee.FeatureCollection(GAUL_LEVEL_DATASETS[1]).filter(country_filter).geometry()
    return ee.FeatureCollection(
        [
            ee.Feature(country_geom)
            .set("country", country)
            .set("region_name", country)
            .set("region_id", country)
            .set("gaul_code", country)
            .set("admin_level", 0)
        ]
    )


def list_regions(country: str, admin_level: int, parent_region_name: Optional[str] = None) -> List[str]:
    if admin_level == 0:
        return [country]
    fc = ee.FeatureCollection(GAUL_LEVEL_DATASETS[admin_level]).filter(build_country_filter(country))
    parent_filter = build_parent_region_filter(admin_level, parent_region_name)
    if parent_filter is not None:
        fc = fc.filter(parent_filter)
    names = fc.aggregate_array(LEVEL_NAME_FIELDS[admin_level]).getInfo()
    if not names:
        raise ValueError(
            f"Tidak ada region ditemukan untuk country='{country}' pada admin level {admin_level}."
        )
    return sorted({str(name) for name in names})


def get_region_match_count(
    country: str,
    admin_level: int,
    region_name: Optional[str] = None,
    parent_region_name: Optional[str] = None,
) -> int:
    if admin_level == 0:
        return 1
    fc = ee.FeatureCollection(GAUL_LEVEL_DATASETS[admin_level]).filter(build_country_filter(country))
    parent_filter = build_parent_region_filter(admin_level, parent_region_name)
    if parent_filter is not None:
        fc = fc.filter(parent_filter)
    if region_name:
        fc = fc.filter(ee.Filter.eq(LEVEL_NAME_FIELDS[admin_level], region_name))
    return int(fc.size().getInfo())


def fetch_region_records(regions_fc: ee.FeatureCollection) -> List[Dict[str, Any]]:
    region_ids = regions_fc.aggregate_array("region_id").getInfo()
    region_names = regions_fc.aggregate_array("region_name").getInfo()
    admin_levels = regions_fc.aggregate_array("admin_level").getInfo()
    countries = regions_fc.aggregate_array("country").getInfo()
    gaul_codes = regions_fc.aggregate_array("gaul_code").getInfo()

    records: List[Dict[str, Any]] = []
    for region_id, region_name, admin_level, country, gaul_code in zip(
        region_ids,
        region_names,
        admin_levels,
        countries,
        gaul_codes,
    ):
        records.append(
            {
                "region_id": str(region_id),
                "region_name": str(region_name),
                "admin_level": int(admin_level),
                "country": str(country),
                "gaul_code": str(gaul_code),
            }
        )
    return records


def fetch_region_centroids(
    country: str,
    admin_level: int,
    region_name: Optional[str],
    all_regions: bool,
    parent_region_name: Optional[str] = None,
    boundary_mode: str = "gaul",
    custom_geojson_path: Optional[str] = None,
    custom_asset_id: Optional[str] = None,
    custom_region_name_field: Optional[str] = None,
    custom_region_id_field: Optional[str] = None,
    custom_filter_field: Optional[str] = None,
    custom_filter_value: Optional[str] = None,
) -> pd.DataFrame:
    regions_fc = resolve_regions(
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

    def _with_centroid(feature: ee.Feature) -> ee.Feature:
        feature = ee.Feature(feature)
        coords = feature.geometry().centroid(1000).coordinates()
        return feature.set(
            "longitude",
            ee.List(coords).get(0),
            "latitude",
            ee.List(coords).get(1),
        )

    info = regions_fc.map(_with_centroid).getInfo()
    rows: List[Dict[str, Any]] = []
    for feature in info.get("features", []):
        props = feature.get("properties", {})
        rows.append(
            {
                "country": props.get("country"),
                "region_name": props.get("region_name"),
                "region_id": props.get("region_id"),
                "admin_level": props.get("admin_level"),
                "latitude": props.get("latitude"),
                "longitude": props.get("longitude"),
            }
        )
    return pd.DataFrame(rows)


def build_wave_regions(regions_fc: ee.FeatureCollection, buffer_km: float) -> ee.FeatureCollection:
    if buffer_km <= 0:
        return regions_fc

    buffer_m = buffer_km * 1000.0

    def _buffer_feature(feature: ee.Feature) -> ee.Feature:
        feature = ee.Feature(feature)
        buffered = ee.Feature(feature.geometry().buffer(buffer_m))
        return buffered.copyProperties(feature)

    return regions_fc.map(_buffer_feature)


def collection_has_data(collection: ee.ImageCollection) -> bool:
    return int(collection.limit(1).size().getInfo()) > 0


def combine_images(images: Iterable[ee.Image]) -> ee.Image:
    items = list(images)
    if not items:
        raise ValueError("Tidak ada image untuk digabung.")

    combined = items[0]
    for image in items[1:]:
        combined = combined.addBands(image)
    return combined


def build_era5_hourly_collection(period: Period) -> ee.ImageCollection:
    return ee.ImageCollection("ECMWF/ERA5/HOURLY").filterDate(
        period.start.isoformat(),
        period.end_exclusive.isoformat(),
    )


def build_era5_hourly_collection_between(start: Any, end_exclusive: Any) -> ee.ImageCollection:
    return ee.ImageCollection("ECMWF/ERA5/HOURLY").filterDate(start, end_exclusive)


def add_era5_derived_bands(image: ee.Image) -> ee.Image:
    temperature_c = image.select("temperature_2m").subtract(273.15)
    dewpoint_c = image.select("dewpoint_temperature_2m").subtract(273.15)
    relative_humidity = temperature_c.expression(
        "100 * exp((17.625 * td) / (243.04 + td) - (17.625 * t) / (243.04 + t))",
        {
            "t": temperature_c,
            "td": dewpoint_c,
        },
    ).rename("relative_humidity_pct")
    wind_speed = (
        image.select("u_component_of_wind_10m")
        .pow(2)
        .add(image.select("v_component_of_wind_10m").pow(2))
        .sqrt()
        .rename("wind_speed_10m")
    )
    return image.addBands(relative_humidity).addBands(wind_speed)


def build_climate_image(hourly: ee.ImageCollection) -> ee.Image:
    hourly = hourly.map(add_era5_derived_bands)
    images = [
        hourly.select("temperature_2m").mean().subtract(273.15).rename("climate_temp_mean_c"),
        hourly.select("temperature_2m").min().subtract(273.15).rename("climate_temp_min_c"),
        hourly.select("temperature_2m").max().subtract(273.15).rename("climate_temp_max_c"),
        hourly.select("dewpoint_temperature_2m")
        .mean()
        .subtract(273.15)
        .rename("climate_dewpoint_mean_c"),
        hourly.select("relative_humidity_pct")
        .mean()
        .clamp(0, 100)
        .rename("climate_relative_humidity_pct"),
        hourly.select("wind_speed_10m").mean().rename("climate_wind_speed_10m_ms"),
        hourly.select("mean_sea_level_pressure")
        .mean()
        .rename("climate_mean_sea_level_pressure_pa"),
        hourly.select("mean_surface_downward_short_wave_radiation_flux")
        .mean()
        .rename("climate_solar_radiation_flux_w_m2"),
    ]
    return combine_images(images)


def build_wave_image(hourly: ee.ImageCollection) -> ee.Image:
    return combine_images(
        [
            hourly.select("significant_height_of_combined_wind_waves_and_swell")
            .mean()
            .rename("wave_sig_height_m"),
            hourly.select("mean_wave_period").mean().rename("wave_mean_period_s"),
        ]
    )


def build_rainfall_collection(period: Period) -> ee.ImageCollection:
    return ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(
        period.start.isoformat(),
        period.end_exclusive.isoformat(),
    )


def build_rainfall_collection_between(start: Any, end_exclusive: Any) -> ee.ImageCollection:
    return ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").filterDate(start, end_exclusive)


def build_rainfall_image(collection: ee.ImageCollection) -> ee.Image:
    return collection.select("precipitation").sum().rename("rainfall_chirps_mm")


def mask_modis_clouds(image: ee.Image) -> ee.Image:
    state = image.select("state_1km")
    cloud_state = state.bitwiseAnd(3)
    cloud_shadow = state.rightShift(2).bitwiseAnd(1)
    cirrus = state.rightShift(8).bitwiseAnd(3)
    internal_cloud = state.rightShift(10).bitwiseAnd(1)
    adjacent_cloud = state.rightShift(13).bitwiseAnd(1)

    mask = (
        cloud_state.eq(0)
        .Or(cloud_state.eq(3))
        .And(cloud_shadow.eq(0))
        .And(cirrus.lte(1))
        .And(internal_cloud.eq(0))
        .And(adjacent_cloud.eq(0))
    )
    return image.updateMask(mask)


def build_ndvi_collection(period: Period) -> ee.ImageCollection:
    def _to_ndvi(image: ee.Image) -> ee.Image:
        masked = mask_modis_clouds(image)
        red = masked.select("sur_refl_b01").multiply(0.0001)
        nir = masked.select("sur_refl_b02").multiply(0.0001)
        denominator = nir.add(red)
        ndvi = (
            nir.subtract(red)
            .divide(denominator)
            .updateMask(denominator.neq(0))
            .clamp(-1, 1)
            .rename("ndvi_mean")
        )
        return ndvi.copyProperties(image, ["system:time_start"])

    return (
        ee.ImageCollection("MODIS/061/MOD09GA")
        .filterDate(period.start.isoformat(), period.end_exclusive.isoformat())
        .map(_to_ndvi)
    )


def build_ndvi_collection_between(start: Any, end_exclusive: Any) -> ee.ImageCollection:
    def _to_ndvi(image: ee.Image) -> ee.Image:
        masked = mask_modis_clouds(image)
        red = masked.select("sur_refl_b01").multiply(0.0001)
        nir = masked.select("sur_refl_b02").multiply(0.0001)
        denominator = nir.add(red)
        ndvi = (
            nir.subtract(red)
            .divide(denominator)
            .updateMask(denominator.neq(0))
            .clamp(-1, 1)
            .rename("ndvi_mean")
        )
        return ndvi.copyProperties(image, ["system:time_start"])

    return ee.ImageCollection("MODIS/061/MOD09GA").filterDate(start, end_exclusive).map(_to_ndvi)


def build_ndvi_image(collection: ee.ImageCollection) -> ee.Image:
    return collection.mean()


def build_evi_collection_between(start: Any, end_exclusive: Any) -> ee.ImageCollection:
    return (
        ee.ImageCollection("MODIS/MOD09GA_006_EVI")
        .filterDate(start, end_exclusive)
        .select("EVI")
        .map(lambda image: ee.Image(image).rename("evi_mean").copyProperties(image, ["system:time_start"]))
    )


def build_evi_image(collection: ee.ImageCollection) -> ee.Image:
    return collection.mean()


def build_pollution_image(period: Period) -> Optional[ee.Image]:
    images: List[ee.Image] = []

    no2_collection = ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_NO2").filterDate(
        period.start.isoformat(),
        period.end_exclusive.isoformat(),
    )
    if collection_has_data(no2_collection):
        images.append(
            no2_collection.select("tropospheric_NO2_column_number_density")
            .mean()
            .rename("pollution_no2_tropo_mol_m2")
        )

    co_collection = ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_CO").filterDate(
        period.start.isoformat(),
        period.end_exclusive.isoformat(),
    )
    if collection_has_data(co_collection):
        images.append(
            co_collection.select("CO_column_number_density")
            .mean()
            .rename("pollution_co_mol_m2")
        )

    aer_collection = ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_AER_AI").filterDate(
        period.start.isoformat(),
        period.end_exclusive.isoformat(),
    )
    if collection_has_data(aer_collection):
        images.append(
            aer_collection.select("absorbing_aerosol_index")
            .mean()
            .rename("pollution_aerosol_index")
        )

    if not images:
        return None

    return combine_images(images)


def reduce_image_by_regions(
    image: ee.Image,
    regions_fc: ee.FeatureCollection,
    scale: int,
) -> Dict[str, Dict[str, Any]]:
    reduced = image.reduceRegions(
        collection=regions_fc,
        reducer=ee.Reducer.mean(),
        scale=scale,
        tileScale=4,
    )
    info = reduced.getInfo()
    rows: Dict[str, Dict[str, Any]] = {}
    for feature in info["features"]:
        properties = feature["properties"]
        region_id = str(properties["region_id"])
        rows[region_id] = {
            key: value
            for key, value in properties.items()
            if key not in IGNORE_REDUCED_KEYS
        }
    return rows


def build_masked_placeholder_image(band_names: List[str]) -> ee.Image:
    image = ee.Image.constant([0] * len(band_names)).rename(band_names)
    return image.updateMask(ee.Image.constant(0))


def build_climate_image_between(start: Any, end_exclusive: Any) -> ee.Image:
    hourly = build_era5_hourly_collection_between(start, end_exclusive)
    return ee.Image(
        ee.Algorithms.If(
            hourly.size().gt(0),
            build_climate_image(hourly),
            build_masked_placeholder_image(GROUP_BANDS["climate"]),
        )
    )


def build_wave_image_between(start: Any, end_exclusive: Any) -> ee.Image:
    hourly = build_era5_hourly_collection_between(start, end_exclusive)
    return ee.Image(
        ee.Algorithms.If(
            hourly.size().gt(0),
            build_wave_image(hourly),
            build_masked_placeholder_image(GROUP_BANDS["wave"]),
        )
    )


def build_rainfall_image_between(start: Any, end_exclusive: Any) -> ee.Image:
    rainfall = build_rainfall_collection_between(start, end_exclusive)
    return ee.Image(
        ee.Algorithms.If(
            rainfall.size().gt(0),
            build_rainfall_image(rainfall),
            build_masked_placeholder_image(GROUP_BANDS["rainfall"]),
        )
    )


def build_ndvi_image_between(start: Any, end_exclusive: Any) -> ee.Image:
    ndvi = build_ndvi_collection_between(start, end_exclusive)
    return ee.Image(
        ee.Algorithms.If(
            ndvi.size().gt(0),
            build_ndvi_image(ndvi),
            build_masked_placeholder_image(GROUP_BANDS["ndvi"]),
        )
    )


def build_evi_image_between(start: Any, end_exclusive: Any) -> ee.Image:
    evi = build_evi_collection_between(start, end_exclusive)
    return ee.Image(
        ee.Algorithms.If(
            evi.size().gt(0),
            build_evi_image(evi),
            build_masked_placeholder_image(GROUP_BANDS["evi"]),
        )
    )


def build_pollution_image_between(start: Any, end_exclusive: Any) -> ee.Image:
    no2_collection = ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_NO2").filterDate(start, end_exclusive)
    co_collection = ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_CO").filterDate(start, end_exclusive)
    aer_collection = ee.ImageCollection("COPERNICUS/S5P/OFFL/L3_AER_AI").filterDate(start, end_exclusive)

    no2_image = ee.Image(
        ee.Algorithms.If(
            no2_collection.size().gt(0),
            no2_collection.select("tropospheric_NO2_column_number_density")
            .mean()
            .rename("pollution_no2_tropo_mol_m2"),
            build_masked_placeholder_image(["pollution_no2_tropo_mol_m2"]),
        )
    )
    co_image = ee.Image(
        ee.Algorithms.If(
            co_collection.size().gt(0),
            co_collection.select("CO_column_number_density").mean().rename("pollution_co_mol_m2"),
            build_masked_placeholder_image(["pollution_co_mol_m2"]),
        )
    )
    aer_image = ee.Image(
        ee.Algorithms.If(
            aer_collection.size().gt(0),
            aer_collection.select("absorbing_aerosol_index").mean().rename("pollution_aerosol_index"),
            build_masked_placeholder_image(["pollution_aerosol_index"]),
        )
    )
    return combine_images([no2_image, co_image, aer_image])


def build_base_support_dataframe(
    periods: List[Period],
    region_records: List[Dict[str, Any]],
    frequency: str,
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for period in periods:
        for record in region_records:
            rows.append(
                {
                    "country": record["country"],
                    "region_name": record["region_name"],
                    "region_id": record["region_id"],
                    "gaul_code": record["gaul_code"],
                    "admin_level": record["admin_level"],
                    "frequency": frequency,
                    "period_start": period.start.isoformat(),
                    "period_end": period.end_inclusive.isoformat(),
                    "period_label": period.label,
                }
            )
    frame = pd.DataFrame(rows)
    frame["period_start"] = pd.to_datetime(frame["period_start"])
    frame["period_end"] = pd.to_datetime(frame["period_end"])
    return frame


def feature_collection_info_to_frame(info: Dict[str, Any]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for feature in info.get("features", []):
        properties = dict(feature.get("properties", {}))
        rows.append(properties)
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if "region_id" in frame.columns:
        frame["region_id"] = frame["region_id"].astype(str)
    if "period_start" in frame.columns:
        frame["period_start"] = pd.to_datetime(frame["period_start"])
    if "period_end" in frame.columns:
        frame["period_end"] = pd.to_datetime(frame["period_end"])
    return frame


def normalize_group_metric_columns(frame: pd.DataFrame, group_name: str) -> pd.DataFrame:
    expected_metrics = GROUP_BANDS[group_name]
    result = frame.copy()
    if len(expected_metrics) == 1 and "mean" in result.columns and expected_metrics[0] not in result.columns:
        result = result.rename(columns={"mean": expected_metrics[0]})
    for column in expected_metrics:
        if column not in result.columns:
            result[column] = pd.NA
    return result


def build_daily_periods_from_target_periods(periods: List[Period]) -> List[Period]:
    if not periods:
        return []
    return build_periods(periods[0].start, periods[-1].end_inclusive, "daily")


def build_daily_membership_frame(periods: List[Period]) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for period in periods:
        cursor = period.start
        while cursor < period.end_exclusive:
            rows.append(
                {
                    "source_date": pd.Timestamp(cursor),
                    "period_start": pd.Timestamp(period.start),
                    "period_end": pd.Timestamp(period.end_inclusive),
                    "period_label": period.label,
                }
            )
            cursor += ONE_DAY
    return pd.DataFrame(rows)


def summarize_group_frame_from_daily(
    daily_frame: pd.DataFrame,
    periods: List[Period],
    group_name: str,
) -> pd.DataFrame:
    output_columns = ["region_id", "period_start", "period_end", "period_label"] + GROUP_OUTPUT_COLUMNS[group_name]
    if daily_frame.empty or not periods:
        return pd.DataFrame(columns=output_columns)

    period_membership = build_daily_membership_frame(periods)
    if period_membership.empty:
        return pd.DataFrame(columns=output_columns)

    working = daily_frame.copy()
    working["source_date"] = pd.to_datetime(working["period_start"]).dt.normalize()
    working = working.drop(columns=["period_start", "period_end", "period_label"], errors="ignore")
    for metric_name in GROUP_BANDS[group_name]:
        if metric_name in working.columns:
            working[metric_name] = pd.to_numeric(working[metric_name], errors="coerce")
    working = working.merge(period_membership, how="inner", on="source_date")
    if working.empty:
        return pd.DataFrame(columns=output_columns)

    group_keys = ["region_id", "period_start", "period_end", "period_label"]
    named_aggs: Dict[str, pd.NamedAgg] = {}
    for metric_name in GROUP_BANDS[group_name]:
        named_aggs[metric_name] = pd.NamedAgg(
            column=metric_name,
            aggfunc=PRIMARY_AGGREGATION_BY_METRIC.get(metric_name, "mean"),
        )
        if PRIMARY_AGGREGATION_BY_METRIC.get(metric_name) == "sum":
            named_aggs[f"{metric_name}_daily_mean"] = pd.NamedAgg(column=metric_name, aggfunc="mean")
        named_aggs[f"{metric_name}_daily_min"] = pd.NamedAgg(column=metric_name, aggfunc="min")
        named_aggs[f"{metric_name}_daily_max"] = pd.NamedAgg(column=metric_name, aggfunc="max")
        named_aggs[f"{metric_name}_valid_days"] = pd.NamedAgg(column=metric_name, aggfunc="count")

    summarized = working.groupby(group_keys, dropna=False).agg(**named_aggs).reset_index()
    for column in summarized.columns:
        if column.endswith("_valid_days"):
            summarized[column] = summarized[column].astype("Int64")
    return summarized.reindex(columns=output_columns)


def fill_group_valid_day_columns(frame: pd.DataFrame, group_name: str) -> pd.DataFrame:
    result = frame.copy()
    valid_day_columns = [column for column in GROUP_OUTPUT_COLUMNS[group_name] if column.endswith("_valid_days")]
    for column in valid_day_columns:
        if column in result.columns:
            result[column] = result[column].fillna(0).astype("Int64")
    return result


def period_batches(periods: List[Period], batch_size: int) -> Iterable[List[Period]]:
    for index in range(0, len(periods), batch_size):
        yield periods[index : index + batch_size]


def is_response_size_error(exc: Exception) -> bool:
    return "Response size exceeds limit" in str(exc)


def is_timeout_error(exc: Exception) -> bool:
    detail = str(exc)
    return "Computation timed out" in detail or "deadline exceeded" in detail.lower()


def is_retryable_chunk_error(exc: Exception) -> bool:
    return is_response_size_error(exc) or is_timeout_error(exc)


def fetch_group_chunk_frame(
    period_chunk: List[Period],
    regions_fc: ee.FeatureCollection,
    scale: int,
    image_builder,
    group_name: str,
) -> pd.DataFrame:
    period_fc = ee.FeatureCollection(
        [
            ee.Feature(
                None,
                {
                    "period_start": period.start.isoformat(),
                    "period_end": period.end_inclusive.isoformat(),
                    "period_end_exclusive": period.end_exclusive.isoformat(),
                    "period_label": period.label,
                },
            )
            for period in period_chunk
        ]
    )

    def _reduce_period(period_feature: ee.Feature) -> ee.FeatureCollection:
        period_feature = ee.Feature(period_feature)
        start = ee.Date(period_feature.get("period_start"))
        end_exclusive = ee.Date(period_feature.get("period_end_exclusive"))
        image = image_builder(start, end_exclusive)
        reduced = image.reduceRegions(
            collection=regions_fc,
            reducer=ee.Reducer.mean(),
            scale=scale,
            tileScale=TILE_SCALE_BY_GROUP.get(group_name, 4),
        )
        return ee.FeatureCollection(reduced).map(
            lambda feature: ee.Feature(feature).set(
                "period_start",
                period_feature.get("period_start"),
                "period_end",
                period_feature.get("period_end"),
                "period_label",
                period_feature.get("period_label"),
            )
        )

    try:
        reduced_info = ee.FeatureCollection(period_fc.map(_reduce_period)).flatten().getInfo()
    except Exception as exc:
        if is_retryable_chunk_error(exc) and len(period_chunk) > 1:
            mid = max(1, len(period_chunk) // 2)
            print(
                f"Batch grup {group_name} bermasalah ({len(period_chunk)} periode: {exc}), "
                f"membelah jadi {mid} + {len(period_chunk) - mid}",
                flush=True,
            )
            left = fetch_group_chunk_frame(
                period_chunk[:mid],
                regions_fc=regions_fc,
                scale=scale,
                image_builder=image_builder,
                group_name=group_name,
            )
            right = fetch_group_chunk_frame(
                period_chunk[mid:],
                regions_fc=regions_fc,
                scale=scale,
                image_builder=image_builder,
                group_name=group_name,
            )
            return pd.concat([left, right], ignore_index=True)
        raise

    frame = feature_collection_info_to_frame(reduced_info)
    if frame.empty:
        columns = ["region_id", "period_start", "period_end", "period_label"] + GROUP_BANDS[group_name]
        return pd.DataFrame(columns=columns)

    frame = normalize_group_metric_columns(frame, group_name)
    keep_columns = ["region_id", "period_start", "period_end", "period_label"] + GROUP_BANDS[group_name]
    return frame.reindex(columns=keep_columns).drop_duplicates().reset_index(drop=True)


def reduce_group_over_periods(
    periods: List[Period],
    regions_fc: ee.FeatureCollection,
    scale: int,
    image_builder,
    group_start: date,
    group_name: str,
) -> pd.DataFrame:
    valid_periods = [period for period in periods if should_query(period, group_start)]
    if not valid_periods:
        columns = ["region_id", "period_start", "period_end", "period_label"] + GROUP_BANDS[group_name]
        return pd.DataFrame(columns=columns)

    print(f"Mengambil grup {group_name}: {len(valid_periods)} periode sumber", flush=True)
    batch_size = DEFAULT_PERIOD_BATCH_SIZE_BY_GROUP.get(group_name, DEFAULT_PERIOD_BATCH_SIZE)
    total_batches = math.ceil(len(valid_periods) / batch_size)
    chunk_frames: List[pd.DataFrame] = []
    for batch_index, period_chunk in enumerate(period_batches(valid_periods, batch_size), start=1):
        chunk_start = period_chunk[0].start.isoformat()
        chunk_end = period_chunk[-1].end_inclusive.isoformat()
        print(
            f"  Batch {batch_index}/{total_batches} grup {group_name}: {len(period_chunk)} periode sumber "
            f"({chunk_start} s.d. {chunk_end})",
            flush=True,
        )
        chunk_frames.append(
            fetch_group_chunk_frame(
                period_chunk,
                regions_fc=regions_fc,
                scale=scale,
                image_builder=image_builder,
                group_name=group_name,
            )
        )

    return pd.concat(chunk_frames, ignore_index=True).drop_duplicates().reset_index(drop=True)


def merge_group_rows(
    base_rows: Dict[str, Dict[str, Any]],
    reduced_rows: Dict[str, Dict[str, Any]],
) -> None:
    for region_id, values in reduced_rows.items():
        if region_id not in base_rows:
            continue
        base_rows[region_id].update(values)


def should_query(period: Period, group_start: date) -> bool:
    return period.end_exclusive > group_start


def merge_group_frame_into_base(
    base_frame: pd.DataFrame,
    daily_periods: List[Period],
    target_periods: List[Period],
    regions_fc: ee.FeatureCollection,
    scale: int,
    image_builder,
    group_start: date,
    group_name: str,
) -> pd.DataFrame:
    daily_frame = reduce_group_over_periods(
        periods=daily_periods,
        regions_fc=regions_fc,
        scale=scale,
        image_builder=image_builder,
        group_start=group_start,
        group_name=group_name,
    )
    summarized_frame = summarize_group_frame_from_daily(
        daily_frame=daily_frame,
        periods=target_periods,
        group_name=group_name,
    )
    merged = base_frame.merge(
        summarized_frame,
        how="left",
        on=["region_id", "period_start", "period_end", "period_label"],
    )
    return fill_group_valid_day_columns(merged, group_name)


def collect_support_dataframe(
    args: argparse.Namespace,
    periods: List[Period],
    regions_fc: ee.FeatureCollection,
    selected_groups: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    groups = set(selected_groups or {"climate", "wave", "rainfall", "ndvi", "evi", "pollution"})
    region_records = fetch_region_records(regions_fc)
    wave_regions_fc = build_wave_regions(regions_fc, args.wave_buffer_km)
    frame = build_base_support_dataframe(periods, region_records, args.frequency)
    daily_periods = build_daily_periods_from_target_periods(periods)

    if "climate" in groups:
        frame = merge_group_frame_into_base(
            base_frame=frame,
            daily_periods=daily_periods,
            target_periods=periods,
            regions_fc=regions_fc,
            scale=SCALE_BY_GROUP["climate"],
            image_builder=build_climate_image_between,
            group_start=ERA5_START,
            group_name="climate",
        )

    if "wave" in groups:
        frame = merge_group_frame_into_base(
            base_frame=frame,
            daily_periods=daily_periods,
            target_periods=periods,
            regions_fc=wave_regions_fc,
            scale=SCALE_BY_GROUP["wave"],
            image_builder=build_wave_image_between,
            group_start=ERA5_START,
            group_name="wave",
        )

    if "rainfall" in groups:
        frame = merge_group_frame_into_base(
            base_frame=frame,
            daily_periods=daily_periods,
            target_periods=periods,
            regions_fc=regions_fc,
            scale=SCALE_BY_GROUP["rainfall"],
            image_builder=build_rainfall_image_between,
            group_start=CHIRPS_START,
            group_name="rainfall",
        )

    if "ndvi" in groups:
        frame = merge_group_frame_into_base(
            base_frame=frame,
            daily_periods=daily_periods,
            target_periods=periods,
            regions_fc=regions_fc,
            scale=SCALE_BY_GROUP["ndvi"],
            image_builder=build_ndvi_image_between,
            group_start=MODIS_START,
            group_name="ndvi",
        )

    if "evi" in groups:
        frame = merge_group_frame_into_base(
            base_frame=frame,
            daily_periods=daily_periods,
            target_periods=periods,
            regions_fc=regions_fc,
            scale=SCALE_BY_GROUP["evi"],
            image_builder=build_evi_image_between,
            group_start=MODIS_START,
            group_name="evi",
        )

    if "pollution" in groups:
        frame = merge_group_frame_into_base(
            base_frame=frame,
            daily_periods=daily_periods,
            target_periods=periods,
            regions_fc=regions_fc,
            scale=SCALE_BY_GROUP["pollution"],
            image_builder=build_pollution_image_between,
            group_start=S5P_START,
            group_name="pollution",
        )

    return frame.sort_values(["region_name", "period_start"]).reset_index(drop=True)


def add_runup_proxy(frame: pd.DataFrame, beta: Optional[float]) -> pd.DataFrame:
    if beta is None:
        return frame

    if "wave_sig_height_m" not in frame.columns or "wave_mean_period_s" not in frame.columns:
        return frame

    height = pd.to_numeric(frame["wave_sig_height_m"], errors="coerce")
    period = pd.to_numeric(frame["wave_mean_period_s"], errors="coerce")
    wavelength = 9.81 * period.pow(2) / (2 * math.pi)
    valid = (height > 0) & (period > 0)
    runup = pd.Series(float("nan"), index=frame.index, dtype=float)
    runup_numeric = 1.1 * (
        0.35 * beta * (height * wavelength).pow(0.5)
        + ((height * wavelength * (0.563 * (beta**2) + 0.004)).pow(0.5) / 2.0)
    )
    runup.loc[valid] = runup_numeric.loc[valid]

    frame["wave_deepwater_wavelength_m"] = wavelength.where(valid)
    frame["runup_stockdon_proxy_m"] = runup
    return frame


def normalize_region_name(value: Any) -> str:
    return " ".join(str(value).strip().lower().split())


def load_dengue_frame(args: argparse.Namespace) -> pd.DataFrame:
    if not args.dengue_file:
        raise ValueError("dengue_file kosong.")

    dengue_path = Path(args.dengue_file)
    if not dengue_path.exists():
        raise FileNotFoundError(f"File dengue tidak ditemukan: {dengue_path}")

    if dengue_path.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        frame = pd.read_excel(dengue_path, sheet_name=args.dengue_sheet)
    else:
        frame = pd.read_csv(dengue_path)

    required_columns = {args.dengue_date_col, args.dengue_value_col}
    missing_columns = [col for col in required_columns if col not in frame.columns]
    if missing_columns:
        raise ValueError(f"Kolom dengue tidak ditemukan: {missing_columns}")

    rename_map = {
        args.dengue_date_col: "dengue_source_date",
        args.dengue_value_col: "dengue_cases",
    }
    if args.dengue_region_col:
        if args.dengue_region_col not in frame.columns:
            raise ValueError(f"Kolom region dengue tidak ditemukan: {args.dengue_region_col}")
        rename_map[args.dengue_region_col] = "region_name"

    frame = frame.rename(columns=rename_map).copy()
    frame["dengue_source_date"] = pd.to_datetime(frame["dengue_source_date"])
    frame["dengue_cases"] = pd.to_numeric(frame["dengue_cases"], errors="coerce")
    if args.dengue_region_col:
        frame["region_name_key"] = frame["region_name"].map(normalize_region_name)
    return frame


def assign_period_start(timestamp: pd.Timestamp, periods: List[Period]) -> Optional[pd.Timestamp]:
    current_date = timestamp.date()
    for period in periods:
        if period.start <= current_date < period.end_exclusive:
            return pd.Timestamp(period.start)
    return None


def aggregate_dengue_to_periods(
    support_frame: pd.DataFrame,
    periods: List[Period],
    args: argparse.Namespace,
) -> pd.DataFrame:
    dengue_frame = load_dengue_frame(args)
    dengue_frame["period_start"] = dengue_frame["dengue_source_date"].map(
        lambda ts: assign_period_start(ts, periods)
    )
    dengue_frame = dengue_frame.dropna(subset=["period_start"])

    if args.dengue_region_col:
        grouping = ["region_name_key", "period_start"]
    else:
        grouping = ["period_start"]

    aggregated = (
        dengue_frame.groupby(grouping, dropna=False)["dengue_cases"]
        .agg(args.dengue_agg)
        .reset_index()
    )

    merged = support_frame.copy()
    merged["region_name_key"] = merged["region_name"].map(normalize_region_name)

    if args.dengue_region_col:
        merged = merged.merge(aggregated, how="left", on=["region_name_key", "period_start"])
    else:
        merged = merged.merge(aggregated, how="left", on=["period_start"])

    return merged.drop(columns=["region_name_key"])


def build_long_frame(wide_frame: pd.DataFrame) -> pd.DataFrame:
    id_columns = [
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
    value_columns = [column for column in wide_frame.columns if column not in id_columns]
    return wide_frame.melt(
        id_vars=id_columns,
        value_vars=value_columns,
        var_name="metric",
        value_name="value",
    )


def build_metadata_frame(args: argparse.Namespace, periods: List[Period]) -> pd.DataFrame:
    metadata = [
        {"key": "country", "value": args.country},
        {"key": "region_name", "value": args.region_name or ""},
        {"key": "parent_region_name", "value": args.parent_region_name or ""},
        {"key": "admin_level", "value": args.admin_level},
        {"key": "all_regions", "value": args.all_regions},
        {"key": "frequency", "value": args.frequency},
        {"key": "start_date", "value": periods[0].start.isoformat()},
        {"key": "end_date", "value": periods[-1].end_inclusive.isoformat()},
        {"key": "wave_buffer_km", "value": args.wave_buffer_km},
        {"key": "beach_slope_beta", "value": args.beach_slope_beta if args.beach_slope_beta else ""},
        {"key": "dengue_file", "value": args.dengue_file or ""},
    ]
    return pd.DataFrame(metadata)


def write_excel(
    output_path: Path,
    wide_frame: pd.DataFrame,
    long_frame: pd.DataFrame,
    metadata_frame: pd.DataFrame,
    periods: List[Period],
    merged_dengue_frame: Optional[pd.DataFrame],
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    periods_frame = pd.DataFrame(
        [
            {
                "period_start": period.start.isoformat(),
                "period_end": period.end_inclusive.isoformat(),
                "period_label": period.label,
            }
            for period in periods
        ]
    )

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        wide_frame.to_excel(writer, sheet_name="data_wide", index=False)
        long_frame.to_excel(writer, sheet_name="data_long", index=False)
        metadata_frame.to_excel(writer, sheet_name="metadata", index=False)
        pd.DataFrame(DATASET_ROWS).to_excel(writer, sheet_name="datasets", index=False)
        periods_frame.to_excel(writer, sheet_name="periods", index=False)
        if merged_dengue_frame is not None:
            merged_dengue_frame.to_excel(writer, sheet_name="merged_dengue", index=False)


def main() -> int:
    args = parse_args()
    initialize_earth_engine(args.gee_project, args.authenticate)

    if args.list_regions:
        names = list_regions(args.country, args.admin_level, args.parent_region_name)
        print("\n".join(names))
        return 0

    if get_region_match_count(args.country, 1) == 0:
        raise ValueError(
            f"Country '{args.country}' tidak ditemukan di GAUL. Coba cek penulisan nama negara."
        )

    if args.region_name and get_region_match_count(
        args.country,
        args.admin_level,
        args.region_name,
        args.parent_region_name,
    ) == 0:
        raise ValueError(
            (
                f"Region '{args.region_name}' tidak ditemukan di '{args.country}' "
                f"pada admin level {args.admin_level}. Coba jalankan --list-regions."
            )
        )

    if args.all_regions and get_region_match_count(
        args.country,
        args.admin_level,
        parent_region_name=args.parent_region_name,
    ) == 0:
        raise ValueError(
            (
                f"Tidak ada region ditemukan untuk '{args.country}' "
                f"pada admin level {args.admin_level}."
            )
        )

    start_date = parse_iso_date(args.start_date)
    end_date = parse_iso_date(args.end_date)
    periods = build_periods(start_date, end_date, args.frequency)

    regions_fc = build_regions(
        country=args.country,
        admin_level=args.admin_level,
        region_name=args.region_name,
        all_regions=args.all_regions,
        parent_region_name=args.parent_region_name,
    )

    support_frame = collect_support_dataframe(args, periods, regions_fc)
    support_frame = add_runup_proxy(support_frame, args.beach_slope_beta)
    long_frame = build_long_frame(support_frame)
    metadata_frame = build_metadata_frame(args, periods)

    merged_dengue_frame = None
    if args.dengue_file:
        merged_dengue_frame = aggregate_dengue_to_periods(support_frame, periods, args)

    output_path = Path(args.output).expanduser().resolve()
    write_excel(
        output_path=output_path,
        wide_frame=support_frame,
        long_frame=long_frame,
        metadata_frame=metadata_frame,
        periods=periods,
        merged_dengue_frame=merged_dengue_frame,
    )

    print(f"Selesai. File Excel tersimpan di: {output_path}")
    print(f"Jumlah baris data: {len(support_frame)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Dibatalkan pengguna.", file=sys.stderr)
        raise SystemExit(130)
