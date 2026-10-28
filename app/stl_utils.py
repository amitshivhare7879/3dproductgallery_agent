"""
Reuses the same math your Django app already does for STL weight/dimension
calculation. If your Django project's parser differs (different density
assumptions, infill %, etc.), swap the constants below to match it so prices
stay consistent between the bot and the website.
"""
from . import model_utils

MATERIAL_DENSITY_G_CM3 = model_utils.MATERIAL_DENSITIES["PLA"]
DEFAULT_INFILL_PCT = model_utils.DEFAULT_INFILL_PCT


def analyze_stl(path: str) -> dict:
    return model_utils.analyze_model(path, "model.stl")

