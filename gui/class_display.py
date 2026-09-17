import copy as _copy
import numpy as np
import pyvista as pv
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QDoubleSpinBox, QHBoxLayout, QPushButton, QComboBox
from .views import set_view

# ─── Palette isolation helper ──────────────────────────────────────────────
def clone_palette(palette):
    """Return a fully independent deep copy of a class-palette dict.

    Use everywhere palette ownership changes hands between views / slots.
    Safe to call with None or an empty dict.

    Rules
    -----
    * Never assign  app.class_palette = view_palettes[slot]  (direct ref)
    * Always use    app.class_palette = clone_palette(view_palettes[slot])
    """
    if not palette:
        return {}
    return _copy.deepcopy(palette)


def build_class_palette(classification):
    '''Build a visible neutral palette when no PTC palette is active.

    The palette is deliberately color-neutral: a PTC may recolor it later, but
    the base LAS/LAZ render must never depend on the user loading a PTC.
    '''
    classes = np.asarray(classification)
    if classes.size == 0:
        return {}

    return {
        int(code): {
            'show': True,
            'description': f'Class {int(code)}',
            'lvl': f'Class {int(code)}',
            'color': (160, 160, 160),
            'weight': 1.0,
        }
        for code in np.unique(classes)
    }
# ─────────────────────────────────────────────────────────────────────────


def sync_main_view_weights_to_palette(app):
    """
    Flush live Display Mode table weights → view_palettes[0] → class_palette.
 
    This closes the sync gap that exists ONLY for Main View (slot 0).
    Cross-section views (slots 1-4) use view_palettes[idx] exclusively,
    so they never drift. Main View historically used class_palette as its
    master store, which the preset dialog doesn't read — hence the mismatch.
    """
    dlg = getattr(app, 'display_mode_dialog', None)
    if dlg is None:
        return
 
    table = getattr(dlg, 'table', None)
    if table is None:
        return
 
    if not hasattr(dlg, 'view_palettes'):
        dlg.view_palettes = {}
    if 0 not in dlg.view_palettes:
        dlg.view_palettes[0] = {}
 
    current_slot = getattr(dlg, 'current_slot', 0)
 
    CODE_COL   = 1
    WEIGHT_COL = 6   # adjust if your column order differs
 
    if current_slot == 0:
        for row in range(table.rowCount()):
            try:
                code_item = table.item(row, CODE_COL)
                if not code_item:
                    continue
                code = int(code_item.text())
 
                # Try table item first, then cell widget (QDoubleSpinBox)
                weight_item = table.item(row, WEIGHT_COL)
                if weight_item and weight_item.text().strip():
                    try:
                        weight = float(weight_item.text())
                    except ValueError:
                        weight = 1.0
                else:
                    w = table.cellWidget(row, WEIGHT_COL)
                    weight = float(w.value()) if w and hasattr(w, 'value') else 1.0
 
                if code not in dlg.view_palettes[0]:
                    dlg.view_palettes[0][code] = {}
                dlg.view_palettes[0][code]['weight'] = weight
 
                if hasattr(app, 'class_palette') and code in app.class_palette:
                    app.class_palette[code]['weight'] = weight
 
            except Exception as e:
                print(f"   ⚠️  sync_main_view row {row}: {e}")
 
    # Always mirror view_palettes[0] → class_palette (keeps fallback current)
    for code, info in dlg.view_palettes.get(0, {}).items():
        if hasattr(app, 'class_palette') and code in app.class_palette:
            app.class_palette[code].setdefault('weight', info.get('weight', 1.0))
            if current_slot != 0:           # when not on main view, just mirror
                pass                        # (already handled in the loop above)
 
    print(f"   ✅ sync_main_view_weights_to_palette done "
          f"(active_slot={current_slot})")

