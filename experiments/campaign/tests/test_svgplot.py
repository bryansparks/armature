from campaign_runner import svgplot


def test_escape():
    assert svgplot.escape("<a&b>") == "&lt;a&amp;b&gt;"


def test_line_chart_is_self_contained_svg():
    s = svgplot.line_chart({"auth": [0.9, 0.7, 0.5], "dash": [0.8, 0.6, 0.4]})
    assert s.startswith("<svg") and s.rstrip().endswith("</svg>")
    assert "<polyline" in s
    assert "auth" in s and "dash" in s
    # no external assets
    assert "href=" not in s and "src=" not in s


def test_scatter_plots_points():
    s = svgplot.scatter([0, 1, 2], [0.9, 0.7, 0.5])
    assert s.startswith("<svg") and s.rstrip().endswith("</svg>")
    assert "<circle" in s


def test_bar_plots_rects():
    s = svgplot.bar(["auth", "dash"], [0.02, 0.5])
    assert s.startswith("<svg") and s.rstrip().endswith("</svg>")
    assert "<rect" in s