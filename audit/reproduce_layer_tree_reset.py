"""Run in a subprocess: inspect the GIS panel's blocked model reset safely.

Checks reset notifications without dereferencing freed items. The installed Qt
clears selection even with blocked signals: this does NOT reproduce the crash.
"""
from PySide6.QtWidgets import QApplication, QTreeWidget, QTreeWidgetItem

app = QApplication.instance() or QApplication([])
for block_model in (False, True):
    tree = QTreeWidget()
    item = QTreeWidgetItem(["selected TIFF"])
    tree.addTopLevelItem(item)
    tree.setCurrentItem(item)
    resets = []
    tree.model().modelReset.connect(lambda: resets.append(True))
    tree.blockSignals(True)
    tree.model().blockSignals(block_model)
    tree.clear()
    tree.model().blockSignals(False)
    tree.blockSignals(False)
    print({"block_model": block_model, "rows": tree.topLevelItemCount(),
           "reset_signals": len(resets),
           "current_index_still_valid": tree.selectionModel().currentIndex().isValid()},
          flush=True)
    # Reset the selection before Qt teardown can touch the stale item.
    tree.selectionModel().reset()
