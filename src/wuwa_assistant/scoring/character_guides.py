"""Offline creator-guide facts with explicitly project-defined score weights.

Guide priority order is evidence; the numerical conversion is a heuristic. No
runtime HTTP, usage-population import, or inferred panel totals happen here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from html import escape
from math import isfinite
from typing import Any

from wuwa_assistant.models.names import character_name_key

from .data import _load_json_resource, load_roll_data, profiles_to_seed_definitions
from .models import (
    CharacterScoringProfile,
    Provenance,
    ProvenanceStatus,
    ScoreThresholds,
    ScoringContext,
    StatKey,
    StatWeight,
)

AUTO_PROFILE_ID = "character_guides"
STAT_LABELS = {
    StatKey.CRIT_RATE: "暴擊",
    StatKey.CRIT_DAMAGE: "暴擊傷害",
    StatKey.ATTACK_PCT: "攻擊%",
    StatKey.FLAT_ATTACK: "固定攻擊",
    StatKey.HP_PCT: "生命%",
    StatKey.FLAT_HP: "固定生命",
    StatKey.DEFENSE_PCT: "防禦%",
    StatKey.FLAT_DEFENSE: "固定防禦",
    StatKey.ENERGY_REGEN: "共鳴效率",
    StatKey.BASIC_ATTACK_DAMAGE: "普攻傷害",
    StatKey.HEAVY_ATTACK_DAMAGE: "重擊傷害",
    StatKey.RESONANCE_SKILL_DAMAGE: "共鳴技能傷害",
    StatKey.RESONANCE_LIBERATION_DAMAGE: "共鳴解放傷害",
}
MAIN_STAT_LABELS = {str(key): label for key, label in STAT_LABELS.items()} | {
    "healing_bonus": "治療效果加成",
    "glacio_damage": "冷凝傷害加成",
    "fusion_damage": "熱熔傷害加成",
    "aero_damage": "氣動傷害加成",
    "havoc_damage": "湮滅傷害加成",
    "spectro_damage": "衍射傷害加成",
    "electro_damage": "導電傷害加成",
}


@dataclass(frozen=True, slots=True)
class UsageReference:
    """Manually supplied popularity observations, never numerical score weights."""

    provided_on: str
    screenshot_index: int
    substats: tuple[tuple[StatKey, float | None], ...]
    main_stats: tuple[tuple[int, str, float | None], ...]
    main_echoes: tuple[tuple[str, float | None], ...]
    set_icons: tuple[tuple[str, float | None], ...]
    notes: str
    reference_kind: str = "usage"

    @property
    def summary(self) -> str:
        if self.reference_kind == "preset":
            return (
                f"使用者提供的遊戲內預設推薦({self.provided_on},圖 {self.screenshot_index}):"
                + "、".join(STAT_LABELS[stat] for stat, _ in self.substats)
                + "。畫面未提供使用率,不補成 0% 或估造百分比;排列不等於精確收益順序。"
            )
        return (
            f"使用者提供的遊戲內推薦({self.provided_on},圖 {self.screenshot_index}):"
            + "、".join(
                f"{STAT_LABELS[stat]} {_reference_rate(percent)}" for stat, percent in self.substats
            )
            + "。百分比是使用率,不是掉落機率或傷害係數;未顯示不表示零使用率。"
        )

    def html(self, weights: tuple[StatWeight, ...]) -> str:
        label = "遊戲內預設推薦" if self.reference_kind == "preset" else "遊戲內使用率"
        by_stat = {weight.stat: weight.weight for weight in weights}
        rows = "".join(
            f"<tr><td>{escape(STAT_LABELS[stat])}</td><td>{_reference_rate(percent)}</td>"
            f"<td>{by_stat[stat]:g}</td></tr>"
            for stat, percent in self.substats
        )
        mains = ";".join(
            f"COST{cost} {MAIN_STAT_LABELS[key]} {_reference_rate(percent)}"
            for cost, key, percent in self.main_stats
        )
        echoes = ";".join(
            f"{name} {_reference_rate(percent)}" for name, percent in self.main_echoes
        )
        sets = ";".join(f"{name} {_reference_rate(percent)}" for name, percent in self.set_icons)
        return (
            f"<h4>{label}與本工具權重</h4>"
            f"<p>{escape(self.summary)}</p>"
            "<table border='1' cellspacing='0' cellpadding='5'>"
            f"<tr><th>副詞條</th><th>{label}</th><th>基礎權重</th></tr>"
            f"{rows}</table>"
            "<p>權重為效率達標前的工具設定;雙暴接近時可同級,"
            "效率依循環需要調整,不按使用率逐項等比例換算。</p>"
            f"<p>主詞條推薦:{escape(mains)}<br>主音聲骸推薦:{escape(echoes)}"
            f"<br>合鳴套裝與件數:{escape(sets)}</p><p>{escape(self.notes)}</p>"
        )


def _reference_rate(value: float | None) -> str:
    return "未提供使用率" if value is None else f"{value:.1f}%"


def _usage_reference(row: dict[str, Any]) -> UsageReference | None:
    raw = row.get("usage_reference")
    if raw is None:
        return None
    kind = raw.get("reference_kind", "usage")
    if kind not in ("usage", "preset"):
        raise ValueError("invalid game recommendation kind")

    def rate(value: object) -> float | None:
        if kind == "preset":
            if value is not None:
                raise ValueError("preset recommendation cannot claim usage percentages")
            return None
        return _usage_percent(value)

    substats = tuple((StatKey(key), rate(value)) for key, value in raw["substats_percent"].items())
    mains = tuple(
        (int(cost), key, rate(value))
        for cost, stats in raw["main_stats_percent"].items()
        for key, value in stats.items()
    )
    if any(cost not in (1, 3, 4) or key not in MAIN_STAT_LABELS for cost, key, _ in mains):
        raise ValueError("invalid usage main stat")
    return UsageReference(
        provided_on=raw["provided_on"],
        screenshot_index=int(raw["screenshot_index"]),
        substats=substats,
        main_stats=mains,
        main_echoes=tuple((name, rate(value)) for name, value in raw["main_echo_percent"].items()),
        set_icons=tuple((item["label"], rate(item["percent"])) for item in raw["set_icons"]),
        notes=raw["notes"],
        reference_kind=kind,
    )


def _usage_percent(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or not 0 <= value <= 100
    ):
        raise ValueError("usage percentage must be finite and within [0, 100]")
    return float(value)


@dataclass(frozen=True, slots=True)
class GuideSource:
    url: str
    author: str
    title: str


@dataclass(frozen=True, slots=True)
class CharacterGuide:
    role_id: str
    name: str
    aliases: tuple[str, ...]
    role: str
    tiers: tuple[tuple[StatKey, ...], ...]
    weights: tuple[StatWeight, ...]
    energy_target: float | None
    main_stats: str
    sets: str
    caveat: str
    sources: tuple[GuideSource, ...]
    retrieved_on: str
    data_version: str
    profile_version: int
    element: str = ""
    allowed_sets: tuple[str, ...] = ()
    main_stats_by_cost: tuple[tuple[int, tuple[str, ...]], ...] = ()
    equipment_verified_on: str = ""
    usage: UsageReference | None = None
    weight_rationale: str = ""
    set_combinations: tuple[tuple[tuple[str, int], ...], ...] = ()
    set_aliases: tuple[tuple[str, str], ...] = ()

    @property
    def usage_note(self) -> str:
        return "" if self.usage is None else self.usage.summary

    def canonical_set_name(self, name: str) -> str:
        return dict(self.set_aliases).get(name, name)

    def sets_complete(self, counts: Mapping[str, int]) -> bool:
        normalized: dict[str, int] = {}
        for name, count in counts.items():
            key = self.canonical_set_name(name)
            normalized[key] = normalized.get(key, 0) + count
        combinations = self.set_combinations or tuple(((name, 5),) for name in self.allowed_sets)
        return any(
            all(normalized.get(name, 0) >= required for name, required in combo)
            for combo in combinations
        )

    @property
    def set_requirement_label(self) -> str:
        combinations = self.set_combinations or tuple(((name, 5),) for name in self.allowed_sets)
        return " 或 ".join(
            "+".join(f"{name} {count}件" for name, count in combo) for combo in combinations
        )

    def main_keys(self, cost: int | None) -> tuple[str, ...]:
        return next((keys for c, keys in self.main_stats_by_cost if c == cost), ())

    @property
    def template_kind(self) -> str:
        return f"character_guide_{self.role_id}"

    @property
    def priority_label(self) -> str:
        return " > ".join("/".join(STAT_LABELS[s] for s in tier) for tier in self.tiers)

    def profile(self) -> CharacterScoringProfile:
        return CharacterScoringProfile(
            profile_id=self.template_kind,
            name=f"{self.name} · 攻略參考",
            template_kind=self.template_kind,
            version=self.profile_version,
            description=(
                f"{self.role}。{self.priority_label}。{self.caveat}\n{self.weight_rationale}\n"
                f"來源: {self.sources[0].author}(官方平台玩家攻略); 查核 {self.retrieved_on}。\n"
                f"{self.sources[0].url}\n攻略與遊戲內推薦交叉參考後的優先級換算為 "
                "1/.8/.6/.4/.25/.15, 未列詞條為 0; "
                "數字及達標後效率權重 .1 是專案估值, 不是攻略的傷害係數。"
                + (f"\n遊戲內推薦補充: {self.usage_note}" if self.usage_note else "")
            ),
            thresholds=ScoreThresholds(),
            weights=self.weights,
            roll_data_version=load_roll_data().data_version,
            provenance=Provenance(
                ProvenanceStatus.PROJECT_DEFAULT,
                self.sources[0].url,
                False,
                f"{self.data_version}; community guides and user-supplied usage; "
                "project numerical weights",
            ),
        )

    def html(self) -> str:
        energy_label = "未列需求" if self.energy_target is None else f"{self.energy_target:g}%"
        sources = "<br>".join(
            f'<a href="{escape(s.url, quote=True)}">{escape(s.author)} · {escape(s.title)}</a>'
            for s in self.sources
        )
        return (
            f"<h3>{escape(self.name)} · {escape(self.role)}</h3>"
            f"<p>需求參考順序: {escape(self.priority_label)}</p>"
            f"<p>評分調整理由:{escape(self.weight_rationale)}</p>"
            + ("" if self.usage is None else self.usage.html(self.weights))
            + f"<p>主詞條: {escape(self.main_stats)}<br>套裝: {escape(self.sets)}</p>"
            f"<p>配裝方向查核: {escape(self.equipment_verified_on or self.retrieved_on)}。"
            "自動判定依所選攻略的主用/備用套裝和對應 COST 主詞條; "
            "不同不代表不能使用。人工指定優先, 副詞條評分另算。</p>"
            f"<p>{escape(self.caveat)}</p>"
            f"<p>建議共鳴效率: {energy_label} (依角色攻略, 非通用硬門檻)。"
            "建議值用於配裝方向, 不視為已量測的面板數值。"
            "若已有實際面板資料, 達標後額外效率權重降至 .1, 其他權重不變。</p>"
            "<p>同一公式、不同角色權重。上述順序綜合攻略與遊戲內推薦; 1/.8/.6/.4/.25/.15 的數字, "
            "未列詞條為 0, 以及達標後 .1 都是工具估值, 不代表精確傷害收益。"
            "0 只表示這份參考不給分, 不表示該詞條絕對無用。</p>"
            "<p>公式: 每條權重 x [0.55 + 0.45 x (數值-最低值)/(最高值-最低值)]; "
            "以隨機五詞條品質 P99 對應 90 分, 其上至理論頂值對應 90 到 100 分。"
            "P99 依官方調諧機率與目前角色權重推算, "
            "作為實用高品質基準, 不是五條都頂值; 分數本身不是百分位。"
            "舊版自訂設定檔保留原本最高五項權重基準。只算副詞條; 未開詞條不算已獲得分數。"
            "主詞條、套裝、循環、隊伍增益及實際傷害另行確認。跨角色分數表示各自參考目標的接近程度, "
            "不是角色強弱或相同資源下的提升量。評分分界是工具設定。</p>"
            '<p>調諧數值機率: <a href="https://sdk-global.kurogames.com/pro/agreement/common/language_ko/product_info">'
            "庫洛官方機率公示</a> (查核2026-09-23)。種類等機率且不重複, 數值按官方機率; "
            "不是每檔一樣容易出現。歷史自訂設定檔沿用其版本。</p>"
            "<p>角色來源為官方平台收錄的玩家攻略, 不是官方數值標準。"
            "遊戲內推薦由使用者手動提供; 不會自動下載遊戲內使用率。"
            "遊戲內推薦可作為個人主要依據: 在「評分設定」複製設定檔、調整權重與說明, "
            "再於角色選用; 明確指定的設定檔優先於自動攻略。人氣比例不能直接當作權重。</p>"
            f"<p>查核日期: {self.retrieved_on}; 來源未提供更新日期, 版本與配隊變動時應重查。</p>"
            f"<p>{sources}</p>"
        )


@lru_cache(maxsize=1)
def load_character_guides() -> tuple[CharacterGuide, ...]:
    raw = _load_json_resource("character-guides.v1.json")
    guides = []
    for row in raw["guides"]:
        tiers = tuple(tuple(StatKey(s) for s in tier) for tier in row["priority_tiers"])
        flat = [s for tier in tiers for s in tier]
        tier_weights = row.get("tier_weights", raw["tier_weights"])
        if (
            len(set(flat)) != len(flat)
            or len(tiers) > len(tier_weights)
            or any(not tier for tier in tiers)
            or any(
                isinstance(w, bool)
                or not isinstance(w, (int, float))
                or not isfinite(w)
                or not 0 <= w <= 1
                for w in tier_weights
            )
            or tier_weights != sorted(tier_weights, reverse=True)
        ):
            raise ValueError("invalid character guide priority tiers")
        weights = {s: tier_weights[i] for i, tier in enumerate(tiers) for s in tier}
        reference = row.get("equipment_reference", {})
        combinations = tuple(
            tuple((str(name), int(count)) for name, count in combination.items())
            for combination in reference.get("set_combinations", [])
        )
        if any(
            sum(count for _, count in combo) != 5
            or any(
                count < 1 or name not in reference.get("allowed_sets", []) for name, count in combo
            )
            for combo in combinations
        ):
            raise ValueError("invalid character guide set combination")
        guides.append(
            CharacterGuide(
                role_id=row["role_id"],
                name=row["name"],
                aliases=tuple(row["aliases"]),
                role=row["role"],
                tiers=tiers,
                weights=tuple(StatWeight(s, weights.get(s, 0.0)) for s in StatKey),
                energy_target=row["energy_target"],
                main_stats=row["main_stats"],
                sets=row["sets"],
                caveat=row["caveat"],
                sources=tuple(
                    GuideSource(s["url"], s["author"], s["title"]) for s in row["sources"]
                ),
                retrieved_on=row.get("retrieved_on", raw["retrieved_on"]),
                data_version=raw["data_version"],
                profile_version=int(row["profile_version"]),
                element=row.get("equipment_reference", {}).get("element", ""),
                allowed_sets=tuple(row.get("equipment_reference", {}).get("allowed_sets", [])),
                main_stats_by_cost=tuple(
                    (int(cost), tuple(keys))
                    for cost, keys in row.get("equipment_reference", {})
                    .get("main_stats_by_cost", {})
                    .items()
                ),
                equipment_verified_on=row.get("equipment_reference", {}).get("verified_on", ""),
                usage=_usage_reference(row),
                weight_rationale=row.get("weight_rationale", "沿用已查核攻略的優先順序。"),
                set_combinations=combinations,
                set_aliases=tuple(reference.get("set_aliases", {}).items()),
            )
        )
    return tuple(guides)


def guide_for_character(name: str) -> CharacterGuide | None:
    key = character_name_key(name)
    return next(
        (
            g
            for g in load_character_guides()
            if any(character_name_key(alias) == key for alias in (g.name, *g.aliases))
        ),
        None,
    )


def guide_for_kind(kind: str) -> CharacterGuide | None:
    return next((g for g in load_character_guides() if g.template_kind == kind), None)


def load_character_seed_definitions() -> list[dict[str, Any]]:
    return profiles_to_seed_definitions(g.profile() for g in load_character_guides())


def apply_guide_context(
    profile: CharacterScoringProfile,
    context: ScoringContext,
) -> tuple[CharacterScoringProfile, tuple[str, ...]]:
    guide = guide_for_kind(profile.template_kind)
    if guide is None:
        return profile, ()
    target = guide.energy_target
    override = context.conditions.get("guide_energy_target")
    if (
        isinstance(override, (int, float))
        and not isinstance(override, bool)
        and isfinite(override)
        and 100 <= override <= 1000
    ):
        target = float(override)
    if target is None:
        return profile, ()
    target_label = "自訂目標" if target != guide.energy_target else "攻略參考"
    panel = context.current_totals.get(StatKey.ENERGY_REGEN)
    if panel is None:
        return profile, (f"建議共鳴效率 {target:g}% ({target_label}); 依此需求評估效率詞條",)
    if panel < target:
        return profile, (f"面板效率 {panel:g}%/{target_label} {target:g}%; 循環仍需確認",)
    return replace(
        profile,
        weights=tuple(
            replace(w, weight=min(w.weight, 0.1)) if w.stat is StatKey.ENERGY_REGEN else w
            for w in profile.weights
        ),
    ), (f"面板效率 {panel:g}% 已達{target_label} {target:g}%; 效率額外權重降至 0.1",)
