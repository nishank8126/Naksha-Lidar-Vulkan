from types import SimpleNamespace

from gui.prj_block_identifier import PRJBlockIdentifierDialog


class _Points:
    def __init__(self, points):
        self._points = points

    def GetNumberOfPoints(self):
        return len(self._points)

    def GetPoint(self, index):
        return self._points[index]


class _Mapper:
    def __init__(self, points):
        self._points = _Points(points)

    def Update(self):
        pass

    def GetInput(self):
        return self

    def GetPoints(self):
        return self._points


class _Matrix:
    def MultiplyPoint(self, point):
        # Exercise actor-to-world conversion as well as point extraction.
        return (point[0] + 100.0, point[1] - 50.0, point[2], point[3])


class _Actor:
    is_block_polygon = True
    grid_name = "MONASTERO000002"

    def __init__(self, points):
        self._mapper = _Mapper(points)

    def GetMapper(self):
        return self._mapper

    def GetMatrix(self):
        return _Matrix()

    def GetVisibility(self):
        return True


def test_preview_uses_exact_rendered_block_actor_geometry():
    rendered = [
        (1.0, 2.0, 7.0),
        (5.0, 2.0, 7.0),
        (5.0, 8.0, 7.0),
        (1.0, 2.0, 7.0),
    ]
    actor = _Actor(rendered)
    dialog = SimpleNamespace(
        app=SimpleNamespace(
            snt_actors=[{"actors": [actor]}],
            dxf_actors=[],
            # Deliberately different cached geometry: the preview must not use it.
            snt_block_polygons=[{
                "grid_name": "MONASTERO000002",
                "points_2d": [(0, 0), (10, 0), (10, 10)],
            }],
        )
    )

    polygon = PRJBlockIdentifierDialog._find_rendered_snt_block_polygon(
        dialog, "MONASTERO000002.laz"
    )

    assert polygon == [(101.0, -48.0), (105.0, -48.0), (105.0, -42.0)]


def test_preview_actor_lookup_ignores_text_and_other_blocks():
    text_actor = SimpleNamespace(
        is_block_polygon=False,
        grid_name="MONASTERO000002",
    )
    other_actor = _Actor([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
    other_actor.grid_name = "MONASTERO000003"
    dialog = SimpleNamespace(
        app=SimpleNamespace(
            snt_actors=[{"actors": [text_actor, other_actor]}],
            dxf_actors=[],
        )
    )

    polygon = PRJBlockIdentifierDialog._find_rendered_snt_block_polygon(
        dialog, "MONASTERO000002"
    )

    assert polygon is None


def test_actor_lookup_ignores_stale_actor_removed_from_renderer():
    stale = _Actor([(0, 0, 0), (1, 0, 0), (1, 1, 0)])
    visible = _Actor([(10, 10, 0), (11, 10, 0), (11, 11, 0)])

    class _Renderer:
        @staticmethod
        def HasViewProp(actor):
            return actor is visible

    dialog = SimpleNamespace(
        app=SimpleNamespace(
            vtk_widget=SimpleNamespace(renderer=_Renderer()),
            snt_actors=[{"actors": [stale, visible]}],
            dxf_actors=[],
        )
    )

    actor = PRJBlockIdentifierDialog._find_rendered_snt_block_actor(
        dialog, "MONASTERO000002"
    )

    assert actor is visible


def test_highlight_clones_exact_actor_mapper_and_transform():
    import vtk

    points = vtk.vtkPoints()
    for point in ((0, 0, 0), (3, 0, 0), (3, 2, 0), (0, 2, 0)):
        points.InsertNextPoint(*point)
    lines = vtk.vtkCellArray()
    for first, second in ((0, 1), (1, 2), (2, 3), (3, 0)):
        line = vtk.vtkLine()
        line.GetPointIds().SetId(0, first)
        line.GetPointIds().SetId(1, second)
        lines.InsertNextCell(line)
    polydata = vtk.vtkPolyData()
    polydata.SetPoints(points)
    polydata.SetLines(lines)
    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(polydata)
    source = vtk.vtkActor()
    source.SetMapper(mapper)
    source.SetPosition(100.0, 200.0, 7.0)

    renderer = vtk.vtkRenderer()
    renderer.AddActor(source)

    class _RenderWindow:
        def Render(self):
            pass

    dialog = SimpleNamespace(
        app=SimpleNamespace(
            vtk_widget=SimpleNamespace(
                renderer=renderer,
                GetRenderWindow=lambda: _RenderWindow(),
            )
        ),
        _highlight_actors=[],
    )
    blocks = [{
        "label": "MONASTERO000002",
        "boundary_coords": [(100, 200), (103, 200), (103, 202), (100, 202)],
        "_rendered_boundary_actor": source,
    }]

    bounds = PRJBlockIdentifierDialog._highlight_prj_boundaries(dialog, blocks)

    assert bounds == (100.0, 200.0, 103.0, 202.0)
    assert len(dialog._highlight_actors) == 1
    preview = dialog._highlight_actors[0]
    assert preview.GetMapper() is source.GetMapper()
    assert preview.GetProperty().GetColor() == (1.0, 1.0, 0.0)
    assert preview.GetPosition() == source.GetPosition()
