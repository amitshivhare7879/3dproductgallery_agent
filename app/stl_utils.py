"""
Reuses the same math your Django app already does for STL weight/dimension
calculation. If your Django project's parser differs (different density
assumptions, infill %, etc.), swap the constants below to match it so prices
stay consistent between the bot and the website.
"""
from stl import mesh
import numpy as np

# Default PLA density in g/cm^3. Adjust if your site uses a different
# material/infill assumption.
MATERIAL_DENSITY_G_CM3 = 1.24
DEFAULT_INFILL_PCT = 20  # assume prints are not fully solid


def analyze_stl(path: str) -> dict:
    m = mesh.Mesh.from_file(path)

    # Bounding box dimensions in mm (STL units are typically mm)
    minx, maxx = m.x.min(), m.x.max()
    miny, maxy = m.y.min(), m.y.max()
    minz, maxz = m.z.min(), m.z.max()
    dims_mm = (float(maxx - minx), float(maxy - miny), float(maxz - minz))

    # Volume via signed tetrahedron sum (mesh.get_mass_properties is convenient)
    volume_mm3, _cog, _inertia = m.get_mass_properties()
    volume_cm3 = abs(volume_mm3) / 1000.0  # mm^3 -> cm^3

    effective_volume_cm3 = volume_cm3 * (DEFAULT_INFILL_PCT / 100.0)
    weight_g = effective_volume_cm3 * MATERIAL_DENSITY_G_CM3

    return {
        "dims_cm": tuple(round(d / 10, 2) for d in dims_mm),
        "volume_cm3": round(volume_cm3, 2),
        "weight_g": round(weight_g, 1),
    }