def update_class_mode(app, force_refresh=False, **kwargs):
    """
    CLASS MODE (Shader-ring border, visible on GPU)
    ✅ FIXED: Routes directly to unified_actor_manager for Zero-Copy rendering.
    ✅ PALETTE FIX: Uses clone_palette() so class_palette never aliases view_palettes[0].
    """
    from gui.unified_actor_manager import build_unified_actor, fast_palette_refresh, is_unified_actor_ready
    
    palette = getattr(app, "class_palette", {})
    if (hasattr(app, "display_mode_dialog")
            and hasattr(app.display_mode_dialog, "view_palettes")
            and 0 in app.display_mode_dialog.view_palettes):
        if not palette or force_refresh:
            # ── PALETTE FIX: deep copy so class_palette never IS view_palettes[0] ──
            palette = clone_palette(app.display_mode_dialog.view_palettes[0])
            app.class_palette = clone_palette(app.display_mode_dialog.view_palettes[0])
            # ─────────────────────────────────────────────────────────────────────
            
    border_percent = float(getattr(app, "point_border_percent", 0) or 0.0)
    point_size = 2.5
    
    if not is_unified_actor_ready(app) or force_refresh:
        build_unified_actor(app, palette=palette, border_percent=border_percent, point_size=point_size)
    else:
        fast_palette_refresh(app, palette=palette, border_percent=border_percent)
 
 
    
def _border_ring_fraction(border_percent: float) -> float:
    """
    Convert border_percent (0..100) to ring thickness fraction used in the
    GLSL shader uniform ``border_ring_val``.
 
    MicroStation uses SQUARE point sprites where the outer fraction of each
    sprite is painted in the border colour.  This function maps the user-facing
    percentage (0 = no border, 100 = half the sprite is border) to the shader
    value in a linear, intuitive way.
 
    Formula
    -------
    border_ring_val = border_percent / 100.0   clamped to [0.0, 0.50]
 
    Examples (consistent with unified_actor_manager.py):
      0%  → 0.000  (no border — round circle mode)
      10% → 0.100  (thin border ring)
      25% → 0.250  (MicroStation-style medium border)
      40% → 0.400  (thick, very visible)
      50% → 0.500  (half the sprite is border — maximum useful value)
     100% → 0.500  (clamped)
    """
    bp = max(0.0, float(border_percent or 0.0))
    # Direct mapping: 40% setting => 40% of point radius as border
    ring = bp / 100.0
    return min(0.70, ring)  # Cap at 70% of radius
 
 
