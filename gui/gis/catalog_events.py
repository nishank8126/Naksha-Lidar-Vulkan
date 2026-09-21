"""Single notification channel for live Catalog/model synchronization."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class CatalogEvents(QObject):
    datasourceCreated = Signal(str)
    datasetCreated = Signal(str, str)
    layerCreated = Signal(str, str, str)
    schemaChanged = Signal(str)
    itemDeleted = Signal(str, str)
    itemRenamed = Signal(str, str, str)


_events: CatalogEvents | None = None


def catalog_events() -> CatalogEvents:
    global _events
    if _events is None:
        _events = CatalogEvents()
    return _events


def notify_created(result, *, kind: str, container: str | None = None,
                   parent_dataset: str = "") -> None:
    if result is None or not getattr(result, "success", False):
        return
    bus = catalog_events()
    path = str(getattr(result, "path", "") or container or "")
    name = str(getattr(result, "name", "") or "")
    if kind == "datasource":
        bus.datasourceCreated.emit(path)
    elif kind == "feature_dataset":
        bus.datasetCreated.emit(str(container or path), name)
    elif kind in {"layer", "table"}:
        bus.layerCreated.emit(str(container or path), parent_dataset, name)
    else:
        bus.schemaChanged.emit(str(container or path))
