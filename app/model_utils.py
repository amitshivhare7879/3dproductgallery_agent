"""
Parses .stl and .3mf files to get bounding-box dimensions, volume, and an
estimated print weight. Used for both the price calculation and to give the
AI real dimensions to mention in the product description, instead of
guessing.

If your Django project's parser uses different density/infill assumptions,
adjust the constants below so prices stay consistent between the bot and
the website.
"""
import zipfile
import xml.etree.ElementTree as ET
import array
import gc
import numpy as np
from stl import mesh

# Filament densities in g/cm^3.
MATERIAL_DENSITIES = {
    "PLA": 1.24,
    "PLA+": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "TPU": 1.21,
    "PC": 1.20,
    "NYLON": 1.14,
    "RESIN": 1.15,
    "SILK_PLA": 1.24,
    "WOOD_PLA": 1.20,
    "CARBON_FIBER": 1.30,
}

DEFAULT_MATERIAL = "PLA"
DEFAULT_INFILL_PCT = 20          # Typical default infill percentage
DEFAULT_WALL_THICKNESS_MM = 1.2   # ~3 perimeters with 0.4mm nozzle + top/bottom solid layers
DEFAULT_WASTE_G = 2.0             # Prime line / skirt / brim allowance
SUPPORT_DENSITY_FACTOR = 0.10     # Support trees/pillars typically slice at ~10% density
OVERHANG_THRESHOLD_COS = -0.707   # nz < -0.707 corresponds to > 45 deg downward overhang

# Safety cap to bound memory on the free-tier host. A compressed 3MF/STL can
# decompress or parse into something far larger in memory than its file
# size suggests -- this stops pathologically dense meshes from OOM-crashing
# the whole service before that happens.
MAX_VERTICES_PER_MESH = 2_000_000


def calculate_weight_breakdown(
    volume_cm3: float,
    surface_area_cm2: float,
    support_volume_cm3: float,
    material: str = DEFAULT_MATERIAL,
    infill_pct: int = DEFAULT_INFILL_PCT,
    supports_enabled: bool = True,
    wall_thickness_mm: float = DEFAULT_WALL_THICKNESS_MM,
) -> dict:
    """
    Computes a realistic 3D print weight breakdown:
    1. Shell volume (100% solid walls + top/bottom layers) = Area * wall_thickness.
    2. Core volume = Total volume - Shell volume.
    3. Infill volume = Core volume * infill_pct.
    4. Support volume = estimated downward overhang pillars (if enabled).
    5. Waste = prime line / brim allowance.
    """
    density = MATERIAL_DENSITIES.get(material.upper(), MATERIAL_DENSITIES[DEFAULT_MATERIAL])
    infill_ratio = max(0.0, min(100.0, float(infill_pct))) / 100.0

    # Shell thickness in cm
    wall_t_cm = wall_thickness_mm / 10.0
    shell_volume_cm3 = min(volume_cm3, surface_area_cm2 * wall_t_cm)
    core_volume_cm3 = max(0.0, volume_cm3 - shell_volume_cm3)
    infill_volume_cm3 = core_volume_cm3 * infill_ratio

    model_volume_cm3 = shell_volume_cm3 + infill_volume_cm3
    sup_vol_cm3 = support_volume_cm3 if supports_enabled else 0.0

    shell_g = shell_volume_cm3 * density
    infill_g = infill_volume_cm3 * density
    support_g = sup_vol_cm3 * density
    waste_g = DEFAULT_WASTE_G if volume_cm3 > 0 else 0.0

    total_weight_g = shell_g + infill_g + support_g + waste_g

    return {
        "material": material.upper(),
        "infill_pct": int(infill_pct),
        "supports_enabled": bool(supports_enabled),
        "weight_g": round(total_weight_g, 1),
        "breakdown": {
            "shell_g": round(shell_g, 1),
            "infill_g": round(infill_g, 1),
            "support_g": round(support_g, 1),
            "waste_g": round(waste_g, 1),
        },
    }