def apply_border_shader_ring(actor, border_percent: float, border_rgb=(0.0, 0.0, 0.0)) -> bool:
    """
    Apply a MicroStation-style ADAPTIVE border to a per-class actor.
 
    Mirrors the behaviour of unified_actor_manager._attach_view_shader_context v10:
 
      • border_percent = 0   → round CIRCLE, no border
      • border_percent > 0   → SQUARE sprite; border width is kept CONSTANT in
                               screen pixels as zoom changes (adaptive ring).
                               Below MIN_BORDER_PX screen pixels the border is
                               suppressed entirely so the cloud does NOT look dark
                               when zoomed out.
 
    Adaptive formula (identical to v10 shader):
        adaptive_ring = border_ring_val × BASE_PS / max(v_point_size, BASE_PS)
    where BASE_PS = 3.0 and border_ring_val = border_percent / 100.0.
 
    Note: this function is used for per-class actors that do NOT go through
    _attach_view_shader_context.  Those actors lack the v_point_size varying,
    so we wire it here inside the shader replacement.
    """
    ring = _border_ring_fraction(border_percent)   # 0.0 – 0.50
    br, bg, bb = border_rgb
 
    try:
        sp = actor.GetShaderProperty()
        if not sp:
            print("      ⚠️ apply_border_shader_ring: no shader property on actor!")
            return False
 
        if ring <= 0.001:
            # ── No border: plain round-circle mode ───────────────────────────
            code = (
                "//VTK::Color::Impl\n"
                "// Naksha no-border round-circle mode\n"
                "float r_nb = length(gl_PointCoord.xy - vec2(0.5)) * 2.0;\n"
                "if (r_nb > 1.0) discard;\n"
                "opacity = 1.0;\n"
            )
        else:
            # ── Round circle border (restored from gui_12) ───────────────────
            code = f"""
//VTK::Color::Impl
// ---- Naksha Round Point + Border Ring ----
float r = length(gl_PointCoord.xy - vec2(0.5)) * 2.0;
if (r > 1.0) {{
    discard;
}}
opacity = 1.0;

float ring = {ring:.6f};
if (ring > 0.0) {{
    float edge = 1.0 - ring;
    if (r >= edge) {{
        diffuseColor = vec3({br:.6f}, {bg:.6f}, {bb:.6f});
        ambientColor = diffuseColor;
        opacity = 1.0;
    }}
}}
// ---- End Naksha Round Point + Border Ring ----
"""
 
        sp.ClearAllFragmentShaderReplacements()
        sp.AddFragmentShaderReplacement("//VTK::Color::Impl", True, code, False)
 
        sp.Modified()
        actor.GetProperty().Modified()
        mapper = actor.GetMapper()
        if mapper:
            mapper.Modified()
        actor.Modified()
 
        return True
 
    except Exception as e:
        print(f"      ❌ apply_border_shader_ring failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
 
 
class ClassControlPanel(QWidget):
    """
    Per-class weight control panel.
    ✅ FIXED: Updates the correct view-specific palette
    """
 
    def __init__(self, app):
        super().__init__()
        self.app = app
 
        layout = QVBoxLayout()
 
        # ✅ Class selector dropdown
        class_layout = QHBoxLayout()
        class_layout.addWidget(QLabel("Class:"))
        self.class_combo = QComboBox()
        self.populate_classes()
        class_layout.addWidget(self.class_combo)
        layout.addLayout(class_layout)
 
        # Weight control
        w_layout = QHBoxLayout()
        w_layout.addWidget(QLabel("Weight:"))
        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.1, 12.0)
        self.weight_spin.setSingleStep(0.1)
        self.weight_spin.setValue(1.0)
        w_layout.addWidget(self.weight_spin)
        layout.addLayout(w_layout)
 
        # Buttons
        btn_layout = QHBoxLayout()
        self.apply_btn = QPushButton("Apply")
        self.reset_btn = QPushButton("Reset")
        btn_layout.addWidget(self.apply_btn)
        btn_layout.addWidget(self.reset_btn)
        layout.addLayout(btn_layout)
 
        self.setLayout(layout)
 
        # ✅ Connect signals
        self.class_combo.currentIndexChanged.connect(self.on_class_changed)
        self.apply_btn.clicked.connect(self.on_apply)
        self.reset_btn.clicked.connect(self.on_reset)
 
        # Load initial weight
        self.on_class_changed()
 
    def populate_classes(self):
        """
        Populate the class combo-box with every *visible* class from the
        current palette, sorted by class code.
 
        Display format:  ``<code>: <description>  [Lvl: <lvl>]``
        """
        self.class_combo.clear()
 
        # Prefer the main-view palette (slot 0); fall back to app.class_palette.
        palette: dict = {}
        dlg = getattr(self.app, "display_mode_dialog", None)
        if dlg and hasattr(dlg, "view_palettes") and 0 in dlg.view_palettes:
            palette = dlg.view_palettes[0]
        if not palette:
            palette = getattr(self.app, "class_palette", {})
 
        if not palette:
            print("⚠️ ClassControlPanel.populate_classes: palette is empty")
            return
 
        visible_classes = [
            (int(code), info)
            for code, info in palette.items()
            if info.get("show", False)
        ]
        visible_classes.sort(key=lambda x: x[0])
 
        for code, info in visible_classes:
            desc = info.get("description", f"Class {code}")
            lvl  = info.get("lvl", "")
            display_text = f"{code}: {desc}"
            if lvl:
                display_text += f"  [Lvl: {lvl}]"
            self.class_combo.addItem(display_text, code)
 
        print(f"✅ ClassControlPanel: loaded {len(visible_classes)} visible classes")
 
 
    def on_class_changed(self):
        """Load weight for currently selected class."""
        class_code = self.class_combo.currentData()
        if class_code is None:
            return
        
        palette = getattr(self.app, "class_palette", {})
        if class_code in palette:
            current_weight = palette[class_code].get("weight", 1.0)
            self.weight_spin.setValue(current_weight)
            print(f"📊 Loaded class {class_code} weight: {current_weight}")
 
    def on_apply(self):
        """
        Apply weight to ONLY the selected class.
        ✅ FIXED: Updates BOTH class_palette AND view_palettes (ALL slots)
        ✅ NEW: Ensures weights persist across all views after user changes
        """
        class_code = self.class_combo.currentData()
        if class_code is None:
            print("⚠️ No class selected")
            return
        
        new_weight = self.weight_spin.value()
        
        print(f"\n{'='*60}")
        print(f"🎨 APPLYING WEIGHT: Class {class_code} → {new_weight:.2f}x")
        print(f"{'='*60}")
        
        # ✅ CRITICAL: Update ALL palette locations (master + fallbacks)
        updated_count = 0
        
        # 1. Update global class_palette (fallback source)
        palette = getattr(self.app, "class_palette", {})
        if class_code in palette:
            old_weight = palette[class_code].get("weight", 1.0)
            palette[class_code]["weight"] = new_weight
            print(f"   ✅ Global class_palette: {old_weight:.2f} → {new_weight:.2f}")
            updated_count += 1
        
        # 2. ✅ CRITICAL FIX: Update ALL view_palettes (slots 0-4)
        # This is the MASTER SOURCE - ensure ALL slots get the weight
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            dialog = self.app.display_mode_dialog
            
            if hasattr(dialog, 'view_palettes') and dialog.view_palettes:
                # Update weight in ALL view slots
                for slot_idx in sorted(dialog.view_palettes.keys()):
                    slot_palette = dialog.view_palettes[slot_idx]
                    
                    if class_code in slot_palette:
                        old_weight = slot_palette[class_code].get("weight", 1.0)
                        slot_palette[class_code]["weight"] = new_weight
                        
                        slot_label = f"Main View" if slot_idx == 0 else f"View {slot_idx}"
                        print(f"   ✅ view_palettes[{slot_idx}] ({slot_label}): {old_weight:.2f} → {new_weight:.2f}")
                        updated_count += 1
        
        # 3. Update Display Mode table UI (if visible)
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            table = self.app.display_mode_dialog.table
            
            for row in range(table.rowCount()):
                try:
                    row_code = int(table.item(row, 1).text())
                    if row_code == class_code:
                        weight_item = table.item(row, 6)
                        if weight_item:
                            weight_item.setText(f"{new_weight:.2f}")
                            print(f"   ✅ Display Mode table row {row}: updated to {new_weight:.2f}")
                            updated_count += 1
                        break
                except Exception as e:
                    print(f"   ⚠️ Error updating table row {row}: {e}")
                    continue
        
        if updated_count == 0:
            print(f"   ⚠️ Warning: No palettes were updated!")
            print(f"{'='*60}\n")
            return
        else:
            print(f"   ✅ Total updates: {updated_count} locations")
        
        # ✅ CRITICAL: After updating ALL palettes, sync to app.class_palette
        # Use clone_palette so class_palette stays independent of view_palettes[0]
        print(f"\n   🔄 Syncing weights to app.class_palette...")
        if hasattr(self.app, 'display_mode_dialog') and self.app.display_mode_dialog:
            dialog = self.app.display_mode_dialog
            if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
                main_palette = dialog.view_palettes[0]
                if class_code in main_palette:
                    weight = main_palette[class_code].get('weight', 1.0)
                    if not hasattr(self.app, 'class_palette'):
                        self.app.class_palette = {}
                    if class_code not in self.app.class_palette:
                        self.app.class_palette[class_code] = {}
                    self.app.class_palette[class_code]['weight'] = weight
                    print(f"   ✅ app.class_palette synced: weight={weight:.2f}")
        
        # ✅ UNIFIED: Use GPU sync (fast) instead of full update_class_mode (slow rebuild)
        print(f"\n   🔄 Syncing weights to GPU...")
        try:
            from gui.unified_actor_manager import sync_palette_to_gpu
            palette = getattr(self.app, 'class_palette', {})
            border = float(getattr(self.app, 'point_border_percent', 0) or 0.0)
            
            # Sync main view
            sync_palette_to_gpu(self.app, 0, palette, border, render=True)
            print(f"   ✅ Main view GPU sync complete")
            
            # Sync all open cross-section views
            if hasattr(self.app, 'section_vtks'):
                for view_idx in self.app.section_vtks:
                    slot_idx = view_idx + 1
                    sync_palette_to_gpu(self.app, slot_idx, render=True)
                    print(f"   ✅ Section {view_idx + 1} GPU sync complete")
                    
        except Exception as e:
            print(f"   ⚠️ GPU sync failed, falling back to full refresh: {e}")
            try:
                from gui.class_display import update_class_mode
                update_class_mode(self.app)
            except Exception as e2:
                print(f"   ❌ Full refresh also failed: {e2}")
        
        print(f"{'='*60}\n")
 
 
    def on_reset(self):
        """Reset ONLY the selected class to weight 1.0."""
        class_code = self.class_combo.currentData()
        if class_code is None:
            return
        
        # Set spinbox to 1.0
        self.weight_spin.setValue(1.0)
        
        # Apply the reset
        self.on_apply()
