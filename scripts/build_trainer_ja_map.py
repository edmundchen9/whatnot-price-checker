"""
Emit trainer_names_ja_map.json from curated English→Japanese trainer/item names.

Run: py -3 scripts/build_trainer_ja_map.py

Japanese strings are official or common JP print names (no machine translation).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "src" / "whatnot_price_checker" / "trainer_names_ja_map.json"

# English card name -> Japanese as printed on JP cards (or katakana character names)
MANUAL_EN_TO_JA: dict[str, str] = {
    "Ace Trainer": "エリートトレーナー",
    "Bosses Orders": "ボスの指令",
    "Boss's Orders": "ボスの指令",
    "Professor's Research": "博士の研究",
    "Professor Oak": "オーキドはかせ",
    "Professor Birch": "ハギロウはかせ",
    "Professor Elm": "ウツギはかせ",
    "Professor Juniper": "アララギはかせ",
    "Professor Sycamore": "プラターヌはかせ",
    "Professor Sada": "オモダカはかせ",
    "Professor Turo": "タイムはかせ",
    "Ultra Ball": "ハイパーボール",
    "Great Ball": "スーパーボール",
    "Poke Ball": "モンスターボール",
    "Nest Ball": "ネストボール",
    "Quick Ball": "クイックボール",
    "Level Ball": "レベルボール",
    "Lure Ball": "ルアボール",
    "Friend Ball": "フレンドボール",
    "Heavy Ball": "ヘビーボール",
    "Moon Ball": "ムーンボール",
    "Timer Ball": "タイマーボール",
    "Repeat Ball": "リピートボール",
    "Net Ball": "ネットボール",
    "Premier Ball": "プレミアボール",
    "Feather Ball": "フェザーボール",
    "Hisuian Heavy Ball": "ヒスイのヘビーボール",
    "Heal Ball": "ヒーリングボール",
    "Dive Ball": "ダイブボール",
    "Rare Candy": "ふしぎなアメ",
    "Switch": "きりかえ",
    "Escape Rope": "あなぬけのヒモ",
    "Super Rod": "すごいつりざお",
    "Energy Retrieval": "エネルギー回収",
    "Energy Search": "エネルギー転送",
    "Energy Switch": "エネルギーつなぎ",
    "Energy Recycler": "エネルギーリサイクラー",
    "Superior Energy Retrieval": "スーパーエネルギー回収",
    "Crushing Hammer": "クラッシャーハンマー",
    "Enhanced Hammer": "強化ハンマー",
    "Field Blower": "フィールドブロアー",
    "Air Balloon": "ふうせん",
    "Counter Catcher": "カウンターキャッチャー",
    "Lost Vacuum": "ロストスイーパー",
    "Path to the Peak": "ぼうぐんのいわと",
    "Collapsed Stadium": "スタジアムのせき",
    "Temple of Sinnoh": "いにしえのひびき",
    "Iono": "ナンジャモ",
    "Marnie": "マリィ",
    "N": "Ｎ",
    "Misty": "カスミ",
    "Brock": "タケシ",
    "Giovanni": "サカキ",
    "Cynthia": "シロナ",
    "Leon": "ダンデ",
    "Hop": "ホップ",
    "Bede": "ビート",
    "Mallow": "マオ",
    "Lillie": "リーリエ",
    "Guzma": "グズマ",
    "Raihan": "キバナ",
    "Klara": "クララ",
    "Adaman": "アデバン",
    "Irida": "カイ",
    "Arven": "ペパー",
    "Penny": "ボタン",
    "Larry": "アオキ",
    "Rika": "チリ",
    "Poppy": "ポピー",
    "Grusha": "グルーシャ",
    "Mela": "メロコ",
    "Eri": "エリ",
    "Dendra": "キハダ",
    "Clavell": "クラベル",
    "Geeta": "オモダカ",
    "Hassel": "ハッサク",
    "Brassius": "コルサ",
    "Jacq": "ジニア",
    "Bea": "サイトウ",
    "Colress": "アクロマ",
    "Allister": "アリスター",
    "Ryme": "ライム",
    "Kofu": "ハギ",
    "Kieran": "スグリ",
    "Drayton": "カキツバタ",
    "Amarys": "アマリス",
    "Choice Band": "こだわりハチマキ",
    "Choice Belt": "こだわりベルト",
    "Muscle Band": "ちからのハチマキ",
    "Float Stone": "かるいし",
    "Rocky Helmet": "ゴツゴツメット",
    "Big Charm": "おおきなおまもり",
    "Escape Board": "エスケープボード",
    "VS Seeker": "ＶＳシーカー",
    "Pal Pad": "ともだちてちょう",
    "Evolution Incense": "しんかのおこう",
    "Cross Switcher": "クロススイッチャー",
    "Crystal Cave": "クリスタルこうどう",
    "Training Court": "トレーニングコート",
    "Turffield Stadium": "ターフスタジアム",
    "Welcoming Lantern": "ようこそランタン",
    "Prime Catcher": "プライムキャッチャー",
    "Forest Seal Stone": "もりの封印石",
    "Sky Seal Stone": "そらの封印石",
    "Lucky Runner": "ラッキーランナー",
    "Pokegear 3.0": "ポケギア3.0",
    "Blaine": "カツラ",
    "Fantina": "メリッサ",
    "Candice": "スズナ",
    "Volkner": "デンジ",
    "Nessa": "ルリナ",
    "Kabu": "カブ",
    "Opal": "ポプラ",
    "Gordie": "マクワ",
    "Piers": "ネズ",
    "Mustard": "マスタード",
    "Koga": "キョウ",
    "Janine": "アンズ",
}


def main() -> None:
    ja_to_en: dict[str, str] = {}
    for en, ja in MANUAL_EN_TO_JA.items():
        ja_to_en[ja] = en

    ordered = {k: ja_to_en[k] for k in sorted(ja_to_en.keys())}
    OUT_JSON.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(ordered)} entries to {OUT_JSON}")


if __name__ == "__main__":
    main()
