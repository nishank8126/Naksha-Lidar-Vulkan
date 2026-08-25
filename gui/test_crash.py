# test_crash.py  —  Run this to test crash reporter WITHOUT email
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.crash_reporter import CrashReporter, AppState

# ── Simulate real app state (what the user was doing) ──
AppState.set_tool("BrushClassification")
AppState.set_file("C:/Projects/site_scan/S.MARIA000004.laz")
AppState.set_action("Applying brush stroke on Ground class")
AppState.set_point_count(4_500_000)
AppState.set_display_mode("Classification")

# ── Install the reporter ──
reporter = CrashReporter()
reporter.install()

# ── Trigger a fake crash ──
print("Triggering test crash...")

def fake_processing():
    def inner_function():
        raise MemoryError("Out of GPU memory while rendering point cloud")
    inner_function()

fake_processing()