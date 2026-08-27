from types import SimpleNamespace

from gui.cross_section.interactor_classify import ClassificationInteractor


def _bare_interactor(app):
    interactor = ClassificationInteractor.__new__(ClassificationInteractor)
    interactor.app = app
    return interactor


def test_view_switch_cancels_only_unfinished_section_and_cut_gestures():
    app = SimpleNamespace()
    current = _bare_interactor(app)

    calls = []

    class Peer:
        def __init__(self, name, pending):
            self.name = name
            self.pending = pending

        def _cancel_incomplete_classification_gesture(self):
            calls.append(self.name)
            return self.pending

    pending_section = Peer("section", True)
    idle_section = Peer("idle", False)
    pending_cut = Peer("cut", True)
    main_view = Peer("main", True)

    app.classify_interactors = {
        0: pending_section,
        1: current,
        2: idle_section,
        3: pending_section,  # Duplicate references must be cancelled only once.
    }
    app.cut_classify_interactor = pending_cut
    app.classify_interactor = main_view

    cancelled = current._cancel_incomplete_classification_gestures_in_other_views()

    assert cancelled == 2
    assert calls == ["section", "idle", "cut"]
    assert "main" not in calls


def test_cancelling_incomplete_gesture_resets_preview_state_but_keeps_tool_active():
    app = SimpleNamespace(
        active_classify_tool="above_line",
        _classification_preview_active=True,
        _suppress_section_refresh=True,
    )
    peer = _bare_interactor(app)
    peer.P1 = (1.0, 2.0, 3.0)
    peer.P1_display_cut = (10, 20)
    peer.is_dragging = True
    peer.click_to_finalize = True
    peer.is_drawing_freehand = False
    peer.drawing_points = []
    peer.drawing_points_display_cut = []
    peer.drawing_points_world_cut = []
    peer._gesture_tool = "above_line"
    peer._press_pos = (10, 20)
    peer._line_press_max_move_px = 12.0
    peer._last_line_preview_P2 = (4.0, 5.0, 6.0)
    peer._last_rectangle_preview_P2 = None
    peer._last_circle_preview_P2 = None

    stopped = []
    restored_text = []
    cleared = []
    peer._stop_deferred_left_release_watch = lambda: stopped.append(True)
    peer._suppress_snt_text = lambda hidden: restored_text.append(hidden)

    def clear_previews():
        cleared.append(True)
        peer.P1 = None
        peer.P1_display_cut = None
        peer.is_dragging = False
        peer.click_to_finalize = False

    peer._clear_all_previews = clear_previews

    assert peer._cancel_incomplete_classification_gesture() is True
    assert stopped == [True]
    assert restored_text == [False]
    assert cleared == [True]
    assert peer._gesture_tool is None
    assert peer._press_pos is None
    assert peer._last_line_preview_P2 is None
    assert app._classification_preview_active is False
    assert app._suppress_section_refresh is False
    assert app.active_classify_tool == "above_line"


def test_idle_peer_is_not_cleared_or_rendered():
    app = SimpleNamespace(active_classify_tool="rectangle")
    peer = _bare_interactor(app)
    peer.P1 = None
    peer.P1_display_cut = None
    peer.is_dragging = False
    peer.click_to_finalize = False
    peer.is_drawing_freehand = False
    peer.drawing_points = []
    peer.drawing_points_display_cut = []
    peer.drawing_points_world_cut = []
    peer._last_line_preview_P2 = None
    peer._last_rectangle_preview_P2 = None
    peer._last_circle_preview_P2 = None
    peer._clear_all_previews = lambda: (_ for _ in ()).throw(
        AssertionError("idle peer should not be cleared")
    )

    assert peer._cancel_incomplete_classification_gesture() is False
    assert app.active_classify_tool == "rectangle"


def test_live_brush_transaction_is_not_discarded_as_preview_state():
    app = SimpleNamespace(active_classify_tool="brush")
    peer = _bare_interactor(app)
    peer._gesture_tool = "brush"
    peer.P1 = (1.0, 2.0, 3.0)
    peer.is_dragging = True
    peer._clear_all_previews = lambda: (_ for _ in ()).throw(
        AssertionError("live brush state must be completed by its release path")
    )

    assert peer._cancel_incomplete_classification_gesture() is False
