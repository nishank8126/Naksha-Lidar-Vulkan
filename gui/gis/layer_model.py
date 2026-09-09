"""Canonical datasource-backed GIS layer descriptor used by Naksha.

The object describes native data on disk/database.  VTK actors are only a render
cache and the Digitizer is only an edit/create overlay; neither is the source of
truth for imported GIS datasets.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class GISLayerModel:
    path: str
    layer_name: str
    driver: str = ""
    kind: str = "vector"          # vector | table | raster
    geometry_type: str = ""
    feature_count: int = -1
    source_crs_wkt: Optional[str] = None
    project_crs_wkt: Optional[str] = None
    fields: list[dict[str, Any]] = field(default_factory=list)
    has_z: Optional[bool] = None
    has_m: Optional[bool] = None
    feature_dataset: Optional[str] = None
    read_only: bool = True

    @property
    def source_uri(self) -> str:
        return f"{self.path}|layer={self.layer_name}" if self.layer_name else self.path

    def registry_metadata(self) -> dict[str, Any]:
        return {
            "native_model": self,
            "storage_model": "ogr-native",
            "source_uri": self.source_uri,
            "source_layer": self.layer_name,
            "source_driver": self.driver,
            "geometry_type": self.geometry_type,
            "feature_count": max(0, int(self.feature_count)) if self.feature_count >= 0 else self.feature_count,
            "source_crs_wkt": self.source_crs_wkt,
            "project_crs_wkt": self.project_crs_wkt,
            "fields": list(self.fields),
            "has_z": self.has_z,
            "has_m": self.has_m,
            "feature_dataset": self.feature_dataset,
        }

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source_uri"] = self.source_uri
        return data


def attach_native_model(entry: dict | None, model: GISLayerModel) -> dict | None:
    if entry is not None:
        entry.update(model.registry_metadata())
    return entry
