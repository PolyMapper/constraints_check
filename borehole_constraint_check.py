# -*- coding: utf-8 -*-
"""
Borehole proximity / constraint analysis
----------------------------------------

Reads a borehole point feature class and a JSON config file.
Runs a set of rule-based spatial checks for each borehole.
Writes a flat output table (CSV, and optionally XLSX if pandas is installed).

Author: ChatGPT starter script
Environment: ArcGIS Pro / ArcPy
"""

import arcpy
import os
import json
import csv
import traceback
from collections import OrderedDict


# ============================================================
# USER INPUTS
# ============================================================

# Example direct inputs for testing.
# You can later convert these to ArcGIS Pro tool parameters if preferred.

BOREHOLE_FC = r"Z:\Projects\Morven\APRX\Working\ARUP SI Survey\ARUP SI Survey.gdb\MergedGIBoreholes_20260326_SingleTest"
BOREHOLE_ID_FIELD = "HoleID"
CONFIG_JSON = r"Z:\Code_Repo\Scripts\Vector\Constraint Check\borehole_constraint_check_config.json"
OUTPUT_CSV = r"Z:\Projects\Morven\APRX\Working\ARUP SI Survey\borehole_analysis_output_singletest_1.csv"

# Optional temp workspace for scratch outputs
SCRATCH_GDB = arcpy.env.scratchGDB


# ============================================================
# GENERAL HELPERS
# ============================================================

def msg(text):
    arcpy.AddMessage(str(text))


def warn(text):
    arcpy.AddWarning(str(text))


def err(text):
    arcpy.AddError(str(text))


def safe_str(value):
    if value is None:
        return ""
    return str(value).strip()


def field_exists(dataset, field_name):
    return field_name.lower() in [f.name.lower() for f in arcpy.ListFields(dataset)]


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_folder_for_file(path):
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder)


def get_desc(dataset):
    return arcpy.Describe(dataset)


def get_sr(dataset):
    return get_desc(dataset).spatialReference


def is_projected(sr):
    try:
        return bool(sr) and sr.type == "Projected"
    except Exception:
        return False


def ensure_projected_boreholes(borehole_fc, target_sr=None):
    """
    Ensures boreholes are in a projected CRS.
    If target_sr is provided, projects to that.
    Otherwise uses borehole CRS if already projected.
    """
    borehole_sr = get_sr(borehole_fc)

    if target_sr:
        if borehole_sr.factoryCode == target_sr.factoryCode:
            return borehole_fc
        out_fc = os.path.join(SCRATCH_GDB, arcpy.CreateUniqueName("boreholes_proj", SCRATCH_GDB))
        msg("Projecting boreholes to target CRS: {0}".format(target_sr.name))
        arcpy.management.Project(borehole_fc, out_fc, target_sr)
        return out_fc

    if is_projected(borehole_sr):
        return borehole_fc

    raise ValueError(
        "Borehole feature class must be in a projected coordinate system in metres, "
        "or supply a projected target CRS in the JSON config."
    )


def project_dataset_if_needed(dataset, target_sr, cache):
    """
    Projects a dataset to the target spatial reference if required.
    Uses a simple cache so we only project each dataset once.
    """
    key = (dataset, target_sr.factoryCode if target_sr else None)
    if key in cache:
        return cache[key]

    ds_sr = get_sr(dataset)
    if ds_sr.factoryCode == target_sr.factoryCode:
        cache[key] = dataset
        return dataset

    out_name = arcpy.CreateUniqueName("proj_" + os.path.basename(str(dataset)).replace(".", "_"), SCRATCH_GDB)
    out_fc = os.path.join(SCRATCH_GDB, out_name)
    msg("Projecting dataset: {0}".format(dataset))
    arcpy.management.Project(dataset, out_fc, target_sr)
    cache[key] = out_fc
    return out_fc


def make_feature_layer(dataset, layer_name):
    if arcpy.Exists(layer_name):
        arcpy.management.Delete(layer_name)
    return arcpy.management.MakeFeatureLayer(dataset, layer_name).getOutput(0)


def get_geometry_token():
    return "SHAPE@"


def get_borehole_fields(borehole_fc, id_field, extra_fields):
    fields = [id_field, get_geometry_token()]
    for f in extra_fields:
        if f != id_field and field_exists(borehole_fc, f):
            fields.append(f)
    return fields


def get_dataset_field_value(row_dict, field_name):
    if not field_name:
        return ""
    return safe_str(row_dict.get(field_name))


def format_distance_text(distance_value):
    if distance_value is None:
        return ""

    try:
        return "distance away: {0:.1f} m".format(float(distance_value))
    except Exception:
        return "distance away: {0} m".format(distance_value)


