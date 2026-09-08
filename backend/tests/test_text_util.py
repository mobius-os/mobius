from app.text_util import elide


def test_elide_bounds_total_output_and_keeps_both_ends():
  source = "start-" + ("x" * 5000) + "-finish"

  result, truncated = elide(source, 100)

  assert truncated is True
  assert len(result) == 100
  assert result.startswith("start-")
  assert result.endswith("-finish")
  assert "characters truncated" in result


def test_elide_non_positive_limit_disables_trimming():
  assert elide("unchanged", 0) == ("unchanged", False)


def test_elide_tiny_limit_still_honors_the_bound():
  result, truncated = elide("oversized", 4)

  assert truncated is True
  assert len(result) == 4
