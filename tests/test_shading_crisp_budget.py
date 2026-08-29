import gui.shading_display as shading


def test_default_crisp_budget_keeps_large_all_class_tile_out_of_hatched_fallback(
        monkeypatch):
    monkeypatch.delenv("NAKSHA_SHADING_CRISP_MAX_MIXED_FACES", raising=False)

    budget = shading._crisp_mixed_face_budget(69_202_527)

    assert budget == 17_300_632
    assert budget > 14_192_010


def test_crisp_budget_honors_explicit_memory_override(monkeypatch):
    monkeypatch.setenv("NAKSHA_SHADING_CRISP_MAX_MIXED_FACES", "4000000")

    assert shading._crisp_mixed_face_budget(69_202_527) == 4_000_000
