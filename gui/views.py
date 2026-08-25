def set_view(app, mode):
    # do not touch the camera. Just record the requested view.
    preserve = getattr(app, "_preserve_view", False)
    already_in_3d = getattr(app, "current_view", None) == "3d"

    # Hard block: prevent entering 3D unless explicitly allowed.
    if mode == "3d" and not (
        getattr(app, "_allow_3d_switch", False) or already_in_3d
    ):
        # Do not change current_view.
        print("Blocked auto-switch to 3D (use View -> 3D button)")
        try:
            if hasattr(app, "statusBar"):
                app.statusBar().showMessage("3D blocked (use View -> 3D)", 2000)
        except Exception:
            pass
        return

    # Only set current_view after passing the guard.
    app.current_view = mode
    if preserve:
        return

    if mode == "top":
        app.vtk_widget.view_xy()
        app.vtk_widget.camera.SetViewUp(0, 1, 0)
        try:
            cam = app.vtk_widget.renderer.GetActiveCamera()
            cam.ParallelProjectionOn()
        except Exception:
            pass

    elif mode == "front":
        app.vtk_widget.view_xz()
        app.vtk_widget.camera.SetViewUp(0, 0, 1)

    elif mode == "side":
        app.vtk_widget.view_yz()
        app.vtk_widget.camera.SetViewUp(0, 0, 1)

    elif mode == "3d":
        try:
            import math
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera

            interactor = getattr(app.vtk_widget, "interactor", None)
            if interactor is not None:
                style = interactor.GetInteractorStyle()
                style_name = style.GetClassName() if style is not None else "None"
                if style_name != "vtkInteractorStyleTrackballCamera":
                    interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())

            renderer = app.vtk_widget.renderer
            cam = renderer.GetActiveCamera()
            cam.ParallelProjectionOff()

            bounds = renderer.ComputeVisiblePropBounds()
            valid_bounds = (
                bounds
                and len(bounds) == 6
                and all(math.isfinite(v) for v in bounds)
                and bounds[0] < bounds[1]
                and bounds[2] < bounds[3]
                and bounds[4] <= bounds[5]
            )

            if valid_bounds:
                cx = 0.5 * (bounds[0] + bounds[1])
                cy = 0.5 * (bounds[2] + bounds[3])
                cz = 0.5 * (bounds[4] + bounds[5])

                sx = max(bounds[1] - bounds[0], 1.0)
                sy = max(bounds[3] - bounds[2], 1.0)
                sz = max(bounds[5] - bounds[4], 1.0)
                diag = math.sqrt(sx * sx + sy * sy + sz * sz)
                distance = max(diag * 1.75, max(sx, sy, sz) * 2.5)

                vx, vy, vz = 1.0, -1.0, 0.75
                vmag = math.sqrt(vx * vx + vy * vy + vz * vz)
                vx /= vmag
                vy /= vmag
                vz /= vmag

                cam.SetFocalPoint(cx, cy, cz)
                cam.SetPosition(
                    cx + vx * distance,
                    cy + vy * distance,
                    cz + vz * distance,
                )
                cam.SetViewUp(0.0, 0.0, 1.0)
                renderer.ResetCameraClippingRange()
            else:
                app.vtk_widget.isometric_view()
        except Exception:
            try:
                app.vtk_widget.isometric_view()
            except Exception:
                pass

    elif mode == "dynamic":
        pass

    app.vtk_widget.render()