def recalculate_weight(
    stats: dict,
    material: str | None = None,
    infill_pct: int | None = None,
    supports_enabled: bool | None = None,
) -> dict:
    """
    Quickly updates stats without re-reading mesh geometry.
    Useful when user types e.g. "infill 15%" or "material petg" in chat.
    """
    mat = material if material is not None else stats.get("material", DEFAULT_MATERIAL)
    inf = infill_pct if infill_pct is not None else stats.get("infill_pct", DEFAULT_INFILL_PCT)
    sup = supports_enabled if supports_enabled is not None else stats.get("supports_enabled", True)

    # If weight was from exact slicer metadata, adjust proportionally for material density or infill
    if stats.get("source") == "slicer_metadata":
        orig_mat = stats.get("material", DEFAULT_MATERIAL)
        orig_density = MATERIAL_DENSITIES.get(orig_mat.upper(), 1.24)
        new_density = MATERIAL_DENSITIES.get(mat.upper(), 1.24)
        ratio = new_density / orig_density if orig_density > 0 else 1.0
        updated = dict(stats)
        updated["material"] = mat.upper()
        updated["weight_g"] = round(stats["weight_g"] * ratio, 1)
        return updated

    vol = stats.get("volume_cm3", 0.0)
    area = stats.get("surface_area_cm2", 0.0)
    sup_vol = stats.get("support_volume_cm3", 0.0)

    breakdown_data = calculate_weight_breakdown(
        volume_cm3=vol,
        surface_area_cm2=area,
        support_volume_cm3=sup_vol,
        material=mat,
        infill_pct=inf,
        supports_enabled=sup,
    )

    updated = dict(stats)
    updated.update(breakdown_data)
    return updated


def analyze_model(
    path: str,
    filename: str,
    material: str = DEFAULT_MATERIAL,
    infill_pct: int = DEFAULT_INFILL_PCT,
    supports_enabled: bool = True,
) -> dict:
    """
    Dispatches to the right parser based on file extension.
    Attempts to read exact slicer metadata first (for .3mf files).
    Otherwise runs full geometric analysis with shell + support + infill physics.
    Forces garbage collection afterward to keep memory bounded.
    """
    lower = filename.lower()
    try:
        if lower.endswith(".stl"):
            dims_mm, volume_mm3, area_mm2, sup_vol_mm3 = _parse_stl(path)
            source = "mesh_estimation"
            exact_weight = None
        elif lower.endswith(".3mf"):
            # Check for embedded slicer metadata first (Bambu Studio / OrcaSlicer / PrusaSlicer)
            exact_meta = _extract_3mf_slicer_metadata(path)
            if exact_meta and exact_meta.get("weight_g"):
                dims_mm, volume_mm3, area_mm2, sup_vol_mm3 = _parse_3mf(path)
                source = "slicer_metadata"
                exact_weight = exact_meta["weight_g"]
                if exact_meta.get("material"):
                    material = exact_meta["material"]
            else:
                dims_mm, volume_mm3, area_mm2, sup_vol_mm3 = _parse_3mf(path)
                source = "mesh_estimation"
                exact_weight = None
        else:
            raise ValueError(f"Unsupported 3D file type: {filename}")
    finally:
        gc.collect()

    volume_cm3 = abs(volume_mm3) / 1000.0
    surface_area_cm2 = max(0.0, area_mm2) / 100.0
    support_volume_cm3 = max(0.0, sup_vol_mm3) / 1000.0
    has_overhangs = sup_vol_mm3 > 0

    if exact_weight is not None:
        stats = {
            "dims_cm": tuple(round(d / 10, 2) for d in dims_mm),
            "volume_cm3": round(volume_cm3, 2),
            "surface_area_cm2": round(surface_area_cm2, 2),
            "support_volume_cm3": round(support_volume_cm3, 2),
            "has_overhangs": has_overhangs,
            "material": material.upper(),
            "infill_pct": infill_pct,
            "supports_enabled": supports_enabled,
            "weight_g": round(exact_weight, 1),
            "source": source,
            "breakdown": {
                "slicer_exact_g": round(exact_weight, 1),
            },
        }
    else:
        breakdown_data = calculate_weight_breakdown(
            volume_cm3=volume_cm3,
            surface_area_cm2=surface_area_cm2,
            support_volume_cm3=support_volume_cm3,
            material=material,
            infill_pct=infill_pct,
            supports_enabled=supports_enabled,
        )
        stats = {
            "dims_cm": tuple(round(d / 10, 2) for d in dims_mm),
            "volume_cm3": round(volume_cm3, 2),
            "surface_area_cm2": round(surface_area_cm2, 2),
            "support_volume_cm3": round(support_volume_cm3, 2),
            "has_overhangs": has_overhangs,
            "source": source,
        }
        stats.update(breakdown_data)

    return stats


