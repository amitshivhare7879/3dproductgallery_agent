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
import numpy as np
from stl import mesh

# Default PLA density in g/cm^3. Adjust if your site uses a different
# material/infill assumption.
MATERIAL_DENSITY_G_CM3 = 1.24
DEFAULT_INFILL_PCT = 20  # assume prints are not fully solid


def analyze_model(path: str, filename: str) -> dict:
    """Dispatches to the right parser based on file extension."""
    lower = filename.lower()
    if lower.endswith(".stl"):
        dims_mm, volume_mm3 = _parse_stl(path)
    elif lower.endswith(".3mf"):
        dims_mm, volume_mm3 = _parse_3mf(path)
    else:
        raise ValueError(f"Unsupported 3D file type: {filename}")

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

    minx, maxx = m.x.min(), m.x.max()
    miny, maxy = m.y.min(), m.y.max()
    minz, maxz = m.z.min(), m.z.max()
    dims_mm = (float(maxx - minx), float(maxy - miny), float(maxz - minz))

    volume_mm3, _cog, _inertia = m.get_mass_properties()
    return dims_mm, float(volume_mm3)


def _parse_3mf(path: str) -> tuple[tuple[float, float, float], float]:
    """
    A .3mf file is a zip archive; the mesh geometry lives in
    3D/3dmodel.model as XML (<mesh><vertices>/<triangles></mesh>).
    This ignores per-object <transform> matrices for simplicity -- fine for
    a rough dimension/weight estimate, not exact for scaled/rotated parts.
    """
    with zipfile.ZipFile(path) as z:
        with z.open("3D/3dmodel.model") as f:
            tree = ET.parse(f)

    root = tree.getroot()
    # Namespace varies by 3MF version -- match any element ending in the
    # right local tag name instead of hardcoding the namespace URI.
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    all_vertices = []
    all_min = np.array([np.inf, np.inf, np.inf])
    all_max = np.array([-np.inf, -np.inf, -np.inf])
    total_volume_mm3 = 0.0

    for mesh_el in root.iter():
        if local(mesh_el.tag) != "mesh":
            continue

        verts = []
        for vertices_el in mesh_el:
            if local(vertices_el.tag) != "vertices":
                continue
            for v in vertices_el:
                if local(v.tag) != "vertex":
                    continue
                verts.append((float(v.get("x")), float(v.get("y")), float(v.get("z"))))
        verts_arr = np.array(verts)
        if len(verts_arr) == 0:
            continue

        all_min = np.minimum(all_min, verts_arr.min(axis=0))
        all_max = np.maximum(all_max, verts_arr.max(axis=0))

        for triangles_el in mesh_el:
            if local(triangles_el.tag) != "triangles":
                continue
            for t in triangles_el:
                if local(t.tag) != "triangle":
                    continue
                v1, v2, v3 = int(t.get("v1")), int(t.get("v2")), int(t.get("v3"))
                p1, p2, p3 = verts_arr[v1], verts_arr[v2], verts_arr[v3]
                # Signed tetrahedron volume relative to the origin
                total_volume_mm3 += np.dot(p1, np.cross(p2, p3)) / 6.0

    if not np.isfinite(all_min).all():
        raise ValueError("No mesh geometry found in 3MF file")

    dims_mm = tuple(float(d) for d in (all_max - all_min))
    return dims_mm, total_volume_mm3
