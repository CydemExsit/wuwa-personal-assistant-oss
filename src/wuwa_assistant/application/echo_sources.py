"""Reviewed non-Tacet acquisition facts. See docs/echo-demand.md for evidence."""

from wuwa_assistant.planning.farming import FarmZone

OTHER_ECHO_SOURCES = (
    FarmZone(
        "boss:nightmare-lampylumen",
        "夢魘·輝螢軍勢",
        "黎那汐塔 · 貝奧海域",
        ("高天共奏之曲", "凌冽決斷之心"),
        "https://wutheringwaves.kurogames.com/en/main/news/detail/2381",
        (4,),
        "首領",
    ),
    FarmZone(
        "boss:lampylumen",
        "輝螢軍勢",
        "瑝瓏 · 虎口礦場",
        ("凝夜白霜",),
        "https://zilliongamer.com/wuthering-waves/c/echeos/lampylumen-myriad",
        (4,),
        "首領",
    ),
    FarmZone(
        "boss:impermanence-heron",
        "無常凶鷺",
        "瑝瓏 · 荒石高地",
        ("輕雲出月",),
        "https://theriagames.com/guide/wuthering-waves-impermanence-heron/",
        (4,),
        "首領",
    ),
    FarmZone(
        "wild:glacio-dreadmane",
        "雪鬃狼",
        "乘霄山 · 依資料塢追蹤",
        ("凝夜白霜", "輕雲出月"),
        "https://wutheringwaves.gg/version-1-1-thaw-of-eons/",
        (3,),
        "野外吸收",
    ),
    FarmZone(
        "instance:dreamless",
        "無妄者",
        "北落野 · 無冠巨像之心",
        ("沉日劫明",),
        "https://zilliongamer.com/wuthering-waves/c/echeos/dreamless",
        (4,),
        "週本",
    ),
    FarmZone(
        "nest:fallen-grave",
        "千歿沉島夢魘聚落",
        "黎那汐塔 · 隱海實驗場",
        ("失序彼岸之夢",),
        "https://wutheringwaves.kurogames.com/zh-tw/main/news/detail/2908",
        (1, 3),
        "夢魘聚落",
    ),
)
