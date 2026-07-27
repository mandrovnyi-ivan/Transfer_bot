from pipeline import TransferPipeline, merge_source_names, slug_aliases


def test_slug_aliases_adds_last_name_variant() -> None:
    assert slug_aliases("bradley-barcola") == ["bradley-barcola", "barcola"]
    assert slug_aliases("castro") == ["castro"]


def test_merge_source_names_keeps_unique_order() -> None:
    merged = merge_source_names('["FabrizioRomano", "Plettigoal"]', "FabrizioRomano")
    assert merged == ["FabrizioRomano", "Plettigoal"]
    merged = merge_source_names('["FabrizioRomano"]', "David_Ornstein")
    assert merged == ["FabrizioRomano", "David_Ornstein"]


def test_higher_tier_detection() -> None:
    assert TransferPipeline._is_higher_tier("tier1", "yellow") is True
    assert TransferPipeline._is_higher_tier("yellow", "tier1") is False