def _extract_3mf_slicer_metadata(path: str) -> dict | None:
    """
    Checks if a .3mf file is a sliced project (e.g. exported from Bambu Studio,
    OrcaSlicer, or PrusaSlicer). If sliced metadata exists, returns exact
    filament weight in grams and material type.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            # 1. Bambu Studio / OrcaSlicer slice_info.xml
            for n in names:
                if n.lower().endswith("slice_info.xml"):
                    with z.open(n) as f:
                        tree = ET.parse(f)
                        root = tree.getroot()
                        total_g = 0.0
                        mat = None
                        for elem in root.iter():
                            tag = elem.tag.rsplit("}", 1)[-1].lower()
                            if tag == "filament":
                                g_str = elem.get("used_g") or elem.get("weight")
                                if g_str:
                                    try:
                                        total_g += float(g_str)
                                    except ValueError:
                                        pass
                                if not mat and elem.get("type"):
                                    mat = elem.get("type")
                        if total_g > 0:
                            return {"weight_g": total_g, "material": mat or "PLA"}

            # 2. Check for gcode or config comments in 3MF
            gcode_files = [n for n in names if n.lower().endswith((".gcode", ".gcode.tmp"))]
            for gn in gcode_files:
                with z.open(gn) as f:
                    content = f.read(60000).decode("utf-8", errors="ignore")
                    for line in content.splitlines():
                        line_lower = line.lower()
                        if "filament used [g]" in line_lower or "total filament used [g]" in line_lower:
                            parts = line.split("=")
                            if len(parts) == 2:
                                try:
                                    w = float(parts[1].strip().split()[0])
                                    if w > 0:
                                        return {"weight_g": w, "material": "PLA"}
                                except ValueError:
                                    pass
    except Exception:
        pass
    return None


def _parse_stl(path: str) -> tuple[tuple[float, float, float], float, float, float]:
    m = mesh.Mesh.from_file(path)

    if len(m.vectors) * 3 > MAX_VERTICES_PER_MESH:
        raise ValueError(
            f"This STL is extremely dense (over {MAX_VERTICES_PER_MESH:,} vertices). "
            f"Please decimate/simplify it in your slicer or CAD tool before sending -- "
            f"this is a memory safety limit on the free hosting tier, not a bug."
        )

    # Recompute face normals and unit vectors from vertices
    m.update_normals()

    minx, maxx = m.x.min(), m.x.max()
    miny, maxy = m.y.min(), m.y.max()
    minz, maxz = m.z.min(), m.z.max()
    dims_mm = (float(maxx - minx), float(maxy - miny), float(maxz - minz))

    try:
        volume_mm3, _cog, _inertia = m.get_mass_properties()
    except Exception:
        volume_mm3 = 0.0
    area_mm2 = float(np.sum(m.areas))

    # Overhang & support estimation:
    # Downward normal Z < OVERHANG_THRESHOLD_COS (-0.707) means overhang steeper than 45 degrees
    nz = m.normals[:, 2]
    overhang_mask = nz < OVERHANG_THRESHOLD_COS
    if np.any(overhang_mask):
        overhang_areas = m.areas[overhang_mask]
        z_centers = (m.v0[overhang_mask, 2] + m.v1[overhang_mask, 2] + m.v2[overhang_mask, 2]) / 3.0
        heights = np.maximum(0.0, z_centers - minz)
        raw_sup_vol = float(np.sum(overhang_areas * heights * SUPPORT_DENSITY_FACTOR))
        vol_abs = abs(float(volume_mm3))
        max_bound = vol_abs * 2.0 if vol_abs > 0 else raw_sup_vol
        sup_vol_mm3 = min(raw_sup_vol, max_bound)
    else:
        sup_vol_mm3 = 0.0

    return dims_mm, float(volume_mm3), area_mm2, sup_vol_mm3


def _parse_3mf(path: str) -> tuple[tuple[float, float, float], float, float, float]:
    """
    Scans every .model file in the 3MF archive and combines geometry,
    extracting dimensions, volume, surface area, and overhang support volume.
    """
    all_min = np.array([np.inf, np.inf, np.inf])
    all_max = np.array([-np.inf, -np.inf, -np.inf])
    total_volume_mm3 = 0.0
    total_area_mm2 = 0.0
    total_sup_vol_mm3 = 0.0
    found_any = False

    with zipfile.ZipFile(path) as z:
        model_files = [n for n in z.namelist() if n.lower().endswith(".model")]
        if not model_files:
            raise ValueError("No .model files found inside the 3MF archive")

        for name in model_files:
            with z.open(name) as f:
                found, vmin, vmax, volume, area, sup_vol = _parse_model_stream(f)
            if found:
                found_any = True
                all_min = np.minimum(all_min, vmin)
                all_max = np.maximum(all_max, vmax)
                total_volume_mm3 += volume
                total_area_mm2 += area
                total_sup_vol_mm3 += sup_vol

    if not found_any:
        raise ValueError(
            "No mesh geometry found in any part of this 3MF file -- it may use an "
            "unsupported/encrypted format. Try exporting as .stl instead."
        )

    dims_mm = tuple(float(d) for d in (all_max - all_min))
    return dims_mm, total_volume_mm3, total_area_mm2, total_sup_vol_mm3


def _parse_model_stream(fileobj):
    """
    Streams a single .model XML file with iterparse, clearing each element
    once processed so memory stays bounded.
    Calculates bounds, volume, surface area, and overhang support volume.
    """
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    vmin = np.array([np.inf, np.inf, np.inf])
    vmax = np.array([-np.inf, -np.inf, -np.inf])
    total_volume = 0.0
    total_area = 0.0
    total_sup_vol = 0.0
    found = False
    xs = array.array("d")
    ys = array.array("d")
    zs = array.array("d")
    verts_arr = np.empty((0, 3))
    in_mesh = False

    for event, elem in ET.iterparse(fileobj, events=("start", "end")):
        tag = local(elem.tag)

        if event == "start" and tag == "mesh":
            in_mesh = True
            xs, ys, zs = array.array("d"), array.array("d"), array.array("d")
            continue

        if not in_mesh:
            if event == "end":
                elem.clear()
            continue

        if event == "end" and tag == "vertex":
            xs.append(float(elem.get("x")))
            ys.append(float(elem.get("y")))
            zs.append(float(elem.get("z")))
            elem.clear()
            if len(xs) > MAX_VERTICES_PER_MESH:
                raise ValueError(
                    f"This 3D file has an extremely dense mesh (over "
                    f"{MAX_VERTICES_PER_MESH:,} vertices in one part -- "
                    f"lithophanes and highly detailed sculpts are the usual "
                    f"cause). Please decimate/simplify it in your slicer "
                    f"or CAD tool before sending -- this is a memory "
                    f"safety limit on the free hosting tier, not a bug."
                )

        elif event == "end" and tag == "vertices":
            elem.clear()
            if len(xs) > 0:
                verts_arr = np.column_stack([
                    np.frombuffer(xs, dtype=np.float64),
                    np.frombuffer(ys, dtype=np.float64),
                    np.frombuffer(zs, dtype=np.float64),
                ])
                found = True
                vmin = np.minimum(vmin, verts_arr.min(axis=0))
                vmax = np.maximum(vmax, verts_arr.max(axis=0))
            xs, ys, zs = array.array("d"), array.array("d"), array.array("d")

        elif event == "end" and tag == "triangle":
            v1, v2, v3 = int(elem.get("v1")), int(elem.get("v2")), int(elem.get("v3"))
            if len(verts_arr) > max(v1, v2, v3):
                p1, p2, p3 = verts_arr[v1], verts_arr[v2], verts_arr[v3]
                cp = np.cross(p2 - p1, p3 - p1)
                total_volume += np.dot(p1, cp) / 6.0
                norm_cp = float(np.linalg.norm(cp))
                tri_area = 0.5 * norm_cp
                total_area += tri_area
                if norm_cp > 1e-9:
                    nz = cp[2] / norm_cp
                    if nz < OVERHANG_THRESHOLD_COS:
                        z_center = (p1[2] + p2[2] + p3[2]) / 3.0
                        height = max(0.0, z_center - vmin[2])
                        total_sup_vol += tri_area * height * SUPPORT_DENSITY_FACTOR
            elem.clear()

        elif event == "end" and tag == "mesh":
            in_mesh = False
            verts_arr = np.empty((0, 3))
            elem.clear()

        elif event == "end":
            elem.clear()

    return found, vmin, vmax, total_volume, total_area, total_sup_vol