def build_feature_parts(row_dict, dataset_cfg):
    label = dataset_cfg.get("label")
    name_field = dataset_cfg.get("name_field")
    type_field = dataset_cfg.get("type_field")
    extra_fields = dataset_cfg.get("summary_fields", [])

    main_parts = []
    extra_parts = []

    if label:
        main_parts.append("{0}:".format(label))

    name_val = get_dataset_field_value(row_dict, name_field)
    type_val = get_dataset_field_value(row_dict, type_field)

    if name_val:
        main_parts.append(name_val)

    if type_val and type_val != name_val:
        if name_val:
            main_parts.append("({0})".format(type_val))
        else:
            main_parts.append(type_val)

    for fld in extra_fields:
        v = get_dataset_field_value(row_dict, fld)
        if v:
            extra_parts.append("{0}: {1}".format(fld, v))

    header = " ".join(main_parts).strip()
    return header, extra_parts


def build_feature_summary(row_dict, dataset_cfg, distance_value=None):
    header, extra_parts = build_feature_parts(row_dict, dataset_cfg)
    distance_text = format_distance_text(distance_value)

    if extra_parts:
        if header:
            summary = header if header.endswith(":") else "{0}:".format(header)
        else:
            summary = ""
        detail_lines = ["	- {0}".format(part) for part in extra_parts]
        if distance_text:
            detail_lines.append("\t- {0}".format(distance_text))
        if summary:
            return "{0}\n{1}".format(summary, "\n".join(detail_lines)).strip()
        return "\n".join(detail_lines).strip()

    if header and distance_text:
        summary = "{0} ({1})".format(header, distance_text)
    elif header:
        summary = header
    elif distance_text:
        summary = "({0})".format(distance_text)
    else:
        summary = ""

    return summary.strip()


def merge_result_strings(items, sep="\n"):
    clean = []
    seen = set()
    for item in items:
        txt = safe_str(item)
        if not txt:
            continue
        if txt in seen:
            continue
        seen.add(txt)
        clean.append(txt)
    return sep.join(clean)


def split_result_items(result_text):
    txt = safe_str(result_text)
    if not txt:
        return []
    lines = txt.split("\n")
    items = []
    current = []

    for line in lines:
        if not safe_str(line):
            continue

        is_detail_line = line.startswith("\t") or line.startswith(" ")
        if not is_detail_line and current:
            items.append("\n".join(current).strip())
            current = []
        current.append(line)

    if current:
        items.append("\n".join(current).strip())

    return items


def attach_distance_to_summary(summary_text, distance_value):
    summary_text = safe_str(summary_text)
    distance_text = format_distance_text(distance_value)
    if not distance_text:
        return summary_text

    if "\n" in summary_text:
        return "{0}\n\t- {1}".format(summary_text, distance_text).strip()

    if summary_text:
        return "{0} ({1})".format(summary_text, distance_text)

    return "({0})".format(distance_text)


def sql_where(dataset_cfg):
    return safe_str(dataset_cfg.get("where_clause"))


def feature_layer_with_optional_where(dataset, dataset_cfg, base_name):
    lyr_name = arcpy.CreateUniqueName(base_name, "in_memory")
    lyr = make_feature_layer(dataset, lyr_name)
    where = sql_where(dataset_cfg)
    if where:
        arcpy.management.SelectLayerByAttribute(lyr, "NEW_SELECTION", where)
    return lyr


def count_selected(layer):
    return int(arcpy.management.GetCount(layer).getOutput(0))


# ============================================================
# RULE FUNCTIONS
# ============================================================

def rule_intersect_any(bh_geom, dataset_cfg, dataset_path):
    """
    Returns Yes/No. True if the borehole intersects at least one feature.
    """
    lyr = feature_layer_with_optional_where(dataset_path, dataset_cfg, "lyr_intersect_any")
    arcpy.management.SelectLayerByLocation(
        lyr,
        overlap_type="INTERSECT",
        select_features=bh_geom,
        selection_type="NEW_SELECTION"
    )
    return "Yes" if count_selected(lyr) > 0 else "No"


