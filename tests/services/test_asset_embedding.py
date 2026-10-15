from types import SimpleNamespace

from services.asset_embedding import build_indexed_text


def test_build_indexed_text_is_deterministic_and_skips_empty_fields() -> None:
    description = SimpleNamespace(
        summary=" Человек собирает деталь. ",
        visible_objects=[" человек ", " деталь "],
        action=" собирает ",
        setting=" мастерская ",
        shot_type=" средний план ",
        camera_motion=None,
        visible_text=[" A-42 "],
    )

    assert build_indexed_text(description) == (
        "Человек собирает деталь.\n"
        "Объекты: человек, деталь\n"
        "Действие: собирает\n"
        "Обстановка: мастерская\n"
        "Тип плана: средний план\n"
        "Текст в кадре: A-42"
    )
