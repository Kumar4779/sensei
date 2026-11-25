import pytest

from macpilot.coords import SCALE, NormPoint, Viewport, ground, normalize


@pytest.mark.parametrize("viewport", [Viewport(1000, 1000), Viewport(1280, 720), Viewport(390, 844), Viewport(1366, 768)])
def test_corners_and_centre_ground_to_the_viewport(viewport):
    assert ground(NormPoint(0, 0), viewport) == (0, 0)
    assert ground(NormPoint(SCALE, SCALE), viewport) == (viewport.width, viewport.height)
    assert ground(NormPoint(500, 500), viewport) == (viewport.width / 2, viewport.height / 2)


def test_non_square_viewport_scales_each_axis_on_its_own():
    vp = Viewport(1280, 720)
    assert ground(NormPoint(250, 750), vp) == (320.0, 540.0)
    assert normalize(320, 540, vp) == NormPoint(250, 750)


@pytest.mark.parametrize("viewport", [Viewport(1280, 720), Viewport(390, 844), Viewport(1366, 768)])
def test_round_trip_norm_to_pixels_to_norm_is_exact(viewport):
    for x in range(0, SCALE + 1, 37):
        for y in range(0, SCALE + 1, 41):
            assert normalize(*ground(NormPoint(x, y), viewport), viewport) == NormPoint(x, y)


def test_round_trip_pixels_to_norm_to_pixels_is_within_one_normalized_unit():
    vp = Viewport(1280, 720)
    for px, py in [(0, 0), (1, 1), (639, 359), (1279, 719), (777, 123)]:
        gx, gy = ground(normalize(px, py, vp), vp)
        assert abs(gx - px) <= vp.width / SCALE / 2 + 1e-9 and abs(gy - py) <= vp.height / SCALE / 2 + 1e-9


def test_the_coordinate_space_does_not_depend_on_screenshot_size():
    # The same normalized point means the same viewport location whatever the
    # screenshot's pixel size (e.g. a 2x retina capture or a downscaled copy).
    vp = Viewport(1280, 720)
    assert ground(NormPoint(500, 500), vp) == (640.0, 360.0)


@pytest.mark.parametrize("x, y", [(-1, 0), (0, 1001), (True, 5), ("500", 500)])
def test_out_of_range_points_are_rejected(x, y):
    with pytest.raises(ValueError):
        NormPoint(x, y)


def test_bad_viewports_and_off_screen_pixels_are_rejected():
    with pytest.raises(ValueError):
        Viewport(0, 720)
    with pytest.raises(ValueError):
        normalize(1281, 10, Viewport(1280, 720))