def rule_intersect_detail(bh_geom, dataset_cfg, dataset_path):
    """
    Returns a list of intersecting features.
    """
    lyr = feature_layer_with_optional_where(dataset_path, dataset_cfg, "lyr_intersect_detail")
    arcpy.management.SelectLayerByLocation(
        lyr,
        overlap_type="INTERSECT",
        select_features=bh_geom,
        selection_type="NEW_SELECTION"
    )

    if count_selected(lyr) == 0:
        return ""

    fields = []
    for fld in set(
        [dataset_cfg.get("name_field"), dataset_cfg.get("type_field")] +
        dataset_cfg.get("summary_fields", [])
    ):
        if fld and field_exists(dataset_path, fld):
            fields.append(fld)

    summaries = []
    with arcpy.da.SearchCursor(lyr, fields) as cursor:
        for row in cursor:
            row_dict = dict(zip(fields, row))
            summaries.append(build_feature_summary(row_dict, dataset_cfg))

    return merge_result_strings(sorted(set(summaries)))


def rule_list_within_distance(bh_geom, dataset_cfg, dataset_path, distance_m):
    """
    Returns all features within distance, with summary and distance.
    """
    lyr = feature_layer_with_optional_where(dataset_path, dataset_cfg, "lyr_within_distance")
    arcpy.management.SelectLayerByLocation(
        lyr,
        overlap_type="WITHIN_A_DISTANCE",
        select_features=bh_geom,
        search_distance="{0} Meters".format(distance_m),
        selection_type="NEW_SELECTION"
    )

    if count_selected(lyr) == 0:
        return ""

    fields = []
    for fld in set(
        [dataset_cfg.get("name_field"), dataset_cfg.get("type_field")] +
        dataset_cfg.get("summary_fields", [])
    ):
        if fld and field_exists(dataset_path, fld):
            fields.append(fld)

    fields.append(get_geometry_token())

    closest_by_summary = {}
    with arcpy.da.SearchCursor(lyr, fields) as cursor:
        for row in cursor:
            row_vals = list(row)
            geom = row_vals[-1]
            attr_vals = row_vals[:-1]
            row_dict = dict(zip(fields[:-1], attr_vals))

            try:
                dist = bh_geom.distanceTo(geom)
            except Exception:
                dist = None

            summary_key = build_feature_summary(row_dict, dataset_cfg)
            existing = closest_by_summary.get(summary_key)

            if existing is None:
                closest_by_summary[summary_key] = dist
                continue

            if dist is not None and (existing is None or dist < existing):
                closest_by_summary[summary_key] = dist

    ordered_items = sorted(
        closest_by_summary.items(),
        key=lambda kv: float("inf") if kv[1] is None else kv[1]
    )
    summaries = []
    for summary_key, best_dist in ordered_items:
        if not summary_key:
            continue
        summaries.append(attach_distance_to_summary(summary_key, best_dist))

    return merge_result_strings(summaries)


def rule_nearest_feature(bh_geom, dataset_cfg, dataset_path, distance_m=None):
    """
    Returns the nearest feature summary, optionally only if within threshold.
    """
    fields = []
    for fld in set(
        [dataset_cfg.get("name_field"), dataset_cfg.get("type_field")] +
        dataset_cfg.get("summary_fields", [])
    ):
        if fld and field_exists(dataset_path, fld):
            fields.append(fld)

    fields.append(get_geometry_token())

    nearest_summary = ""
    nearest_dist = None

    lyr = feature_layer_with_optional_where(dataset_path, dataset_cfg, "lyr_nearest_feature")

    with arcpy.da.SearchCursor(lyr, fields) as cursor:
        for row in cursor:
            row_vals = list(row)
            geom = row_vals[-1]
            attr_vals = row_vals[:-1]
            row_dict = dict(zip(fields[:-1], attr_vals))

            try:
                dist = bh_geom.distanceTo(geom)
            except Exception:
                continue

            if distance_m is not None and dist > float(distance_m):
                continue

            if nearest_dist is None or dist < nearest_dist:
                nearest_dist = dist
                nearest_summary = build_feature_summary(row_dict, dataset_cfg, dist)

    return nearest_summary


def rule_habitat_at_point(bh_geom, dataset_cfg, dataset_path):
    """
    Same as intersect detail, but kept as its own rule type for clarity.
    """
    return rule_intersect_detail(bh_geom, dataset_cfg, dataset_path)


def rule_intersect_any_multi(bh_geom, config, rule, projected_datasets):
    """
    For rules using multiple datasets, returns Yes if any dataset intersects.
    """
    for ds_key in rule["datasets"]:
        dataset_cfg = config["datasets"][ds_key]
        dataset_path = projected_datasets[ds_key]
        result = rule_intersect_any(bh_geom, dataset_cfg, dataset_path)
        if result == "Yes":
            return "Yes"
    return "No"


