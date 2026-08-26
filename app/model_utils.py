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

# Default PLA density in g/cm^3. Adjust if your site uses a different
# material/infill assumption.
MATERIAL_DENSITY_G_CM3 = 1.24
DEFAULT_INFILL_PCT = 20  # assume prints are not fully solid

# Safety cap to bound memory on the free-tier host. A compressed 3MF/STL can
# decompress or parse into something far larger in memory than its file
# size suggests -- this stops pathologically dense meshes from OOM-crashing
# the whole service before that happens.
#
# 2,000,000 with the array.array-based vertex storage below uses roughly
# the same real memory the old 400,000-with-Python-tuples version did (the
# switch to array.array cut per-vertex memory ~6x). Sized to comfortably
# cover lithophanes (one vertex per source-image pixel, easily 1-2M+) while
# still failing safely on truly pathological files instead of OOM-crashing.
# If real usage still hits this, lower it back down rather than raise it
# further -- Render's free tier is a hard 512MB ceiling either way.
MAX_VERTICES_PER_MESH = 2_000_000


def analyze_model(path: str, filename: str) -> dict:
    """
    Dispatches to the right parser based on file extension. Explicitly
    forces garbage collection afterward -- mesh parsing can build sizeable
    temporary numpy arrays, and on a memory-capped free-tier host we want
    those released immediately rather than waiting for Python's normal GC
    cycle, since the next request could arrive before that happens.
    """
    lower = filename.lower()
    try:
        if lower.endswith(".stl"):
            dims_mm, volume_mm3 = _parse_stl(path)
        elif lower.endswith(".3mf"):
            dims_mm, volume_mm3 = _parse_3mf(path)
        else:
            raise ValueError(f"Unsupported 3D file type: {filename}")
    finally:
        gc.collect()

    volume_cm3 = abs(volume_mm3) / 1000.0  # mm^3 -> cm^3
    effective_volume_cm3 = volume_cm3 * (DEFAULT_INFILL_PCT / 100.0)
    weight_g = effective_volume_cm3 * MATERIAL_DENSITY_G_CM3

    return {
        "dims_cm": tuple(round(d / 10, 2) for d in dims_mm),
        "volume_cm3": round(volume_cm3, 2),
        "weight_g": round(weight_g, 1),
    }


def _parse_stl(path: str) -> tuple[tuple[float, float, float], float]:
    m = mesh.Mesh.from_file(path)

    if len(m.vectors) * 3 > MAX_VERTICES_PER_MESH:
        raise ValueError(
            f"This STL is extremely dense (over {MAX_VERTICES_PER_MESH:,} vertices). "
            f"Please decimate/simplify it in your slicer or CAD tool before sending -- "
            f"this is a memory safety limit on the free hosting tier, not a bug."
        )

    minx, maxx = m.x.min(), m.x.max()
    miny, maxy = m.y.min(), m.y.max()
    minz, maxz = m.z.min(), m.z.max()
    dims_mm = (float(maxx - minx), float(maxy - miny), float(maxz - minz))

    volume_mm3, _cog, _inertia = m.get_mass_properties()
    return dims_mm, float(volume_mm3)


def _parse_3mf(path: str) -> tuple[tuple[float, float, float], float]:
    """
    A .3mf file is a zip archive. Simple single-part 3MF files have their
    mesh directly in 3D/3dmodel.model, but many slicer/CAD exports (Bambu
    Studio, PrusaSlicer, Fusion360, etc.) split each object into its own
    file under 3D/Objects/*.model and reference them from the root model.
    This scans EVERY .model file in the archive and combines whatever mesh
    geometry it finds, so both layouts work.

    IMPORTANT: uses iterparse (streaming) instead of a full ET.parse, and
    clears each element as it's consumed. A compressed 3MF can decompress
    to XML many times its zip size -- building a full DOM tree for a
    complex mesh can spike memory well past what a free-tier host allows,
    which is what crashed the service. Streaming + an early bail-out on
    excessive vertex count keeps peak memory bounded.

    Ignores per-object <transform> matrices for simplicity -- fine for a
    rough dimension/weight estimate, not exact for scaled/rotated parts.
    """
    all_min = np.array([np.inf, np.inf, np.inf])
    all_max = np.array([-np.inf, -np.inf, -np.inf])
    total_volume_mm3 = 0.0
    found_any = False

    with zipfile.ZipFile(path) as z:
        model_files = [n for n in z.namelist() if n.lower().endswith(".model")]
        if not model_files:
            raise ValueError("No .model files found inside the 3MF archive")

        for name in model_files:
            with z.open(name) as f:
                found, vmin, vmax, volume = _parse_model_stream(f)
            if found:
                found_any = True
                all_min = np.minimum(all_min, vmin)
                all_max = np.maximum(all_max, vmax)
                total_volume_mm3 += volume

    if not found_any:
        raise ValueError(
            "No mesh geometry found in any part of this 3MF file -- it may use an "
            "unsupported/encrypted format. Try exporting as .stl instead."
        )

    dims_mm = tuple(float(d) for d in (all_max - all_min))
    return dims_mm, total_volume_mm3


def _parse_model_stream(fileobj):
    """
    Streams a single .model XML file with iterparse, clearing each element
    once processed so memory stays roughly proportional to one mesh's
    vertex buffer at a time, not the whole document.

    Vertex coordinates are accumulated in array.array('d') (raw C doubles,
    ~8 bytes/value with no per-object overhead) rather than a Python list of
    tuples (~45+ bytes per float once tuple/object overhead is counted).
    That's roughly a 5-6x memory reduction for the vertex buffer, which is
    what let MAX_VERTICES_PER_MESH be raised safely -- lithophanes in
    particular can easily hit 1-2M+ vertices (one per pixel of the source
    image), far more than typical decorative prints.
    """
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    vmin = np.array([np.inf, np.inf, np.inf])
    vmax = np.array([-np.inf, -np.inf, -np.inf])
    total_volume = 0.0
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
            xs, ys, zs = array.array("d"), array.array("d"), array.array("d")  # free the raw buffers

        elif event == "end" and tag == "triangle":
            v1, v2, v3 = int(elem.get("v1")), int(elem.get("v2")), int(elem.get("v3"))
            if len(verts_arr) > max(v1, v2, v3):
                p1, p2, p3 = verts_arr[v1], verts_arr[v2], verts_arr[v3]
                total_volume += np.dot(p1, np.cross(p2, p3)) / 6.0
            elem.clear()

        elif event == "end" and tag == "mesh":
            in_mesh = False
            verts_arr = np.empty((0, 3))
            elem.clear()

        elif event == "end":
            elem.clear()

    return found, vmin, vmax, total_volume