def rule_list_within_distance_multi(bh_geom, config, rule, projected_datasets):
    """
    For rules using multiple datasets, returns a merged list across all datasets.
    """
    distance_m = rule.get("distance_m")
    all_items = []

    for ds_key in rule["datasets"]:
        dataset_cfg = config["datasets"][ds_key]
        dataset_path = projected_datasets[ds_key]
        result = rule_list_within_distance(bh_geom, dataset_cfg, dataset_path, distance_m)
        all_items.extend(split_result_items(result))

    return merge_result_strings(all_items)


def rule_intersect_detail_multi(bh_geom, config, rule, projected_datasets):
    all_items = []

    for ds_key in rule["datasets"]:
        dataset_cfg = config["datasets"][ds_key]
        dataset_path = projected_datasets[ds_key]
        result = rule_intersect_detail(bh_geom, dataset_cfg, dataset_path)
        all_items.extend(split_result_items(result))

    return merge_result_strings(all_items)


def rule_nearest_feature_multi(bh_geom, config, rule, projected_datasets):
    distance_m = rule.get("distance_m")
    best_summary = ""
    best_dist = None

    for ds_key in rule["datasets"]:
        dataset_cfg = config["datasets"][ds_key]
        dataset_path = projected_datasets[ds_key]

        fields = []
        for fld in set(
            [dataset_cfg.get("name_field"), dataset_cfg.get("type_field")] +
            dataset_cfg.get("summary_fields", [])
        ):
            if fld and field_exists(dataset_path, fld):
                fields.append(fld)
        fields.append(get_geometry_token())

        lyr = feature_layer_with_optional_where(dataset_path, dataset_cfg, "lyr_nearest_multi")

        with arcpy.da.SearchCursor(lyr, fields) as cursor:
            for row in cursor:
                row_vals = list(row)
                geom = row_vals[-1]
                attr_vals = row_vals[:-1]
                row_dict = dict(zip(fields[:-1], attr_vals))

                try:
                    dist = bh_geom.distanceTo(geom)
                except Exception:
                    continue

                if distance_m is not None and dist > float(distance_m):
                    continue

                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    best_summary = build_feature_summary(row_dict, dataset_cfg, dist)

    return best_summary


def evaluate_rule(bh_geom, config, rule, projected_datasets):
    rule_type = rule["type"]

    if len(rule["datasets"]) == 1:
        ds_key = rule["datasets"][0]
        dataset_cfg = config["datasets"][ds_key]
        dataset_path = projected_datasets[ds_key]

        if rule_type == "intersect_any":
            return rule_intersect_any(bh_geom, dataset_cfg, dataset_path)

        elif rule_type == "intersect_detail":
            return rule_intersect_detail(bh_geom, dataset_cfg, dataset_path)

        elif rule_type == "list_within_distance":
            return rule_list_within_distance(
                bh_geom, dataset_cfg, dataset_path, rule["distance_m"]
            )

        elif rule_type == "nearest_feature":
            return rule_nearest_feature(
                bh_geom, dataset_cfg, dataset_path, rule.get("distance_m")
            )

        elif rule_type == "habitat_at_point":
            return rule_habitat_at_point(bh_geom, dataset_cfg, dataset_path)

        else:
            raise ValueError("Unsupported rule type: {0}".format(rule_type))

    else:
        if rule_type == "intersect_any":
            return rule_intersect_any_multi(bh_geom, config, rule, projected_datasets)

        elif rule_type == "intersect_detail":
            return rule_intersect_detail_multi(bh_geom, config, rule, projected_datasets)

        elif rule_type == "list_within_distance":
            return rule_list_within_distance_multi(bh_geom, config, rule, projected_datasets)

        elif rule_type == "nearest_feature":
            return rule_nearest_feature_multi(bh_geom, config, rule, projected_datasets)

        else:
            raise ValueError(
                "Unsupported multi-dataset rule type: {0}".format(rule_type)
            )


# ============================================================
# VALIDATION
# ============================================================

def validate_config(config):
    if "datasets" not in config:
        raise ValueError("JSON config is missing 'datasets'")
    if "rules" not in config:
        raise ValueError("JSON config is missing 'rules'")

    for ds_key, ds_cfg in config["datasets"].items():
        path = ds_cfg.get("path")
        if not path:
            raise ValueError("Dataset '{0}' is missing 'path'".format(ds_key))
        if not arcpy.Exists(path):
            raise ValueError("Dataset path does not exist for '{0}': {1}".format(ds_key, path))

    for rule in config["rules"]:
        for req in ["id", "type", "datasets", "output_field"]:
            if req not in rule:
                raise ValueError("Rule is missing required key '{0}': {1}".format(req, rule))

        if not isinstance(rule["datasets"], list) or len(rule["datasets"]) == 0:
            raise ValueError("Rule '{0}' datasets must be a non-empty list".format(rule["id"]))

        for ds_key in rule["datasets"]:
            if ds_key not in config["datasets"]:
                raise ValueError(
                    "Rule '{0}' references unknown dataset '{1}'".format(rule["id"], ds_key)
                )

        if rule["type"] in ["list_within_distance", "nearest_feature"] and "distance_m" not in rule:
            warn("Rule '{0}' has no distance_m. That may be intentional.".format(rule["id"]))


# ============================================================
# OUTPUT
# ============================================================

def write_csv(output_csv, rows, output_fields):
    ensure_folder_for_file(output_csv)

    with open(output_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    msg("CSV written: {0}".format(output_csv))


def try_write_excel_from_csv(csv_path):
    """
    Optional convenience export if pandas/openpyxl are available.
    """
    try:
        import pandas as pd
        xlsx_path = os.path.splitext(csv_path)[0] + ".xlsx"
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
        df.to_excel(xlsx_path, index=False)
        msg("Excel written: {0}".format(xlsx_path))
    except Exception as ex:
        warn("Could not create XLSX output automatically: {0}".format(ex))


# ============================================================
# MAIN
# ============================================================

def main():
    msg("Loading config...")
    config = load_json(CONFIG_JSON)
    validate_config(config)

    if not arcpy.Exists(BOREHOLE_FC):
        raise ValueError("Borehole feature class does not exist: {0}".format(BOREHOLE_FC))

    if not field_exists(BOREHOLE_FC, BOREHOLE_ID_FIELD):
        raise ValueError("Borehole ID field not found: {0}".format(BOREHOLE_ID_FIELD))

    # Optional target CRS from JSON
    target_sr = None
    if "target_projection" in config:
        wkid = config["target_projection"].get("wkid")
        if wkid:
            target_sr = arcpy.SpatialReference(int(wkid))

    # Prepare boreholes
    borehole_fc = ensure_projected_boreholes(BOREHOLE_FC, target_sr=target_sr)
    borehole_sr = get_sr(borehole_fc)
    msg("Using borehole CRS: {0}".format(borehole_sr.name))

    # Project datasets once if needed
    projected_datasets = {}
    dataset_projection_cache = {}

    for ds_key, ds_cfg in config["datasets"].items():
        ds_path = ds_cfg["path"]
        projected_datasets[ds_key] = project_dataset_if_needed(
            ds_path, borehole_sr, dataset_projection_cache
        )

    # Borehole base fields to carry into output
    borehole_output_fields = config.get("borehole_output_fields", [])
    cursor_fields = get_borehole_fields(borehole_fc, BOREHOLE_ID_FIELD, borehole_output_fields)

    rows_out = []

    msg("Running analysis...")
    with arcpy.da.SearchCursor(borehole_fc, cursor_fields) as cursor:
        for i, row in enumerate(cursor, start=1):
            row_dict_raw = dict(zip(cursor_fields, row))
            bh_id = row_dict_raw[BOREHOLE_ID_FIELD]
            bh_geom = row_dict_raw[get_geometry_token()]

            msg("Processing borehole {0}: {1}".format(i, bh_id))

            out_row = OrderedDict()
            out_row["Borehole_Reference"] = safe_str(bh_id)

            # Carry configured borehole fields through
            for fld in borehole_output_fields:
                if fld == BOREHOLE_ID_FIELD:
                    continue
                out_row[fld] = safe_str(row_dict_raw.get(fld))

            # Evaluate rules
            for rule in config["rules"]:
                try:
                    result = evaluate_rule(bh_geom, config, rule, projected_datasets)
                    out_row[rule["output_field"]] = safe_str(result)
                except Exception as rule_ex:
                    warn(
                        "Rule failed for borehole '{0}', rule '{1}': {2}".format(
                            bh_id, rule["id"], rule_ex
                        )
                    )
                    out_row[rule["output_field"]] = "ERROR"

            rows_out.append(out_row)

    # Build output fields
    output_fields = ["Borehole_Reference"] + [
        f for f in borehole_output_fields if f != BOREHOLE_ID_FIELD
    ] + [rule["output_field"] for rule in config["rules"]]

    write_csv(OUTPUT_CSV, rows_out, output_fields)
    try_write_excel_from_csv(OUTPUT_CSV)

    msg("Done.")


if __name__ == "__main__":
    try:
        arcpy.env.overwriteOutput = True
        main()
    except Exception as ex:
        err(str(ex))
        err(traceback.format_exc())
        raise
